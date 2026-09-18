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
worker_tasks: Dict[int, asyncio.Task] = {}


def launch_clinic_worker(clinic: dict) -> ClinicWorker:
    """Creates and starts a ClinicWorker for this clinic and registers it
    for webhook routing. Called once per active clinic at app startup, and
    once more, immediately, whenever a new clinic is added through the Add
    Clinic admin page -- that's what lets a new clinic go live in this
    same running process the moment its form is saved, with no restart."""
    worker = ClinicWorker(clinic)
    clinic_workers[clinic["id"]] = worker
    workers_by_phone_id[clinic["whatsapp_phone_number_id"]] = worker
    worker_tasks[clinic["id"]] = asyncio.create_task(worker.run())
    logger.info("Launched worker for clinic id=%s slug=%s (%s)", clinic["id"], clinic["slug"], clinic["name"])
    return worker


def get_worker_by_phone_number_id(phone_number_id: str) -> Optional[ClinicWorker]:
    return workers_by_phone_id.get(phone_number_id)


def stop_clinic_worker(clinic_id: int) -> bool:
    """Stops a running clinic's worker (if any) and unregisters it, so no
    further WhatsApp messages get routed to it and its MCP subprocess is
    shut down cleanly. Called from the admin page's Delete button, right
    before the clinic's row/data are removed from the store. Returns True
    if a worker was found and stopped, False if this clinic had no worker
    running (e.g. it was already inactive)."""
    worker = clinic_workers.pop(clinic_id, None)
    if worker is not None:
        workers_by_phone_id.pop(worker.clinic["whatsapp_phone_number_id"], None)

    task = worker_tasks.pop(clinic_id, None)
    if task is not None and not task.done():
        task.cancel()  # worker.run()'s `async with stdio_client(...)` cleans up the MCP subprocess on cancellation

    logger.info("Stopped worker for clinic id=%s", clinic_id)
    return worker is not None
