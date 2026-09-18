"""
The staff-facing clinic dashboard -- meant to be left open on a screen at
the clinic's own front desk (or checked from a phone), showing what Sia has
been doing on WhatsApp: today's live schedule, conversations that need a
human to step in, a feed of everyone who's messaged recently (including
people who never actually booked), and effectiveness insights (booking
conversion, busiest hours, most-requested services, estimated pipeline
value).

Two endpoints per clinic, both requiring auth (see require_dashboard_auth):
  - GET /clinic/{slug}/dashboard      -- the human-readable page
  - GET /clinic/{slug}/api/dashboard  -- the exact same data as plain JSON,
    so this can be pulled into the clinic's own systems (a practice
    management tool, a Google Sheet via a script, another internal
    dashboard) without scraping the HTML page.

Kept as plain server-rendered HTML with a page auto-refresh, same spirit as
admin.py -- no build step, no JS framework, nothing to break on an old
front-desk PC. Charts are plain inline SVG generated server-side (no chart
library, no client-side JS at all) -- hover tooltips come for free from
native SVG <title> elements. See claude/staff-dashboard.md for what's
covered and what isn't (no-show tracking, CSAT, etc. -- deliberately left
for later).
"""
import os
import math
import html
import logging
import secrets as _secrets
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from shared import clinics_store, db, analytics

logger = logging.getLogger(__name__)

router = APIRouter()
security = HTTPBasic()

PERIOD_CHOICES = [
    (1, "Today"),
    (7, "7 days"),
    (30, "30 days"),
    (0, "All time"),  # 0 here means "all time" -- see _days_param
]

# Logo -- served from the /static mount main.py already sets up (see
# main.py's STATIC_DIR / "static" mount, originally added for the
# brochure image). One shared logo works fine across every clinic's
# dashboard; it's Mawaid's own branding, not per-clinic.
LOGO_URL = "/static/logo.png"

# ---------------------------------------------------------------------
# Chart colors -- from the validated reference palette (see the dataviz
# skill's references/palette.md). Status colors for the outcome donut
# (booked/flagged/no-booking are states, not arbitrary categories); a
# single sequential blue for magnitude bars (volume by month, by hour,
# by service).
# ---------------------------------------------------------------------
CHART_BLUE = "#2a78d6"
STATUS_GOOD = "#0ca30c"
STATUS_WARNING = "#fab219"
STATUS_MUTED = "#898781"
INK_PRIMARY = "#0b0b0b"
INK_SECONDARY = "#52514e"
INK_MUTED = "#898781"
BASELINE = "#c3c2b7"


def _days_param(days: int) -> Optional[int]:
    return None if days <= 0 else days


def require_dashboard_auth(clinic: dict, credentials: HTTPBasicCredentials):
    """Accepts EITHER the site-wide admin login (so whoever manages
    /admin/clinics never has to remember every clinic's own password), OR
    that specific clinic's own dashboard_password with its slug as the
    username (what you actually hand to that clinic's front desk)."""
    admin_username = os.environ.get("ADMIN_USERNAME", "admin")
    admin_password = os.environ.get("ADMIN_PASSWORD")
    if admin_password and _secrets.compare_digest(credentials.username, admin_username) \
            and _secrets.compare_digest(credentials.password, admin_password):
        return

    clinic_password = clinic.get("dashboard_password") or clinics_store.ensure_dashboard_password(clinic["id"]) or ""
    if clinic_password and _secrets.compare_digest(credentials.username, clinic["slug"]) \
            and _secrets.compare_digest(credentials.password, clinic_password):
        return

    raise HTTPException(
        status_code=401,
        detail="Incorrect dashboard username or password",
        headers={"WWW-Authenticate": "Basic"},
    )


def _get_clinic_or_404(slug: str) -> dict:
    clinic = clinics_store.get_clinic_by_slug(slug)
    if not clinic:
        raise HTTPException(status_code=404, detail=f"No clinic with slug '{slug}'")
    return clinic


