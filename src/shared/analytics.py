"""
Aggregation queries behind the staff-facing clinic dashboard (see
app/dashboard.py). Kept in its own module, separate from shared/db.py's
one-row-at-a-time CRUD queries, since these are read-only reporting
queries that combine several tables at once.

Like the rest of shared/, these functions run against whichever database
shared/db.get_db_path() currently resolves to. The caller (app/dashboard.py)
is responsible for calling shared.db.set_current_db_path(clinic["db_path"])
first, exactly the way a ClinicWorker does -- because that's a
contextvars.ContextVar, one FastAPI request reading clinic A's dashboard can
never see clinic B's data, even if both are requested at the same instant.

What counts as "this period" throughout: conversations by their started_at,
appointments by when they were BOOKED (created_at) -- not appointment_time,
which is the future slot the patient is booked into -- and patients by when
they first messaged (created_at). Rows written before these created_at
columns existed are NULL and are excluded from period-based counts rather
than guessed at; see the migration notes in shared/db.py.
"""
import logging
from collections import Counter
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from shared.db import db

logger = logging.getLogger(__name__)


def _period_cutoff(days: Optional[int]) -> Optional[str]:
    """None means all-time (no cutoff). Otherwise an ISO timestamp for
    "days ago from now", used as a WHERE started_at/created_at >= ? bound."""
    if days is None:
        return None
    return (datetime.now() - timedelta(days=days)).isoformat()


