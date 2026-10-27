"""The two proposal layouts (BE-13) as HTML for WeasyPrint: Compact (up to 10 kWp, three
pages) and Detailed (above 10 kWp, eight pages). The frame is constant — header band and
faint watermark on every page, the page-footer line on every page but the last, and the
QR code with the contact / offices letterhead on the last page only — and only the data
changes. Geometry, colours and type sizes are measured from Grid Nova's sample PDFs.

Every interpolated value is HTML-escaped (`_e`), the brand images are embedded as data:
URIs and fetched only through app/pdf/fetcher.py (data: only). A render that would still
contain a {token}, a placeholder marker or the word "None" raises instead of producing a
customer document.
"""
from __future__ import annotations

import base64
import html
import io
import re
from datetime import date
from decimal import Decimal
from functools import lru_cache
from typing import Optional

from app.pdf.fonts import FONT_STACK
from app.proposals import document as d
from app.proposals import document_config

PT_PAGE_W, PT_PAGE_H = 595.3, 841.9
MARGIN_TOP, MARGIN_SIDE, MARGIN_BOTTOM = 112, 56.2, 66
# Room the QR code and the letterhead take at the foot of the last page, kept free by an
# in-flow spacer that travels with the sign-off (so the flow never depends on which page
# is last, and the page count settles in two passes).
LAST_PAGE_RESERVE = 841.9 - MARGIN_BOTTOM - 636
_LEFTOVER = re.compile(r"\{[a-z_0-9]+\}|\[PLACEHOLDER|\[UNCONFIRMED|DUMMY|\bNone\b")


class DocumentRenderError(RuntimeError):
    pass


def _e(value) -> str:
    """Escaped for HTML; the samples' double spaces (around "|", before "(") are kept."""
    return html.escape("" if value is None else str(value), quote=True).replace("  ", "&nbsp; ")


@lru_cache(maxsize=8)
def _data_uri(path: str, mime: str) -> str:
    with open(path, "rb") as fh:
        return f"data:{mime};base64," + base64.b64encode(fh.read()).decode()


@lru_cache(maxsize=4)
def _qr_uri(url: str) -> str:
    import segno
    buf = io.BytesIO()
    segno.make(url, error="m").save(buf, kind="svg", scale=4, border=0, xmldecl=False, svgns=True)
    return "data:image/svg+xml;base64," + base64.b64encode(buf.getvalue()).decode()


@lru_cache(maxsize=16)
def _qr_png_uri(url: str) -> str:
    """The Studio's verification QR as a PNG (an image the proof-check can find on the page)."""
    import segno
    buf = io.BytesIO()
    segno.make(url, error="m").save(buf, kind="png", scale=10, border=0)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def qr_png_size(url: str) -> tuple[int, int]:
    import segno
    return segno.make(url, error="m").symbol_size(scale=10, border=0)


# ── Text context ─────────────────────────────────────────────────────────────────

class _Ctx(dict):
    def fill(self, text: str) -> str:
        return re.sub(r"\{([a-z_0-9]+)\}", lambda m: str(self[m.group(1)]) if m.group(1) in self else m.group(0),
                      text or "")


def _amount(value, whole: bool) -> str:
    return d.grouped(Decimal(str(value)), whole)


def _rs(value, whole: bool) -> str:
    return f"{document_config.load()['common']['amount_label']} {_amount(value, whole)}"


def context(doc: d.ProposalDocument, figs: dict, cfg: Optional[dict] = None) -> _Ctx:
    cfg = cfg or document_config.load()
    from app.pricing import price_list
    phase = doc.connection.phase
    whole = figs["whole_rupees"]
    cap = figs["capacity_kwp"] or ""
    tech = doc.system.module_technology
    phrases = cfg["module_phrases"]["dcr" if doc.plant.dcr else "non_dcr"]
    ctx = _Ctx(
        capacity_kwp=cap, module_technology=tech, dcr_label="DCR" if doc.plant.dcr else "Non-DCR",
        phase_hyphen={"single": "single-phase", "three": "three-phase"}.get(phase, ""),
        phase_words={"single": "single phase", "three": "three phase"}.get(phase, ""),
        phase_title={"single": "Single Phase", "three": "Three Phase"}.get(phase, ""),
        phase_cap={"single": "Single-phase", "three": "Three-phase"}.get(phase, ""),
        phase_supply={"single": "Single-Phase", "three": "Three-Phase"}.get(phase, ""),
        package_name=price_list.display_name(doc.plant.package) if doc.plant.package else "",
        site_town=doc.site.town or "", site_state=doc.site.state or "",
        discom=doc.connection.discom or "DISCOM", sub_division=doc.connection.sub_division or "",
        customer_short_name=doc.addressee.name or "",
        approved_capacity_kwp=d.kw_text(doc.pm_surya_ghar.approved_capacity_kwp),
        standard_warranty_years=document_config.standard_warranty_years(),
        standard_warranty=price_list.commercial_terms()["standard_warranty"],
        o_and_m=price_list.commercial_terms()["o_and_m"],
        panel_life_span=price_list.commercial_terms()["solar_panel_life_span"],
        validity_days=figs["validity_days"],
        category=doc.connection.category or "",
        per_kwp=figs["generation"]["units_per_kwp_per_month"],
        monthly=_amount(figs["generation"]["monthly_units"] or 0, True),
        annual=_amount(figs["generation"]["annual_units"] or 0, True),
        dcr_plant_label=cfg["compact"]["dcr_plant_label"],
    )
    space = figs["space_required_sqft"] or {"low": 0, "high": 0, "single": 0}
    ctx.update(space_low=_amount(space["low"], True), space_high=_amount(space["high"], True),
               space_required_sqft=_amount(space["single"], True))
    for key in ("letter", "short", "table", "pair"):
        ctx[f"module_{'phrase' if key == 'letter' else key}"] = ctx.fill(phrases[key])
    if figs["total"] is not None:
        ctx.update(total=_rs(figs["total"], whole))
    if figs["subsidy"]["applies"]:
        ctx.update(subsidy=_rs(figs["subsidy"]["total"], whole), subsidy_up_to=_amount(figs["subsidy"]["total"], True),
                   effective_cost=_rs(figs["subsidy"]["effective_cost"], whole))
    common = cfg["common"]
    residence = doc.segment == "residential" and not doc.housing_society
    ctx["location"] = ctx.fill(common["location_residence" if residence else "location_site"])
    ctx["package_clause"] = ctx.fill(cfg["compact"]["package_clause"]) if doc.plant.package else ""
    ctx["package_suffix"] = ctx.fill(cfg["compact"]["package_suffix"]) if doc.plant.package else ""
    ctx["closing_working_with"] = ctx.fill(common["closing_working_with"])
    if doc.connection.discom and doc.connection.sub_division:
        grid = common["grid_integration_with_subdivision"]
    elif doc.connection.discom:
        grid = common["grid_integration_with_discom"]
    else:
        grid = common["grid_integration_local"]
    ctx["grid_integration"] = ctx.fill(grid)
    lay = cfg[figs["layout"]]
    ctx["inverter"] = ctx.fill(doc.system.inverter or lay["inverter_default"])
    ctx["mounting"] = ctx.fill(doc.system.mounting or common["mounting"])
    ctx["space"] = ctx.fill(common["space_compact" if figs["layout"] == "compact" else "space_detailed"])
    ctx["list_kw"] = cap
    return ctx


# ── Pieces ───────────────────────────────────────────────────────────────────────

