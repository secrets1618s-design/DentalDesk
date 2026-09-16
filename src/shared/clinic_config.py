"""
Loads the clinic's editable settings from config/clinic_config.yaml —
services & prices, general hours, parking, phone, holidays, and the list
of dentists.

Non-developers edit config/clinic_config.yaml directly to change any of
this; this module just reads that file so the rest of the app can use it.
The file is re-read fresh each time it's requested (see note on caching
below), so edits take effect the next time the app is started.
"""
import os
import logging
from typing import Any, Dict, List

import yaml

logger = logging.getLogger(__name__)

CONFIG_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "config", "clinic_config.yaml"
)

# Cached after the first read for this run of the app. Restart the app
# (or the MCP server) to pick up edits made to clinic_config.yaml.
_cached_config: Dict[str, Any] | None = None


def load_clinic_config() -> Dict[str, Any]:
    """Reads (and caches) config/clinic_config.yaml."""
    global _cached_config
    if _cached_config is None:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            _cached_config = yaml.safe_load(f) or {}
        logger.info("Loaded clinic config from %s", CONFIG_PATH)
    return _cached_config


def get_clinic_info() -> Dict[str, Any]:
    """Clinic-level info: name, general_hours, parking, phone, holidays."""
    return load_clinic_config().get("clinic", {})


def get_services() -> List[Dict[str, Any]]:
    """The list of services and prices."""
    return load_clinic_config().get("services", [])


def get_dentists_config() -> List[Dict[str, Any]]:
    """The list of dentists defined in the config file."""
    return load_clinic_config().get("dentists", [])
