"""Binary sensor platform for NILM Appliance Disaggregator."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import NilmLineCoordinator
from .entity import NilmApplianceEntity, NilmLineEntity


class NilmSkewGuardBinarySensor(NilmLineEntity, BinarySensorEntity):
    """Diagnostic binary sensor indicating active parent-child skew protection."""

    _attr_translation_key = "skew_guard_active"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: NilmLineCoordinator) -> None:
        super().__init__(coordinator, "skew_guard_active")

    @property
    def is_on(self) -> bool | None:
        frame = self.coordinator.data
        return None if frame is None else frame.skew_guard_active


class NilmApplianceRunningBinarySensor(NilmApplianceEntity, BinarySensorEntity):
    """Binary sensor indicating whether an appliance cycle/session is active."""

    _attr_translation_key = "appliance_running"
    _attr_device_class = BinarySensorDeviceClass.RUNNING

    def __init__(
        self,
        coordinator: NilmLineCoordinator,
        appliance_id: str,
        appliance_name: str,
        archetype_label: str,
    ) -> None:
        super().__init__(coordinator, appliance_id, appliance_name, archetype_label, "running")

    @property
    def is_on(self) -> bool | None:
        frame = self.coordinator.data
        if frame is None or self._appliance_id not in frame.appliances:
            return False
        return frame.appliances[self._appliance_id].is_running


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up SourceLine and Appliance binary sensors."""
    coord: NilmLineCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[BinarySensorEntity] = [NilmSkewGuardBinarySensor(coord)]
    for aid, prof in coord.engine.profiles.items():
        entities.append(
            NilmApplianceRunningBinarySensor(coord, aid, prof.name, prof.archetype.value)
        )
    async_add_entities(entities)