def _css(cfg: dict, layout: str, pages: int, draft: bool, qr_url: Optional[str] = None, studio: bool = False) -> str:
    c = cfg["colours"]
    brand = cfg["brand"]
    header = _data_uri(str(document_config.asset_path("header_image")), "image/jpeg")
    watermark = _data_uri(str(document_config.asset_path("watermark_image")), "image/jpeg")
    letterhead = _data_uri(str(document_config.asset_path("letterhead_image")), "image/jpeg")
    qr = _qr_uri(brand["qr_url"]) if qr_url is None else _qr_png_uri(qr_url)
    # @page backgrounds are positioned from the page area, so offsets subtract the margins.
    hx, hy = -MARGIN_SIDE, -MARGIN_TOP
    wx, wy = 147.9 - MARGIN_SIDE, 283.6 - MARGIN_TOP
    head_bg = f"url('{header}') no-repeat {hx}pt {hy}pt / {PT_PAGE_W}pt 93.7pt"
    wm_bg = f"url('{watermark}') no-repeat {wx}pt {wy}pt / 279.35pt 288.55pt"
    qr_bg = f"url('{qr}') no-repeat {513.5 - MARGIN_SIDE}pt {641.3 - MARGIN_TOP}pt / 53.4pt 53.4pt"
    lh_bg = f"url('{letterhead}') no-repeat {hx}pt {721.4 - MARGIN_TOP}pt / {PT_PAGE_W}pt 120.45pt"
    big = layout in ("detailed", "itemised")
    s = {  # type sizes (pt), measured from the samples
        "kicker": 10.5 if big else 9.5, "title": 17 if big else 16, "strip_label": 7.5 if big else 7,
        "strip_value": 9.5 if big else 9, "card_label": 8 if big else 7.5, "card": 9.5 if big else 9,
        "subject": 10 if big else 9.5, "h": 13 if big else 12, "hsub": 8.5 if big else 8,
        "sub": 10.5 if big else 9.5, "hl_value": 13 if big else 12, "hl_label": 7.5 if big else 6.5,
    }
    stamp = ""
    if draft:
        stamp = f""".draft-stamp {{ position: fixed; top: 300pt; left: -60pt; width: 600pt; text-align: center;
  transform: rotate(-35deg); font-size: 34pt; font-weight: bold; color: rgba(200, 30, 30, 0.22);
  letter-spacing: 1pt; white-space: nowrap; }}"""
    return f"""
@page {{ size: A4; margin: {MARGIN_TOP}pt {MARGIN_SIDE}pt {MARGIN_BOTTOM}pt {MARGIN_SIDE}pt;
  background: {head_bg}, {wm_bg};
  @bottom-center {{ content: element(pagefoot); width: {PT_PAGE_W - 2 * MARGIN_SIDE}pt; vertical-align: top; }} }}
@page :nth({pages}) {{
  background: {head_bg}, {qr_bg}, {lh_bg}, {wm_bg};
  @bottom-center {{ content: none; }} }}
html {{ font-family: {FONT_STACK}; color: {c['text']}; font-size: 9.5pt; line-height: 1.28; }}
body {{ margin: 0; }}
.pagefoot {{ position: running(pagefoot); border-top: 0.6pt solid {c['border']}; margin-top: 12pt;
  padding-top: 6pt; text-align: center; font-size: 7.5pt; color: {c['muted']}; }}
.pagefoot b {{ color: {c['navy']}; }}
.pagefoot .n {{ font-size: 9.5pt; }}
.pagefoot .cur::after {{ content: counter(page); }}
.pagefoot .tot::after {{ content: counter(pages); }}
.brk {{ break-before: page; }}
.kicker {{ text-align: center; color: {c['muted']}; font-weight: bold; font-size: {s['kicker']}pt; }}
.title {{ text-align: center; color: {c['navy']}; font-weight: bold; font-size: {s['title']}pt; margin: 4pt 0 2pt; }}
.subline {{ text-align: center; color: {c['orange']}; font-weight: bold; font-size: 8pt; letter-spacing: 0.4pt; }}
table {{ border-collapse: collapse; width: 100%; }}
.strip {{ margin: 11pt 0 19pt; }}
.strip td {{ background: {c['fill']}; border: 0.6pt solid {c['border']}; text-align: center; padding: 4pt 4pt 5pt; }}
.strip .l {{ color: {c['muted']}; font-weight: bold; font-size: {s['strip_label']}pt; }}
.strip .v {{ color: {c['navy']}; font-weight: bold; font-size: {s['strip_value']}pt; padding-top: 2pt; }}
.cards {{ display: table; width: 100%; border-spacing: 0; margin-bottom: 12pt; }}
.cards > div {{ display: table-cell; width: 50%; vertical-align: top; }}
.card {{ background: {c['fill']}; border-left: 2.4pt solid {c['orange']}; padding: 5pt 8pt 5pt 5pt;
  font-size: {s['card']}pt; line-height: 1.33; }}
.cards > div:first-child .card {{ margin-right: 9pt; }}
.card .l {{ color: {c['orange']}; font-weight: bold; font-size: {s['card_label']}pt; margin-bottom: 2pt; }}
.card .n {{ color: {c['navy']}; font-weight: bold; font-size: 9.5pt; }}
.card b {{ color: {c['navy']}; }}
.subject {{ color: {c['navy']}; font-weight: bold; font-size: {s['subject']}pt; margin: 0 0 9pt; }}
.salute {{ font-weight: bold; margin: 0 0 6pt; }}
p {{ margin: 0 0 6pt; text-align: justify; }}
.sign {{ margin-top: 0; }}
.sign .name {{ color: {c['navy']}; font-weight: bold; margin-top: 20pt; }}
.sign .t {{ color: {c['muted']}; font-size: 8.5pt; }}
.caps {{ color: {c['orange']}; font-weight: bold; font-size: 7.5pt; margin: 14pt 0 6pt; }}
.stats td {{ background: {c['fill']}; border-top: 2.2pt solid {c['navy']}; text-align: center; padding: 4pt 3pt 4pt;
  width: 25%; }}
.stats td + td {{ border-left: 1.5pt solid #FFFFFF; }}
.stats td:nth-child(even) {{ border-top-color: {c['orange']}; }}
.stats .v {{ color: {c['navy']}; font-weight: bold; font-size: 9.5pt; }}
.stats.hl .v {{ font-size: {s['hl_value']}pt; }}
.stats .l {{ color: {c['muted']}; font-weight: bold; font-size: {s['hl_label']}pt; padding-top: 1pt; }}
.stats.three td {{ width: 33.3%; }}
.fine {{ color: {c['muted']}; font-style: italic; font-size: 7pt; margin-top: 5pt; line-height: 1.25; }}
.fine.m {{ font-size: 7.5pt; }}
h2 {{ font-size: {s['h']}pt; color: {c['navy']}; margin: 0; padding-bottom: 3pt; line-height: 1.2;
  border-bottom: 1.4pt solid {c['orange']}; }}
h2 .no {{ color: {c['orange']}; }}
.hsub {{ color: {c['muted']}; font-style: italic; font-size: {s['hsub']}pt; margin: 3pt 0 6pt; }}
.sec {{ margin-top: 13pt; }}
.sec.first {{ margin-top: 0; }}
h3 {{ color: {c['navy']}; font-size: {s['sub']}pt; margin: 8pt 0 5pt; line-height: 1.2; }}
.kv td {{ border: 0.6pt solid {c['border']}; padding: 3.7pt 6pt; font-size: 9pt; vertical-align: middle;
  line-height: 1.2; }}
.kv td.k {{ background: {c['label_fill']}; color: {c['navy']}; font-weight: bold; width: {'35' if big else '31'}%; }}
.kv td.v {{ background: #FFFFFF; }}
.kv.w4 td {{ font-size: 8.5pt; padding: 2.8pt 6pt; }}
.kv.w4 td.k {{ width: 19.6%; }}
.kv.w4 td.v {{ width: 30.4%; }}
.kv.makes td.k {{ width: 39%; }}
.grid th {{ background: {c['navy']}; color: #FFFFFF; font-size: 8.5pt; text-align: left; padding: 4.5pt 6pt;
  line-height: 1.2;
  border: 0.6pt solid {c['navy']}; }}
.grid td {{ border: 0.6pt solid {c['border']}; padding: 3.6pt 6pt; font-size: 9pt; vertical-align: middle;
  background: #FFFFFF; line-height: 1.2; }}
.grid td.k {{ background: {c['label_fill']}; color: {c['navy']}; font-weight: bold; font-size: 8.5pt; }}
.grid.sub td {{ font-size: 8.5pt; padding: 2.8pt 6pt; }}
.grid tr:nth-child(even) td {{ background: {c['zebra']}; }}
.grid .c {{ text-align: center; }}
.grid .r {{ text-align: right; }}
.grid tr.gst td {{ background: {c['label_fill']}; color: {c['navy']}; font-weight: bold; font-size: 8.5pt;
  text-align: right; }}
.grid tr.tot td {{ background: {c['navy']}; color: #FFFFFF; font-weight: bold; font-size: 8.5pt; text-align: right;
  border-color: #FFFFFF; }}
.grid tr.tot td.amt {{ font-size: 9.5pt; }}
.words {{ font-size: 8.5pt; margin: 6pt 0 0; }}
.words b {{ color: {c['navy']}; }}
.wordbox {{ background: {c['fill']}; border-left: 2.6pt solid {c['navy']}; padding: 8pt 10pt; font-size: 9pt;
  margin: 18pt 0 8pt; line-height: 1.55; }}
.wordbox b {{ color: {c['navy']}; }}
.note {{ background: {c['note_fill']}; border-left: 2.6pt solid {c['orange']}; padding: 8pt 10pt; font-size: 9pt;
  margin-top: 18pt; text-align: justify; }}
.note b {{ color: {c['navy']}; }}
.closure {{ background: {c['fill']}; border-left: 2.6pt solid {c['navy']}; padding: 8pt 10pt; margin-top: 20pt; }}
.closure .l {{ color: {c['navy']}; font-weight: bold; font-size: 9pt; margin-bottom: 3pt; }}
ul {{ margin: 0; padding: 0; list-style: none; }}
li {{ position: relative; padding-left: 20pt; margin: 0 0 3.3pt; }}
li::before {{ content: "•"; color: {c['orange']}; font-weight: bold; position: absolute; left: 7pt; }}
.signoff {{ margin-top: 16pt; }}
.signoff .f {{ color: {c['navy']}; font-weight: bold; font-size: 9pt; }}
.signoff .s {{ margin-top: 17pt; }}
.signoff .s b {{ color: {c['navy']}; }}
.signoff .s span {{ color: {c['muted']}; font-size: 8.5pt; }}
.avoid {{ break-inside: avoid; }}
.lastspace {{ height: {LAST_PAGE_RESERVE:.1f}pt; }}
{stamp}{_STUDIO_CSS if studio else ""}
"""


