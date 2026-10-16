"""The placeholder registry (config/placeholder_registry.yaml): every value Grid Nova has
not supplied or confirmed yet, where it lives, and whether it is still the dummy.

Whether an entry is resolved is never stored for probed entries — it is read from the
value itself each time (see PROBES), so editing the value is what resolves it. Only
`manual` entries are resolved by hand (placeholder_resolutions, append-only).

The same module maps every marker in the source to an "anchor" — a YAML key path, or
the Python constant/function it sits in — so tests/test_placeholder_registry.py can fail
the build when a marker exists that no entry claims.
"""
from __future__ import annotations

import ast
import importlib
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Optional

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
_PATH = REPO_ROOT / "config" / "placeholder_registry.yaml"

AREAS = ("proposals", "pricing", "onboarding", "associates", "execution", "payments", "company", "policies")
BLOCKING = ("blocks_launch", "blocks_feature", "tunable")
PROBES = ("yaml_contains_marker", "yaml_equals", "constant_equals", "setting_equals", "count_query", "manual")
# An entry's recorded standing, on top of its probe. `resolved` / `superseded` (by a
# later source — e.g. Grid Nova's price list replacing a placeholder engine for every
# list-priced path) count as resolved even where the old value is still in the code;
# `partially_resolved` keeps probing and explains what is still missing. Each needs a
# status_note saying what decided it.
STATUSES = ("open", "resolved", "superseded", "partially_resolved")

# What counts as a marker. Text dummies begin with DUMMY_TEXT_PREFIX.
MARKERS = re.compile(r"\[PLACEHOLDER|\[UNCONFIRMED|DUMMY")
DUMMY_TEXT_PREFIX = "DUMMY —"
CONSTANT_PREFIX = "PLACEHOLDER_"
SCANNED_DIRS = ("app", "config", "scripts")
SCANNED_SUFFIXES = (".py", ".yaml", ".yml", ".html", ".j2")


class RegistryError(RuntimeError):
    pass


@dataclass(frozen=True)
class Entry:
    id: str
    area: str
    location: dict
    description: str
    dummy_value: Any
    needed_from_grid_nova: str
    blocking: str
    probe: str
    query: Optional[str] = None
    status: str = "open"
    status_note: Optional[str] = None

    @property
    def file(self) -> Optional[str]:
        return self.location.get("file")

    def location_label(self) -> str:
        if "env" in self.location:
            return "environment: " + ", ".join(self.location["env"])
        if "key" in self.location:
            return f"{self.location['file']} → {self.location['key']}"
        return f"{self.location['file']} → {', '.join(self.location['symbols'])}"


@dataclass(frozen=True)
class Exemption:
    file: str
    anchor: str
    reason: str


@dataclass
class Registry:
    entries: list[Entry]
    exemptions: list[Exemption]
    by_id: dict[str, Entry] = field(default_factory=dict)


def _validate(data: dict) -> Registry:
    def need(cond: bool, message: str) -> None:
        if not cond:
            raise RegistryError(f"{_PATH.name}: {message}")

    entries = []
    for raw in data.get("entries") or []:
        eid = raw.get("id")
        need(isinstance(eid, str) and eid, "every entry needs an id")
        for key in ("area", "location", "description", "needed_from_grid_nova", "blocking", "probe"):
            need(raw.get(key) not in (None, ""), f"{eid}: {key} is required")
        need("dummy_value" in raw, f"{eid}: dummy_value is required")
        need(raw["area"] in AREAS, f"{eid}: area must be one of {AREAS}")
        need(raw["blocking"] in BLOCKING, f"{eid}: blocking must be one of {BLOCKING}")
        need(raw["probe"] in PROBES, f"{eid}: probe must be one of {PROBES}")
        loc = raw["location"]
        need(isinstance(loc, dict) and (("file" in loc and ("key" in loc or "symbols" in loc)) or "env" in loc),
             f"{eid}: location needs file+key, file+symbols, or env")
        if raw["probe"] in ("yaml_contains_marker", "yaml_equals"):
            need("key" in loc, f"{eid}: a YAML probe needs location.key")
        if raw["probe"] == "constant_equals":
            need("symbols" in loc and isinstance(raw["dummy_value"], dict)
                 and set(raw["dummy_value"]) == set(loc["symbols"]),
                 f"{eid}: constant_equals needs a dummy_value for exactly each symbol")
        if raw["probe"] == "setting_equals":
            need("env" in loc and isinstance(raw["dummy_value"], dict) and set(raw["dummy_value"]) == set(loc["env"]),
                 f"{eid}: setting_equals needs a dummy_value for exactly each env setting")
        if raw["probe"] == "count_query":
            need(raw.get("query") in QUERIES, f"{eid}: query must be one of {sorted(QUERIES)}")
        status = raw.get("status", "open")
        need(status in STATUSES, f"{eid}: status must be one of {STATUSES}")
        need(status == "open" or (isinstance(raw.get("status_note"), str) and raw["status_note"].strip()),
             f"{eid}: a {status} entry needs a status_note")
        entries.append(Entry(id=eid, area=raw["area"], location=loc, description=raw["description"],
                             dummy_value=raw["dummy_value"], needed_from_grid_nova=raw["needed_from_grid_nova"],
                             blocking=raw["blocking"], probe=raw["probe"], query=raw.get("query"),
                             status=status, status_note=raw.get("status_note")))
    ids = [e.id for e in entries]
    need(len(set(ids)) == len(ids), "entry ids must be unique")
    exemptions = []
    for raw in data.get("exemptions") or []:
        need(all(raw.get(k) for k in ("file", "anchor", "reason")), "every exemption needs file, anchor and reason")
        exemptions.append(Exemption(file=raw["file"], anchor=raw["anchor"], reason=raw["reason"]))
    return Registry(entries=entries, exemptions=exemptions, by_id={e.id: e for e in entries})


