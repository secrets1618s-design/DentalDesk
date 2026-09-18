"""
Control-plane store for every clinic running inside this ONE Mawaid
service.

Each row here is one dental clinic: its own WhatsApp Business Account
credentials (access token, phone number ID, WABA ID), its own clinic
details (hours, address, services, dentists, offers brochure), and its own
subscription/trial plan. This is what the "Add Clinic" admin page (see
src/app/admin.py) reads and writes -- adding a row here, plus subscribing
its WABA to this app, is the ONLY thing needed to bring a new clinic
online. No code change, no git push, no new Railway service, no restart.

Each clinic still gets its OWN separate SQLite database file (patients,
appointments, conversations, messages) and its OWN separate
clinic_config.yaml file, written under data/clinics/<slug>/ on the
persistent volume -- so clinics are fully isolated from each other on
disk, the same way separate Railway services used to isolate them, just
without needing separate services. A bug in one clinic's data can never
leak into another clinic's, because they are physically different files.
"""
import os
import re
import json
import shutil
import sqlite3
import logging
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

CONTROL_DB_PATH = os.environ.get(
    "CONTROL_DB_PATH",
    os.path.join(os.path.dirname(__file__), "..", "..", "data", "control.db"),
)

DATA_ROOT = os.environ.get(
    "CLINICS_DATA_ROOT",
    os.path.join(os.path.dirname(__file__), "..", "..", "data", "clinics"),
)

LEGACY_CONFIG_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "config", "clinic_config.yaml"
)
LEGACY_DB_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "data", "dentaldesk_app.db"
)
LEGACY_STATIC_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "static"
)