PAGE_STYLE = """
<style>
  body { font-family: -apple-system, Segoe UI, Arial, sans-serif; max-width: 1100px; margin: 24px auto; padding: 0 16px; color: #1a1a1a; }
  .brand-row { display: flex; align-items: center; gap: 16px; margin-bottom: 6px; }
  .brand-row img { width: 128px; height: 128px; border-radius: 24px; flex: none; }
  h1 { font-size: 22px; margin: 0; }
  .subtitle { color: #666; font-size: 13px; margin-bottom: 18px; }
  h2 { font-size: 16px; margin-top: 30px; border-bottom: 1px solid #ddd; padding-bottom: 6px; }
  .periods { margin: 10px 0 4px 0; }
  .periods a { display: inline-block; padding: 6px 14px; margin-right: 6px; border-radius: 16px; background: #f0f0f0; color: #333; text-decoration: none; font-size: 13px; }
  .periods a.active { background: #1a7f37; color: white; }
  .kpi-grid { display: flex; flex-wrap: wrap; gap: 12px; margin-top: 10px; }
  .kpi-card { flex: 1 1 150px; background: #f7f7f7; border-radius: 8px; padding: 14px 16px; }
  .kpi-value { font-size: 26px; font-weight: 700; }
  .kpi-label { font-size: 12px; color: #666; margin-top: 2px; }
  table { border-collapse: collapse; width: 100%; margin-top: 8px; }
  th, td { text-align: left; padding: 8px; border-bottom: 1px solid #eee; font-size: 13px; }
  th { background: #f7f7f7; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: 600; }
  .badge-yes { background: #e6ffed; color: #1a7f37; }
  .badge-no { background: #f0f0f0; color: #999; }
  .badge-flag { background: #fff3cd; color: #8a6300; }
  .badge-cancelled { background: #ffeef0; color: #cf222e; }
  .empty { color: #999; font-size: 13px; padding: 10px 0; }
  .bar-row { display: flex; align-items: center; gap: 8px; font-size: 12px; margin: 2px 0; }
  .bar-hour { width: 34px; color: #666; text-align: right; }
  .bar-track { flex: 1; background: #f0f0f0; border-radius: 3px; height: 12px; overflow: hidden; }
  .bar-fill { background: #1a7f37; height: 100%; }
  .bar-count { width: 24px; color: #666; }
  .attention-item { background: #fff8e6; border: 1px solid #f0d78c; border-radius: 6px; padding: 10px 12px; margin-bottom: 8px; }
  .attention-reason { font-size: 13px; margin-top: 4px; }
  .api-hint { color: #666; font-size: 12px; margin-top: 30px; padding-top: 10px; border-top: 1px solid #eee; }
  .api-hint code { background: #f0f0f0; padding: 2px 5px; border-radius: 3px; }
  .back-link { display: inline-block; margin-bottom: 10px; font-size: 13px; color: #1a7f37; text-decoration: none; }
  .conv-meta { color: #666; font-size: 13px; margin-bottom: 16px; }
  .transcript { margin-top: 10px; }
  .bubble-row { display: flex; margin-bottom: 10px; }
  .bubble-row.patient { justify-content: flex-start; }
  .bubble-row.sia { justify-content: flex-end; }
  .bubble { max-width: 65%; padding: 8px 12px; border-radius: 12px; font-size: 14px; line-height: 1.4; white-space: pre-wrap; }
  .bubble-row.patient .bubble { background: #f0f0f0; color: #1a1a1a; border-bottom-left-radius: 3px; }
  .bubble-row.sia .bubble { background: #dcf3e3; color: #14532d; border-bottom-right-radius: 3px; }
  .bubble-time { font-size: 10px; color: #999; margin-top: 3px; }
  .view-link { font-size: 12px; }
  .chart-wrap { margin: 10px 0 20px 0; }
  .donut-wrap { display: flex; align-items: center; gap: 24px; flex-wrap: wrap; margin: 10px 0 20px 0; }
  .legend { display: flex; flex-direction: column; gap: 8px; font-size: 13px; }
  .legend-item { display: flex; align-items: center; gap: 8px; }
  .legend-dot { width: 10px; height: 10px; border-radius: 50%; display: inline-block; flex: none; }
  .filter-bar { background: #f7f7f7; border-radius: 8px; padding: 12px 14px; margin: 16px 0 6px 0; }
  .filter-bar form { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
  .filter-bar input[type=text] { padding: 6px 10px; border: 1px solid #ddd; border-radius: 6px; font-size: 13px; min-width: 180px; }
  .filter-bar input[type=date] { padding: 5px 8px; border: 1px solid #ddd; border-radius: 6px; font-size: 13px; }
  .filter-bar .date-field { display: flex; align-items: center; gap: 4px; font-size: 12px; color: #666; }
  .filter-bar button { padding: 6px 14px; border: none; border-radius: 6px; background: #1a7f37; color: white; font-size: 13px; cursor: pointer; }
  .filter-clear { font-size: 12px; color: #666; text-decoration: none; margin-left: 4px; }
  .filter-hint { font-size: 11px; color: #999; margin-top: 6px; }
  .truncated-note { font-size: 12px; color: #999; margin-top: 6px; }
</style>
"""


