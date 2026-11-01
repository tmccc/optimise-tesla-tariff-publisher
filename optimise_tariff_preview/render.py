"""Self-contained, inert HTML for an Optimise tariff dry-run report.

The renderer performs no I/O. It has no network client, JavaScript, external
assets or write-to-Tesla capability. All provider/report text is HTML escaped.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from html import escape
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def _text(value: Any, default: str = "Not provided") -> str:
    if value is None or value == "":
        return default
    return str(value)[:2000]


def _h(value: Any, default: str = "Not provided") -> str:
    return escape(_text(value, default), quote=True)


def _instant(value: Any) -> datetime | None:
    try:
        result = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if result.tzinfo is None or result.utcoffset() is None:
            return None
        return result.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def _number(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        if len(str(value)) > 128:
            return None
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def _price(value: Any, places: int = 2) -> str:
    number = _number(value)
    if number is None:
        return "—"
    if not number:
        return "0" + ("." + "0" * places if places else "")
    # Scientific notation avoids unbounded fixed-point expansion of bad input.
    if not -9 <= number.adjusted() <= 9:
        return _h(str(number))
    raw = format(number, "f")
    whole, dot, fraction = raw.partition(".")
    fraction = fraction.rstrip("0").ljust(places, "0")
    return _h(whole + ("." + fraction if fraction else ""))


def _time(value: Any, zone: ZoneInfo | timezone = timezone.utc) -> str:
    instant = _instant(value)
    if instant is None:
        return _h(value, "Unavailable")
    return _h(instant.astimezone(zone).strftime("%d %b %Y %H:%M %Z"))


def _age(as_of: Any, source_time: Any) -> str:
    current, source = _instant(as_of), _instant(source_time)
    if current is None or source is None:
        return "Unavailable"
    seconds = (current - source).total_seconds()
    if seconds < 0:
        return "Timestamp is in the future"
    if seconds < 60:
        return f"{seconds:.0f} seconds"
    return f"{seconds / 60:.1f} minutes"


def _issues(items: Any, kind: str) -> str:
    if not isinstance(items, list) or not items:
        return f'<p class="muted">No {kind} reported.</p>'
    rows = []
    for item in items:
        if isinstance(item, dict):
            code = _h(item.get("code"), "unspecified")
            message = _h(item.get("message"), "No detail supplied")
            context = []
            if item.get("channel") is not None:
                context.append("channel " + _h(item["channel"]))
            if item.get("period_index") is not None:
                context.append("period index " + _h(item["period_index"]))
            suffix = ' <span class="muted">(' + "; ".join(context) + ")</span>" if context else ""
            rows.append(f"<li><code>{code}</code> — {message}{suffix}</li>")
        else:
            rows.append(f"<li>{_h(item)}</li>")
    return "<ul>" + "".join(rows) + "</ul>"


def _incompatible(row: dict[str, Any]) -> bool:
    buy, sell = _number(row.get("import_p_per_kwh")), _number(row.get("export_p_per_kwh"))
    return bool(
        row.get("compatible") is False
        or (buy is not None and buy < 0)
        or (sell is not None and sell < 0)
        or (buy is not None and sell is not None and buy < sell)
    )


def _chart(periods: list[dict[str, Any]], zone: ZoneInfo | timezone) -> str:
    values: list[float] = []
    points = []
    for index, row in enumerate(periods):
        start, end = _instant(row.get("valid_from")), _instant(row.get("valid_to"))
        if start is None or end is None or end <= start:
            continue
        prices = []
        for key in ("import_p_per_kwh", "export_p_per_kwh"):
            number = _number(row.get(key))
            if number is None:
                prices.append(None)
                continue
            try:
                value = float(number)
                if not -1e9 <= value <= 1e9:
                    value = None
            except (OverflowError, ValueError):
                value = None
            prices.append(value)
            if value is not None:
                values.append(value)
        points.append((index, start, end, prices, _incompatible(row)))
    if not points or not values:
        return '<p class="empty-chart">No valid timed price values are available to plot.</p>'

    left, right, top, bottom = 74.0, 1088.0, 32.0, 260.0
    first, last = min(p[1] for p in points), max(p[2] for p in points)
    duration = (last - first).total_seconds()
    low, high = min(0.0, min(values)), max(0.0, max(values))
    if low == high:
        low, high = low - 1, high + 1
    padding = (high - low) * 0.08
    low, high = low - padding, high + padding

    def x(instant: datetime) -> float:
        return left + (instant - first).total_seconds() / duration * (right - left)

    def y(value: float) -> float:
        return bottom - (value - low) / (high - low) * (bottom - top)

    drawing = [
        '<svg viewBox="0 0 1120 320" role="img" aria-labelledby="chart-title chart-desc">',
        '<title id="chart-title">Import and export price preview</title>',
        '<desc id="chart-desc">Half-hour price steps in pence per kilowatt hour. Red shaded intervals cannot be copied faithfully to the documented Tesla custom-tariff format. Missing intervals are not filled.</desc>',
    ]
    for _, start, end, _, bad in points:
        if bad:
            drawing.append(f'<rect x="{x(start):.3f}" y="{top}" width="{x(end)-x(start):.3f}" height="{bottom-top}" fill="#fce3e3"/>')
    for tick in range(5):
        value = low + (high - low) * tick / 4
        pos = y(value)
        drawing.append(f'<line x1="{left}" y1="{pos:.3f}" x2="{right}" y2="{pos:.3f}" stroke="#dce3e8"/>')
        drawing.append(f'<text x="{left-12}" y="{pos+4:.3f}" text-anchor="end">{value:.2f}</text>')
    drawing.append('<text x="12" y="18">p/kWh</text>')
    for tick in range(7):
        position = left + (right - left) * tick / 6
        instant = first + (last - first) * tick / 6
        label = instant.astimezone(zone).strftime("%d %b %H:%M %Z")
        drawing.append(f'<text x="{position:.3f}" y="288" text-anchor="middle">{_h(label)}</text>')
    colours = ("#137c52", "#315dcb")
    previous = [None, None]
    for index, start, end, prices, _ in points:
        for channel, price in enumerate(prices):
            if price is None:
                previous[channel] = None
                continue
            old = previous[channel]
            if old is not None and old[0] == start:
                drawing.append(f'<line x1="{x(start):.3f}" y1="{y(old[1]):.3f}" x2="{x(start):.3f}" y2="{y(price):.3f}" stroke="{colours[channel]}" stroke-width="2"/>')
            title = ("Import" if channel == 0 else "Export") + f" · interval {index + 1} · {price:g} p/kWh"
            drawing.append(f'<line x1="{x(start):.3f}" y1="{y(price):.3f}" x2="{x(end):.3f}" y2="{y(price):.3f}" stroke="{colours[channel]}" stroke-width="2.5"><title>{_h(title)}</title></line>')
            previous[channel] = (end, price)
    drawing.append("</svg>")
    return "".join(drawing)


def _estimate(value: Any) -> str:
    return "Yes" if value is True else "No" if value is False else "Unknown"


def _table(periods: list[dict[str, Any]], zone: ZoneInfo | timezone) -> str:
    body = []
    for index, row in enumerate(periods):
        bad = _incompatible(row)
        compatible = "INCOMPATIBLE" if bad else "Compatible" if row.get("compatible") is True else "Not checked"
        local = _time(row.get("valid_from"), zone) + " → " + _time(row.get("valid_to"), zone)
        utc = _time(row.get("valid_from")) + " → " + _time(row.get("valid_to"))
        quality = _h(row.get("import_quality"), "Unknown") + " / " + _h(row.get("export_quality"), "Unknown")
        estimates = _estimate(row.get("import_estimate")) + " / " + _estimate(row.get("export_estimate"))
        basis = _h(row.get("import_interval_basis"), "Unknown") + " / " + _h(row.get("export_interval_basis"), "Unknown")
        cells = [str(index + 1), local, utc,
                 _price(row.get("import_p_per_kwh")), _price(row.get("export_p_per_kwh")),
                 _price(row.get("import_gbp_per_kwh"), 4), _price(row.get("export_gbp_per_kwh"), 4),
                 quality, estimates, basis, compatible,
                 "Yes" if row.get("in_candidate") is True else "No" if row.get("in_candidate") is False else "Not supplied"]
        body.append('<tr class="' + ("bad" if bad else "") + '">' + "".join(f"<td>{cell}</td>" for cell in cells) + "</tr>")
    if not body:
        body.append('<tr><td colspan="12">No aligned periods available. Missing prices have not been replaced with zero.</td></tr>')
    return """<div class="table-wrap"><table id="price-table"><thead><tr>