@contextmanager
def _db():
    os.makedirs(os.path.dirname(CONTROL_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(CONTROL_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS clinics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,

    whatsapp_access_token TEXT NOT NULL,
    whatsapp_phone_number_id TEXT NOT NULL UNIQUE,
    whatsapp_waba_id TEXT NOT NULL,

    general_hours TEXT,
    parking TEXT,
    phone TEXT,
    address TEXT,
    google_maps_link TEXT,
    offers_text TEXT,
    offers_image_filename TEXT,
    holidays_json TEXT NOT NULL DEFAULT '[]',
    services_json TEXT NOT NULL DEFAULT '[]',
    dentists_json TEXT NOT NULL DEFAULT '[]',

    subscription_plan TEXT NOT NULL DEFAULT 'trial',
    subscription_started_at TEXT NOT NULL,

    db_path TEXT NOT NULL,
    config_path TEXT NOT NULL,
    static_dir TEXT NOT NULL,

    created_at TEXT NOT NULL
);
"""


def init_control_db():
    os.makedirs(os.path.dirname(CONTROL_DB_PATH), exist_ok=True)
    os.makedirs(DATA_ROOT, exist_ok=True)
    with _db() as conn:
        conn.executescript(SCHEMA_SQL)


def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    d = dict(row)
    d["holidays"] = json.loads(d.pop("holidays_json") or "[]")
    d["services"] = json.loads(d.pop("services_json") or "[]")
    d["dentists"] = json.loads(d.pop("dentists_json") or "[]")
    d["active"] = bool(d["active"])
    return d


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return slug or "clinic"


def _unique_slug(conn, base_slug: str) -> str:
    slug = base_slug
    n = 2
    while conn.execute("SELECT 1 FROM clinics WHERE slug=?", (slug,)).fetchone():
        slug = f"{base_slug}-{n}"
        n += 1
    return slug


def list_clinics() -> List[Dict[str, Any]]:
    init_control_db()
    with _db() as conn:
        rows = conn.execute("SELECT * FROM clinics ORDER BY id").fetchall()
        return [_row_to_dict(r) for r in rows]


def get_clinic(clinic_id: int) -> Optional[Dict[str, Any]]:
    with _db() as conn:
        row = conn.execute("SELECT * FROM clinics WHERE id=?", (clinic_id,)).fetchone()
        return _row_to_dict(row) if row else None


def get_clinic_by_phone_number_id(phone_number_id: str) -> Optional[Dict[str, Any]]:
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM clinics WHERE whatsapp_phone_number_id=?", (phone_number_id,)
        ).fetchone()
        return _row_to_dict(row) if row else None


def write_clinic_config_yaml(clinic: Dict[str, Any]):
    """(Re)writes this clinic's clinic_config.yaml from its DB row, so the
    per-clinic MCP tool process (which reads that file, exactly the way the
    original single-clinic app did) sees the same data entered in the Add
    Clinic form."""
    import yaml

    data = {
        "clinic": {
            "name": clinic["name"],
            "general_hours": clinic.get("general_hours") or "",
            "parking": clinic.get("parking") or "",
            "phone": clinic.get("phone") or "",
            "address": clinic.get("address") or "",
            "google_maps_link": clinic.get("google_maps_link") or "",
            "offers_text": clinic.get("offers_text") or "",
            "offers_image_filename": clinic.get("offers_image_filename"),
            "holidays": clinic.get("holidays") or [],
        },
        "services": clinic.get("services") or [],
        "dentists": clinic.get("dentists") or [],
    }
    os.makedirs(os.path.dirname(clinic["config_path"]), exist_ok=True)
    with open(clinic["config_path"], "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)


def add_clinic(
    *,
    name: str,
    whatsapp_access_token: str,
    whatsapp_phone_number_id: str,
    whatsapp_waba_id: str,
    general_hours: str = "",
    parking: str = "",
    phone: str = "",
    address: str = "",
    google_maps_link: str = "",
    offers_text: str = "",
    offers_image_filename: Optional[str] = None,
    holidays: Optional[List[str]] = None,
    services: Optional[List[Dict[str, Any]]] = None,
    dentists: Optional[List[Dict[str, Any]]] = None,
    subscription_plan: str = "trial",
    subscription_started_at: Optional[str] = None,
) -> Dict[str, Any]:
    """Adds a new clinic and creates its own private data folder
    (data/clinics/<slug>/) for its database, config file, and uploaded
    brochure image. Returns the full clinic record (including its new id
    and slug)."""
    init_control_db()
    started_at = subscription_started_at or datetime.now().strftime("%Y-%m-%d")

    with _db() as conn:
        slug = _unique_slug(conn, slugify(name))
        clinic_dir = os.path.join(DATA_ROOT, slug)
        static_dir = os.path.join(clinic_dir, "static")
        os.makedirs(static_dir, exist_ok=True)

        db_path = os.path.join(clinic_dir, "dentaldesk_app.db")
        config_path = os.path.join(clinic_dir, "clinic_config.yaml")

        cur = conn.execute(
            """
            INSERT INTO clinics (
                slug, name, active,
                whatsapp_access_token, whatsapp_phone_number_id, whatsapp_waba_id,
                general_hours, parking, phone, address, google_maps_link,
                offers_text, offers_image_filename,
                holidays_json, services_json, dentists_json,
                subscription_plan, subscription_started_at,
                db_path, config_path, static_dir, created_at
            ) VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                slug, name,
                whatsapp_access_token, whatsapp_phone_number_id, whatsapp_waba_id,
                general_hours, parking, phone, address, google_maps_link,
                offers_text, offers_image_filename,
                json.dumps(holidays or []), json.dumps(services or []), json.dumps(dentists or []),
                subscription_plan, started_at,
                db_path, config_path, static_dir, datetime.now().isoformat(),
            ),
        )
        clinic_id = cur.lastrowid

    clinic = get_clinic(clinic_id)
    write_clinic_config_yaml(clinic)
    logger.info("Added new clinic id=%s slug=%s name=%r", clinic_id, slug, name)
    return clinic


def update_offers_image_filename(clinic_id: int, filename: str):
    with _db() as conn:
        conn.execute("UPDATE clinics SET offers_image_filename=? WHERE id=?", (filename, clinic_id))


def set_clinic_active(clinic_id: int, active: bool):
    with _db() as conn:
        conn.execute("UPDATE clinics SET active=? WHERE id=?", (1 if active else 0, clinic_id))


