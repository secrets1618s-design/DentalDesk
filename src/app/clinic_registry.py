"""
In-memory registry of the ClinicWorker for every clinic currently running
in this process, plus the one function that brings a NEW clinic online
without a restart, redeploy, or new Railway service. Kept in its own
module (rather than inside main.py or admin.py) so both can import it
without a circular import.
"""
import asyncio
import logging
from typing import Dict, Optional

from app.agent import ClinicWorker

logger = logging.getLogger(__name__)

clinic_workers: Dict[int, ClinicWorker] = {}
workers_by_phone_id: Dict[str, ClinicWorker] = {}


def launch_clinic_worker(clinic: dict) -> ClinicWorker:
    """Creates and starts a ClinicWorker for this clinic and registers it
    for webhook routing. Called once per active clinic at app startup, and
    once more, immediately, whenever a new clinic is added through the Add
    Clinic admin page -- that's what lets a new clinic go live in this
    same running process the moment its form is saved, with no restart."""
    worker = ClinicWorker(clinic)
    clinic_workers[clinic["id"]] = worker
    workers_by_phone_id[clinic["whatsapp_phone_number_id"]] = worker
    asyncio.create_task(worker.run())
    logger.info("Launched worker for clinic id=%s slug=%s (%s)", clinic["id"], clinic["slug"], clinic["name"])
    return worker


def get_worker_by_phone_number_id(phone_number_id: str) -> Optional[ClinicWorker]:
    return workers_by_phone_id.get(phone_number_id)