<th>#</th><th>Local interval</th><th>UTC interval</th><th>Import p/kWh</th><th>Export p/kWh</th>
<th>Import £/kWh</th><th>Export £/kWh</th><th>Quality<br>import / export</th><th>Estimated?<br>import / export</th>
<th>Interval basis<br>import / export</th><th>Tesla price rules</th><th>In candidate</th></tr></thead><tbody>""" + "".join(body) + "</tbody></table></div>"


def render_html(report: dict[str, Any]) -> str:
    """Render a report without reading files or making any network requests."""
    report = report if isinstance(report, dict) else {}
    periods = [row for row in report.get("periods", []) if isinstance(row, dict)] if isinstance(report.get("periods"), list) else []
    coverage = report.get("coverage") if isinstance(report.get("coverage"), dict) else {}
    source = report.get("source") if isinstance(report.get("source"), dict) else {}
    errors = report.get("errors") if isinstance(report.get("errors"), list) else []
    warnings = report.get("warnings") if isinstance(report.get("warnings"), list) else []
    blocked = bool(errors) or report.get("valid") is False or any(_incompatible(row) for row in periods)
    state = "BLOCKED" if blocked else "PREVIEW VALID · UPLOADS DISABLED" if report.get("valid") is True and report.get("ready_for_upload") is False else "REVIEW"
    as_of = report.get("as_of", report.get("now", report.get("generated_at")))
    captured = report.get("source_captured_at", report.get("captured_at", source.get("captured_at")))
    last_success = report.get("source_last_success", report.get("last_success", source.get("last_success")))
    zone_name = _text(report.get("timezone"), "Europe/London")
    try:
        zone: ZoneInfo | timezone = ZoneInfo(zone_name)
        zone_display = zone_name
    except (ZoneInfoNotFoundError, ValueError):
        zone = timezone.utc
        zone_display = zone_name + " (unavailable; times shown in UTC)"
    start = coverage.get("start", periods[0].get("valid_from") if periods else None)
    end = coverage.get("end", periods[-1].get("valid_to") if periods else None)
    source_label = source.get("label", source.get("type", "Home Assistant Optimise price sensors"))
    unsupported = sum(_incompatible(row) for row in periods)
    full_days = "Today: " + ("complete" if coverage.get("full_today") is True else "not complete")
    full_days += " · Tomorrow: " + ("complete" if coverage.get("full_tomorrow") is True else "not complete")
    manifest = report.get("payload_manifest") if isinstance(report.get("payload_manifest"), dict) else {}
    has_payload = isinstance(report.get("tesla_payload"), dict) and bool(report["tesla_payload"])
    payload_description = (
        "A candidate Tesla payload has been generated for inspection only. It is not sent anywhere."
        if has_payload
        else "No candidate Tesla payload is available. The interval preview remains available for diagnosis."
    )
    if manifest.get("repeats_daily") is True:
        payload_description += " The candidate repeats its time-of-day schedule daily; that repetition must not be treated as a forecast beyond the supplied horizon."
    if manifest.get("partial_history_remapped") is True or manifest.get("historical_clock_prices_may_be_remapped") is True:
        payload_description += " Some clock times are mapped from a rolling horizon rather than a complete observed calendar day."
    blockers = report.get("upload_blockers")
    blocker_html = _issues(blockers, "additional upload blockers") if isinstance(blockers, list) else ""

    return """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>Optimise → Tesla tariff dry-run</title><style>
