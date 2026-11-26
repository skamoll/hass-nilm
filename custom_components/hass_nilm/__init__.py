"""The NILM Appliance Disaggregator integration."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import ServiceValidationError, Unauthorized, UnknownUser
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.service import async_register_admin_service
from homeassistant.util import slugify

from .const import (
    CONF_APPLIANCE_ID,
    CONF_APPLIANCE_NAME,
    CONF_APPLIANCES,
    CONF_ARCHETYPE,
    CONF_DEDICATED_METER,
    CONF_EDGE_TOLERANCE_W,
    CONF_END_IDLE_HOLD_S,
    CONF_HIGH_BLOCK_MIN_S,
    CONF_HIGH_BLOCK_THRESHOLD_W,
    CONF_IDLE_THRESHOLD_W,
    CONF_MAX_ON_S,
    CONF_MAX_PAUSE_S,
    CONF_MAX_POWER_W,
    CONF_MIN_CYCLE_ENERGY_KWH,
    CONF_MIN_HIGH_BLOCKS,
    CONF_MIN_ON_S,
    CONF_MIN_POWER_W,
    CONF_NOMINAL_POWER_W,
    CONF_PHASES,
    CONF_PRIORITY,
    CONF_PROGRAMS,
    DOMAIN,
    LINE_MANUFACTURER,
    LINE_MODEL,
)
from .coordinator import NilmLineCoordinator, dict_to_appliance_profile
from .engine import Archetype
from .recorder_scanner import async_scan_entity_history

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
]


async def _async_ensure_admin(hass: HomeAssistant, call: ServiceCall) -> None:
    """Reject non-admin users while allowing system/automation calls without user_id."""
    user_id = call.context.user_id if call.context else None
    if user_id:
        user = await hass.auth.async_get_user(user_id)
        if user is None:
            raise UnknownUser(context=call.context, user_id=user_id)
        if not user.is_admin:
            raise Unauthorized(context=call.context, user_id=user_id)


def _resolve_entry(hass: HomeAssistant, call: ServiceCall) -> ConfigEntry:
    entries: dict[str, NilmLineCoordinator] = hass.data.get(DOMAIN, {})
    entry_id = call.data.get("config_entry_id")
    if entry_id:
        if entry_id in entries:
            return entries[entry_id].entry
        raise ServiceValidationError(f"hass_nilm entry {entry_id} is not loaded")
    if len(entries) == 1:
        return next(iter(entries.values())).entry
    raise ServiceValidationError("Specify config_entry_id when multiple NILM lines exist")


def _register_services(hass: HomeAssistant) -> None:
    if hass.data.get(f"{DOMAIN}_services_registered"):
        return

    async def handle_scan_recorder(call: ServiceCall) -> dict[str, Any]:
        await _async_ensure_admin(hass, call)
        return await async_scan_entity_history(
            hass,
            str(call.data["entity_id"]),
            days=int(call.data.get("days", 14)),
            idle_threshold_w=float(call.data.get("idle_threshold_w", 30.0)),
            min_step_w=float(call.data.get("min_step_w", 120.0)),
            merge_gap_s=float(call.data.get("merge_gap_s", 900.0)),
            high_power_threshold_w=float(call.data.get("high_power_threshold_w", 1000.0)),
            min_cycle_energy_kwh=float(call.data.get("min_cycle_energy_kwh", 0.05)),
        )

    async def handle_upsert_appliance(call: ServiceCall) -> None:
        await _async_ensure_admin(hass, call)
        entry = _resolve_entry(hass, call)
        payload = dict(call.data)
        payload.pop("config_entry_id", None)
        aid = str(payload.get(CONF_APPLIANCE_ID) or slugify(str(payload[CONF_APPLIANCE_NAME])))
        payload[CONF_APPLIANCE_ID] = aid
        # Validate profile deserialization before persisting
        try:
            prof = dict_to_appliance_profile(payload)
        except (KeyError, TypeError, ValueError) as err:
            raise ServiceValidationError(f"Invalid appliance profile: {err}") from err
        if prof.max_power_w < prof.min_power_w:
            raise ServiceValidationError("max_power_w must be >= min_power_w")

        apps = [
            a for a in entry.options.get(CONF_APPLIANCES, []) if a.get(CONF_APPLIANCE_ID) != aid
        ]
        apps.append(payload)
        hass.config_entries.async_update_entry(
            entry,
            options={**entry.options, CONF_APPLIANCES: apps},
        )

    async def handle_remove_appliance(call: ServiceCall) -> None:
        await _async_ensure_admin(hass, call)
        entry = _resolve_entry(hass, call)
        aid = str(call.data[CONF_APPLIANCE_ID])
        apps = [
            a for a in entry.options.get(CONF_APPLIANCES, []) if a.get(CONF_APPLIANCE_ID) != aid
        ]
        dev_reg = dr.async_get(hass)
        dev = dev_reg.async_get_device(identifiers={(DOMAIN, f"{entry.entry_id}_{aid}")})
        if dev is not None:
            dev_reg.async_remove_device(dev.id)
        hass.config_entries.async_update_entry(
            entry,
            options={**entry.options, CONF_APPLIANCES: apps},
        )

    async_register_admin_service(
        hass,
        DOMAIN,
        "scan_recorder",
        handle_scan_recorder,
        vol.Schema(
            {
                vol.Required("entity_id"): cv.entity_id,
                vol.Optional("days", default=14): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=90)
                ),
                vol.Optional("idle_threshold_w", default=30.0): vol.Coerce(float),
                vol.Optional("min_step_w", default=120.0): vol.Coerce(float),
                vol.Optional("merge_gap_s", default=900.0): vol.Coerce(float),
                vol.Optional("high_power_threshold_w", default=1000.0): vol.Coerce(float),
                vol.Optional("min_cycle_energy_kwh", default=0.05): vol.Coerce(float),
            }
        ),
        supports_response=SupportsResponse.ONLY,
    )

    async_register_admin_service(
        hass,
        DOMAIN,
        "upsert_appliance",
        handle_upsert_appliance,
        vol.Schema(
            {
                vol.Optional("config_entry_id"): cv.string,
                vol.Optional(CONF_APPLIANCE_ID): cv.string,
                vol.Required(CONF_APPLIANCE_NAME): cv.string,
                vol.Optional(CONF_ARCHETYPE, default=Archetype.STEP.value): vol.In(
                    [a.value for a in Archetype]
                ),
                vol.Required(CONF_MIN_POWER_W): vol.Coerce(float),
                vol.Required(CONF_MAX_POWER_W): vol.Coerce(float),
                vol.Optional(CONF_NOMINAL_POWER_W): vol.Coerce(float),
                vol.Optional(CONF_PRIORITY, default=100): vol.Coerce(int),
                vol.Optional(CONF_EDGE_TOLERANCE_W, default=80.0): vol.Coerce(float),
                vol.Optional(CONF_MIN_ON_S, default=15.0): vol.Coerce(float),
                vol.Optional(CONF_MAX_ON_S, default=14400.0): vol.Coerce(float),
                vol.Optional(CONF_IDLE_THRESHOLD_W, default=15.0): vol.Coerce(float),
                vol.Optional(CONF_MAX_PAUSE_S, default=900.0): vol.Coerce(float),
                vol.Optional(CONF_END_IDLE_HOLD_S, default=180.0): vol.Coerce(float),
                vol.Optional(CONF_HIGH_BLOCK_THRESHOLD_W, default=1000.0): vol.Coerce(float),
                vol.Optional(CONF_HIGH_BLOCK_MIN_S, default=60.0): vol.Coerce(float),
                vol.Optional(CONF_MIN_CYCLE_ENERGY_KWH, default=0.02): vol.Coerce(float),
                vol.Optional(CONF_MIN_HIGH_BLOCKS, default=0): vol.Coerce(int),
                vol.Optional(CONF_DEDICATED_METER, default=False): cv.boolean,
                vol.Optional(CONF_PHASES): list,
                vol.Optional(CONF_PROGRAMS): list,
            }
        ),
    )

    async_register_admin_service(
        hass,
        DOMAIN,
        "remove_appliance",
        handle_remove_appliance,
        vol.Schema(
            {
                vol.Optional("config_entry_id"): cv.string,
                vol.Required(CONF_APPLIANCE_ID): cv.string,
            }
        ),
    )

    hass.data[f"{DOMAIN}_services_registered"] = True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload integration entry when options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up NILM Appliance Disaggregator from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    _register_services(hass)

    coordinator = NilmLineCoordinator(hass, entry)
    # Register the SourceLine device first: appliance devices link to it by registry id.
    line_device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        name=coordinator.engine.config.name,
        manufacturer=LINE_MANUFACTURER,
        model=LINE_MODEL,
        entry_type=dr.DeviceEntryType.SERVICE,
    )
    coordinator.line_device_id = line_device.id
    await coordinator.async_config_entry_first_refresh()
    hass.data[DOMAIN][entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    coordinator.async_start_listeners()
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    coord: NilmLineCoordinator | None = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if coord is not None:
        coord.async_stop_listeners()

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok and DOMAIN in hass.data:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        if not hass.data[DOMAIN]:
            for svc in ("scan_recorder", "upsert_appliance", "remove_appliance"):
                hass.services.async_remove(DOMAIN, svc)
            hass.data.pop(f"{DOMAIN}_services_registered", None)
    return unload_ok
