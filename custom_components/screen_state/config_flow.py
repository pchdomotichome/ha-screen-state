"""Flujo de configuración de Screen State."""

from __future__ import annotations

from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import CONF_HOST, CONF_PORT, CONF_SLUG, CONF_TOKEN, DEFAULT_PORT, DOMAIN


def _clean(value: Any) -> str:
    return str(value or "").strip()


class ScreenStateConfigFlow(ConfigFlow, domain=DOMAIN):
    """Configura un PC con pc-screen-agent."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            slug = _clean(user_input.get(CONF_SLUG)).lower().replace(" ", "_")
            host = _clean(user_input.get(CONF_HOST))
            port = int(user_input.get(CONF_PORT) or DEFAULT_PORT)
            token = _clean(user_input.get(CONF_TOKEN))

            if not slug or not host:
                errors["base"] = "invalid_input"
            else:
                await self.async_set_unique_id(slug)
                self._abort_if_unique_id_configured()

                if await self._can_reach(self.hass, host, port, token):
                    title = slug.replace("_", " ").title()
                    return self.async_create_entry(title=title, data={
                        CONF_SLUG: slug,
                        CONF_HOST: host,
                        CONF_PORT: port,
                        CONF_TOKEN: token,
                    })
                errors["base"] = "cannot_connect"

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required(CONF_SLUG): str,
                vol.Required(CONF_HOST): str,
                vol.Optional(CONF_PORT, default=DEFAULT_PORT): int,
                vol.Optional(CONF_TOKEN, default=""): str,
            }),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Cambiar host/puerto/token de un equipo ya configurado."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            host = _clean(user_input.get(CONF_HOST)) or entry.data[CONF_HOST]
            port = int(user_input.get(CONF_PORT) or entry.data[CONF_PORT])
            token = _clean(user_input.get(CONF_TOKEN))

            if await self._can_reach(self.hass, host, port, token):
                return self.async_update_reload_and_abort(
                    entry,
                    data={
                        CONF_SLUG: entry.data[CONF_SLUG],
                        CONF_HOST: host,
                        CONF_PORT: port,
                        CONF_TOKEN: token,
                    },
                )
            errors["base"] = "cannot_connect"

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema({
                vol.Required(CONF_HOST, default=entry.data[CONF_HOST]): str,
                vol.Required(CONF_PORT, default=entry.data[CONF_PORT]): int,
                vol.Optional(CONF_TOKEN, default=entry.data.get(CONF_TOKEN, "")): str,
            }),
            errors=errors,
        )

    @staticmethod
    async def _can_reach(hass, host: str, port: int, token: str) -> bool:
        """Comprueba que el agente responde en /health."""
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        url = f"http://{host}:{port}/health"
        try:
            session = async_get_clientsession(hass)
            async with session.get(
                url, headers=headers, timeout=aiohttp.ClientTimeout(total=8)
            ) as response:
                if response.status != 200:
                    return False
                data = await response.json()
                return bool(data.get("ok"))
        except (aiohttp.ClientError, TimeoutError, ValueError):
            return False