:root{font:16px system-ui,-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;color:#17222c;background:#f1f4f6}
*{box-sizing:border-box}body{margin:0}main{max-width:1280px;margin:auto;padding:26px 20px 48px}
h1{font-size:1.8rem;line-height:1.2;margin:.4em 0}h2{font-size:1.2rem;margin:0 0 14px}p{line-height:1.55}
.kicker{font-size:.8rem;font-weight:800;letter-spacing:.1em;color:#465866}.safety{padding:14px 18px;border:2px solid #395974;background:#eaf2fa;border-radius:10px;font-weight:700}
.state{display:inline-block;border-radius:6px;padding:6px 10px;background:#fce3e3;color:#8b2222;font-weight:800}.review{background:#fff1c9;color:#755300}
.intro{max-width:850px}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px;margin:22px 0}
.card,section{background:white;border:1px solid #dbe2e7;border-radius:10px;padding:18px}.card span{display:block;font-size:.8rem;color:#5a6a78;margin-bottom:6px}.card strong{font-size:1rem;line-height:1.5}
section{margin:16px 0}.muted{color:#596b79;font-size:.9rem}code{background:#edf1f5;padding:2px 5px;border-radius:4px;overflow-wrap:anywhere}
li{margin:9px 0;line-height:1.5;overflow-wrap:anywhere}.issues{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:16px}
.legend{display:flex;gap:22px;flex-wrap:wrap;font-size:.9rem}.dot{display:inline-block;width:12px;height:4px;vertical-align:middle;margin-right:6px}.import{background:#137c52}.export{background:#315dcb}.incompatible{background:#f0b5b5;height:12px}
svg{display:block;width:100%;min-width:600px;height:auto}svg text{font:12px system-ui,sans-serif;fill:#526474}.chart-wrap{overflow-x:auto}.empty-chart{padding:36px 0;color:#596b79}
.table-wrap{overflow:auto;max-height:70vh;border:1px solid #dbe2e7}table{border-collapse:collapse;width:100%;font-size:.82rem}th,td{padding:10px;border-bottom:1px solid #dbe2e7;text-align:left;vertical-align:top}th{position:sticky;top:0;background:#e9eff4;z-index:1;white-space:nowrap}td{white-space:nowrap}tr.bad{background:#fff0f0}tr.bad td:last-child{color:#982828;font-weight:800}
footer{color:#596b79;font-size:.85rem;margin-top:22px}a{color:#245e91}details{margin-top:12px}summary{cursor:pointer;font-weight:600}
</style></head><body><main>
<div class="kicker">HOME ASSISTANT → TESLA · LOCAL PREVIEW</div>
<h1>Optimise tariff dry-run</h1>
<p class="safety">DRY RUN — NO TESLA WRITES. No tariff or Powerwall control command is sent by this report.</p>
<p class="intro"><span class="state""" + ("" if blocked else " review") + '">' + state + """</span> Preview generated for review. An uploadable payload is not implied by a rendered schedule.</p>
<div class="cards">
<div class="card"><span>Source</span><strong>""" + _h(source_label) + """</strong></div>
<div class="card"><span>Validation time</span><strong>""" + _time(as_of) + """</strong></div>
<div class="card"><span>Snapshot age at validation</span><strong>""" + _h(_age(as_of, captured)) + """</strong></div>
<div class="card"><span>Successful price-poll age</span><strong>""" + _h(_age(as_of, last_success)) + """</strong></div>
<div class="card"><span>Aligned intervals</span><strong>""" + _h(coverage.get("period_count", len(periods))) + """</strong></div>
<div class="card"><span>Intervals failing Tesla price rules</span><strong>""" + str(unsupported) + """</strong></div>
</div>
<section><h2>Coverage and source quality</h2><p>""" + _time(start, zone) + " → " + _time(end, zone) + """</p>
<p class="muted">Timezone: """ + _h(zone_display) + " · " + _h(full_days) + """</p>
<p>Forecasts are planning inputs, not settlement prices. “Estimated: No” does not make a forecast final. Provisional interval ends and incomplete calendar-day coverage remain visible below.</p>
<details><summary>Source timestamps</summary><p>Snapshot: """ + _time(captured) + "<br>Last successful poll: " + _time(last_success) + "<br>Report generated: " + _time(report.get("generated_at")) + """</p></details></section>
<section><h2>Candidate payload</h2><p>""" + _h(payload_description) + """</p><p class="muted">Known candidate validity: """ + _time(manifest.get("known_valid_from", manifest.get("source_valid_from", start)), zone) + " → " + _time(manifest.get("known_valid_until", manifest.get("source_valid_until", end)), zone) + "</p><p class=\"muted\">The interval table also shows source quotes outside the candidate; its final column identifies exactly which intervals were used.</p>" + blocker_html + """</section>
<div class="issues"><section><h2>Blocking checks</h2>""" + _issues(errors, "blocking errors") + """</section>
<section><h2>Review warnings</h2>""" + _issues(warnings, "warnings") + """</section></div>
<section><h2>Price preview</h2><div class="legend"><span><i class="dot import"></i>Import</span><span><i class="dot export"></i>Export earnings</span><span><i class="dot incompatible"></i>Incompatible interval</span></div>
<div class="chart-wrap">""" + _chart(periods, zone) + """</div><p class="muted">The chart preserves the supplied prices. It does not clamp negative prices, raise import prices, or invent missing values. Gaps remain gaps.</p></section>
<section><h2>All aligned intervals</h2>""" + _table(periods, zone) + """</section>
<footer>This standalone file contains no scripts, external assets, credentials, network requests or Tesla write capability. Exact numeric values and validation results are available in the accompanying JSON report.</footer>
</main></body></html>"""