@lru_cache(maxsize=1)
def load() -> Registry:
    with open(_PATH, encoding="utf-8") as fh:
        return _validate(yaml.safe_load(fh) or {})


# ── Anchors: where a line of source belongs ─────────────────────────────────────

_YAML_KEY = re.compile(r"^(\s*)(-\s+)?([A-Za-z0-9_\-]+|\"[^\"]+\"|'[^']+'):(\s|$)")
_BLOCK_SCALAR = re.compile(r":\s*[|>][-+]?\s*(#.*)?$")


def yaml_line_anchors(text: str) -> list[str]:
    """The dotted key path each line belongs to (one per line; "" for none). A
    comment belongs to the key that follows it; a block scalar's lines to its key;
    a list item's lines to the list's key."""
    lines = text.splitlines()
    anchors = [""] * len(lines)
    stack: list[tuple[int, str]] = []
    pending: list[int] = []
    block: Optional[tuple[int, str]] = None   # (indent of the block's key, path)
    for i, line in enumerate(lines):
        stripped = line.strip()
        indent = len(line) - len(line.lstrip(" "))
        if block is not None:
            if not stripped or indent > block[0]:
                anchors[i] = block[1]
                continue
            block = None
        if not stripped:
            continue
        if stripped.startswith("#"):
            pending.append(i)
            continue
        m = _YAML_KEY.match(line)
        if m:
            key_indent = len(m.group(1)) + (len(m.group(2)) if m.group(2) else 0)
            while stack and stack[-1][0] >= key_indent:
                stack.pop()
            stack.append((key_indent, m.group(3).strip("\"'")))
            path = ".".join(k for _, k in stack)
            if _BLOCK_SCALAR.search(line):
                block = (key_indent, path)
        else:
            # A bare list item keeps the list's key; anything else closes deeper keys.
            deeper = (lambda k: k > indent) if stripped.startswith("-") else (lambda k: k >= indent)
            while stack and deeper(stack[-1][0]):
                stack.pop()
            path = ".".join(k for _, k in stack)
        anchors[i] = path
        for p in pending:
            anchors[p] = path
        pending = []
    return anchors


def python_line_anchors(text: str) -> list[str]:
    """The anchor of each line: the innermost function/class (Class.method), else the
    module-level statement's assigned name, else — for a comment between statements —
    the next statement's. "__doc__" for the module docstring."""
    tree = ast.parse(text)
    n = len(text.splitlines())
    anchors = [""] * n

    def stmt_name(node) -> str:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return node.name
        if isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            return names[0] if names else ""
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            return node.target.id
        if (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str) and node is tree.body[0]):
            return "__doc__"
        return ""

    def start(node) -> int:
        decorators = getattr(node, "decorator_list", None) or []
        return min([node.lineno] + [d.lineno for d in decorators])

    covered = [False] * n
    starts = {}
    for node in tree.body:
        name = stmt_name(node)
        starts[start(node)] = name
        for line in range(start(node), node.end_lineno + 1):
            anchors[line - 1], covered[line - 1] = name, True
        if isinstance(node, ast.ClassDef):
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for line in range(start(child), child.end_lineno + 1):
                        anchors[line - 1] = f"{node.name}.{child.name}"
    # Lines between statements (comments, blanks) belong to the next statement.
    following = ""
    for line in range(n, 0, -1):
        if line in starts:
            following = starts[line]
        if not covered[line - 1]:
            anchors[line - 1] = following
    return anchors