# Studio documents only: the commercial page carries more (discount, payment methods, the
# housing-society CFA), so its spacing is a little tighter to keep it on one page.
_STUDIO_CSS = """.v2c .wordbox { margin: 10pt 0 6pt; padding: 6pt 10pt; line-height: 1.45; }
.v2c h3 { margin: 6pt 0 4pt; }"""


def _section_head(no: Optional[str], title: str, sub: Optional[str], first: bool = False, brk: bool = False) -> str:
    cls = "sec" + (" first" if first else "") + (" brk" if brk else "")
    num = f'<span class="no">{_e(no)}&nbsp;&nbsp;|&nbsp;&nbsp;</span>' if no else ""
    subline = f'<div class="hsub">{_e(sub)}</div>' if sub else ""
    return f'<div class="{cls}"><h2>{num}{_e(title)}</h2>{subline}'


def _kv(rows: list[tuple[str, str]], cls: str = "kv") -> str:
    body = "".join(f'<tr><td class="k">{_e(k)}</td><td class="v">{_e(v)}</td></tr>' for k, v in rows if v)
    return f'<table class="{cls}">{body}</table>'


def _stats(cells: list[tuple[str, str]], cls: str = "stats") -> str:
    tds = "".join(f'<td><div class="v">{_e(v)}</div><div class="l">{_e(label)}</div></td>' for v, label in cells)
    extra = " three" if len(cells) == 3 else ""
    return f'<table class="{cls}{extra}"><tr>{tds}</tr></table>'


def _bullets(items: list[str]) -> str:
    return "<ul>" + "".join(f"<li>{_e(i)}</li>" for i in items) + "</ul>"


def _strip(doc, ctx, ref_text: str, date_text: str, cfg) -> str:
    common = cfg["common"]
    labels = common["strip_labels"]
    project = ctx.fill(common["project_label_dcr" if doc.plant.dcr else "project_label"])
    a = doc.addressee
    who = (a.name or "") if a.customer_type == "organisation" else " ".join(p for p in (a.title, a.name) if p)
    cells = [(labels["date"], date_text, 19.6), (labels["reference"], ref_text, 22.3),
             (labels["project"], project, 23.8), (labels["customer"], who.upper(), 34.3)]
    tds = "".join(f'<td style="width:{w}%"><div class="l">{_e(label)}</div><div class="v">{_e(v)}</div></td>'
                  for label, v, w in cells)
    return f'<table class="strip"><tr>{tds}</tr></table>'


def _to_card(doc, cfg) -> str:
    a = doc.addressee
    common = cfg["common"]
    name = " ".join(p for p in (a.title, a.name) if p)
    lines = [f'<div class="l">{_e(common["to_label"])}</div>', f'<div class="n">{_e(name)}</div>']
    last = len(a.address_lines) - 1
    lines += [f"<div>{_e(line)}{',' if i < last and not line.endswith(',') else ''}</div>"
              for i, line in enumerate(a.address_lines)]
    if a.customer_type == "organisation" and a.attention:
        lines.append(f'<div><b>{_e(common["attention_label"])}</b> {_e(a.attention)}</div>')
    if a.mobile:
        lines.append(f"<div><b>Mobile:</b> +91 {_e(a.mobile[:5])} {_e(a.mobile[5:])}</div>")
    if a.aadhaar_last4:
        lines.append(f"<div><b>Aadhaar:</b> XXXX XXXX {_e(a.aadhaar_last4)}</div>")
    return '<div class="card">' + "".join(lines) + "</div>"


def _connection_card(doc, cfg) -> str:
    cn = doc.connection
    rows = [("DISCOM", cn.discom), ("Consumer No.", cn.consumer_number), ("Sub-division", cn.sub_division),
            ("Sanctioned Load", f"{d.kw_text(cn.sanctioned_load_kw)} kW" if cn.sanctioned_load_kw else None),
            ("Category", cn.category)]
    body = "".join(f"<div><b>{_e(k)}:</b> {_e(v)}</div>" for k, v in rows if v)
    return f'<div class="card"><div class="l">{_e(cfg["common"]["connection_card_label"])}</div>{body}</div>'


def _site_card(doc, cfg) -> str:
    common = cfg["common"]
    last = len(doc.site.address_lines) - 1
    body = "".join(f"<div>{_e(line)}{',' if i < last and not line.endswith(',') else ''}</div>"
                   for i, line in enumerate(doc.site.address_lines))
    return (f'<div class="card"><div class="l">{_e(common["site_card_label"])}</div>'
            f'<div class="n">{_e(common["site_card_title"])}</div>{body}</div>')


