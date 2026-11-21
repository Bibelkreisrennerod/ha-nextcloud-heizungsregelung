"""Einrichtung ueber die Oberflaeche."""

from __future__ import annotations

import logging
from typing import Any
import xml.etree.ElementTree as ET

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_PASSWORD, CONF_URL, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .caldav import NextcloudAuthError, NextcloudClient, NextcloudError
from .targets import RuleError, parse_rules
from .const import (
    CONF_CALENDARS,
    CONF_DUTY_CYCLE_SENSOR,
    CONF_STOKERCLOUD,
    CONF_TARGET_RULES,
    CONF_FLOW_SENSOR,
    CONF_OUTSIDE_SENSOR,
    CONF_WEATHER,
    DEFAULT_WEATHER,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


def _normalize_url(url: str) -> str:
    url = url.strip().rstrip("/")
    if "://" not in url:
        url = "https://" + url
    for suffix in ("/remote.php/dav", "/remote.php"):
        if url.endswith(suffix):
            url = url[: -len(suffix)]
    return url


class NextcloudRoomsConfigFlow(ConfigFlow, domain=DOMAIN):
    """Zugangsdaten abfragen, dann Kalender auswaehlen."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return NextcloudRoomsOptionsFlow()

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._calendars: list[dict[str, str]] = []

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            url = _normalize_url(user_input[CONF_URL])
            username = user_input[CONF_USERNAME].strip()
            client = NextcloudClient(
                async_get_clientsession(self.hass), url, username, user_input[CONF_PASSWORD]
            )
            try:
                self._calendars = await client.async_discover_calendars()
            except NextcloudAuthError:
                errors["base"] = "invalid_auth"
            except (NextcloudError, ET.ParseError):
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unerwarteter Fehler bei der Einrichtung")
                errors["base"] = "unknown"
            else:
                if not self._calendars:
                    errors["base"] = "no_calendars"
                else:
                    await self.async_set_unique_id(f"{url}|{username}".lower())
                    self._abort_if_unique_id_configured()
                    self._data = {
                        CONF_URL: url,
                        CONF_USERNAME: username,
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                    }
                    return await self.async_step_calendars()

        schema = vol.Schema(
            {
                vol.Required(CONF_URL): TextSelector(TextSelectorConfig(type=TextSelectorType.URL)),
                vol.Required(CONF_USERNAME): TextSelector(),
                vol.Required(CONF_PASSWORD): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.PASSWORD)
                ),
            }
        )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(schema, user_input or {}),
            errors=errors,
        )

    async def async_step_calendars(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            chosen = [cal for cal in self._calendars if cal["url"] in user_input[CONF_CALENDARS]]
            if chosen:
                self._data[CONF_CALENDARS] = chosen
                return self.async_create_entry(
                    title=f"Nextcloud ({self._data[CONF_USERNAME]})", data=self._data
                )
            errors["base"] = "no_selection"

        return self.async_show_form(step_id="calendars", data_schema=self._calendar_schema(), errors=errors)

    def _calendar_schema(self) -> vol.Schema:
        return vol.Schema(
            {
                vol.Required(CONF_CALENDARS): SelectSelector(
                    SelectSelectorConfig(
                        options=[
                            SelectOptionDict(value=cal["url"], label=cal["name"])
                            for cal in self._calendars
                        ],
                        multiple=True,
                        mode=SelectSelectorMode.LIST,
                    )
                )
            }
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Kalenderauswahl aendern, ohne den Eintrag (und das Gelernte) zu loeschen."""
        entry = self._get_reconfigure_entry()
        current = list(entry.data.get(CONF_CALENDARS, []))
        if not self._calendars:
            client = NextcloudClient(
                async_get_clientsession(self.hass),
                entry.data[CONF_URL],
                entry.data[CONF_USERNAME],
                entry.data[CONF_PASSWORD],
            )
            try:
                found = await client.async_discover_calendars()
            except NextcloudAuthError:
                return self.async_abort(reason="invalid_auth")
            except (NextcloudError, ET.ParseError):
                return self.async_abort(reason="cannot_connect")
            # bisher gewaehlte Kalender bleiben sichtbar, auch wenn sie nicht mehr angeboten werden
            urls = {cal["url"] for cal in found}
            self._calendars = found + [cal for cal in current if cal["url"] not in urls]
        errors: dict[str, str] = {}
        if user_input is not None:
            chosen = [cal for cal in self._calendars if cal["url"] in user_input[CONF_CALENDARS]]
            if chosen:
                return self.async_update_reload_and_abort(entry, data_updates={CONF_CALENDARS: chosen})
            errors["base"] = "no_selection"
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                self._calendar_schema(), {CONF_CALENDARS: [cal["url"] for cal in current]}
            ),
            errors=errors,
            description_placeholders={"user": entry.data[CONF_USERNAME]},
        )


class NextcloudRoomsOptionsFlow(OptionsFlow):
    """Messwerte fuer die Steuerung und Regeln fuer die Zieltemperatur je Termin."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        placeholders = {"line": ""}
        if user_input is not None:
            try:
                parse_rules(user_input.get(CONF_TARGET_RULES))
            except RuleError as err:
                errors[CONF_TARGET_RULES] = "invalid_rule"
                placeholders["line"] = f"{err.args[0]}: {err.args[1]} – {err.args[2]}"
            else:
                return self.async_create_entry(data=user_input)
        options = user_input if user_input is not None else self.config_entry.options
        sensor = EntitySelector(EntitySelectorConfig(domain="sensor"))
        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_WEATHER, default=options.get(CONF_WEATHER, DEFAULT_WEATHER)
                ): EntitySelector(EntitySelectorConfig(domain="weather")),
                vol.Optional(CONF_STOKERCLOUD): TextSelector(),
                vol.Optional(CONF_FLOW_SENSOR): sensor,
                vol.Optional(CONF_OUTSIDE_SENSOR): sensor,
                vol.Optional(CONF_DUTY_CYCLE_SENSOR): sensor,
                vol.Optional(CONF_TARGET_RULES): TextSelector(TextSelectorConfig(multiline=True)),
            }
        )
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(schema, options),
            errors=errors,
            description_placeholders=placeholders,
        )