def _module_constants(text: str) -> list[tuple[int, str]]:
    out = []
    for node in ast.parse(text).body:
        names = []
        if isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names = [node.target.id]
        out += [(node.lineno, name) for name in names if name.startswith(CONSTANT_PREFIX)]
    return out


@dataclass(frozen=True)
class MarkerHit:
    file: str
    line: int
    anchor: str
    text: str


def _source_files() -> list[Path]:
    files = []
    for d in SCANNED_DIRS:
        for path in sorted((REPO_ROOT / d).rglob("*")):
            if path.suffix in SCANNED_SUFFIXES and "__pycache__" not in path.parts and path != _PATH:
                files.append(path)
    return files


def _anchors_for(path: Path, text: str) -> list[str]:
    return python_line_anchors(text) if path.suffix == ".py" else yaml_line_anchors(text)


def scan() -> list[MarkerHit]:
    """Every marker line, and every module-level PLACEHOLDER_* constant, in the
    scanned directories (the registry file itself excluded)."""
    hits = []
    for path in _source_files():
        text = path.read_text(encoding="utf-8")
        rel = path.relative_to(REPO_ROOT).as_posix()
        anchors = _anchors_for(path, text) if path.suffix in (".py", ".yaml", ".yml") else None
        lines = text.splitlines()
        for i, line in enumerate(lines):
            if MARKERS.search(line):
                hits.append(MarkerHit(rel, i + 1, anchors[i] if anchors else "", line.strip()))
        if path.suffix == ".py":
            seen = {(h.line, h.anchor) for h in hits if h.file == rel}
            for lineno, name in _module_constants(text):
                if (lineno, name) not in seen:
                    hits.append(MarkerHit(rel, lineno, name, lines[lineno - 1].strip()))
    return hits


def _segments_match(a: list[str], b: list[str]) -> bool:
    return all(x == y or x.startswith("{") or y.startswith("{") for x, y in zip(a, b))


def claims(entry: Entry, hit: MarkerHit) -> bool:
    if entry.file != hit.file:
        return False
    if "key" in entry.location:
        # A key claims its own lines, its children, and a section comment on a parent.
        return _segments_match(entry.location["key"].split("."), hit.anchor.split("."))
    symbols = entry.location.get("symbols", [])
    return any(hit.anchor == s or hit.anchor.startswith(s + ".") for s in symbols)


def unclaimed(hits: Optional[list[MarkerHit]] = None) -> tuple[list[MarkerHit], list[Exemption]]:
    """(markers no entry or exemption claims, exemptions that match no marker)."""
    reg = load()
    hits = scan() if hits is None else hits
    used: set[int] = set()
    orphans = []
    for hit in hits:
        if any(claims(e, hit) for e in reg.entries):
            continue
        matched = [i for i, ex in enumerate(reg.exemptions) if ex.file == hit.file and ex.anchor == hit.anchor]
        if matched:
            used.update(matched)
            continue
        orphans.append(hit)
    for hit in hits:
        used.update(i for i, ex in enumerate(reg.exemptions) if ex.file == hit.file and ex.anchor == hit.anchor)
    stale = [ex for i, ex in enumerate(reg.exemptions) if i not in used]
    return orphans, stale


# ── Probes: is the dummy still in place? ────────────────────────────────────────

def _yaml_value(data: Any, dotted: str) -> Any:
    node = data
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            raise KeyError(dotted)
        node = node[part]
    return node


def _resolve_key(data: dict, key: str) -> str:
    return re.sub(r"\{([^}]+)\}", lambda m: str(_yaml_value(data, m.group(1))), key)


def _probe_yaml_marker(entry: Entry) -> bool:
    path = REPO_ROOT / entry.file
    text = path.read_text(encoding="utf-8")
    key = _resolve_key(yaml.safe_load(text) or {}, entry.location["key"])
    _yaml_value(yaml.safe_load(text) or {}, key)  # KeyError if the key has gone
    lines = text.splitlines()
    return any(MARKERS.search(lines[i]) for i, a in enumerate(yaml_line_anchors(text))
               if a == key or a.startswith(key + "."))


def _probe_yaml_equals(entry: Entry) -> bool:
    data = yaml.safe_load((REPO_ROOT / entry.file).read_text(encoding="utf-8")) or {}
    return _yaml_value(data, _resolve_key(data, entry.location["key"])) == entry.dummy_value


def _probe_constant(entry: Entry) -> bool:
    module = importlib.import_module(entry.file[:-3].replace("/", "."))
    return any(getattr(module, s) == entry.dummy_value[s] for s in entry.location["symbols"])