def _subject_and_salutation(doc, ctx, cfg) -> str:
    return (f'<div class="subject">Subject: {_e(ctx.fill(cfg["common"]["subject"]))}</div>'
            f'<div class="salute">{_e(doc.addressee.salutation)}</div>')


def _signature_letter(cfg) -> str:
    sig = cfg["signatory"]
    return (f'<div class="sign"><div>{_e(sig["for_line"])},</div><div class="name">{_e(sig["name"])}</div>'
            f'<div class="t">{_e(sig["title"])}</div></div>')


def _signoff(cfg) -> str:
    sig = cfg["signatory"]
    return (f'<div class="signoff avoid"><div class="f">{_e(sig["for_line"])}</div>'
            f'<div class="s"><b>{_e(sig["name"])}</b> <span>&nbsp;|&nbsp;&nbsp;{_e(sig["title"])}</span></div>'
            '<div class="lastspace"></div></div>')


def _boq(doc, ctx, with_standard: bool) -> str:
    head = '<tr><th class="c" style="width:6.5%">S. No.</th><th style="width:{dw}%">Description</th>' \
           '<th class="c" style="width:{qw}%">Quantity</th>{std}<th>Make / Brand</th></tr>'
    std = '<th style="width:19.8%">Standard</th>' if with_standard else ""
    rows = []
    for i, row in enumerate(doc.boq, 1):
        cells = [f'<td class="c">{i}</td>', f"<td>{_e(ctx.fill(row.description))}</td>",
                 f'<td class="c">{_e(ctx.fill(row.quantity))}</td>']
        if with_standard:
            cells.append(f"<td>{_e(row.standard or '–')}</td>")
        cells.append(f"<td>{_e(row.make)}</td>")
        rows.append("<tr>" + "".join(cells) + "</tr>")
    widths = {"dw": 30.6, "qw": 15.3} if with_standard else {"dw": 37, "qw": 12.6}
    return f'<table class="grid">{head.format(std=std, **widths)}{"".join(rows)}</table>'


def _price_table(doc, ctx, figs, cfg, layout: str) -> str:
    whole = figs["whole_rupees"]
    common = cfg["common"]
    lay = cfg[layout]
    desc = ctx.fill(lay["price_description"])
    gst_label = common["gst_line_detail"] if layout == "compact" else common["gst_line"]
    first_amount = figs["basic"]
    discount_rows = ""
    if isinstance(doc, d.StudioDocument) and doc.display.show_discount_line and figs.get("discount"):
        r = _studio_render_cfg()
        first_amount = figs["discount"]["price_before_discount"]
        discount_rows = (f'<tr class="gst"><td colspan="3">{_e(r["discount_line"])}</td>'
                         f'<td>(–) {_e(_amount(figs["discount"]["amount"], whole))}</td></tr>'
                         f'<tr class="gst"><td colspan="3">{_e(r["offered_line"])}</td>'
                         f'<td>{_e(_amount(figs["basic"], whole))}</td></tr>')
    return (
        '<table class="grid">'
        f'<tr><th class="c" style="width:{8.5 if layout == "detailed" else 7}%">S. No.</th><th>Description</th>'
        '<th class="c" style="width:16%">Capacity'
        '</th><th class="r" style="width:16.6%">Amount (Rs.)</th></tr>'
        f'<tr><td class="c">1</td><td>{_e(desc)}</td><td class="c">{_e(ctx["capacity_kwp"])} kWp</td>'
        f'<td class="r">{_e(_amount(first_amount, whole))}</td></tr>{discount_rows}'
        f'<tr class="gst"><td colspan="3">{_e(gst_label)}</td><td>{_e(_amount(figs["gst"], whole))}</td></tr>'
        f'<tr class="tot"><td colspan="3">{_e(common["total_line"])}</td>'
        f'<td class="amt">{_e(_amount(figs["total"], whole))}</td></tr></table>')


def _footer(cfg, ctx) -> str:
    title = ctx.fill(cfg["common"]["footer_title"])
    return (f'<div class="pagefoot"><b>{_e(cfg["brand"]["company_line"])}</b>&nbsp;&nbsp;&nbsp;|&nbsp;&nbsp;&nbsp;'
            f'{_e(title)}&nbsp;&nbsp;&nbsp;|&nbsp;&nbsp;&nbsp;Page <span class="n cur"></span> of '
            f'<span class="n tot"></span></div>')


# ── Compact ──────────────────────────────────────────────────────────────────────