def _fmt_dt(value: Optional[str]) -> str:
    if not value:
        return "—"
    # Values are stored as ISO strings like "2026-09-18T14:05:00"; just trim
    # to something readable without pulling in a date-formatting dependency.
    return value.replace("T", " ")[:16]


def _fmt_money(value) -> str:
    if value is None:
        return "—"
    return f"{value:,.0f} SAR"


def _filter_query_suffix(name_filter: str, date_from: str, date_to: str) -> str:
    """&name=...&from=...&to=... fragment (possibly empty) for chaining
    onto other query strings, so links (period switch, conversation view,
    back button) don't silently drop the current filters."""
    parts = []
    if name_filter:
        parts.append(f"name={quote(name_filter)}")
    if date_from:
        parts.append(f"from={quote(date_from)}")
    if date_to:
        parts.append(f"to={quote(date_to)}")
    return ("&" + "&".join(parts)) if parts else ""


def _svg_bar_chart(
    items, *, value_key: str, label_key: str, width: int = 760, height: int = 180,
    bar_color: str = CHART_BLUE, value_fmt=None, show_values: bool = True, label_every: int = 1,
) -> str:
    """Vertical bar chart: <=24px bars, 4px rounded data-ends, single
    baseline, sparing direct labels (per marks-and-anatomy.md). Native
    <title> on each bar gives a hover tooltip with no JS."""
    if not items:
        return '<div class="empty">Not enough data yet.</div>'
    value_fmt = value_fmt or (lambda v: str(v))
    n = len(items)
    max_val = max((i[value_key] for i in items), default=0) or 1
    pad_left, pad_right, pad_top, pad_bottom = 8, 8, 22, 26
    plot_w = width - pad_left - pad_right
    plot_h = height - pad_top - pad_bottom
    slot_w = plot_w / n
    bar_w = min(24.0, slot_w * 0.55)

    parts = [
        f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" '
        f'preserveAspectRatio="xMinYMin meet" role="img" aria-label="bar chart">'
    ]
    baseline_y = pad_top + plot_h
    for idx, item in enumerate(items):
        v = item[value_key]
        bar_h = (v / max_val) * plot_h if max_val else 0
        x = pad_left + idx * slot_w + (slot_w - bar_w) / 2
        y = baseline_y - bar_h
        r = min(4.0, bar_h / 2) if bar_h > 0 else 0
        label = html.escape(str(item[label_key]))
        title = f"{label}: {html.escape(value_fmt(v))}"
        parts.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{max(bar_h, 1):.1f}" '
            f'rx="{r:.1f}" ry="{r:.1f}" fill="{bar_color}"><title>{title}</title></rect>'
        )
        if show_values and v:
            parts.append(
                f'<text x="{x + bar_w / 2:.1f}" y="{y - 6:.1f}" text-anchor="middle" '
                f'font-size="10" fill="{INK_SECONDARY}">{html.escape(value_fmt(v))}</text>'
            )
        if idx % label_every == 0:
            parts.append(
                f'<text x="{x + bar_w / 2:.1f}" y="{height - 8:.1f}" text-anchor="middle" '
                f'font-size="10" fill="{INK_MUTED}">{label}</text>'
            )
    parts.append(
        f'<line x1="{pad_left}" y1="{baseline_y:.1f}" x2="{width - pad_right}" y2="{baseline_y:.1f}" '
        f'stroke="{BASELINE}" stroke-width="1"/>'
    )
    parts.append("</svg>")
    return f'<div class="chart-wrap">{"".join(parts)}</div>'


