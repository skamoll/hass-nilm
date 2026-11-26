"""Repairs flow: "which appliance is this pattern?"."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult

from .const import DOMAIN


class PatternNamingFlow(RepairsFlow):
    """Ask the user to name (or ignore) a discovered load pattern."""

    def __init__(self, entry_id: str, pattern_id: str) -> None:
        self._entry_id = entry_id
        self._pattern_id = pattern_id

    def _manager(self):
        coord = self.hass.data.get(DOMAIN, {}).get(self._entry_id)
        return getattr(coord, "patterns", None)

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        return self.async_show_menu(step_id="init", menu_options=["name_it", "ignore"])

    async def async_step_name_it(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            name = str(user_input.get("name", "")).strip()
            manager = self._manager()
            if not name:
                errors["name"] = "name_required"
            elif manager is None:
                return self.async_abort(reason="line_not_loaded")
            else:
                await manager.async_resolve(self._pattern_id, name)
                return self.async_create_entry(data={})
        return self.async_show_form(
            step_id="name_it",
            data_schema=vol.Schema({vol.Required("name"): str}),
            errors=errors,
        )

    async def async_step_ignore(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        manager = self._manager()
        if manager is None:
            return self.async_abort(reason="line_not_loaded")
        await manager.async_resolve(self._pattern_id, None)
        return self.async_create_entry(data={})


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, str | int | float | None] | None,
) -> RepairsFlow:
    """Create the fix flow for a pattern issue."""
    data = data or {}
    return PatternNamingFlow(str(data.get("entry_id")), str(data.get("pattern_id")))