def _compact(doc, figs, ctx, cfg, ref_text, date_text) -> str:
    c, common = cfg["compact"], cfg["common"]
    pmsg = figs["subsidy"]["applies"]
    whole = figs["whole_rupees"]
    parts = []
    if doc.plant.package:
        parts.append(ctx.fill(c["package_part"]).upper())
    parts.append(ctx.fill(c["modules_part"]).upper())
    if pmsg:
        parts.append(c["pm_surya_ghar_eligible"])
    v2 = isinstance(doc, d.StudioDocument)
    if v2 and doc.cover_highlight:
        parts = [doc.cover_highlight.upper()]
    out = [f'<div class="kicker">{_e(common["kicker"])}</div>', f'<div class="title">{_e(ctx.fill(common["title"]))}</div>',
           '<div class="subline">' + "&nbsp;&nbsp;·&nbsp;&nbsp;".join(_e(p) for p in parts) + "</div>",
           _strip(doc, ctx, ref_text, date_text, cfg),
           f'<div class="cards"><div>{_to_card(doc, cfg)}</div><div>{_connection_card(doc, cfg)}</div></div>',
           _subject_and_salutation(doc, ctx, cfg)]
    paragraphs = [ctx.fill(p) for p in c["letter"]]
    registered = pmsg and doc.pm_surya_ghar.portal_registered
    if registered:
        paragraphs[-1] += " " + ctx.fill(c["letter_pm_surya_ghar"])
    closing = ctx.fill(c["letter_closing_residence"]) if doc.segment == "residential" else ctx["closing_working_with"]
    paragraphs.append(ctx.fill(c["letter_warranty"]) + " " + closing)
    out += [f"<p>{_e(p)}</p>" for p in paragraphs]
    out.append(_signature_letter(cfg))
    if registered:
        out.append(f'<div class="caps">{_e(c["status_label"])}</div>')
        out.append(_stats([(ctx.fill(cell["value"]), cell["label"]) for cell in c["status_cells"]]))
    out.append(f'<div class="caps">{_e(common["highlights_label"])}</div>')
    if pmsg:
        out.append(_stats([(ctx.fill(h["value"]), ctx.fill(h["label"])) for h in c["highlights_pm_surya_ghar"]],
                          "stats hl"))
        out.append(f'<div class="fine">{_e(c["highlights_footnote"])}</div>')
    else:
        out.append(_stats([(ctx.fill(h["value"]), h["label"]) for h in c["highlights"]], "stats hl"))
    # Page 2 — 01 system, 02 BOQ, 03 customer scope.
    labels = c["system_labels"]
    capacity_row = c["system_rows"]["capacity_pm_surya_ghar"] if (
        registered and doc.pm_surya_ghar.approved_capacity_kwp == doc.plant.capacity_kwp) else c["system_rows"]["capacity"]
    out.append(_section_head("01", c["s1_title"], c["s1_sub"], first=True, brk=True))
    out.append(_kv([(labels["system_type"], ctx.fill(c["system_rows"]["system_type"])),
                    (labels["capacity"], ctx.fill(capacity_row)), (labels["modules"], ctx["module_table"]),
                    (labels["inverter"], ctx["inverter"]), (labels["mounting"], ctx["mounting"]),
                    (labels["grid"], ctx["grid_integration"]), (labels["space"], ctx["space"])]))
    out.append(f'<h3>{_e(c["generation_label"])}</h3>')
    out.append(_stats([(ctx.fill(g["value"]), g["label"]) for g in c["generation_cards"]]))
    out.append(f'<div class="fine m">{_e(common["generation_note"])}</div></div>')
    sub = ctx.fill(c["s2_sub_package"]) if doc.plant.package else c["s2_sub"]
    out.append(_section_head("02", c["s2_title"], sub) + _boq(doc, ctx, False) + "</div>")
    out.append(_section_head("03", c["s3_title"], c["s3_sub_pm_surya_ghar"] if pmsg else c["s3_sub"])
               + _bullets([ctx.fill(b) for b in doc.customer_scope]) + "</div>")
    # Page 3 — 04 commercial offer (& subsidy), warranty, notes, sign-off.
    list_priced = doc.price.mode == "price_list" and doc.plant.package
    out.append(_section_head("04", c["s4_title_pm_surya_ghar"] if pmsg else c["s4_title"],
                             ctx.fill(c["s4_sub_price_list"]) if list_priced else c["s4_sub"], first=True, brk=True))
    out.append(_price_table(doc, ctx, figs, cfg, "compact"))
    out.append(f'<div class="words"><b>{_e(common["in_words"])}</b> {_e(figs["words"]["total"])}'
               f'{_e(common["words_suffix"])}</div>')
    out.append(f'<h3>{_e(c["payment_label"])}</h3>')
    out.append(_stats([(f"{p['percent']}%  –  {_rs(p['amount'], whole)}", p["label"].upper())
                       for p in figs["payments"]]))
    if v2:
        out.append(_payment_methods())
    if pmsg:
        rows = [(c["subsidy_total_line"], _amount(figs["total"], whole))]
        for i, slab in enumerate(figs["subsidy"]["slabs"]):
            template = c["subsidy_first_slab"] if i == 0 else c["subsidy_third_slab"]
            line = _Ctx(kw=slab["kw"], rate=_amount(slab["rate"], True)).fill(template)
            rows.append((line, f"(–) {_amount(slab['amount'], whole)}"))
        for top_up in figs["subsidy"].get("state_lines") or []:
            rows.append((ctx.fill(c["subsidy_state_line"]), f"(–) {_amount(top_up['amount'], whole)}"))
        body = "".join(f'<tr><td class="k">{_e(k)}</td><td class="r" style="width:30%">{_e(v)}</td></tr>'
                       for k, v in rows)
        body += (f'<tr class="tot"><td>{_e(c["subsidy_effective"])}</td>'
                 f'<td class="amt">{_e(_rs(figs["subsidy"]["effective_cost"], whole))}</td></tr>')
        out.append(f'<h3>{_e(c["subsidy_label"])}</h3><table class="grid sub">{body}</table>')
        out.append(f'<div class="fine m">{_e(c["subsidy_note"])}</div>')
    out.append(f'<h3>{_e(c["warranty_label"])}</h3>')
    body = ""
    for pair in c["warranty_rows"]:
        body += "<tr>" + "".join(f'<td class="k">{_e(cell["label"])}</td><td class="v">{_e(ctx.fill(cell["value"]))}'
                                 "</td>" for cell in pair) + "</tr>"
    out.append(f'<table class="kv w4">{body}</table>')
    if v2 and doc.display.show_package_comparison and figs.get("package_comparison"):
        out.append(_package_comparison(doc, ctx, figs))
    if doc.notes:
        out.append(f'<h3>{_e(c["notes_label"])}</h3>' + _bullets([ctx.fill(n) for n in doc.notes]))
    if v2:
        out.append(_special_notes(doc, ctx))
    if v2 and doc.display.show_savings and figs.get("savings"):
        out.append("</div>" + _savings_page(figs, whole) + "<div>")
    out.append(_signoff(cfg) + "</div>")
    return "\n".join(out)


# ── Detailed ─────────────────────────────────────────────────────────────────────