def _probe_setting(entry: Entry) -> bool:
    from app.config import settings
    return any(getattr(settings, name.lower()) == entry.dummy_value[name] for name in entry.location["env"])


def _equipment_catalogue_not_real(db) -> int:
    """Active dummy catalogue rows, or (when there are none) the absence of any real
    active module or inverter — either way the real catalogue is still missing."""
    from sqlalchemy import func, select
    from app.memory.models import Equipment, EquipmentCategory

    dummies = db.execute(select(func.count()).select_from(Equipment).where(
        Equipment.is_placeholder.is_(True), Equipment.is_active.is_(True))).scalar_one()
    real = db.execute(select(func.count()).select_from(Equipment).where(
        Equipment.is_placeholder.is_(False), Equipment.is_active.is_(True),
        Equipment.category.in_([EquipmentCategory.module, EquipmentCategory.inverter]))).scalar_one()
    return dummies + (0 if real else 1)


def _commission_rates_not_confirmed(db) -> int:
    """1 until a non-placeholder commission rate schedule has been published (C5-RATES)."""
    from sqlalchemy import func, select
    from app.memory.models import CommissionRateSchedule

    real = db.execute(select(func.count()).select_from(CommissionRateSchedule).where(
        CommissionRateSchedule.is_placeholder.is_(False))).scalar_one()
    return 0 if real else 1


def _project_archive_empty(db) -> int:
    """1 until at least one project from a non-voided import batch is on file (ARC-1)."""
    from sqlalchemy import func, select
    from app.memory.models import ArchivedProject

    present = db.execute(select(func.count()).select_from(ArchivedProject).where(
        ArchivedProject.voided_at.is_(None))).scalar_one()
    return 0 if present else 1


QUERIES: dict[str, Callable] = {"equipment_catalogue_not_real": _equipment_catalogue_not_real,
                                "commission_rates_not_confirmed": _commission_rates_not_confirmed,
                                "project_archive_empty": _project_archive_empty}


def _resolutions(db) -> dict:
    from sqlalchemy import select
    from app.memory.models import PlaceholderResolution
    return {r.entry_id: r for r in db.execute(select(PlaceholderResolution)).scalars()}


def evaluate(entry: Entry, db=None, resolutions: Optional[dict] = None) -> dict:
    """{"resolved": True/False/None, "error": str|None, "resolution": {...}|None}.
    None = cannot tell without a database (count_query/manual with no session).
    A `resolved` or `superseded` status is resolved whatever the probe says."""
    if entry.status in ("resolved", "superseded"):
        return {"resolved": True, "error": None,
                "resolution": {"status": entry.status, "note": entry.status_note}}
    try:
        if entry.probe == "yaml_contains_marker":
            return {"resolved": not _probe_yaml_marker(entry), "error": None, "resolution": None}
        if entry.probe == "yaml_equals":
            return {"resolved": not _probe_yaml_equals(entry), "error": None, "resolution": None}
        if entry.probe == "constant_equals":
            return {"resolved": not _probe_constant(entry), "error": None, "resolution": None}
        if entry.probe == "setting_equals":
            return {"resolved": not _probe_setting(entry), "error": None, "resolution": None}
        if db is None:
            return {"resolved": None, "error": None, "resolution": None}
        if entry.probe == "count_query":
            return {"resolved": QUERIES[entry.query](db) == 0, "error": None, "resolution": None}
        res = (resolutions if resolutions is not None else _resolutions(db)).get(entry.id)
        return {"resolved": res is not None, "error": None,
                "resolution": None if res is None else {"resolved_by": res.resolved_by,
                                                        "resolved_at": res.resolved_at.isoformat(),
                                                        "note": res.note}}
    except Exception as exc:  # a broken probe must show up, never hide the entry
        return {"resolved": False, "error": f"{type(exc).__name__}: {exc}", "resolution": None}


def evaluate_all(db=None) -> list[tuple[Entry, dict]]:
    reg = load()
    resolutions = _resolutions(db) if db is not None else {}
    return [(e, evaluate(e, db, resolutions)) for e in reg.entries]


def unresolved_ids(ids: list[str], db=None) -> list[str]:
    """Which of these entries are still dummies (unknown counts as unresolved)."""
    reg = load()
    return [i for i in ids if evaluate(reg.by_id[i], db)["resolved"] is not True]


def unresolved_launch_blockers(db=None) -> int:
    return sum(1 for e, s in evaluate_all(db) if e.blocking == "blocks_launch" and s["resolved"] is not True)
