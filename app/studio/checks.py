"""The Proposal Studio proof-check (BE-18 §5): deterministic, run on every save and again on
the exact PDF that is issued. No AI.

    run(doc, figs, ...)  → [{id, level: error|warning, field, message}]

Errors block approval and issue. Warnings need an explicit "I have checked this"
acknowledgement (recorded in the proposal's event log) before approval or issue; a spelling
warning can instead be ignored for the word on this proposal.

The rendered-document checks read the PDF itself (pypdf): the page count for the layout,
the page-footer line on every page but the last, the QR code and the letterhead on the last
page only (found by the images each page actually draws), every "<n> kWp" printed, and any
leftover token or placeholder marker in the text.
"""
from __future__ import annotations

import hashlib
import io
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from typing import Optional

from app.proposals import document as d
from app.studio import config as studio_config

ERROR, WARNING = "error", "warning"
# Rendered text that must never reach a customer.
LEFTOVER = re.compile(r"\{[a-z_0-9]+\}|\[PLACEHOLDER|\[UNCONFIRMED|DUMMY|\bNone\b|\bnan\b|\bundefined\b")
CAPACITY = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*kWp", re.IGNORECASE)
GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z0-9]{10}[0-9A-Z]Z[0-9A-Z]$")
_GSTIN_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
LETTERHEAD_SIZE = (1449, 293)      # app/pdf/assets/footer-letterhead.jpg, pixels
_TOKEN = re.compile(r"[\w'’-]+")
_BRITISH = ((r"isation$", "ization"), (r"ise$", "ize"), (r"ised$", "ized"), (r"ising$", "izing"), (r"our$", "or"),
            (r"tre$", "ter"), (r"lled$", "led"), (r"lling$", "ling"), (r"ogue$", "og"))


def check(cid: str, level: str, field: Optional[str], message: str) -> dict:
    return {"id": cid, "level": level, "field": field, "message": message}


def fingerprint(item: dict) -> str:
    """Identifies one warning across saves, so an acknowledgement survives an unrelated edit
    but not a change to what was warned about."""
    return hashlib.sha256(f'{item["id"]}|{item["field"]}|{item["message"]}'.encode()).hexdigest()[:24]


# ── Amount in words → number (the engine's Indian-system words) ──────────────────

_SMALL = {w: i for i, w in enumerate(("Zero", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine",
                                      "Ten", "Eleven", "Twelve", "Thirteen", "Fourteen", "Fifteen", "Sixteen",
                                      "Seventeen", "Eighteen", "Nineteen"))}
_TENS = {"Twenty": 20, "Thirty": 30, "Forty": 40, "Fifty": 50, "Sixty": 60, "Seventy": 70, "Eighty": 80, "Ninety": 90}


class WordsError(ValueError):
    pass


def _whole(words: list[str]) -> int:
    total, current = 0, 0
    for word in words:
        if word in _SMALL:
            current += _SMALL[word]
        elif word in _TENS:
            current += _TENS[word]
        elif "-" in word:
            tens, _, unit = word.partition("-")
            if tens not in _TENS or unit not in _SMALL or not 0 < _SMALL[unit] < 10:
                raise WordsError(f"not a number word: {word!r}")
            current += _TENS[tens] + _SMALL[unit]
        elif word == "Hundred":
            current *= 100
        elif word == "Thousand":
            total, current = total + current * 1000, 0
        elif word == "Lakh":
            total, current = total + current * 100_000, 0
        elif word == "Crore":
            total, current = (total + current) * 10_000_000, 0
        else:
            raise WordsError(f"not a number word: {word!r}")
    return total + current


def words_to_amount(text: str) -> Decimal:
    """'Rupees Two Lakh One Thousand Four Hundred Sixty-Five and Fifty Paise Only' → 201465.50."""
    words = text.split()
    if len(words) < 3 or words[0] != "Rupees" or words[-1] != "Only":
        raise WordsError("an amount in words reads 'Rupees … Only'")
    body = words[1:-1]
    paise = 0
    if "and" in body:
        i = body.index("and")
        if body[-1] != "Paise":
            raise WordsError("'and' must introduce the paise")
        paise = _whole(body[i + 1:-1])
        body = body[:i]
        if not 0 < paise < 100:
            raise WordsError("paise must be 1–99")
    return Decimal(_whole(body)) + Decimal(paise) / 100


# ── GSTIN ───────────────────────────────────────────────────────────────────────

def gstin_valid(gstin: str) -> bool:
    """Format (state code, PAN, entity, Z, check) and the mod-36 check character."""
    gstin = (gstin or "").strip().upper()
    if not GSTIN_RE.match(gstin):
        return False
    total = 0
    for i, ch in enumerate(gstin[:14]):
        value = _GSTIN_CHARS.index(ch) * (2 if i % 2 else 1)
        total += value // 36 + value % 36
    return _GSTIN_CHARS[(36 - total % 36) % 36] == gstin[14]