def _detailed(doc, figs, ctx, cfg, ref_text, date_text) -> str:
    t, common = cfg["detailed"], cfg["common"]
    whole = figs["whole_rupees"]
    a = doc.addressee
    v2 = isinstance(doc, d.StudioDocument)
    out = [f'<div class="kicker">{_e(common["kicker"])}</div>', f'<div class="title">{_e(ctx.fill(common["title"]))}</div>']
    if v2 and doc.cover_highlight:
        out.append(f'<div class="subline">{_e(doc.cover_highlight.upper())}</div>')
    out += [_strip(doc, ctx, ref_text, date_text, cfg),
           f'<div class="cards"><div>{_to_card(doc, cfg)}</div><div>{_site_card(doc, cfg)}</div></div>',
           _subject_and_salutation(doc, ctx, cfg)]
    out += [f"<p>{_e(ctx.fill(p))}</p>" for p in t["letter"]]
    out.append(_signature_letter(cfg))
    out.append(f'<div class="caps">{_e(common["highlights_label"])}</div>')
    out.append(_stats([(ctx.fill(h["value"]), h["label"]) for h in t["highlights"]], "stats hl"))
    # Page 2 — proposal at a glance, 01 project overview.
    g = t["glance_labels"]
    payment_line = "  |  ".join(f"{p['percent']}% {p['short_label'] or p['label']}"
                                                   for p in figs["payments"])
    customer = " ".join(p for p in (a.title, a.name) if p)
    out.append(_section_head(None, t["glance_title"], t["glance_sub"], first=True, brk=True))
    out.append(_kv([(g["customer"], customer), (g["site"], ", ".join(doc.site.address_lines)),
                    (g["plant"], ctx.fill(t["glance_plant"])),
                    (g["category"], f"{doc.connection.category or ''}  |  {ctx['phase_supply']}"),
                    (g["modules"], ctx["module_pair"]), (g["inverter"], ctx.fill(t["inverter_short"])),
                    (g["generation"], ctx.fill(t["glance_generation"])), (g["basic"], _rs(figs["basic"], whole)),
                    (g["gst"], _rs(figs["gst"], whole)), (g["total"], _rs(figs["total"], whole)),
                    (g["payment"], payment_line), (g["validity"], ctx.fill(t["glance_validity"]))]).replace(
        f'<td class="v">{_e(_rs(figs["total"], whole))}</td>',
        f'<td class="v"><b>{_e(_rs(figs["total"], whole))}</b></td>') + "</div>")
    cs = t["customer_site_labels"]
    out.append(_section_head("01", t["s1_title"], t["s1_sub"]))
    out.append(f'<h3>{_e(t["customer_site_label"])}</h3>')
    out.append(_kv([(cs["customer"], customer),
                    (cs["attention"], a.attention if a.customer_type == "organisation" else None),
                    (cs["office"], ", ".join(a.address_lines)), (cs["site"], ", ".join(doc.site.address_lines)),
                    (cs["mount"], t["mount_type"])]))
    out.append(f'<h3>{_e(t["proposed_system_label"])}</h3>')
    out.append(_kv([(r["label"], ctx.fill(r["value"])) for r in t["proposed_system"]]) + "</div>")
    # Page 3 — summary, resource, infrastructure, advantages, execution stages.
    out.append(f'<div class="brk"><h3 style="margin-top:0">{_e(t["summary_label"])}</h3>'
               f'<p>{_e(ctx.fill(t["summary"]))}</p>')
    out.append(f'<h3>{_e(t["resource_label"])}</h3>' + _kv([(r["label"], ctx.fill(r["value"])) for r in t["resource_rows"]]))
    out.append(f'<div class="fine m">{_e(t["resource_note"])}</div>')
    out.append(f'<h3>{_e(t["infrastructure_label"])}</h3>'
               + _kv([(r["label"], ctx.fill(r["value"])) for r in t["infrastructure_rows"]]))
    out.append(f'<h3>{_e(t["advantages_label"])}</h3>' + _bullets(t["advantages"]))
    heads = t["stages_headers"]
    rows = "".join(f'<tr><td class="c">{i}</td><td>{_e(s)}</td><td>{_e(k)}</td></tr>'
                   for i, (s, k) in enumerate(t["stages"], 1))
    out.append(f'<h3>{_e(t["stages_label"])}</h3><table class="grid avoid"><tr><th class="c" style="width:8.5%">'
               f'{_e(heads[0])}</th><th style="width:30%">{_e(heads[1])}</th><th>{_e(heads[2])}</th></tr>{rows}'
               "</table></div>")
    # Page 4 — 02 technical configuration.
    out.append(_section_head("02", t["s2_title"], t["s2_sub"], first=True, brk=True))
    out.append(f'<h3>{_e(t["config_label"])}</h3>' + _kv([(r["label"], ctx.fill(r["value"])) for r in t["config_rows"]]))
    makes = []
    for r in t["makes_rows"]:
        row = next((b for b in doc.boq if b.kind == r["kind"]), None)
        if row is not None and row.make != "–":
            makes.append((ctx.fill(r["label"]), row.make))
    if makes:
        out.append(f'<h3>{_e(t["makes_label"])}</h3>' + _kv(makes, "kv makes"))
    for block in t["component_text"]:
        out.append(f'<h3>{_e(block["label"])}</h3>')
        out.append(_bullets([ctx.fill(b) for b in block["bullets"]]) if "bullets" in block
                   else f'<p>{_e(ctx.fill(block["text"]))}</p>')
    out.append("</div>")
    # Page 5 — 03 BOQ.
    out.append(_section_head("03", t["s3_title"], t["s3_sub"], first=True, brk=True) + _boq(doc, ctx, True))
    out.append(f'<div class="note"><b>{_e(t["quality_note_label"])}</b> {_e(t["quality_note"])}</div></div>')
    # Page 6 — 04 commercial.
    head = _section_head("04", t["s4_title"], t["s4_sub"], first=True, brk=True)
    out.append(head.replace('class="sec first brk"', 'class="sec first brk v2c"') if v2 else head)
    out.append(f'<h3>{_e(t["pricing_label"])}</h3>' + _price_table(doc, ctx, figs, cfg, "detailed"))
    w = t["words_labels"]
    out.append(f'<div class="wordbox"><div><b>{_e(w["basic"])}</b> {_e(figs["words"]["basic"])}.</div>'
               f'<div><b>{_e(w["gst"])}</b> {_e(figs["words"]["gst"])}.</div>'
               f'<div><b>{_e(w["total"])} {_e(figs["words"]["total"])}.</b></div></div>')
    h = t["payment_headers"]
    rows = "".join(f'<tr><td class="c">{i}</td><td>{_e(p["label"])}</td><td class="c">{_e(p["percent"])}%</td>'
                   f'<td class="r">{_e(_amount(p["amount"], whole))}</td></tr>' for i, p in enumerate(figs["payments"], 1))
    rows += (f'<tr class="gst"><td colspan="2">TOTAL</td><td class="c">100%</td>'
             f'<td>{_e(_amount(figs["total"], whole))}</td></tr>')
    out.append(f'<h3>{_e(t["payment_label"])}</h3><table class="grid"><tr><th class="c" style="width:8.5%">{_e(h[0])}'
               f'</th><th>{_e(h[1])}</th><th class="c" style="width:16%">{_e(h[2])}</th><th class="r" '
               f'style="width:20.5%">{_e(h[3])}</th></tr>{rows}</table>')
    if not v2:
        out.append(f'<div class="fine m">{_e(t["payment_note"])}</div>')
    else:
        out.append(_payment_methods(t["payment_note"]))
        if figs["subsidy"].get("kind") == "housing_society":
            out.append(_society_subsidy(figs, whole))
    out.append(f'<h3>{_e(t["warranty_label"])}</h3>' + _kv([(r["label"], r["value"]) for r in t["warranty_rows"]]))
    notes = [ctx.fill(n) for n in t["commercial_notes"]] + [ctx.fill(n) for n in doc.notes]
    out.append(f'<h3>{_e(t["notes_label"])}</h3>' + _bullets(notes) + (_special_notes(doc, ctx) if v2 else "")
               + "</div>")
    if v2 and doc.display.show_savings and figs.get("savings"):
        out.append(_savings_page(figs, whole))
    # Page 7 — 05 customer scope.
    out.append(_section_head("05", t["s5_title"], t["s5_sub"], first=True, brk=True)
               + _bullets([ctx.fill(b) for b in doc.customer_scope]))
    for block in t["scope_sections"]:
        out.append(f'<h3>{_e(block["label"])}</h3><p>{_e(ctx.fill(block["text"]))}</p>')
    out.append("</div>")
    # Page 8 — 06 company & closing.
    out.append(_section_head("06", t["s6_title"], t["s6_sub"], first=True, brk=True))
    out.append(f'<h3 style="margin-top:6pt">{_e(t["why_label"])}</h3>' + _bullets(t["why"]))
    out.append(f'<h3>{_e(t["quality_label"])}</h3><p>{_e(t["quality_policy"])}</p>')
    out.append(f'<h3>{_e(t["value_label"])}</h3><p>{_e(t["value_offering"])}</p>')
    out.append(f'<div class="closure avoid"><div class="l">{_e(t["closure_label"])}</div>'
               f'<div>{_e(ctx.fill(t["closure"]))}</div></div>')
    out.append(_signoff(cfg) + "</div>")
    return "\n".join(out)


# ── Proposal Studio blocks (schema 2, BE-18) ─────────────────────────────────────

def _studio_render_cfg() -> dict:
    from app.studio import config as studio_config
    return studio_config.load()["render"]


def _payment_methods(lead_in: Optional[str] = None) -> str:
    """The payment-methods line (cheque, NEFT, RTGS, IMPS), after `lead_in` on the same line."""
    from app.studio import config as studio_config
    r = _studio_render_cfg()
    first = f"{_e(lead_in)} " if lead_in else ""
    return (f'<div class="fine m">{first}<b>{_e(r["payment_methods_label"])}</b> '
            f'{_e(studio_config.load()["payment_methods"])}</div>')


def _special_notes(doc, ctx) -> str:
    if not doc.notes_free:
        return ""
    return f'<h3>{_e(_studio_render_cfg()["special_notes_label"])}</h3>' + _bullets([ctx.fill(n) for n in doc.notes_free])


def _package_comparison(doc, ctx, figs) -> str:
    r = _studio_render_cfg()
    heads = r["comparison_headers"]
    whole = all(Decimal(p[k]) == Decimal(p[k]).to_integral_value() for p in figs["package_comparison"]
                for k in ("basic", "gst", "total"))
    rows = "".join(
        f'<tr><td class="k">{_e(p["name"])}{" ✓" if p["chosen"] else ""}</td><td>{_e(p["panels"])}</td>'
        f'<td>{_e(p["inverters"])}</td><td class="r">{_e(_amount(p["basic"], whole))}</td>'
        f'<td class="r">{_e(_amount(p["gst"], whole))}</td><td class="r">{_e(_amount(p["total"], whole))}</td></tr>'
        for p in figs["package_comparison"])
    head = "".join(('<th class="r">' if i >= 3 else "<th>") + f"{_e(h)}</th>" for i, h in enumerate(heads))
    return (f'<h3>{_e(r["comparison_label"])}</h3><table class="grid sub avoid"><tr>{head}</tr>{rows}</table>'
            f'<div class="fine m">{_e(ctx.fill(r["comparison_note"]))}</div>')