def get_dashboard_summary(days: Optional[int] = 7) -> Dict[str, Any]:
    """
    Builds everything the staff dashboard shows, in one call: top-line KPIs,
    today's live schedule, conversations needing staff attention, a recent
    conversations feed (including patients who reached out but never
    booked), the most-requested services, and message volume by hour of day
    -- all scoped to the last `days` days, or all-time if `days` is None.

    This does more, smaller queries than a single giant SQL statement would
    need -- deliberately, so each piece stays readable and easy to adjust as
    the dashboard grows, rather than one query trying to do everything at
    once. Clinic-scale data (dozens to low hundreds of conversations) makes
    this a non-issue performance-wise.
    """
    cutoff = _period_cutoff(days)

    with db() as conn:
        # ---- Conversations in period ----
        if cutoff:
            conv_rows = conn.execute(
                "SELECT * FROM conversations WHERE started_at >= ? ORDER BY started_at DESC", (cutoff,)
            ).fetchall()
        else:
            conv_rows = conn.execute("SELECT * FROM conversations ORDER BY started_at DESC").fetchall()
        conversations = [dict(r) for r in conv_rows]
        total_conversations = len(conversations)
        flagged = [c for c in conversations if c["flagged_for_staff"]]

        # ---- Appointments (bookings) in period, by created_at (when Sia
        # actually booked it) -- see module docstring on why not
        # appointment_time. ----
        if cutoff:
            appt_rows = conn.execute(
                "SELECT * FROM appointments WHERE created_at IS NOT NULL AND created_at >= ? ORDER BY created_at DESC",
                (cutoff,),
            ).fetchall()
        else:
            appt_rows = conn.execute(
                "SELECT * FROM appointments WHERE created_at IS NOT NULL ORDER BY created_at DESC"
            ).fetchall()
        appointments = [dict(r) for r in appt_rows]
        bookings_made = len(appointments)
        cancelled = len([a for a in appointments if a["status"] == "cancelled"])

        # ---- Patients/contacts in period ----
        if cutoff:
            patient_rows = conn.execute(
                "SELECT * FROM patients WHERE created_at IS NOT NULL AND created_at >= ?", (cutoff,)
            ).fetchall()
        else:
            patient_rows = conn.execute("SELECT * FROM patients WHERE created_at IS NOT NULL").fetchall()
        new_contacts = [dict(r) for r in patient_rows]
        # A patient row is created the moment a new WhatsApp number first
        # messages, with a placeholder name of "New Patient" until they
        # actually complete registration -- so "new_contacts" is everyone
        # who reached out, and "new_patients_registered" is the subset who
        # actually gave their name (closer to what "new customers" means).
        new_patients_registered = [p for p in new_contacts if p["name"] and p["name"] != "New Patient"]

        # ---- Messages: avg-per-conversation + hourly distribution ----
        conv_ids = [c["id"] for c in conversations]
        message_count_by_conv: Dict[int, int] = {}
        hourly = Counter()
        if conv_ids:
            placeholders = ",".join("?" for _ in conv_ids)
            msg_rows = conn.execute(
                f"SELECT conversation_id, created_at FROM messages WHERE conversation_id IN ({placeholders})",
                conv_ids,
            ).fetchall()
            for m in msg_rows:
                message_count_by_conv[m["conversation_id"]] = message_count_by_conv.get(m["conversation_id"], 0) + 1
                try:
                    hourly[datetime.fromisoformat(m["created_at"]).hour] += 1
                except (ValueError, TypeError):
                    pass
        avg_messages = (sum(message_count_by_conv.values()) / len(conv_ids)) if conv_ids else 0.0

        # ---- Today's schedule -- always "today" regardless of the period
        # filter above; this is the live front-desk view, not a report. ----
        today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        today_end = datetime.now().replace(hour=23, minute=59, second=59, microsecond=0).isoformat()
        today_rows = conn.execute(
            """
            SELECT a.id, a.appointment_time, a.status, a.service_name, a.price_sar,
                   d.name as dentist_name, p.name as patient_name, p.phone_number
            FROM appointments a
            JOIN dentists d ON d.id = a.dentist_id
            JOIN patients p ON p.id = a.patient_id
            WHERE a.appointment_time >= ? AND a.appointment_time <= ? AND a.status != 'cancelled'
            ORDER BY a.appointment_time
            """,
            (today_start, today_end),
        ).fetchall()
        today_appointments = [dict(r) for r in today_rows]

        # ---- Needs attention: flagged conversations ----
        needs_attention = []
        for c in flagged:
            patient = conn.execute(
                "SELECT name, phone_number FROM patients WHERE id=?", (c["patient_id"],)
            ).fetchone()
            needs_attention.append({
                "conversation_id": c["id"],
                "patient_name": patient["name"] if patient else "Unknown",
                "phone_number": patient["phone_number"] if patient else None,
                "reason": c["flag_reason"],
                "started_at": c["started_at"],
                "status": c["status"],
            })
        needs_attention.sort(key=lambda x: x["started_at"], reverse=True)

        # ---- Recent conversations feed, including leads that never booked
        # -- this is the "who reached out and didn't book" view. "booked"
        # is a heuristic: did this patient have an appointment created
        # during this conversation's own timeframe (its started_at through
        # its ended_at, or through now if still open)? Good enough for a
        # front-desk feed; not meant as an exact attribution system. ----
        recent_conversations = []
        for c in conversations[:50]:
            patient = conn.execute(
                "SELECT name, phone_number FROM patients WHERE id=?", (c["patient_id"],)
            ).fetchone()
            booked = False
            if c["patient_id"] is not None:
                booked_row = conn.execute(
                    """
                    SELECT 1 FROM appointments
                    WHERE patient_id = ? AND created_at IS NOT NULL AND created_at >= ?
                      AND (? IS NULL OR created_at <= ?)
                    LIMIT 1
                    """,
                    (c["patient_id"], c["started_at"], c["ended_at"], c["ended_at"]),
                ).fetchone()
                booked = booked_row is not None
            recent_conversations.append({
                "conversation_id": c["id"],
                "patient_name": patient["name"] if patient else "Unknown",
                "phone_number": patient["phone_number"] if patient else None,
                "started_at": c["started_at"],
                "ended_at": c["ended_at"],
                "status": c["status"],
                "message_count": message_count_by_conv.get(c["id"], 0),
                "flagged": bool(c["flagged_for_staff"]),
                "booked": booked,
            })

        # ---- Most-requested services (only reflects bookings made after
        # this field shipped -- service_name is NULL on older rows) ----
        service_counter = Counter(a["service_name"] for a in appointments if a["service_name"])
        top_services = [{"service_name": name, "count": count} for name, count in service_counter.most_common(8)]

        # ---- Estimated revenue booked: sum of prices on non-cancelled
        # bookings in period. An estimate of pipeline value, NOT confirmed
        # or collected revenue -- it has no idea about no-shows or whether
        # the patient actually paid. ----
        priced = [a["price_sar"] for a in appointments if a["price_sar"] is not None and a["status"] != "cancelled"]
        estimated_revenue = sum(priced) if priced else None

    conversion_rate = (bookings_made / total_conversations * 100) if total_conversations else 0.0
    escalation_rate = (len(flagged) / total_conversations * 100) if total_conversations else 0.0

    return {
        "period_days": days,
        "period_label": f"Last {days} days" if days else "All time",
        "generated_at": datetime.now().isoformat(),
        "kpis": {
            "conversations": total_conversations,
            "bookings_made": bookings_made,
            "cancelled_appointments": cancelled,
            "conversion_rate_pct": round(conversion_rate, 1),
            "new_contacts": len(new_contacts),
            "new_patients_registered": len(new_patients_registered),
            "flagged_conversations": len(flagged),
            "escalation_rate_pct": round(escalation_rate, 1),
            "avg_messages_per_conversation": round(avg_messages, 1),
            "estimated_revenue_booked_sar": estimated_revenue,
        },
        "today_appointments": today_appointments,
        "needs_attention": needs_attention,
        "recent_conversations": recent_conversations,
        "top_services": top_services,
        "hourly_distribution": [{"hour": h, "message_count": hourly.get(h, 0)} for h in range(24)],
    }