def _svg_hbar_chart(
    items, *, value_key: str, label_key: str, width: int = 700, bar_h: int = 18, gap: int = 12,
    bar_color: str = CHART_BLUE, value_fmt=None,
) -> str:
    """Horizontal bar chart -- for magnitude comparisons with longer text
    labels (service names) that would collide as vertical-bar x-labels."""
    if not items:
        return '<div class="empty">Not enough data yet.</div>'
    value_fmt = value_fmt or (lambda v: str(v))
    max_val = max((i[value_key] for i in items), default=0) or 1
    label_w = 150
    right_pad = 48
    plot_w = max(width - label_w - right_pad, 40)
    height = len(items) * (bar_h + gap) + gap

    parts = [
        f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" '
        f'preserveAspectRatio="xMinYMin meet" role="img" aria-label="bar chart">'
    ]
    y = gap
    for item in items:
        v = item[value_key]
        w = (v / max_val) * plot_w if max_val else 0
        r = min(4.0, bar_h / 2)
        label = html.escape(str(item[label_key]))
        parts.append(
            f'<text x="{label_w - 8}" y="{y + bar_h / 2 + 4:.1f}" text-anchor="end" '
            f'font-size="12" fill="{INK_SECONDARY}">{label}</text>'
        )
        parts.append(
            f'<rect x="{label_w}" y="{y}" width="{max(w, 2):.1f}" height="{bar_h}" '
            f'rx="{r:.1f}" ry="{r:.1f}" fill="{bar_color}">'
            f'<title>{label}: {html.escape(value_fmt(v))}</title></rect>'
        )
        parts.append(
            f'<text x="{label_w + w + 8:.1f}" y="{y + bar_h / 2 + 4:.1f}" '
            f'font-size="11" fill="{INK_SECONDARY}">{html.escape(value_fmt(v))}</text>'
        )
        y += bar_h + gap
    parts.append("</svg>")
    return f'<div class="chart-wrap">{"".join(parts)}</div>'


def _svg_donut_chart(slices, *, size: int = 160, thickness: int = 26) -> str:
    """slices: list of (label, value, color) tuples. A status color never
    carries meaning alone -- each slice gets a legend row with a dot + the
    label + the count + the percentage (the 'icon + label' pairing the
    palette's status colors require)."""
    total = sum(v for _, v, _ in slices if v)
    if not total:
        return '<div class="empty">Not enough data yet.</div>'

    cx = cy = size / 2
    r = (size - thickness) / 2
    circumference = 2 * math.pi * r
    offset = 0.0
    arcs = []
    legend_items = []
    for label, value, color in slices:
        if not value:
            continue
        frac = value / total
        dash = circumference * frac
        arcs.append(
            f'<circle cx="{cx}" cy="{cy}" r="{r:.2f}" fill="none" stroke="{color}" stroke-width="{thickness}" '
            f'stroke-dasharray="{dash:.2f} {circumference - dash:.2f}" stroke-dashoffset="{-offset:.2f}" '
            f'transform="rotate(-90 {cx} {cy})"><title>{html.escape(label)}: {value} ({frac * 100:.0f}%)</title></circle>'
        )
        offset += dash
        legend_items.append(
            f'<div class="legend-item"><span class="legend-dot" style="background:{color}"></span>'
            f'{html.escape(label)} — <b>{value}</b> ({frac * 100:.0f}%)</div>'
        )

    svg = (
        f'<svg viewBox="0 0 {size} {size}" width="{size}" height="{size}" role="img" aria-label="donut chart">'
        + "".join(arcs)
        + f'<text x="{cx}" y="{cy - 4}" text-anchor="middle" font-size="22" font-weight="700" fill="{INK_PRIMARY}">{total}</text>'
        + f'<text x="{cx}" y="{cy + 14}" text-anchor="middle" font-size="10" fill="{INK_MUTED}">conversations</text>'
        + "</svg>"
    )
    return f'<div class="donut-wrap"><div>{svg}</div><div class="legend">{"".join(legend_items)}</div></div>'