def remove_clinic(clinic_id: int) -> Optional[Dict[str, Any]]:
    """Permanently removes a clinic from this app: deletes its row from the
    clinics table and deletes its entire per-clinic data folder (database,
    config, uploaded brochure image) under data/clinics/<slug>/.

    Returns the clinic record as it was right before deletion (so the
    caller can show/log its name), or None if no clinic with this id
    exists.

    This is the "in the app" half of removing a clinic only -- it does NOT
    touch anything on Meta's side. The WhatsApp access token stays valid
    there until you revoke it yourself in Meta's Business Settings, and
    the phone number stays registered until disconnected there. See
    claude/how-to-add-remove-clinic-whatsapp-number.md for those steps.
    """
    clinic = get_clinic(clinic_id)
    if not clinic:
        return None

    with _db() as conn:
        conn.execute("DELETE FROM clinics WHERE id=?", (clinic_id,))

    # Only ever delete folders inside DATA_ROOT -- this protects the one
    # clinic that may still be pointed at the LEGACY_* paths (the clinic
    # carried over automatically from the old single-clinic deployment),
    # whose db/config/static files live outside DATA_ROOT and are not
    # this clinic's own private folder to delete.
    clinic_dir = os.path.abspath(os.path.join(DATA_ROOT, clinic["slug"]))
    data_root_abs = os.path.abspath(DATA_ROOT)
    if os.path.commonpath([clinic_dir, data_root_abs]) == data_root_abs and os.path.isdir(clinic_dir):
        shutil.rmtree(clinic_dir, ignore_errors=True)

    logger.info("Removed clinic id=%s slug=%s name=%r", clinic_id, clinic["slug"], clinic["name"])
    return clinic


def update_subscription(clinic_id: int, plan: str, started_at: Optional[str] = None):
    with _db() as conn:
        conn.execute(
            "UPDATE clinics SET subscription_plan=?, subscription_started_at=? WHERE id=?",
            (plan, started_at or datetime.now().strftime("%Y-%m-%d"), clinic_id),
        )


def migrate_legacy_single_clinic_if_needed():
    """One-time upgrade path for the clinic that was already live before
    this multi-clinic feature existed. If the clinics table is empty but
    the OLD single-clinic environment variables (META_ACCESS_TOKEN,
    META_PHONE_NUMBER_ID) are set, this creates clinic #1 from them,
    pointing at the exact same database file and clinic_config.yaml it
    already had -- so existing patients, appointments, and conversation
    history all carry over untouched, with nothing to re-enter by hand.

    Safe to call on every startup: it only does anything the very first
    time (once any clinic exists -- migrated or added via the admin page
    -- this is a no-op forever after).
    """
    init_control_db()
    if list_clinics():
        return

    legacy_token = os.environ.get("META_ACCESS_TOKEN")
    legacy_phone_id = os.environ.get("META_PHONE_NUMBER_ID")
    if not legacy_token or not legacy_phone_id:
        return  # fresh install, nothing to migrate

    legacy_waba_id = os.environ.get("META_WABA_ID", "")

    clinic_info: Dict[str, Any] = {}
    services: List[Dict[str, Any]] = []
    dentists: List[Dict[str, Any]] = []
    if os.path.exists(LEGACY_CONFIG_PATH):
        import yaml
        with open(LEGACY_CONFIG_PATH, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        clinic_info = raw.get("clinic", {}) or {}
        services = raw.get("services", []) or []
        dentists = raw.get("dentists", []) or []

    plan = os.environ.get("SUBSCRIPTION_PLAN", "12_months")
    started_at = os.environ.get("SUBSCRIPTION_STARTED_AT") or datetime.now().strftime("%Y-%m-%d")

    with _db() as conn:
        slug = _unique_slug(conn, slugify(clinic_info.get("name") or "clinic-1"))
        conn.execute(
            """
            INSERT INTO clinics (
                slug, name, active,
                whatsapp_access_token, whatsapp_phone_number_id, whatsapp_waba_id,
                general_hours, parking, phone, address, google_maps_link,
                offers_text, offers_image_filename,
                holidays_json, services_json, dentists_json,
                subscription_plan, subscription_started_at,
                db_path, config_path, static_dir, created_at
            ) VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                slug, clinic_info.get("name") or "Clinic 1",
                legacy_token, legacy_phone_id, legacy_waba_id,
                clinic_info.get("general_hours", ""), clinic_info.get("parking", ""),
                clinic_info.get("phone", ""), clinic_info.get("address", ""),
                clinic_info.get("google_maps_link", ""),
                clinic_info.get("offers_text", ""), clinic_info.get("offers_image_filename"),
                json.dumps(clinic_info.get("holidays", [])), json.dumps(services), json.dumps(dentists),
                plan, started_at,
                LEGACY_DB_PATH,      # keeps using the SAME db file as before -- no data migration needed
                LEGACY_CONFIG_PATH,  # keeps using the SAME yaml file as before -- still git-editable
                LEGACY_STATIC_DIR,   # keeps serving from the existing static/ folder
                datetime.now().isoformat(),
            ),
        )
    logger.info(
        "Migrated existing single-clinic deployment into the new multi-clinic store as slug=%s",
        slug,
    )
