"""Base entity classes and DeviceInfo builders for NILM SourceLines and Appliances."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, LINE_MANUFACTURER, LINE_MODEL
from .coordinator import NilmLineCoordinator


class NilmLineEntity(CoordinatorEntity[NilmLineCoordinator]):
    """Entity attached to the parent NILM SourceLine device."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: NilmLineCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_line_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.entry.entry_id)},
            name=coordinator.engine.config.name,
            manufacturer=LINE_MANUFACTURER,
            model=LINE_MODEL,
            entry_type=DeviceEntryType.SERVICE,
        )


class NilmApplianceEntity(CoordinatorEntity[NilmLineCoordinator]):
    """Entity attached to an inferred/monitored Appliance sub-device."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: NilmLineCoordinator,
        appliance_id: str,
        appliance_name: str,
        archetype_label: str,
        key: str,
    ) -> None:
        super().__init__(coordinator)
        self._appliance_id = appliance_id
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{appliance_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{coordinator.entry.entry_id}_{appliance_id}")},
            via_device_id=coordinator.line_device_id,
            name=appliance_name,
            manufacturer="Hass-NILM",
            model=f"NILM Detector ({archetype_label})",
            entry_type=DeviceEntryType.SERVICE,
        )