def _render_page(clinic: dict, data: dict, days_param: int) -> str:
    kpis = data["kpis"]
    filters = data.get("filters", {})
    name_filter_val = filters.get("name", "")
    date_from_val = filters.get("from", "")
    date_to_val = filters.get("to", "")
    filter_qs = _filter_query_suffix(name_filter_val, date_from_val, date_to_val)

    periods_html = ""
    for value, label in PERIOD_CHOICES:
        active = "active" if value == days_param else ""
        periods_html += f'<a class="{active}" href="?days={value}{filter_qs}">{html.escape(label)}</a>'

    kpi_cards = [
        (kpis["conversations"], "Conversations"),
        (kpis["bookings_made"], "Bookings made"),
        (f'{kpis["conversion_rate_pct"]}%', "Conversion rate"),
        (kpis["new_patients_registered"], "New patients"),
        (kpis["flagged_conversations"], "Flagged for staff"),
        (kpis["cancelled_appointments"], "Cancelled"),
        (kpis["avg_messages_per_conversation"], "Avg. messages/chat"),
        (_fmt_money(kpis["estimated_revenue_booked_sar"]), "Est. booked value"),
    ]
    kpi_html = "".join(
        f'<div class="kpi-card"><div class="kpi-value">{html.escape(str(v))}</div>'
        f'<div class="kpi-label">{html.escape(label)}</div></div>'
        for v, label in kpi_cards
    )

    # Today's schedule
    if data["today_appointments"]:
        rows = ""
        for a in data["today_appointments"]:
            badge = f'<span class="badge badge-cancelled">{html.escape(a["status"])}</span>' if a["status"] == "cancelled" else html.escape(a["status"])
            rows += (
                f'<tr><td>{_fmt_dt(a["appointment_time"])[-5:]}</td>'
                f'<td>{html.escape(a["patient_name"] or "")}</td>'
                f'<td>{html.escape(a["dentist_name"] or "")}</td>'
                f'<td>{html.escape(a["service_name"] or "—")}</td>'
                f'<td>{badge}</td></tr>'
            )
        today_html = f'<table><tr><th>Time</th><th>Patient</th><th>Dentist</th><th>Service</th><th>Status</th></tr>{rows}</table>'
    else:
        today_html = '<div class="empty">No appointments scheduled for today.</div>'

    # Filter bar -- controls the two lists below it (needs attention +
    # recent conversations). A From/To date narrows to an exact day (set
    # both the same) or a range, and overrides the period buttons above
    # for these two lists only. A name search with no date searches the
    # clinic's whole history, not just the current period.
    clear_link = (
        f'<a class="filter-clear" href="?days={days_param}">Clear filters</a>'
        if (name_filter_val or date_from_val or date_to_val) else ""
    )
    filter_bar_html = f"""
  <div class="filter-bar">
    <form method="get">
      <input type="hidden" name="days" value="{days_param}">
      <input type="text" name="name" placeholder="Search patient name…" value="{html.escape(name_filter_val)}">
      <span class="date-field">From <input type="date" name="from" value="{html.escape(date_from_val)}"></span>
      <span class="date-field">To <input type="date" name="to" value="{html.escape(date_to_val)}"></span>
      <button type="submit">Filter</button>
      {clear_link}
    </form>
    <div class="filter-hint">Filters "Needs attention" and "Recent conversations" below. Set From and To to the same date for a single day. A name search with no date searches this clinic's whole history, not just the period buttons above.</div>
  </div>
"""

    # Needs attention
    if data["needs_attention"]:
        items = ""
        for a in data["needs_attention"]:
            conv_url = f'/clinic/{clinic["slug"]}/dashboard/conversation/{a["conversation_id"]}?days={days_param}{filter_qs}'
            items += (
                f'<div class="attention-item"><b>{html.escape(a["patient_name"] or "Unknown")}</b> '
                f'({html.escape(a["phone_number"] or "no number")}) — {_fmt_dt(a["started_at"])} '
                f'— <a class="view-link" href="{conv_url}">View conversation</a>'
                f'<div class="attention-reason">{html.escape(a["reason"] or "No reason given.")}</div></div>'
            )
        attention_html = items
    elif name_filter_val or date_from_val or date_to_val:
        attention_html = '<div class="empty">Nothing flagged matches this filter.</div>'
    else:
        attention_html = '<div class="empty">Nothing flagged right now.</div>'

    # Recent conversations feed
    if data["recent_conversations"]:
        rows = ""
        for c in data["recent_conversations"]:
            if c["flagged"]:
                booked_badge = '<span class="badge badge-flag">flagged</span>'
            elif c["booked"]:
                booked_badge = '<span class="badge badge-yes">booked</span>'
            else:
                booked_badge = '<span class="badge badge-no">no booking</span>'
            conv_url = f'/clinic/{clinic["slug"]}/dashboard/conversation/{c["conversation_id"]}?days={days_param}{filter_qs}'
            rows += (
                f'<tr><td>{_fmt_dt(c["started_at"])}</td>'
                f'<td>{html.escape(c["patient_name"] or "Unknown")}</td>'
                f'<td>{html.escape(c["phone_number"] or "")}</td>'
                f'<td>{c["message_count"]}</td>'
                f'<td>{booked_badge}</td>'
                f'<td><a class="view-link" href="{conv_url}">View</a></td></tr>'
            )
        recent_html = f'<table><tr><th>Started</th><th>Patient</th><th>WhatsApp</th><th>Messages</th><th>Outcome</th><th></th></tr>{rows}</table>'
        if data.get("recent_conversations_truncated"):
            recent_html += f'<div class="truncated-note">Showing the most recent {len(data["recent_conversations"])} matches — narrow the filter (add a name, or a tighter date range) to see more precisely.</div>'
    elif name_filter_val or date_from_val or date_to_val:
        recent_html = '<div class="empty">No conversations match this filter.</div>'
    else:
        recent_html = '<div class="empty">No conversations in this period.</div>'

    # Conversation outcomes -- donut
    ob = data.get("outcome_breakdown", {})
    outcomes_html = _svg_donut_chart([
        ("Booked", ob.get("booked", 0), STATUS_GOOD),
        ("Flagged for staff", ob.get("flagged", 0), STATUS_WARNING),
        ("No booking", ob.get("no_booking", 0), STATUS_MUTED),
    ])

    # Conversations by month -- trend bar chart (fixed 6-month window)
    trend_html = _svg_bar_chart(
        data.get("monthly_trend", []), value_key="count", label_key="label", bar_color=CHART_BLUE,
    )

    # Top services -- horizontal bar chart
    services_html = _svg_hbar_chart(
        data["top_services"], value_key="count", label_key="service_name", bar_color=CHART_BLUE,
    )
    if not data["top_services"]:
        services_html = '<div class="empty">No service breakdown yet — this fills in as bookings come in with a service specified.</div>'

    # Message volume by hour of day -- vertical bar chart, values hidden
    # (24 direct labels would be clutter -- the hover tooltip has the
    # exact count) and hour labels shown every 3 hours.
    hourly_items = [
        {"label": f'{h["hour"]:02d}', "message_count": h["message_count"]}
        for h in data["hourly_distribution"]
    ]
    hourly_html = _svg_bar_chart(
        hourly_items, value_key="message_count", label_key="label", width=900, height=160,
        bar_color=CHART_BLUE, show_values=False, label_every=3,
    )

    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta http-equiv="refresh" content="30">
  <title>{html.escape(clinic['name'])} — Dashboard</title>
  {PAGE_STYLE}
