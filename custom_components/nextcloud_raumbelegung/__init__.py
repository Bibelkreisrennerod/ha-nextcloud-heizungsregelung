"""Nextcloud Raumbelegung: kalenderbasierte, lernende Heizungssteuerung."""

from __future__ import annotations

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN, REMOVED_SETTINGS
from .controller import HeatingController
from .coordinator import RoomCoordinator, RuntimeData

PLATFORMS = [Platform.NUMBER, Platform.SWITCH, Platform.BINARY_SENSOR, Platform.SENSOR]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

SERVICE_CALIBRATE = "kalibrierlauf"
SERVICE_CALIBRATE_STOP = "kalibrierlauf_abbrechen"
SERVICE_LEARN_HISTORY = "aus_historie_lernen"
CALIBRATE_SCHEMA = vol.Schema(
    {
        vol.Required("raeume"): vol.All(cv.ensure_list, [cv.string]),
        vol.Optional("zieltemperatur"): vol.All(vol.Coerce(float), vol.Range(min=15, max=25)),
        vol.Optional("andere_raeume"): vol.All(vol.Coerce(float), vol.Range(min=5, max=20)),
        vol.Optional("hoechstens_stunden"): vol.All(vol.Coerce(float), vol.Range(min=1, max=12)),
    }
)


def _controller(hass: HomeAssistant) -> HeatingController:
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.state is ConfigEntryState.LOADED and entry.runtime_data.controller is not None:
            return entry.runtime_data.controller
    raise HomeAssistantError("Die Integration ist nicht geladen.")


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Dienste fuer Kalibrierlaeufe."""

    async def calibrate(call: ServiceCall) -> None:
        await _controller(hass).async_start_calibration(
            call.data["raeume"],
            call.data.get("zieltemperatur"),
            call.data.get("andere_raeume"),
            call.data.get("hoechstens_stunden"),
        )

    async def calibrate_stop(call: ServiceCall) -> None:
        await _controller(hass).async_stop_calibration("abgebrochen")

    hass.services.async_register(DOMAIN, SERVICE_CALIBRATE, calibrate, schema=CALIBRATE_SCHEMA)
    async def learn_history(call: ServiceCall) -> None:
        await _controller(hass).async_learn_from_history(reset=True)

    hass.services.async_register(DOMAIN, SERVICE_CALIBRATE_STOP, calibrate_stop)
    hass.services.async_register(DOMAIN, SERVICE_LEARN_HISTORY, learn_history)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Eintrag einrichten."""
    _remove_old_entities(hass, entry)
    coordinator = RoomCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    runtime = RuntimeData(coordinator=coordinator)
    entry.runtime_data = runtime
    # Erst die Entitaeten (sie stellen die gespeicherten Einstellungen wieder her),
    # dann die Steuerung starten.
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    controller = HeatingController(hass, entry, runtime)
    await controller.async_start()
    runtime.controller = controller
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


def _remove_old_entities(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Entitaeten frueherer Versionen entfernen (Lastgewicht, Lastbudget, Vorlauf-Schwelle)."""
    registry = er.async_get(hass)
    for item in er.async_entries_for_config_entry(registry, entry.entry_id):
        if any(item.unique_id.endswith(f"_{key}") for key in REMOVED_SETTINGS):
            registry.async_remove(item.entity_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Eintrag entladen."""
    controller = entry.runtime_data.controller
    if controller is not None:
        await controller.async_stop()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