def _society_subsidy(figs, whole: bool) -> str:
    r = _studio_render_cfg()
    sub = figs["subsidy"]
    slab = sub["slabs"][0]
    line = _Ctx(kw=slab["kw"], rate=_amount(slab["rate"], True)).fill(r["society_subsidy_line"])
    body = (f'<tr><td class="k">{_e(r["society_total_line"])}</td><td class="r" style="width:30%">'
            f'{_e(_amount(figs["total"], whole))}</td></tr>'
            f'<tr><td class="k">{_e(line)}</td><td class="r">(–) {_e(_amount(sub["total"], whole))}</td></tr>'
            f'<tr class="tot"><td>{_e(r["society_effective"])}</td>'
            f'<td class="amt">{_e(_rs(sub["effective_cost"], whole))}</td></tr>')
    note = _Ctx(kw_per_house=sub["kw_per_house"], houses=sub["houses"], max_kw=sub["max_kw"]).fill(
        r["society_subsidy_note"])
    return (f'<h3>{_e(r["society_subsidy_label"])}</h3><table class="grid sub avoid">{body}</table>'
            f'<div class="fine m">{_e(note)}</div>')


def _savings_page(figs, whole: bool) -> str:
    r = _studio_render_cfg()
    sav = figs["savings"]
    lb = r["savings_basis_labels"]
    basis = [(lb["consumption"], f'{_amount(sav["monthly_consumption_units"], True)} units'),
             (lb["tariff"], f'Rs. {sav["tariff_per_unit"]} per unit'),
             (lb["generation"], f'~{_amount(sav["year1_generation_units"], True)} units'),
             (lb["savings"], f'Rs. {_amount(sav["year1_savings"], True)} '
                             f'(about Rs. {_amount(sav["year1_monthly_savings"], True)} a month)')]
    if sav["payback_years"] is not None:
        basis.append((r["savings_payback_label"], f'~{sav["payback_years"]} years on Rs. '
                                                   f'{_amount(sav["effective_cost"], whole)}'))
    heads = r["savings_headers"]
    rows = "".join(f'<tr><td class="c">{row["year"]}</td><td class="r">{_e(_amount(row["generation_units"], True))}</td>'
                   f'<td class="r">{_e(_amount(row["savings"], True))}</td>'
                   f'<td class="r">{_e(_amount(row["cumulative"], True))}</td></tr>' for row in sav["table"])
    a = sav["assumptions"]
    pct = lambda key: d.kw_text(Decimal(a[key]) * 100) or "0"  # noqa: E731
    assumptions = _Ctx(first=pct("degradation_first_year"), annual=pct("degradation_annual_after"),
                       technology=a["module_technology"], escalation=pct("tariff_escalation"),
                       export=d.kw_text(Decimal(a["export_rate"])) or "0").fill(r["savings_assumptions"])
    out = [_section_head(None, r["savings_title"], r["savings_sub"], first=True, brk=True),
           f'<h3>{_e(r["savings_inputs_label"])}</h3>' + _kv(basis),
           f'<table class="grid avoid" style="margin-top:10pt"><tr><th class="c" style="width:12%">{_e(heads[0])}</th>'
           + "".join(f'<th class="r">{_e(h)}</th>' for h in heads[1:]) + f"</tr>{rows}</table>",
           f'<div class="note">{_e(sav["note"])}</div>', f'<div class="fine m">{_e(assumptions)}</div>']
    if sav.get("depreciation_line"):
        out.append(f'<div class="fine m">{_e(sav["depreciation_line"])}</div>')
    return "\n".join(out) + "</div>"


def _itemised(doc, figs, ctx, cfg, ref_text, date_text) -> str:
    """Street lighting and other supply: the same frame, a BOQ with prices instead of plant pages."""
    from app.studio import config as studio_config
    t = studio_config.load()["itemised"]["text"]
    cats = studio_config.load()["itemised"]["categories"]
    common, whole = cfg["common"], figs["whole_rupees"]
    labels = common["strip_labels"]
    a = doc.addressee
    who = (a.name or "") if a.customer_type == "organisation" else " ".join(p for p in (a.title, a.name) if p)
    cells = [(labels["date"], date_text, 19.6), (labels["reference"], ref_text, 22.3),
             (labels["project"], t["project_label"], 23.8), (labels["customer"], who.upper(), 34.3)]
    strip = '<table class="strip"><tr>' + "".join(
        f'<td style="width:{w}%"><div class="l">{_e(label)}</div><div class="v">{_e(v)}</div></td>'
        for label, v, w in cells) + "</tr></table>"
    out = [f'<div class="kicker">{_e(common["kicker"])}</div>', f'<div class="title">{_e(t["title"])}</div>']
    if doc.cover_highlight:
        out.append(f'<div class="subline">{_e(doc.cover_highlight.upper())}</div>')
    site = _site_card(doc, cfg) if doc.site.address_lines else ""
    out += [strip, f'<div class="cards"><div>{_to_card(doc, cfg)}</div><div>{site}</div></div>',
            f'<div class="subject">Subject: {_e(t["subject"])}</div>', f'<div class="salute">{_e(a.salutation)}</div>']
    out += [f"<p>{_e(p)}</p>" for p in t["letter"]]
    out.append(_signature_letter(cfg))
    out.append(f'<div class="caps">{_e(common["highlights_label"])}</div>')
    h = t["highlights"]
    out.append(_stats([(str(len(doc.items)), h["items"]), (_rs(figs["total"], whole), h["total"]),
                       (f'{figs["validity_days"]} Days', h["validity"])], "stats hl"))
    # Page 2 — BOQ with prices, totals, words.
    out.append(_section_head("01", t["boq_title"], t["boq_sub"], first=True, brk=True))
    head = ('<tr><th class="c" style="width:5.5%">S. No.</th><th>Description</th><th>Specification</th><th>Make</th>'
            '<th class="r" style="width:8%">Qty</th><th class="c" style="width:7%">Unit</th>'
            '<th class="r" style="width:11%">Unit Rate</th><th class="c" style="width:7%">GST</th>'
            '<th class="r" style="width:13%">Amount (Rs.)</th></tr>')
    rows = "".join(
        f'<tr><td class="c">{line["no"]}</td><td>{_e(item.description)}</td><td>{_e(item.specification or "–")}</td>'
        f'<td>{_e(item.make)}</td><td class="r">{_e(d.kw_text(item.quantity))}</td><td class="c">{_e(item.unit)}</td>'
        f'<td class="r">{_e(_amount(item.unit_rate, whole))}</td><td class="c">{_e(line["gst_percent"])}%</td>'
        f'<td class="r">{_e(_amount(line["amount"], whole))}</td></tr>'
        for item, line in zip(doc.items, figs["items"]))
    rows += (f'<tr class="gst"><td colspan="8">{_e(t["subtotal_line"])}</td>'
             f'<td>{_e(_amount(figs["basic"], whole))}</td></tr>')
    for g in figs["gst_groups"]:
        rows += (f'<tr class="gst"><td colspan="8">GST @ {_e(g["percent"])}% on Rs. {_e(_amount(g["taxable"], whole))}'
                 f'</td><td>{_e(_amount(g["gst"], whole))}</td></tr>')
    rows += (f'<tr class="tot"><td colspan="8">{_e(common["total_line"])}</td>'
             f'<td class="amt">{_e(_amount(figs["total"], whole))}</td></tr>')
    out.append(f'<table class="grid" style="font-size:8pt">{head}{rows}</table>')
    out.append(f'<div class="wordbox"><div><b>{_e(cfg["detailed"]["words_labels"]["total"])}</b> '
               f'{_e(figs["words"]["total"])}.</div></div></div>')
    # Page 3 — payment terms, customer scope, notes, warranty, sign-off.
    out.append(_section_head("02", t["terms_title"], t["terms_sub"], first=True, brk=True))
    rows = "".join(f'<tr><td class="c">{i}</td><td>{_e(p["label"])}</td><td class="c">{_e(p["percent"])}%</td>'
                   f'<td class="r">{_e(_amount(p["amount"], whole))}</td></tr>' for i, p in enumerate(figs["payments"], 1))
    rows += (f'<tr class="gst"><td colspan="2">TOTAL</td><td class="c">100%</td>'
             f'<td>{_e(_amount(figs["total"], whole))}</td></tr>')
    ph = cfg["detailed"]["payment_headers"]
    out.append(f'<h3 style="margin-top:6pt">{_e(cfg["detailed"]["payment_label"])}</h3><table class="grid"><tr>'
               f'<th class="c" style="width:8.5%">{_e(ph[0])}</th><th>{_e(ph[1])}</th><th class="c" style="width:16%">'
               f'{_e(ph[2])}</th><th class="r" style="width:20.5%">{_e(ph[3])}</th></tr>{rows}</table>')
    out.append(_payment_methods())
    out.append(f'<h3>{_e(t["scope_label"])}</h3>' + _bullets([ctx.fill(b) for b in doc.customer_scope]))
    warranty = []
    for cat in dict.fromkeys(item.category for item in doc.items):
        for row in cats[cat].get("warranty_rows") or []:
            warranty.append((row["label"], row["value"]))
    if warranty:
        out.append(f'<h3>{_e(cfg["detailed"]["warranty_label"])}</h3>' + _kv(warranty))
    notes = [ctx.fill(n) for n in cfg["detailed"]["commercial_notes"] if "{validity_days}" in n]
    out.append(f'<h3>{_e(cfg["compact"]["notes_label"])}</h3>' + _bullets(notes + [ctx.fill(n) for n in doc.notes])
               + _special_notes(doc, ctx))
    out.append(_signoff(cfg) + "</div>")
    return "\n".join(out)


