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
front-desk PC. See claude/staff-dashboard.md for what's covered and what
isn't (no-show tracking, CSAT, etc. -- deliberately left for later).
"""
import os
import html
import logging
import secrets as _secrets
from typing import Optional

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
  h1 { font-size: 22px; margin-bottom: 2px; }
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


def _render_page(clinic: dict, data: dict, days_param: int) -> str:
    kpis = data["kpis"]

    periods_html = ""
    for value, label in PERIOD_CHOICES:
        active = "active" if value == days_param else ""
        periods_html += f'<a class="{active}" href="?days={value}">{html.escape(label)}</a>'

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

    # Needs attention
    if data["needs_attention"]:
        items = ""
        for a in data["needs_attention"]:
            conv_url = f'/clinic/{clinic["slug"]}/dashboard/conversation/{a["conversation_id"]}?days={days_param}'
            items += (
                f'<div class="attention-item"><b>{html.escape(a["patient_name"] or "Unknown")}</b> '
                f'({html.escape(a["phone_number"] or "no number")}) — {_fmt_dt(a["started_at"])} '
                f'— <a class="view-link" href="{conv_url}">View conversation</a>'
                f'<div class="attention-reason">{html.escape(a["reason"] or "No reason given.")}</div></div>'
            )
        attention_html = items
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
            conv_url = f'/clinic/{clinic["slug"]}/dashboard/conversation/{c["conversation_id"]}?days={days_param}'
            rows += (
                f'<tr><td>{_fmt_dt(c["started_at"])}</td>'
                f'<td>{html.escape(c["patient_name"] or "Unknown")}</td>'
                f'<td>{html.escape(c["phone_number"] or "")}</td>'
                f'<td>{c["message_count"]}</td>'
                f'<td>{booked_badge}</td>'
                f'<td><a class="view-link" href="{conv_url}">View</a></td></tr>'
            )
        recent_html = f'<table><tr><th>Started</th><th>Patient</th><th>WhatsApp</th><th>Messages</th><th>Outcome</th><th></th></tr>{rows}</table>'
    else:
        recent_html = '<div class="empty">No conversations in this period.</div>'

    # Top services
    if data["top_services"]:
        max_count = max(s["count"] for s in data["top_services"])
        rows = ""
        for s in data["top_services"]:
            pct = int(s["count"] / max_count * 100) if max_count else 0
            rows += (
                f'<div class="bar-row"><div class="bar-hour" style="width:140px; text-align:left;">{html.escape(s["service_name"])}</div>'
                f'<div class="bar-track"><div class="bar-fill" style="width:{pct}%"></div></div>'
                f'<div class="bar-count">{s["count"]}</div></div>'
            )
        services_html = rows
    else:
        services_html = '<div class="empty">No service breakdown yet — this fills in as bookings come in with a service specified.</div>'

    # Hourly distribution (message volume by hour of day)
    max_hour = max((h["message_count"] for h in data["hourly_distribution"]), default=0)
    hourly_rows = ""
    for h in data["hourly_distribution"]:
        pct = int(h["message_count"] / max_hour * 100) if max_hour else 0
        hourly_rows += (
            f'<div class="bar-row"><div class="bar-hour">{h["hour"]:02d}:00</div>'
            f'<div class="bar-track"><div class="bar-fill" style="width:{pct}%"></div></div>'
            f'<div class="bar-count">{h["message_count"]}</div></div>'
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
  <h1>🦷 {html.escape(clinic['name'])} — Sia Dashboard</h1>
  <div class="subtitle">{html.escape(data['period_label'])} · updated {_fmt_dt(data['generated_at'])} · refreshes automatically every 30s</div>

  <div class="periods">{periods_html}</div>

  <div class="kpi-grid">{kpi_html}</div>

  <h2>Today's schedule</h2>
  {today_html}

  <h2>Needs attention ({kpis['flagged_conversations']})</h2>
  {attention_html}

  <h2>Recent conversations</h2>
  {recent_html}

  <h2>Most requested services</h2>
  {services_html}

  <h2>Message volume by hour of day</h2>
  {hourly_rows}

  <div class="api-hint">
    Want this data in your own system? The same numbers are available as JSON at
    <code>/clinic/{html.escape(clinic['slug'])}/api/dashboard?days={days_param}</code> (same login).
  </div>
</body>
</html>"""


def _render_conversation_page(clinic: dict, conversation, patient, messages: list, booked: bool, days_param: int) -> str:
    back_url = f'/clinic/{clinic["slug"]}/dashboard?days={days_param}'

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
    slug: str, conversation_id: int, days: int = Query(7), credentials: HTTPBasicCredentials = Depends(security)
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

    return HTMLResponse(_render_conversation_page(clinic, conversation, patient, messages, booked, days))


@router.get("/clinic/{slug}/dashboard", response_class=HTMLResponse)
async def clinic_dashboard(slug: str, days: int = Query(7), credentials: HTTPBasicCredentials = Depends(security)):
    clinic = _get_clinic_or_404(slug)
    require_dashboard_auth(clinic, credentials)

    db.set_current_db_path(clinic["db_path"])
    data = analytics.get_dashboard_summary(days=_days_param(days))
    return HTMLResponse(_render_page(clinic, data, days))


@router.get("/clinic/{slug}/api/dashboard")
async def clinic_dashboard_api(slug: str, days: int = Query(7), credentials: HTTPBasicCredentials = Depends(security)):
    clinic = _get_clinic_or_404(slug)
    require_dashboard_auth(clinic, credentials)

    db.set_current_db_path(clinic["db_path"])
    data = analytics.get_dashboard_summary(days=_days_param(days))
    return {
        "clinic": {"name": clinic["name"], "slug": clinic["slug"]},
        **data,
    }
