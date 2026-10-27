"""Project Archive (BE-8): Grid Nova's completed projects, imported from a CSV and read back
in the same shape as live projects.

Who: importing, voiding and the import history are Admin only. Reading (list, one project,
statistics) is open to every staff role, but each role sees only the fields config/archive.yaml
gives it — customer contact and money are Admin/Management (and money Accounts) — and a field
a role may not see is absent from the response, not null. The redaction lives in
app/projects/view.py; nothing here filters a field by hand.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Response, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.archive import config, importer, queries
from app.auth.dependencies import AuthenticatedEmployee, get_current_employee
from app.memory.db import get_db
from app.memory.models import ArchiveBatchStatus, ArchiveImportBatch, ArchivedProject
from app.projects import view

router = APIRouter(prefix="/api/v1/dashboard/archive")

CHUNK = 1024 * 1024


def _admin(employee: AuthenticatedEmployee) -> None:
    if not employee.has("archive.manage"):
        raise HTTPException(status_code=403, detail="Only Admin can import, void or list project archive imports.")


def _fail(status: int, code: str, message: str, field: Optional[str] = None) -> HTTPException:
    detail = {"code": code, "message": message}
    if field:
        detail["field"] = field
    return HTTPException(status_code=status, detail=detail)


# ── Response models ──────────────────────────────────────────────────────────────

class Provenanced(BaseModel):
    value: Any = None
    source_table: str
    record_id: Any = None


class ProjectCard(BaseModel):
    source: str
    ref: str
    id: int
    fields: dict[str, Provenanced]


class ProjectDossier(ProjectCard):
    sections: dict[str, list[Provenanced]]


class ProjectPage(BaseModel):
    items: list[ProjectCard]
    page: int
    page_size: int
    total: int


class ImportIssue(BaseModel):
    row: int
    column: Optional[str] = None
    code: str
    message: str


class ImportNotice(BaseModel):
    code: str
    columns: list[str]
    rows: int
    message: str


class ImportReport(BaseModel):
    batch_id: Optional[int] = None
    dry_run: bool
    filename: str
    batch_token: str
    sha256: str
    row_count: int
    accepted_count: int
    rejected_count: int
    error_count: int
    warning_count: int
    counts_by_code: dict[str, int]
    errors: list[ImportIssue]
    errors_truncated: bool
    warnings: list[ImportIssue]
    warnings_truncated: bool
    notices: list[ImportNotice]
    already_imported_batch_id: Optional[int] = None


class BatchSummary(BaseModel):
    id: int
    filename: str
    sha256: str
    uploaded_by: str
    uploaded_at: str
    row_count: int
    accepted_count: int
    rejected_count: int
    status: str
    voided_by: Optional[str] = None
    voided_at: Optional[str] = None
    void_reason: Optional[str] = None


class BatchDetail(BatchSummary):
    report: ImportReport


class BatchList(BaseModel):
    items: list[BatchSummary]


class VoidRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=1000)


class StatsTotal(BaseModel):
    count: int
    capacity_kw: str


class SchemeStat(BaseModel):
    scheme: str
    count: int
    capacity_kw: str


class StateStat(BaseModel):
    state: Optional[str] = None
    count: int
    capacity_kw: str


class DistrictStat(StateStat):
    district: Optional[str] = None


class YearStat(BaseModel):
    year: int
    count: int
    capacity_kw: str


class ArchiveStats(BaseModel):
    total: StatsTotal
    by_scheme: list[SchemeStat]
    by_state: list[StateStat]
    by_district: list[DistrictStat]
    by_year: list[YearStat]


def _batch_json(b: ArchiveImportBatch) -> dict:
    iso = lambda d: d.isoformat() if d else None   # noqa: E731
    return {"id": b.id, "filename": b.filename, "sha256": b.sha256, "uploaded_by": b.uploaded_by,
            "uploaded_at": iso(b.uploaded_at), "row_count": b.row_count, "accepted_count": b.accepted_count,
            "rejected_count": b.rejected_count, "status": b.status.value, "voided_by": b.voided_by,
            "voided_at": iso(b.voided_at), "void_reason": b.void_reason}


# ── Import (Admin) ───────────────────────────────────────────────────────────────

@router.get("/template.csv")
def template(employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """The header row and one commented example row (it starts with #, so the importer
    ignores it). No real data."""
    _admin(employee)
    return Response(content=importer.template_csv().encode("utf-8"), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="project_archive_template.csv"'})


async def _read(file: UploadFile) -> bytes:
    limit = config.import_limits()["max_file_mb"] * 1024 * 1024
    data = bytearray()
    while chunk := await file.read(CHUNK):
        data.extend(chunk)
        if len(data) > limit:
            raise _fail(413, "file_too_large", f"The file is larger than {limit // (1024 * 1024)} MB.")
    return bytes(data)


@router.post("/imports", response_model=ImportReport)
async def import_projects(file: UploadFile = File(...), dry_run: bool = Form(True), batch_token: Optional[str] = Form(None),
                          db: Session = Depends(get_db),
                          employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """Validate a template CSV (dry_run=true, the default — nothing is written) or write
    its accepted rows (dry_run=false with the `batch_token` the dry run returned, which is
    the file's SHA-256). Rows with errors are skipped and listed; warnings are stored with
    the row. The same file cannot be imported twice unless the first batch is voided."""
    _admin(employee)
    raw = await _read(file)
    try:
        parsed = importer.parse(db, raw, (file.filename or "upload.csv").rsplit("/", 1)[-1])
    except importer.FileRejected as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.body())
    earlier = db.execute(select(ArchiveImportBatch.id).where(
        ArchiveImportBatch.sha256 == parsed.sha256, ArchiveImportBatch.status == ArchiveBatchStatus.imported)
        .order_by(ArchiveImportBatch.id)).scalars().first()
    if dry_run:
        return {**importer.report(parsed, dry_run=True), "already_imported_batch_id": earlier}
    if not batch_token:
        raise _fail(422, "batch_token_required", "Send the batch_token from the dry run.", "batch_token")
    if batch_token != parsed.sha256:
        raise _fail(409, "batch_token_mismatch", "The file is not the one that was validated. Run the dry run again.",
                    "batch_token")
    if earlier is not None:
        raise _fail(409, "already_imported", f"This file was already imported as batch {earlier}. Void that batch first.")
    if not parsed.accepted:
        raise _fail(422, "nothing_to_import", "No row passed validation; nothing was imported.")
    try:
        batch = importer.commit(db, parsed, employee.employee_id)
    except IntegrityError:
        db.rollback()
        raise _fail(409, "import_conflict", "A row clashed with a project imported meanwhile. Run the dry run again.")
    return batch.report


@router.get("/imports", response_model=BatchList)
def list_imports(db: Session = Depends(get_db), employee: AuthenticatedEmployee = Depends(get_current_employee)):
    _admin(employee)
    rows = db.execute(select(ArchiveImportBatch).order_by(ArchiveImportBatch.id.desc()).limit(200)).scalars()
    return {"items": [_batch_json(b) for b in rows]}


def _batch(db: Session, batch_id: int) -> ArchiveImportBatch:
    batch = db.get(ArchiveImportBatch, batch_id)
    if batch is None:
        raise _fail(404, "batch_not_found", "No such import batch.")
    return batch


@router.get("/imports/{batch_id}", response_model=BatchDetail)
def import_report(batch_id: int, db: Session = Depends(get_db),
                  employee: AuthenticatedEmployee = Depends(get_current_employee)):
    _admin(employee)
    batch = _batch(db, batch_id)
    return {**_batch_json(batch), "report": batch.report}


@router.post("/imports/{batch_id}/void", response_model=BatchSummary)
def void_import(batch_id: int, body: VoidRequest, db: Session = Depends(get_db),
                employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """Hide every project of the batch from every read path. Nothing is deleted."""
    _admin(employee)
    batch = _batch(db, batch_id)
    if batch.status == ArchiveBatchStatus.voided:
        raise _fail(409, "already_voided", "This batch is already voided.")
    return _batch_json(importer.void(db, batch, employee.employee_id, body.reason.strip()))


# ── Read (every staff role, redacted by role) ─────────────────────────────────────

def _filters(q: Optional[str] = Query(None, max_length=200), state: Optional[str] = None, district: Optional[str] = None,
             scheme: Optional[str] = None, capacity_min: Optional[str] = None, capacity_max: Optional[str] = None,
             commissioned_from: Optional[str] = None, commissioned_to: Optional[str] = None,
             module_make: Optional[str] = None, inverter_make: Optional[str] = None) -> queries.ArchiveFilters:
    return queries.parse_filters(q, state, district, scheme, capacity_min, capacity_max, commissioned_from,
                                 commissioned_to, module_make, inverter_make)


@router.get("/projects", response_model=ProjectPage)
def list_projects(filters: queries.ArchiveFilters = Depends(_filters), page: int = Query(1, ge=1),
                  page_size: int = Query(25, ge=1, le=100), db: Session = Depends(get_db),
                  employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """Newest commissioning first: {items, page, page_size, total}. Filters narrow before
    paging, so `total` is the filtered count. `q` searches the Grid Nova ref and, where the
    role may see them, customer name, address, block, district and state."""
    rows, total = queries.page(db, filters, employee, page, page_size)
    return {"items": [view.project_card(db, r, employee) for r in rows], "page": page, "page_size": page_size,
            "total": total}


@router.get("/projects/{project_id}", response_model=ProjectDossier)
def get_project(project_id: int, db: Session = Depends(get_db),
                employee: AuthenticatedEmployee = Depends(get_current_employee)):
    row = db.get(ArchivedProject, project_id)
    if row is None or row.voided_at is not None:
        raise _fail(404, "project_not_found", "No such archived project.")
    return view.project_dossier(db, row, employee)


@router.get("/stats", response_model=ArchiveStats)
def archive_stats(filters: queries.ArchiveFilters = Depends(_filters), db: Session = Depends(get_db),
                  employee: AuthenticatedEmployee = Depends(get_current_employee)):
    """Counts and capacity sums by scheme, state, district and commissioning year, computed
    in SQL over the same filters as the list. Unmatched place names are grouped as typed."""
    return queries.stats(db, filters, employee)
