"""Diagnostics support for NILM Appliance Disaggregator."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .coordinator import NilmLineCoordinator

TO_REDACT = {"unique_id"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coord: NilmLineCoordinator = hass.data[DOMAIN][entry.entry_id]
    frame = coord.data
    return {
        "entry": async_redact_data(entry.as_dict(), TO_REDACT),
        "line_config": asdict(coord.engine.config),
        "profiles": {aid: asdict(prof) for aid, prof in coord.engine.profiles.items()},
        "latest_frame": None if frame is None else asdict(frame),
    }