# ── Rendered PDF ────────────────────────────────────────────────────────────────

def _images_drawn(obj, resources, reader, out: set, depth: int = 0) -> None:
    """(width, height) of every image a page (or form / pattern) actually draws."""
    from pypdf.generic import ContentStream, NameObject
    if depth > 6:
        return
    res = obj.get("/Resources") or resources
    res = res.get_object() if res is not None else {}
    contents = obj.get_contents() if hasattr(obj, "get_contents") else obj
    if contents is None:
        return
    xobjects = res.get("/XObject")
    xobjects = xobjects.get_object() if xobjects else {}
    patterns = res.get("/Pattern")
    patterns = patterns.get_object() if patterns else {}
    for operands, op in ContentStream(contents, reader).operations:
        if op == b"Do" and operands:
            target = xobjects.get(operands[0])
            if target is None:
                continue
            target = target.get_object()
            if target.get("/Subtype") == "/Image":
                out.add((int(target["/Width"]), int(target["/Height"])))
            else:
                _images_drawn(target, res, reader, out, depth + 1)
        elif op in (b"scn", b"SCN") and operands and isinstance(operands[-1], NameObject):
            target = patterns.get(operands[-1])
            if target is not None:
                _images_drawn(target.get_object(), res, reader, out, depth + 1)


def read_pdf(pdf: bytes) -> dict:
    """{"texts": [page text], "images": [set of (w, h) drawn on each page], "metadata": {…}}."""
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(pdf))
    texts, images = [], []
    for page in reader.pages:
        texts.append(page.extract_text() or "")
        drawn: set = set()
        _images_drawn(page, None, reader, drawn)
        images.append(drawn)
    meta = reader.metadata or {}
    return {"texts": texts, "images": images, "metadata": {k: str(v) for k, v in meta.items()},
            "pdfa": "/OutputIntents" in reader.trailer["/Root"]}


def pdf_checks(doc: d.StudioDocument, pdf: bytes, *, qr_size: Optional[tuple] = None) -> list[dict]:
    """The checks that read the rendered PDF."""
    out = []
    info = read_pdf(pdf)
    texts, images = info["texts"], info["images"]
    pages = len(texts)
    low, high = studio_config.load()["page_ranges"][d.layout(doc)]
    if not low <= pages <= high:
        out.append(check("page_count", ERROR, None, f"The PDF has {pages} pages; a {d.layout(doc)} proposal has "
                                                   f"{low}–{high}."))
    whole = "\n".join(texts)
    leftover = LEFTOVER.search(whole)
    if leftover:
        out.append(check("leftover_text", ERROR, None, f"The PDF still contains {leftover.group(0)!r}."))
    for n, text in enumerate(texts[:-1], 1):
        if not re.search(rf"Page\s*{n}\s*of\s*{pages}", text):
            out.append(check("footer_missing", ERROR, None, f"Page {n} is missing the page-footer line."))
    if pages and re.search(rf"Page\s*{pages}\s*of\s*{pages}", texts[-1]):
        out.append(check("footer_on_last_page", ERROR, None, "The last page carries the page-footer line instead "
                                                             "of the letterhead."))
    last = images[-1] if images else set()
    if LETTERHEAD_SIZE not in last:
        out.append(check("letterhead_missing", ERROR, None, "The last page is missing the letterhead."))
    if qr_size is not None and tuple(qr_size) not in last:
        out.append(check("qr_missing", ERROR, None, "The last page is missing the verification QR code."))
    for n, drawn in enumerate(images[:-1], 1):
        if LETTERHEAD_SIZE in drawn or (qr_size is not None and tuple(qr_size) in drawn):
            out.append(check("letterhead_misplaced", ERROR, None, f"Page {n} shows the letterhead or QR code, "
                                                                  "which belong on the last page only."))
    if doc.plant.capacity_kwp is not None and d.layout(doc) != "itemised":
        allowed = {Decimal(d.kw_text(doc.plant.capacity_kwp))}
        if doc.pm_surya_ghar.portal_registered and doc.pm_surya_ghar.approved_capacity_kwp:
            allowed.add(Decimal(d.kw_text(doc.pm_surya_ghar.approved_capacity_kwp)))
        seen = set()
        for m in CAPACITY.finditer(whole):
            try:
                seen.add(Decimal(m.group(1).replace(",", "")))
            except InvalidOperation:
                continue
        wrong = sorted(seen - allowed)
        if wrong:
            out.append(check("capacity_mismatch", ERROR, "plant.capacity_kwp",
                             f"The PDF prints {', '.join(d.kw_text(w) + ' kWp' for w in wrong)} besides the plant's "
                             f"{d.kw_text(doc.plant.capacity_kwp)} kWp."))
    return out


