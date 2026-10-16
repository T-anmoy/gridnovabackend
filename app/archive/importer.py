"""Project Archive CSV importer (BE-8): validate a file against the template, then write
its accepted rows in one transaction.

CSV only, UTF-8 (a BOM is accepted); Highline converts Grid Nova's sheets to the template
outside this repo, so only the template is accepted — with a clear error per column.
Every cell is data: a cell that looks like a formula is stored as the text it is and never
evaluated (CSV exports go through app/security/csv_cells.py). A dry run writes nothing.

Rows with an error are skipped and listed; a warning never blocks a row — it is kept in the
row's `data_quality_flags`. `row` in a report is the spreadsheet row: the header is row 1.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from sqlalchemy import func, insert, select, update
from sqlalchemy.orm import Session

from app.archive import config
from app.memory.models import (
    ArchiveBatchStatus, ArchiveImportBatch, ArchivedProject, ArchiveScheme, ArchiveSystemType, AuditLog,
    TerritoryBlock, TerritoryDistrict, TerritoryState,
)

TEMPLATE_COLUMNS = (
    "grid_nova_ref", "scheme_or_segment", "system_type", "capacity_kw", "pump_hp", "street_lamp_count",
    "state", "district", "block", "address_text", "pincode", "latitude", "longitude",
    "customer_name", "customer_phone", "customer_email", "discom", "consumer_number",
    "contract_value_inr", "subsidy_amount_inr",
    "module_make", "module_model", "module_wattage_wp", "module_count", "module_dcr",
    "inverter_make", "inverter_model", "inverter_capacity_kw", "inverter_count",
    "structure_type", "battery_details", "other_equipment",
    "order_date", "installation_date", "commissioning_date", "net_meter_date",
    "warranty_end_modules", "warranty_end_inverter",
    "om_status", "notes", "documents",
)
REQUIRED = ("scheme_or_segment", "state", "district", "commissioning_date")
SIZE_FIELDS = ("capacity_kw", "pump_hp", "street_lamp_count")
PERSONAL = ("customer_phone", "customer_email", "consumer_number")
PERSONAL_NOTICE = "personal data imported — visible to Admin/Management only"
EXAMPLE_ROW = {
    "grid_nova_ref": "#EXAMPLE-001 (rows starting with # are ignored)", "scheme_or_segment": "pm_surya_ghar_residential",
    "system_type": "on_grid", "capacity_kw": "3", "state": "West Bengal", "district": "Alipurduar",
    "commissioning_date": "03/2025", "module_make": "Example Make", "module_wattage_wp": "540",
    "module_count": "6", "module_dcr": "yes", "inverter_make": "Example Inverter", "inverter_capacity_kw": "3",
    "inverter_count": "1", "documents": "Commissioning report|https://example.test/report.pdf",
}

_TEXT_LIMITS = {
    "grid_nova_ref": 100, "state": 120, "district": 120, "block": 120, "customer_name": 200, "customer_phone": 32,
    "customer_email": 200, "discom": 120, "consumer_number": 60, "module_make": 120, "module_model": 120,
    "inverter_make": 120, "inverter_model": 120, "structure_type": 120, "om_status": 200, "address_text": 5000,
    "battery_details": 5000, "other_equipment": 5000, "notes": 10000,
}
_MULTILINE = {"address_text", "battery_details", "other_equipment", "notes", "documents"}
# column → (decimal places, largest value)
_DECIMALS = {"capacity_kw": (3, Decimal("999999999")), "pump_hp": (2, Decimal("999999")),
             "contract_value_inr": (2, Decimal("999999999999")), "subsidy_amount_inr": (2, Decimal("999999999999")),
             "module_wattage_wp": (2, Decimal("999999")), "inverter_capacity_kw": (3, Decimal("9999999"))}
_INTS = {"street_lamp_count": 10_000_000, "module_count": 10_000_000, "inverter_count": 1_000_000}
_DATES = ("order_date", "installation_date", "commissioning_date", "net_meter_date", "warranty_end_modules",
          "warranty_end_inverter")
_PAST_DATES = ("order_date", "installation_date", "commissioning_date", "net_meter_date")
_INDIA = (6.0, 38.0, 68.0, 98.0)   # lat min/max, lon min/max — a generous box around India
_UNIT = re.compile(r"\s*(kwp|kw|hp|wp|w)\s*$", re.I)
_PREFIX = re.compile(r"^\s*(₹|rs\.?|inr)\s*", re.I)
_NUMBER = re.compile(r"^\d+(\.\d+)?$")
_DMY = re.compile(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{4})$")
_YMD = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")
_MY = re.compile(r"^(\d{1,2})[/-](\d{4})$")
_YM = re.compile(r"^(\d{4})-(\d{1,2})$")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_RESERVED_REF = re.compile(r"^(GNE-|ARC-\d+$)", re.I)


class FileRejected(Exception):
    def __init__(self, code: str, message: str, status_code: int = 422):
        super().__init__(message)
        self.code, self.message, self.status_code = code, message, status_code

    def body(self) -> dict:
        return {"code": self.code, "message": self.message}


@dataclass
class Issue:
    row: int
    column: Optional[str]
    code: str
    message: str

    def as_dict(self) -> dict:
        return {"row": self.row, "column": self.column, "code": self.code, "message": self.message}


@dataclass
class Parsed:
    sha256: str
    filename: str
    row_count: int = 0
    accepted: list[dict] = field(default_factory=list)   # column values + source_row + flags
    errors: list[Issue] = field(default_factory=list)
    warnings: list[Issue] = field(default_factory=list)
    rejected_rows: set = field(default_factory=set)
    personal_columns: list = field(default_factory=list)
    rows_with_personal: int = 0


# ── The template ─────────────────────────────────────────────────────────────────

def template_csv() -> str:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(TEMPLATE_COLUMNS)
    writer.writerow([EXAMPLE_ROW.get(c, "") for c in TEMPLATE_COLUMNS])
    return out.getvalue()


# ── Cell parsers ─────────────────────────────────────────────────────────────────

def _clean(raw: Optional[str], multiline: bool) -> str:
    text = _CONTROL.sub("", raw or "").replace("\r\n", "\n").replace("\r", "\n")
    text = text.strip()
    return text if multiline else " ".join(text.split())


def parse_decimal(raw: str, places: int, largest: Decimal) -> Decimal:
    text = _PREFIX.sub("", raw).replace(",", "").replace(" ", "")
    text = _UNIT.sub("", text)
    if not _NUMBER.match(text):
        raise ValueError("not a number")
    value = Decimal(text)
    if value > largest:
        raise OverflowError("too large")
    return value.quantize(Decimal(1).scaleb(-places))


def parse_int(raw: str, largest: int) -> int:
    text = _PREFIX.sub("", raw).replace(",", "").replace(" ", "")
    if not _NUMBER.match(text):
        raise ValueError("not a number")
    value = Decimal(text)
    if value != value.to_integral_value():
        raise ValueError("not a whole number")
    if value > largest:
        raise OverflowError("too large")
    return int(value)


def parse_date(raw: str) -> tuple[date, bool]:
    """(date, month_only). dd/mm/yyyy, dd-mm-yyyy, yyyy-mm-dd, or a month/year such as
    03/2025 (stored as the 1st of that month)."""
    text = raw.strip()
    try:
        if m := _DMY.match(text):
            d, mo, y = int(m[1]), int(m[2]), int(m[3])
            return date(y, mo, d), False
        if m := _YMD.match(text):
            return date(int(m[1]), int(m[2]), int(m[3])), False
        if m := _MY.match(text):
            return date(int(m[2]), int(m[1]), 1), True
        if m := _YM.match(text):
            return date(int(m[1]), int(m[2]), 1), True
    except ValueError:
        raise ValueError("not a real calendar date")
    raise ValueError("unrecognised date format")


def _today():
    from app.timezones import _as_ist
    return _as_ist(datetime.now(timezone.utc)).date()


def _norm(name: Optional[str]) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()


def _enum_value(raw: str, enum_cls):
    key = re.sub(r"[^a-z0-9]+", "_", raw.strip().lower()).strip("_")
    try:
        return enum_cls(key)
    except ValueError:
        return None


def parse_documents(raw: str) -> list[dict]:
    items: list[dict] = []
    text = raw.strip()
    if text.startswith("["):
        try:
            loaded = json.loads(text)
        except ValueError:
            raise ValueError("documents is not valid JSON")
        if not isinstance(loaded, list) or not all(isinstance(i, dict) for i in loaded):
            raise ValueError('documents must be a list of {"label", "url"}')
        pairs = [(str(i.get("label") or "").strip(), str(i.get("url") or "").strip()) for i in loaded]
    else:
        pairs = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            label, sep, url = line.rpartition("|")
            pairs.append((label.strip(), url.strip()) if sep else ("", line))
    if len(pairs) > 20:
        raise ValueError("at most 20 documents per project")
    for label, url in pairs:
        if not re.match(r"^https?://\S+$", url, re.I) or len(url) > 2000:
            raise ValueError(f"document URL {url[:60]!r} must be an http(s) link")
        items.append({"label": (label or url)[:200], "url": url})
    return items


# ── Reading the file ──────────────────────────────────────────────────────────────

def decode(raw: bytes) -> str:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise FileRejected("invalid_encoding", "The file must be UTF-8. Save it as 'CSV UTF-8' and upload it again.")
    if "\x00" in text:
        raise FileRejected("invalid_encoding", "The file contains NUL characters; it is not a text CSV.")
    return text


def _header_name(cell: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", cell.strip().lower()).strip("_")


def _records(text: str):
    try:
        yield from csv.reader(io.StringIO(text, newline=""))
    except csv.Error as exc:
        raise FileRejected("invalid_csv", f"The CSV could not be read: {exc}")


class _Territory:
    def __init__(self, db: Session):
        self.states = {}
        self.by_code: dict = {}
        for s in db.execute(select(TerritoryState)).scalars():
            self.states[_norm(s.name)] = s
            self.by_code[str(s.lgd_code)] = s
            self.states[_norm(s.iso_code)] = s
        self.districts = {}
        for d in db.execute(select(TerritoryDistrict)).scalars():
            self.districts[(d.state_id, _norm(d.name))] = d
            self.districts[(d.state_id, str(d.lgd_code))] = d
        self.blocks = {}
        for b in db.execute(select(TerritoryBlock)).scalars():
            self.blocks[(b.district_id, _norm(b.name))] = b
            self.blocks[(b.district_id, str(b.lgd_code))] = b

    def match(self, state: str, district: str, block: Optional[str]):
        s = self.states.get(_norm(state)) or self.by_code.get(state.strip())
        d = self.districts.get((s.id, _norm(district))) or self.districts.get((s.id, district.strip())) if s else None
        b = (self.blocks.get((d.id, _norm(block))) or self.blocks.get((d.id, block.strip()))) if d and block else None
        return s, d, b


def _consumer_key(value: str) -> str:
    return re.sub(r"[\s\-/]", "", value).upper()


def parse(db: Session, raw: bytes, filename: str) -> Parsed:
    """Validate a file. Never writes."""
    limits = config.import_limits()
    if len(raw) > limits["max_file_mb"] * 1024 * 1024:
        raise FileRejected("file_too_large", f"The file is larger than {limits['max_file_mb']} MB.", 413)
    result = Parsed(sha256=hashlib.sha256(raw).hexdigest(), filename=filename)
    text = decode(raw)
    reader = _records(text)
    header_row = next(reader, None)
    if header_row is None:
        raise FileRejected("empty_file", "The file is empty. Use the Project Archive template.")
    header = [_header_name(c) for c in header_row]

    def file_issue(code: str, column: Optional[str], message: str, severity: str = "error") -> None:
        (result.errors if severity == "error" else result.warnings).append(Issue(1, column, code, message))

    seen: set = set()
    for name in header:
        if name in seen and name:
            file_issue("duplicate_column", name, f"Column {name!r} appears more than once.")
        seen.add(name)
    for name in header:
        if name and name not in TEMPLATE_COLUMNS:
            file_issue("unknown_column", name, f"Column {name!r} is not in the template and was ignored.", "warning")
    for name in REQUIRED:
        if name not in header:
            file_issue("missing_column", name, f"The required column {name!r} is missing from the header row.")
    if not any(c in header for c in SIZE_FIELDS):
        file_issue("missing_column", "capacity_kw",
                   "At least one size column is required: capacity_kw, pump_hp or street_lamp_count.")

    data_rows = []
    for number, cells in enumerate(reader, start=2):
        if not any(c.strip() for c in cells):
            continue
        if cells and cells[0].lstrip().startswith("#"):
            continue
        data_rows.append((number, cells))
        if len(data_rows) > limits["max_rows"]:
            raise FileRejected("too_many_rows", f"The file has more than {limits['max_rows']} rows; split it.", 413)
    result.row_count = len(data_rows)
    if any(i.code in ("duplicate_column", "missing_column") for i in result.errors):
        result.rejected_rows = {n for n, _ in data_rows}
        return result

    territory = _Territory(db)
    live_refs = set(db.execute(select(ArchivedProject.grid_nova_ref).where(
        ArchivedProject.voided_at.is_(None), ArchivedProject.grid_nova_ref.is_not(None))).scalars())
    live_consumers = {_consumer_key(c) for c in db.execute(select(ArchivedProject.consumer_number).where(
        ArchivedProject.voided_at.is_(None), ArchivedProject.consumer_number.is_not(None))).scalars() if c.strip()}
    file_refs: dict = {}
    file_consumers: dict = {}
    today = _today()
    personal_seen_columns: set = set()

    for number, cells in data_rows:
        row_errors: list[Issue] = []
        row_warnings: list[Issue] = []

        def err(col, code, msg):
            row_errors.append(Issue(number, col, code, msg))

        def warn(col, code, msg):
            row_warnings.append(Issue(number, col, code, msg))

        if len(cells) > len(header):
            err(None, "too_many_cells", f"The row has {len(cells)} cells but the header has {len(header)} columns.")
        padded = list(cells) + [""] * (len(header) - len(cells))
        raw_by = {}
        for name, value in zip(header, padded):
            if name in TEMPLATE_COLUMNS:
                raw_by[name] = _clean(value, name in _MULTILINE)
        out: dict[str, Any] = {}

        for col in REQUIRED:
            if not raw_by.get(col):
                err(col, "missing_required", f"{col} is required.")

        # text columns
        for col, limit in _TEXT_LIMITS.items():
            value = raw_by.get(col, "")
            if not value:
                continue
            if len(value) > limit:
                err(col, "too_long", f"{col} is longer than {limit} characters.")
            else:
                out[col] = value

        if raw_by.get("scheme_or_segment"):
            scheme = _enum_value(raw_by["scheme_or_segment"], ArchiveScheme)
            if scheme is None:
                err("scheme_or_segment", "invalid_choice",
                    f"{raw_by['scheme_or_segment']!r} is not a scheme. Use one of: "
                    + ", ".join(s.value for s in ArchiveScheme) + ".")
            else:
                out["scheme_or_segment"] = scheme
        if raw_by.get("system_type"):
            kind = _enum_value(raw_by["system_type"], ArchiveSystemType)
            if kind is None:
                err("system_type", "invalid_choice",
                    f"{raw_by['system_type']!r} is not a system type. Use one of: "
                    + ", ".join(s.value for s in ArchiveSystemType) + ".")
            else:
                out["system_type"] = kind
        if raw_by.get("module_dcr"):
            key = re.sub(r"[^a-z]+", "", raw_by["module_dcr"].lower())
            dcr = {"yes": "yes", "y": "yes", "true": "yes", "no": "no", "n": "no", "false": "no",
                   "unknown": "unknown", "na": "unknown", "": "unknown"}.get(key)
            if dcr is None:
                err("module_dcr", "invalid_choice", "module_dcr must be yes, no or unknown.")
            else:
                out["module_dcr"] = dcr

        # numbers
        for col, (places, largest) in _DECIMALS.items():
            value = raw_by.get(col, "")
            if not value:
                continue
            try:
                number_value = parse_decimal(value, places, largest)
            except OverflowError:
                err(col, "out_of_range", f"{col} is larger than the largest value allowed.")
                continue
            except ValueError:
                if col == "capacity_kw":
                    warn(col, "capacity_not_a_number", f"capacity_kw {value!r} is not a number; it was not stored.")
                else:
                    err(col, "invalid_number", f"{col} {value!r} is not a number.")
                continue
            if col == "capacity_kw" and number_value <= 0:
                warn(col, "capacity_not_a_number", "capacity_kw must be above zero; it was not stored.")
                continue
            out[col] = number_value
        for col, largest in _INTS.items():
            value = raw_by.get(col, "")
            if not value:
                continue
            try:
                out[col] = parse_int(value, largest)
            except OverflowError:
                err(col, "out_of_range", f"{col} is larger than the largest value allowed.")
            except ValueError:
                err(col, "invalid_number", f"{col} {value!r} is not a whole number.")
        scheme_now = out.get("scheme_or_segment")
        if not out.get("capacity_kw") and scheme_now not in (ArchiveScheme.pm_kusum_pump, ArchiveScheme.street_lighting) \
                and "capacity_not_a_number" not in {w.code for w in row_warnings}:
            warn("capacity_kw", "missing_capacity", "capacity_kw is blank.")
        if not any(out.get(c) for c in SIZE_FIELDS):
            err(None, "missing_size", "At least one of capacity_kw, pump_hp or street_lamp_count is required.")

        # dates
        flags_month_only = []
        for col in _DATES:
            value = raw_by.get(col, "")
            if not value:
                continue
            try:
                parsed, month_only = parse_date(value)
            except ValueError as exc:
                err(col, "invalid_date", f"{col} {value!r}: {exc}. Use dd/mm/yyyy, dd-mm-yyyy, yyyy-mm-dd or mm/yyyy.")
                continue
            if not 1990 <= parsed.year <= 2100:
                err(col, "invalid_date", f"{col} {value!r} is outside 1990-2100.")
                continue
            out[col] = parsed
            if month_only:
                flags_month_only.append(col)
                warn(col, "date_month_only", f"{col} {value!r} has no day; stored as the 1st of the month.")
            if col in _PAST_DATES and parsed > today:
                warn(col, "future_date", f"{col} {parsed.isoformat()} is in the future.")
        a, b = out.get("installation_date"), out.get("commissioning_date")
        if a and b and b < a:
            warn("commissioning_date", "date_order", "commissioning_date is before installation_date.")

        # contact and place
        email = out.get("customer_email")
        if email and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
            warn("customer_email", "invalid_email", "customer_email does not look like an email address.")
        pin = raw_by.get("pincode", "")
        if pin:
            if re.fullmatch(r"\d{6}", pin):
                out["pincode"] = pin
            else:
                warn("pincode", "invalid_pincode", f"pincode {pin!r} is not six digits; it was not stored.")
        coords = {}
        for col, lo, hi in (("latitude", -90, 90), ("longitude", -180, 180)):
            value = raw_by.get(col, "")
            if not value:
                continue
            try:
                num = float(value.replace(",", "."))
            except ValueError:
                err(col, "invalid_number", f"{col} {value!r} is not a number.")
                continue
            if not (lo <= num <= hi) or num != num:
                err(col, "out_of_range", f"{col} must be between {lo} and {hi}.")
                continue
            coords[col] = num
            out[col] = num
        if len(coords) == 2 and not (_INDIA[0] <= coords["latitude"] <= _INDIA[1]
                                     and _INDIA[2] <= coords["longitude"] <= _INDIA[3]):
            warn("latitude", "coordinates_outside_india", "The coordinates are outside India.")
        elif len(coords) == 1:
            warn("latitude" if "latitude" in coords else "longitude", "incomplete_coordinates",
                 "Only one of latitude and longitude was given.")

        if raw_by.get("documents"):
            try:
                out["documents"] = parse_documents(raw_by["documents"])
            except ValueError as exc:
                err("documents", "invalid_documents", str(exc))

        # place in the territory master
        if out.get("state") and out.get("district"):
            s, d, blk = territory.match(out["state"], out["district"], out.get("block"))
            if s is None:
                warn("state", "unknown_state", f"State {out['state']!r} is not in the territory master.")
            else:
                out["state_id"] = s.id
                if d is None:
                    warn("district", "unknown_district", f"District {out['district']!r} is not in {s.name}.")
                else:
                    out["district_id"] = d.id
                    if out.get("block"):
                        if blk is None:
                            warn("block", "unknown_block", f"Block {out['block']!r} is not in {d.name}.")
                        else:
                            out["block_id"] = blk.id

        # identity: refs and consumer numbers
        ref = out.get("grid_nova_ref")
        if ref:
            if _RESERVED_REF.match(ref):
                err("grid_nova_ref", "reserved_ref", "grid_nova_ref may not start with GNE- or look like ARC-<number>.")
            elif ref in live_refs:
                err("grid_nova_ref", "duplicate_grid_nova_ref", f"{ref!r} is already in the archive.")
            elif ref in file_refs:
                err("grid_nova_ref", "duplicate_grid_nova_ref",
                    f"{ref!r} already appears in row {file_refs[ref]} of this file.")
        consumer = out.get("consumer_number")
        if consumer:
            key = _consumer_key(consumer)
            if key in live_consumers or key in file_consumers:
                where = f"row {file_consumers[key]}" if key in file_consumers else "the archive"
                warn("consumer_number", "duplicate_consumer_number",
                     f"This consumer number is already on {where}.")

        result.errors.extend(row_errors)
        result.warnings.extend(row_warnings)
        if row_errors:
            result.rejected_rows.add(number)
            continue
        if ref:
            file_refs[ref] = number
        if consumer:
            file_consumers.setdefault(_consumer_key(consumer), number)
        had_personal = [c for c in PERSONAL if out.get(c)]
        if had_personal:
            result.rows_with_personal += 1
            personal_seen_columns.update(had_personal)
        out["data_quality_flags"] = [{"code": w.code, "column": w.column, "message": w.message}
                                     for w in row_warnings]
        out["source_row"] = number
        result.accepted.append(out)
    result.personal_columns = [c for c in PERSONAL if c in personal_seen_columns]
    return result


# ── Report and write ──────────────────────────────────────────────────────────────

def report(parsed: Parsed, *, dry_run: bool, batch_id: Optional[int] = None) -> dict:
    cap = config.import_limits()["max_report_issues"]
    by_code: dict = {}
    for issue in parsed.errors + parsed.warnings:
        by_code[issue.code] = by_code.get(issue.code, 0) + 1
    warnings = [i.as_dict() for i in parsed.warnings[:cap]]
    notices = []
    if parsed.rows_with_personal:
        notices.append({"code": "personal_data_present", "columns": parsed.personal_columns,
                        "rows": parsed.rows_with_personal, "message": PERSONAL_NOTICE})
    return {
        "batch_id": batch_id, "dry_run": dry_run, "filename": parsed.filename,
        "batch_token": parsed.sha256, "sha256": parsed.sha256,
        "row_count": parsed.row_count, "accepted_count": len(parsed.accepted),
        "rejected_count": parsed.row_count - len(parsed.accepted),
        "error_count": len(parsed.errors), "warning_count": len(parsed.warnings),
        "counts_by_code": dict(sorted(by_code.items())),
        "errors": [i.as_dict() for i in parsed.errors[:cap]], "errors_truncated": len(parsed.errors) > cap,
        "warnings": warnings, "warnings_truncated": len(parsed.warnings) > cap,
        "notices": notices,
    }


def commit(db: Session, parsed: Parsed, actor: str) -> ArchiveImportBatch:
    """Write the accepted rows and the batch in one transaction (the caller's session)."""
    now = datetime.now(timezone.utc)
    body = report(parsed, dry_run=False)
    batch = ArchiveImportBatch(uploaded_by=actor, uploaded_at=now, filename=parsed.filename[:255],
                               sha256=parsed.sha256, row_count=parsed.row_count,
                               accepted_count=len(parsed.accepted), rejected_count=body["rejected_count"],
                               status=ArchiveBatchStatus.imported, report=dict(body))
    db.add(batch)
    db.flush()
    body["batch_id"] = batch.id
    batch.report = dict(body)
    # Every row names every column, so the bulk insert runs as a few big statements.
    blank = {c.name: None for c in ArchivedProject.__table__.columns if c.name != "id"}
    rows = [{**blank, "import_batch_id": batch.id, "created_at": now, "documents": [], "data_quality_flags": [], **out}
            for out in parsed.accepted]
    for start in range(0, len(rows), 1000):
        db.execute(insert(ArchivedProject), rows[start:start + 1000])
    db.add(AuditLog(entity_type="archive_import", entity_id=str(batch.id), action="imported", performed_by=actor,
                    after_value={"filename": batch.filename, "sha256": batch.sha256, "row_count": batch.row_count,
                                 "accepted": batch.accepted_count, "rejected": batch.rejected_count,
                                 "warnings": len(parsed.warnings), "personal_data": bool(parsed.rows_with_personal)}))
    db.commit()
    return batch


def void(db: Session, batch: ArchiveImportBatch, actor: str, reason: str) -> ArchiveImportBatch:
    now = datetime.now(timezone.utc)
    hidden = db.execute(update(ArchivedProject).where(ArchivedProject.import_batch_id == batch.id,
                                                      ArchivedProject.voided_at.is_(None))
                        .values(voided_at=now)).rowcount
    batch.status = ArchiveBatchStatus.voided
    batch.voided_by, batch.voided_at, batch.void_reason = actor, now, reason
    db.add(AuditLog(entity_type="archive_import", entity_id=str(batch.id), action="voided", performed_by=actor,
                    before_value={"status": "imported"},
                    after_value={"status": "voided", "reason": reason, "rows_hidden": hidden}))
    db.commit()
    return batch