</head>
<body>
  <div class="brand-row"><img src="{LOGO_URL}" alt="Mawaid"><h1>{html.escape(clinic['name'])} — Sia Dashboard</h1></div>
  <div class="subtitle">{html.escape(data['period_label'])} · updated {_fmt_dt(data['generated_at'])} · refreshes automatically every 30s</div>

  <div class="periods">{periods_html}</div>

  <div class="kpi-grid">{kpi_html}</div>

  <h2>Conversation outcomes</h2>
  {outcomes_html}

  <h2>Conversations by month</h2>
  {trend_html}

  <h2>Today's schedule</h2>
  {today_html}

  {filter_bar_html}

  <h2>Needs attention ({len(data['needs_attention'])})</h2>
  {attention_html}

  <h2>Recent conversations</h2>
  {recent_html}

  <h2>Most requested services</h2>
  {services_html}

  <h2>Message volume by hour of day</h2>
  {hourly_html}

  <div class="api-hint">
    Want this data in your own system? The same numbers are available as JSON at
    <code>/clinic/{html.escape(clinic['slug'])}/api/dashboard?days={days_param}</code> (same login).
  </div>
</body>
</html>"""


def _render_conversation_page(
    clinic: dict, conversation, patient, messages: list, booked: bool, days_param: int,
    name_filter: str = "", date_from: str = "", date_to: str = "",
) -> str:
    back_url = f'/clinic/{clinic["slug"]}/dashboard?days={days_param}{_filter_query_suffix(name_filter, date_from, date_to)}'

    if conversation.flagged_for_staff:
        flag_banner = (
            f'<div class="attention-item"><b>Flagged for staff</b>'
            f'<div class="attention-reason">{html.escape(conversation.flag_reason or "No reason given.")}</div></div>'
        )
    else:
        flag_banner = ""

    outcome_badge = '<span class="badge badge-yes">Booked during this conversation</span>' if booked \
        else '<span class="badge badge-no">No booking made</span>'

    # Only the human-readable half of the transcript -- "tool"/"agent_tool_call"
    # rows are Sia's internal tool calls (booking lookups, etc.), not something
    # a patient saw or a receptionist needs to read to understand the chat.
    visible_messages = [m for m in messages if m.sender in ("user", "agent")]

    if visible_messages:
        bubbles = ""
        for m in visible_messages:
            side = "patient" if m.sender == "user" else "sia"
            speaker = (patient.name if patient else "Patient") if side == "patient" else "Sia"
            bubbles += (
                f'<div class="bubble-row {side}"><div>'
                f'<div class="bubble">{html.escape(m.message)}</div>'
                f'<div class="bubble-time">{html.escape(speaker)} · {_fmt_dt(str(m.created_at))}</div>'
                f'</div></div>'
            )
        transcript_html = bubbles
    else:
        transcript_html = '<div class="empty">No messages in this conversation.</div>'

    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Conversation — {html.escape(patient.name if patient else 'Unknown')} — {html.escape(clinic['name'])}</title>
  {PAGE_STYLE}
</head>
<body>
  <div class="brand-row"><img src="{LOGO_URL}" alt="Mawaid"></div>
  <a class="back-link" href="{back_url}">&larr; Back to dashboard</a>
  <h1>💬 {html.escape(patient.name if patient else 'Unknown patient')}</h1>
  <div class="conv-meta">
    {html.escape(patient.phone_number if patient else '')} · started {_fmt_dt(str(conversation.started_at))}
    · status: {html.escape(conversation.status)} · {outcome_badge}
  </div>

  {flag_banner}

  <div class="transcript">{transcript_html}</div>
</body>
</html>"""


