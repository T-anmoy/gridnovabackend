"""Filters, paging and statistics over the archive (BE-8). Filtering and counting happen in
SQL, inside the caller's role: the free-text search only looks at fields the role may see,
so a search can never reveal a hidden value by matching it."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.archive import config
from app.memory.models import ArchivedProject, ArchiveScheme, TerritoryDistrict, TerritoryState


def _bad(code: str, message: str, field: str) -> HTTPException:
    return HTTPException(status_code=422, detail={"code": code, "message": message, "field": field})


@dataclass(frozen=True)
class ArchiveFilters:
    q: Optional[str] = None
    state: Optional[str] = None
    district: Optional[str] = None
    scheme: Optional[ArchiveScheme] = None
    capacity_min: Optional[Decimal] = None
    capacity_max: Optional[Decimal] = None
    commissioned_from: Optional[date] = None
    commissioned_to: Optional[date] = None
    module_make: Optional[str] = None
    inverter_make: Optional[str] = None


def _decimal(value: Optional[str], field: str) -> Optional[Decimal]:
    if value is None or not value.strip():
        return None
    try:
        out = Decimal(value.strip())
    except InvalidOperation:
        raise _bad("invalid_number", f"{field} must be a number.", field)
    if not out.is_finite() or out < 0:
        raise _bad("invalid_number", f"{field} must be a number of zero or more.", field)
    return out


def _day(value: Optional[str], field: str) -> Optional[date]:
    if value is None or not value.strip():
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        raise _bad("invalid_date", f"{field} must be a calendar day, YYYY-MM-DD.", field)


def parse_filters(q=None, state=None, district=None, scheme=None, capacity_min=None, capacity_max=None,
                  commissioned_from=None, commissioned_to=None, module_make=None, inverter_make=None) -> ArchiveFilters:
    chosen = None
    if scheme and scheme.strip():
        try:
            chosen = ArchiveScheme(scheme.strip().lower())
        except ValueError:
            allowed = ", ".join(s.value for s in ArchiveScheme)
            raise _bad("unknown_scheme", f"Unknown scheme {scheme!r}. Valid schemes: {allowed}.", "scheme")
    lo, hi = _decimal(capacity_min, "capacity_min"), _decimal(capacity_max, "capacity_max")
    if lo is not None and hi is not None and lo > hi:
        raise _bad("invalid_range", "capacity_min is above capacity_max.", "capacity_min")
    a, b = _day(commissioned_from, "commissioned_from"), _day(commissioned_to, "commissioned_to")
    if a is not None and b is not None and a > b:
        raise _bad("invalid_range", "commissioned_from is after commissioned_to.", "commissioned_from")

    def text(v):
        return v.strip() if v and v.strip() else None
    return ArchiveFilters(q=text(q), state=text(state), district=text(district), scheme=chosen, capacity_min=lo,
                          capacity_max=hi, commissioned_from=a, commissioned_to=b, module_make=text(module_make),
                          inverter_make=text(inverter_make))


def _like(column, term: str):
    escaped = term.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return func.lower(column).like(f"%{escaped}%", escape="\\")


def conditions(f: ArchiveFilters, role) -> list:
    out = [ArchivedProject.voided_at.is_(None)]
    if f.state:
        names = select(TerritoryState.id).where(func.lower(TerritoryState.name) == f.state.lower())
        out.append(or_(func.lower(ArchivedProject.state) == f.state.lower(), ArchivedProject.state_id.in_(names)))
    if f.district:
        names = select(TerritoryDistrict.id).where(func.lower(TerritoryDistrict.name) == f.district.lower())
        out.append(or_(func.lower(ArchivedProject.district) == f.district.lower(),
                       ArchivedProject.district_id.in_(names)))
    if f.scheme:
        out.append(ArchivedProject.scheme_or_segment == f.scheme)
    if f.capacity_min is not None:
        out.append(ArchivedProject.capacity_kw >= f.capacity_min)
    if f.capacity_max is not None:
        out.append(ArchivedProject.capacity_kw <= f.capacity_max)
    if f.commissioned_from:
        out.append(ArchivedProject.commissioning_date >= f.commissioned_from)
    if f.commissioned_to:
        out.append(ArchivedProject.commissioning_date <= f.commissioned_to)
    if f.module_make:
        out.append(_like(ArchivedProject.module_make, f.module_make))
    if f.inverter_make:
        out.append(_like(ArchivedProject.inverter_make, f.inverter_make))
    if f.q:
        seen = config.visible_fields(role)
        parts = [_like(ArchivedProject.grid_nova_ref, f.q)]
        for name in ("customer_name", "address_text", "block", "district", "state"):
            if name in seen:
                parts.append(_like(getattr(ArchivedProject, name), f.q))
        out.append(or_(*parts))
    return out


def page(db: Session, f: ArchiveFilters, role, page_number: int, page_size: int) -> tuple[list, int]:
    where = conditions(f, role)
    total = db.execute(select(func.count()).select_from(ArchivedProject).where(*where)).scalar_one()
    rows = list(db.execute(select(ArchivedProject).where(*where)
                           .order_by(ArchivedProject.commissioning_date.desc(), ArchivedProject.id.desc())
                           .limit(page_size).offset((page_number - 1) * page_size)).scalars())
    return rows, total


def _grouped(db: Session, where: list, *keys) -> list[dict]:
    labels = [k.label(f"k{i}") for i, k in enumerate(keys)]
    q = (select(*labels, func.count(ArchivedProject.id).label("n"),
                func.coalesce(func.sum(ArchivedProject.capacity_kw), 0).label("kw"))
         .select_from(ArchivedProject)
         .outerjoin(TerritoryState, TerritoryState.id == ArchivedProject.state_id)
         .outerjoin(TerritoryDistrict, TerritoryDistrict.id == ArchivedProject.district_id)
         .where(*where).group_by(*labels).order_by(func.count(ArchivedProject.id).desc(), *labels))
    out = []
    for row in db.execute(q):
        *key, n, kw = row
        out.append((key, int(n), str(Decimal(str(kw)).quantize(Decimal("0.001")))))
    return out


def stats(db: Session, f: ArchiveFilters, role) -> dict:
    where = conditions(f, role)
    state = func.coalesce(TerritoryState.name, ArchivedProject.state)
    district = func.coalesce(TerritoryDistrict.name, ArchivedProject.district)
    year = func.extract("year", ArchivedProject.commissioning_date)
    total_n, total_kw = db.execute(select(func.count(ArchivedProject.id),
                                          func.coalesce(func.sum(ArchivedProject.capacity_kw), 0))
                                   .where(*where)).one()
    return {
        "total": {"count": int(total_n), "capacity_kw": str(Decimal(str(total_kw)).quantize(Decimal("0.001")))},
        "by_scheme": [{"scheme": k[0].value, "count": n, "capacity_kw": kw}
                      for k, n, kw in _grouped(db, where, ArchivedProject.scheme_or_segment)],
        "by_state": [{"state": k[0], "count": n, "capacity_kw": kw} for k, n, kw in _grouped(db, where, state)],
        "by_district": [{"state": k[0], "district": k[1], "count": n, "capacity_kw": kw}
                        for k, n, kw in _grouped(db, where, state, district)],
        "by_year": [{"year": int(k[0]), "count": n, "capacity_kw": kw}
                    for k, n, kw in sorted(_grouped(db, where, year), key=lambda r: r[0][0])],
    }