# ── Spelling ────────────────────────────────────────────────────────────────────

@lru_cache(maxsize=1)
def _speller():
    from spellchecker import SpellChecker
    speller = SpellChecker(language="en", distance=1)
    speller.word_frequency.load_words([w.lower() for w in studio_config.load()["spell_allow"]])
    return speller


def staff_text(doc: d.StudioDocument) -> list[tuple[str, str]]:
    """(field, text) of everything staff typed that is printed (names and addresses aside).
    A defaulted section staff never edited is Grid Nova's config text, not checked here."""
    out = [(f"customer_scope[{i}]", t) for i, t in enumerate(doc.customer_scope)] \
        if "customer_scope" in doc.edited else []
    out += [(f"notes[{i}]", t) for i, t in enumerate(doc.notes)]
    out += [(f"notes_free[{i}]", t) for i, t in enumerate(doc.notes_free)]
    if "boq" in doc.edited:
        out += [(f"boq[{i}].description", r.description) for i, r in enumerate(doc.boq)]
    out += [(f"items[{i}].description", r.description) for i, r in enumerate(doc.items)]
    out += [(f"items[{i}].specification", r.specification) for i, r in enumerate(doc.items) if r.specification]
    if doc.cover_highlight:
        out.append(("cover_highlight", doc.cover_highlight))
    if doc.price.discount is not None:
        out.append(("price.discount.reason", doc.price.discount.reason))
    return out


def spelling(doc: d.StudioDocument, ignore: set[str]) -> list[dict]:
    speller = _speller()
    allow = {w.lower() for w in studio_config.load()["spell_allow"]} | {w.lower() for w in ignore}
    out, seen = [], set()

    def known(word: str) -> bool:
        if word in allow or not speller.unknown([word]):
            return True
        return any(re.search(pat, word) and not speller.unknown([re.sub(pat, rep, word)]) for pat, rep in _BRITISH)

    for field, text in staff_text(doc):
        text = re.sub(r"\{[a-z_0-9]+\}", " ", text)
        for word in _TOKEN.findall(text):
            word = re.sub(r"['’]s$", "", word).strip("'’-_")
            key = word.lower()
            if (len(key) < 3 or any(c.isdigit() or c == "_" for c in key) or word.isupper() or key in seen
                    or (not word.islower() and not word.istitle())):
                continue
            if not known(key) and not all(known(p) for p in key.split("-") if p):
                seen.add(key)
                guess = speller.correction(key)
                hint = f" (did you mean '{guess}'?)" if guess and guess != key else ""
                out.append(check("spelling", WARNING, field, f"'{word}' may be misspelt{hint}.") | {"word": key})
    return out


# ── The whole check ─────────────────────────────────────────────────────────────

