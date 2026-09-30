"""Screen State — entidad de estado de pantalla de un PC, alimentada por pc-screen-agent.

La integración no ejecuta acciones: sólo publica el estado como entidad para que
las automatizaciones de Home Assistant lo usen con sus propias condiciones.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
import json
import logging
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_HOST,
    CONF_PORT,
    CONF_TOKEN,
    DOMAIN,
    PLATFORMS,
    SCAN_INTERVAL_SECONDS,
)

_LOGGER = logging.getLogger(__name__)


class ScreenStateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinador: sondea /state y escucha el stream SSE /events."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.entry = entry
        self.host: str = entry.data[CONF_HOST]
        self.port: int = int(entry.data[CONF_PORT])
        self.token: str = entry.data.get(CONF_TOKEN, "") or ""
        self._stream_task: asyncio.Task | None = None
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}-{entry.data.get('slug', entry.entry_id)}",
            update_interval=timedelta(seconds=SCAN_INTERVAL_SECONDS),
        )

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    async def _async_update_data(self) -> dict[str, Any]:
        """Respaldo por sondeo (además del stream SSE)."""
        session = async_get_clientsession(self.hass)
        try:
            async with session.get(
                f"{self.base_url}/state",
                headers=self.headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as response:
                response.raise_for_status()
                data = await response.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError) as err:
            raise UpdateFailed(f"No se pudo consultar el agente: {err}") from err

        if not isinstance(data, dict) or "active" not in data:
            raise UpdateFailed("Respuesta inesperada del agente")
        return data

    async def stream_events(self) -> None:
        """Consume /events (SSE) y actualiza la entidad en cada cambio."""
        session = async_get_clientsession(self.hass)
        while True:
            try:
                timeout = aiohttp.ClientTimeout(total=None, sock_connect=10)
                async with session.get(
                    f"{self.base_url}/events", headers=self.headers, timeout=timeout
                ) as response:
                    response.raise_for_status()
                    async for line in response.content:
                        if not line.startswith(b"data:"):
                            continue
                        try:
                            payload = json.loads(line[5:].decode().strip())
                        except (ValueError, UnicodeDecodeError):
                            continue
                        if isinstance(payload, dict) and "active" in payload:
                            self.async_set_updated_data(payload)
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 - reconexión automática
                _LOGGER.debug("Stream SSE del agente interrumpido: %s", err)
                await asyncio.sleep(10)

    def start_stream(self) -> None:
        self._stream_task = self.hass.async_create_background_task(
            self.stream_events(), f"{DOMAIN}_stream_{self.entry.entry_id}"
        )

    def stop_stream(self) -> None:
        if self._stream_task and not self._stream_task.done():
            self._stream_task.cancel()
        self._stream_task = None


type ScreenStateConfigEntry = ConfigEntry[ScreenStateCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: ScreenStateConfigEntry) -> bool:
    """Configura un PC desde un config entry."""
    coordinator = ScreenStateCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    coordinator.start_stream()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ScreenStateConfigEntry) -> bool:
    """Descarga la integración."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    coordinator: ScreenStateCoordinator | None = entry.runtime_data
    if coordinator:
        coordinator.stop_stream()
    return unloaded