def studio_cfg_view(doc) -> dict:
    """The layout config a Studio document renders with: proposal_document.yaml as is, with
    "Rooftop" read as "Ground-Mounted" for a ground-mounted plant, and the itemised layout's
    title, footer and subject from config/studio.yaml."""
    import copy

    cfg = copy.deepcopy(document_config.load())
    if not isinstance(doc, d.StudioDocument):
        return cfg
    if doc.mount_type == "ground_mounted":
        # Only the sentences that describe THIS plant; the company paragraphs, quality policy
        # and the rest of the fixed text stay exactly as Grid Nova wrote them.
        swap = (("ROOFTOP", "GROUND-MOUNTED"), ("Rooftop", "Ground-Mounted"), ("rooftop", "ground-mounted"))

        def plant(text: str) -> str:
            for a, b in swap:
                text = text.replace(a, b)
            return text
        for key in ("title", "footer_title", "project_label", "project_label_dcr", "subject", "site_card_title"):
            cfg["common"][key] = plant(cfg["common"][key])
        det = cfg["detailed"]
        det["letter"][2] = plant(det["letter"][2])
        for key in ("glance_plant", "mount_type", "summary", "price_description", "closure"):
            det[key] = plant(det[key])
        for rows in (det["proposed_system"], det["config_rows"]):
            rows[0] = {**rows[0], "value": plant(rows[0]["value"])}
    if d.layout(doc) == "itemised":
        from app.studio import config as studio_config
        t = studio_config.load()["itemised"]["text"]
        cfg["common"] = {**cfg["common"], "title": t["title"], "footer_title": t["footer_title"],
                         "subject": t["subject"], "site_card_title": t["site_card_title"]}
        cfg["itemised"] = cfg["detailed"]
    return cfg


# ── Rendering ────────────────────────────────────────────────────────────────────

def format_date(day: date) -> str:
    return f"{day:%d.%m.%Y}"


def build_html(doc: d.ProposalDocument, *, reference: Optional[str], doc_date: date, draft: bool,
               pages: int = 1, figs: Optional[dict] = None, verify_url: Optional[str] = None) -> str:
    """The document as HTML. A Studio (schema 2) document may pass `verify_url`: its
    last-page QR then points there (a PNG) instead of the website."""
    v2 = isinstance(doc, d.StudioDocument)
    cfg = studio_cfg_view(doc) if v2 else document_config.load()
    figs = figs or d.figures(doc)
    if figs["total"] is None:
        raise DocumentRenderError("The document has no price; it cannot be rendered.")
    ctx = context(doc, figs, cfg)
    ref_text = reference or "Allocated on approval"
    body_fn = {"compact": _compact, "detailed": _detailed, "itemised": _itemised}[figs["layout"]]
    body = body_fn(doc, figs, ctx, cfg, ref_text, format_date(doc_date))
    stamp = '<div class="draft-stamp">DRAFT — NOT FOR CUSTOMER</div>' if draft else ""
    title = _e(ctx.fill(cfg["common"]["footer_title"]))
    meta = ""
    if v2:
        # PDF document information (WeasyPrint reads these into the PDF / XMP metadata).
        subject = ctx.fill(cfg["common"]["subject"]).rstrip(".")
        keywords = ", ".join(k for k in ("Grid Nova Energy", "solar proposal", reference or "",
                                         figs.get("project_type", "").replace("_", " ")) if k)
        meta = (f'<meta name="author" content="{_e(document_config.load()["signatory"]["for_line"][4:])}">'
                f'<meta name="description" content="{_e(subject)}"><meta name="keywords" content="{_e(keywords)}">'
                '<meta name="generator" content="Grid Nova Proposal Studio">')
    page = (f'<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><title>{title}</title>{meta}'
            f"<style>{_css(cfg, figs['layout'], pages, draft, verify_url if v2 else None, studio=v2)}</style>"
            "</head><body>"
            f"{_footer(cfg, ctx)}{stamp}{body}</body></html>")
    leftover = _LEFTOVER.search(re.sub(r"<style>.*?</style>", "", page, flags=re.S))
    if leftover:
        raise DocumentRenderError(f"The document still contains {leftover.group(0)!r}; it cannot be rendered.")
    return page


def render(doc: d.ProposalDocument, *, reference: Optional[str], doc_date: date, draft: bool,
           verify_url: Optional[str] = None, pdf_a: bool = False) -> bytes:
    """PDF bytes. Rendered until the page count settles, so the letterhead and QR land on
    the real last page (@page :nth(N)) and nowhere else. `pdf_a`: an archival PDF/A-3b
    (the Studio's issued documents)."""
    from weasyprint import HTML

    from app.pdf.fetcher import safe_url_fetcher

    figs = d.figures(doc)
    pages = {"compact": 3, "detailed": 8, "itemised": 3}[figs["layout"]]
    for _ in range(4):
        html_doc = build_html(doc, reference=reference, doc_date=doc_date, draft=draft, pages=pages, figs=figs,
                              verify_url=verify_url)
        rendered = HTML(string=html_doc, base_url=None, url_fetcher=safe_url_fetcher()).render()
        if len(rendered.pages) == pages:
            return rendered.write_pdf(pdf_variant="pdf/a-3b") if pdf_a else rendered.write_pdf()
        pages = len(rendered.pages)
    raise DocumentRenderError("The page count did not settle; the document cannot be rendered.")