def document_checks(doc: d.StudioDocument, figs: dict, *, today: date, valid_until: Optional[date] = None,
                    spell_ignore: Optional[list] = None, customer=None, db=None) -> list[dict]:
    """Every check that reads the document and its figures (no PDF)."""
    out = []
    for m in d.studio_missing_required(doc, figs):
        out.append(check("missing_required", ERROR, m["field"], f"Missing: {m['message']}."))
    for b in d.content_blockers(doc):
        out.append(check("content_not_ready", ERROR, None, f"Waiting for {b['message']} ({b['registry']})."))
    if figs["total"] is not None:
        basic, gst, total = (Decimal(figs[k]) for k in ("basic", "gst", "total"))
        if basic + gst != total:
            out.append(check("total_mismatch", ERROR, "price", "Basic price + GST does not equal the total."))
        if figs["payments"] and sum(Decimal(p["amount"]) for p in figs["payments"]) != total:
            out.append(check("milestones_mismatch", ERROR, "payment_terms",
                             "The payment milestones do not add up to the total."))
        for key in ("basic", "gst", "total"):
            words = figs["words"].get(key)
            if words is None:
                continue
            try:
                ok = words_to_amount(words) == Decimal(figs[key])
            except WordsError:
                ok = False
            if not ok:
                out.append(check("words_mismatch", ERROR, "price", f"The {key} amount in words does not read back "
                                                                  f"as {figs[key]}."))
    if doc.payment_terms and sum(t.percent for t in doc.payment_terms) != 100:
        out.append(check("payment_percent", ERROR, "payment_terms", "Payment percentages must total 100."))
    sub = figs["subsidy"]
    if sub.get("applies"):
        eligible = (doc.pm_surya_ghar.subsidy_opted and doc.plant.dcr
                    and (doc.segment == "residential"))
        if not eligible:
            out.append(check("subsidy_ineligible", ERROR, "pm_surya_ghar",
                             "A subsidy is shown but the proposal is not eligible (residential or housing society, "
                             "subsidy opted, DCR modules)."))
        cap = _subsidy_cap(doc)
        if cap is not None and Decimal(sub["total"]) > cap:
            out.append(check("subsidy_above_cap", ERROR, "pm_surya_ghar", f"The subsidy exceeds its cap of Rs. {cap}."))
    mobile = doc.addressee.mobile
    if mobile is not None and not re.fullmatch(r"[6-9]\d{9}", mobile):
        out.append(check("mobile_invalid", ERROR, "addressee.mobile", "The mobile number must be 10 digits."))
    if customer is not None and getattr(customer, "gstin", None) and not gstin_valid(customer.gstin):
        out.append(check("gstin_invalid", ERROR, "customer.gstin", "The customer's GSTIN fails its check digit."))
    if valid_until is not None and valid_until < today:
        out.append(check("validity_past", ERROR, "validity_days", "The validity date is in the past."))
    if db is not None and d.layout(doc) != "itemised":
        allowed = d.allowed_makes(db)
        for i, row in enumerate(doc.boq):
            unknown = [p.strip() for p in row.make.split("/") if p.strip() and p.strip().lower() not in allowed]
            if unknown:
                out.append(check("make_not_in_catalogue", ERROR, f"boq[{i}].make",
                                 f"Make(s) {', '.join(unknown)} are not in the catalogue or price list."))
    if db is not None:
        allowed = d.allowed_makes(db)
        for i, item in enumerate(doc.items):
            unknown = [p.strip() for p in item.make.split("/") if p.strip() and p.strip().lower() not in allowed]
            if unknown:
                out.append(check("make_not_in_catalogue", ERROR, f"items[{i}].make",
                                 f"Make(s) {', '.join(unknown)} are not in the catalogue or price list."))
    out += _warnings(doc, figs)
    out += spelling(doc, set(spell_ignore or []))
    return out


def _subsidy_cap(doc: d.StudioDocument) -> Optional[Decimal]:
    from app.estimate.config import subsidy_config
    from app.estimate import subsidy as pm_surya_ghar
    central = subsidy_config()["central"]
    cat = pm_surya_ghar.category(doc.site.state)
    if doc.housing_society:
        soc = central["housing_society"]
        return Decimal(soc["rate_per_kwp"][cat]) * Decimal(soc["max_kwp"])
    return Decimal(central["residential"][cat]["max_inr"])


def _warnings(doc: d.StudioDocument, figs: dict) -> list[dict]:
    out = []
    cfg = studio_config.load()
    cap = doc.plant.capacity_kwp
    if cap is not None and doc.connection.sanctioned_load_kw and cap > doc.connection.sanctioned_load_kw:
        out.append(check("capacity_above_sanctioned", WARNING, "plant.capacity_kwp",
                         f"{d.kw_text(cap)} kWp is above the sanctioned load of "
                         f"{d.kw_text(doc.connection.sanctioned_load_kw)} kW."))
    limit = Decimal(str(cfg["warn_single_phase_above_kw"]))
    if cap is not None and doc.connection.phase == "single" and cap > limit:
        out.append(check("single_phase_large", WARNING, "connection.phase",
                         f"A single-phase plant above {d.kw_text(limit)} kW is unusual."))
    below = figs.get("below_price_master")
    if below:
        out.append(check("below_price_master", WARNING, "price",
                         f"The basic price is {below['percent']}% below the price master (Rs. {below['list_basic']})."))
    band = studio_config.price_band(doc.project.project_type)
    if band is not None and cap and figs.get("basic"):
        per = Decimal(figs["basic"]) / Decimal(cap)
        if not band[0] <= per <= band[1]:
            out.append(check("price_per_kwp_band", WARNING, "price",
                             f"Rs. {per:.0f} per kWp is outside the usual Rs. {band[0]}–{band[1]}."))
    space = figs.get("space_required_sqft")
    roof = doc.project.roof_area_sqft
    if space and roof and Decimal(space["single"]) > Decimal(roof):
        out.append(check("roof_area_short", WARNING, "project.roof_area_sqft",
                         f"The plant needs about {space['single']} sq. ft.; the roof area entered is "
                         f"{d.kw_text(roof)} sq. ft."))
    if doc.site.state and doc.site.state not in cfg["service_states"]:
        out.append(check("state_outside_service", WARNING, "site.state",
                         f"{doc.site.state} is outside {' and '.join(cfg['service_states'])}."))
    return out