@router.get("/clinic/{slug}/dashboard/conversation/{conversation_id}", response_class=HTMLResponse)
async def clinic_dashboard_conversation(
    slug: str, conversation_id: int, days: int = Query(7),
    name: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None, alias="from"),
    date_to: Optional[str] = Query(None, alias="to"),
    credentials: HTTPBasicCredentials = Depends(security),
):
    clinic = _get_clinic_or_404(slug)
    require_dashboard_auth(clinic, credentials)

    db.set_current_db_path(clinic["db_path"])
    conversation = db.get_conversation(conversation_id)
    if not conversation:
        raise HTTPException(status_code=404, detail="No such conversation")

    patient = db.get_patient(conversation.patient_id) if conversation.patient_id else None
    messages = db.get_messages(conversation_id)

    booked = False
    if conversation.patient_id is not None:
        with db.db() as conn:
            booked_row = conn.execute(
                """
                SELECT 1 FROM appointments
                WHERE patient_id = ? AND created_at IS NOT NULL AND created_at >= ?
                  AND (? IS NULL OR created_at <= ?)
                LIMIT 1
                """,
                (
                    conversation.patient_id, conversation.started_at.isoformat(),
                    conversation.ended_at.isoformat() if conversation.ended_at else None,
                    conversation.ended_at.isoformat() if conversation.ended_at else None,
                ),
            ).fetchone()
            booked = booked_row is not None

    return HTMLResponse(_render_conversation_page(
        clinic, conversation, patient, messages, booked, days, name or "", date_from or "", date_to or "",
    ))


@router.get("/clinic/{slug}/dashboard", response_class=HTMLResponse)
async def clinic_dashboard(
    slug: str, days: int = Query(7),
    name: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None, alias="from"),
    date_to: Optional[str] = Query(None, alias="to"),
    credentials: HTTPBasicCredentials = Depends(security),
):
    clinic = _get_clinic_or_404(slug)
    require_dashboard_auth(clinic, credentials)

    db.set_current_db_path(clinic["db_path"])
    data = analytics.get_dashboard_summary(days=_days_param(days), name_filter=name, date_from=date_from, date_to=date_to)
    return HTMLResponse(_render_page(clinic, data, days))


@router.get("/clinic/{slug}/api/dashboard")
async def clinic_dashboard_api(
    slug: str, days: int = Query(7),
    name: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None, alias="from"),
    date_to: Optional[str] = Query(None, alias="to"),
    credentials: HTTPBasicCredentials = Depends(security),
):
    clinic = _get_clinic_or_404(slug)
    require_dashboard_auth(clinic, credentials)

    db.set_current_db_path(clinic["db_path"])
    data = analytics.get_dashboard_summary(days=_days_param(days), name_filter=name, date_from=date_from, date_to=date_to)
    return {
        "clinic": {"name": clinic["name"], "slug": clinic["slug"]},
        **data,
    }
