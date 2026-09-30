"""Entidad binaria con el estado de la pantalla del PC."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import ScreenStateConfigEntry, ScreenStateCoordinator
from .const import DOMAIN


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ScreenStateConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Crea la entidad binary_sensor.<equipo>_pantalla."""
    async_add_entities([ScreenStateBinarySensor(entry.runtime_data, entry)])


class ScreenStateBinarySensor(
    CoordinatorEntity[ScreenStateCoordinator], BinarySensorEntity
):
    """Estado de la pantalla: on = activa, off = apagada o suspendida."""

    _attr_has_entity_name = True
    _attr_name = "Pantalla"
    _attr_device_class = BinarySensorDeviceClass.RUNNING
    _attr_icon = "mdi:monitor"

    def __init__(
        self, coordinator: ScreenStateCoordinator, entry: ScreenStateConfigEntry
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry

    @property
    def unique_id(self) -> str:
        return f"{self._entry.unique_id}_pantalla"

    @property
    def device_info(self) -> DeviceInfo:
        data: dict[str, Any] = self.coordinator.data or {}
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.unique_id)},
            name=self._entry.title,
            manufacturer="pc-screen-agent",
            model=data.get("hostname") or "PC",
            sw_version=data.get("version"),
            configuration_url=self.coordinator.base_url,
        )

    @property
    def is_on(self) -> bool:
        return bool((self.coordinator.data or {}).get("active"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data: dict[str, Any] = self.coordinator.data or {}
        return {
            "suspended": data.get("suspended"),
            "since": data.get("since"),
            "reason": data.get("reason"),
            "hostname": data.get("hostname"),
            "host_slug": data.get("host_slug"),
            "uptime_s": data.get("uptime_s"),
        }