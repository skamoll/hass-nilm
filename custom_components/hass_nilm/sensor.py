"""Sensor platform for NILM Appliance Disaggregator."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import NilmLineCoordinator
from .engine import ApplianceStatus, LineFrame
from .entity import NilmApplianceEntity, NilmLineEntity


@dataclass(frozen=True, kw_only=True)
class LineSensorDescription(SensorEntityDescription):
    """Describe a SourceLine diagnostic sensor."""

    value_fn: Callable[[LineFrame], Any]


LINE_SENSORS: tuple[LineSensorDescription, ...] = (
    LineSensorDescription(
        key="residual_power",
        translation_key="residual_power",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda f: f.filtered_residual_w,
    ),
    LineSensorDescription(
        key="submeters_power",
        translation_key="submeters_power",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda f: f.submeters_power_w,
    ),
    LineSensorDescription(
        key="inferred_power",
        translation_key="inferred_power",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda f: f.total_inferred_power_w,
    ),
    LineSensorDescription(
        key="unexplained_power",
        translation_key="unexplained_power",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda f: f.unexplained_power_w,
    ),
)


class NilmLineDiagnosticSensor(NilmLineEntity, SensorEntity):
    """Diagnostic power sensor on the SourceLine device."""

    entity_description: LineSensorDescription

    def __init__(
        self, coordinator: NilmLineCoordinator, description: LineSensorDescription
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        frame = self.coordinator.data
        return None if frame is None else self.entity_description.value_fn(frame)


class NilmAppliancePowerSensor(NilmApplianceEntity, SensorEntity):
    """Conservation-bounded instantaneous power sensor for an appliance."""

    _attr_translation_key = "appliance_power"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfPower.WATT

    def __init__(
        self,
        coordinator: NilmLineCoordinator,
        appliance_id: str,
        appliance_name: str,
        archetype_label: str,
    ) -> None:
        super().__init__(coordinator, appliance_id, appliance_name, archetype_label, "power")

    @property
    def native_value(self) -> float | None:
        frame = self.coordinator.data
        if frame is None or self._appliance_id not in frame.appliances:
            return None
        return frame.appliances[self._appliance_id].attributed_power_w


class NilmApplianceEnergySensor(NilmApplianceEntity, RestoreSensor):
    """Cumulative kWh sensor (trapezoidal Riemann + RestoreSensor) ready for Energy Dashboard."""

    _attr_translation_key = "appliance_energy"
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR

    def __init__(
        self,
        coordinator: NilmLineCoordinator,
        appliance_id: str,
        appliance_name: str,
        archetype_label: str,
    ) -> None:
        super().__init__(coordinator, appliance_id, appliance_name, archetype_label, "energy")
        self._restored_kwh: float = 0.0

    async def async_added_to_hass(self) -> None:
        """Restore cumulative kWh across Home Assistant restarts."""
        await super().async_added_to_hass()
        last_data = await self.async_get_last_sensor_data()
        if last_data is not None and last_data.native_value is not None:
            try:
                val = float(last_data.native_value)
            except (TypeError, ValueError):
                val = 0.0
            if val > 0.0:
                self._restored_kwh = val
                self.coordinator.engine.restore_appliance_energy(self._appliance_id, val)

    @property
    def native_value(self) -> float:
        frame = self.coordinator.data
        if frame is None or self._appliance_id not in frame.appliances:
            return round(self._restored_kwh, 4)
        return frame.appliances[self._appliance_id].total_energy_kwh


class NilmAppliancePhaseSensor(NilmApplianceEntity, SensorEntity):
    """Current operational phase sensor for an appliance."""

    _attr_translation_key = "appliance_phase"

    def __init__(
        self,
        coordinator: NilmLineCoordinator,
        appliance_id: str,
        appliance_name: str,
        archetype_label: str,
    ) -> None:
        super().__init__(coordinator, appliance_id, appliance_name, archetype_label, "phase")

    def _status(self) -> ApplianceStatus | None:
        frame = self.coordinator.data
        if frame is None:
            return None
        return frame.appliances.get(self._appliance_id)

    @property
    def native_value(self) -> str:
        st = self._status()
        return "off" if st is None else st.phase

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        st = self._status()
        if st is None:
            return {}
        return {
            "archetype": st.archetype.value,
            "current_cycle_energy_kwh": st.current_cycle_energy_kwh,
            "current_cycle_duration_min": st.current_cycle_duration_min,
            "high_blocks_in_cycle": st.high_blocks_in_cycle,
        }


class NilmApplianceLastCycleSensor(NilmApplianceEntity, SensorEntity):
    """Summary sensor for the most recently completed appliance cycle."""

    _attr_translation_key = "appliance_last_cycle"

    def __init__(
        self,
        coordinator: NilmLineCoordinator,
        appliance_id: str,
        appliance_name: str,
        archetype_label: str,
    ) -> None:
        super().__init__(coordinator, appliance_id, appliance_name, archetype_label, "last_cycle")

    @property
    def native_value(self) -> str:
        frame = self.coordinator.data
        st = None if frame is None else frame.appliances.get(self._appliance_id)
        if st is None or st.last_cycle is None:
            return "none"
        return st.last_cycle.matched_program

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        frame = self.coordinator.data
        st = None if frame is None else frame.appliances.get(self._appliance_id)
        if st is None or st.last_cycle is None:
            return {}
        cyc = st.last_cycle
        return {
            "start_time": cyc.start_time.isoformat(),
            "end_time": cyc.end_time.isoformat(),
            "duration_min": cyc.duration_min,
            "energy_kwh": cyc.energy_kwh,
            "peak_power_w": cyc.peak_power_w,
            "high_blocks": cyc.high_blocks,
            "phases_seen": list(cyc.phases_seen),
            "confidence": cyc.confidence,
        }


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up SourceLine diagnostic sensors and per-appliance NILM sensors."""
    coord: NilmLineCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SensorEntity] = [NilmLineDiagnosticSensor(coord, desc) for desc in LINE_SENSORS]
    for aid, prof in coord.engine.profiles.items():
        arch = prof.archetype.value
        entities.extend(
            [
                NilmAppliancePowerSensor(coord, aid, prof.name, arch),
                NilmApplianceEnergySensor(coord, aid, prof.name, arch),
                NilmAppliancePhaseSensor(coord, aid, prof.name, arch),
                NilmApplianceLastCycleSensor(coord, aid, prof.name, arch),
            ]
        )
    async_add_entities(entities)
