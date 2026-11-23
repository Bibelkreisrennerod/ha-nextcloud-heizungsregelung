"""Nextcloud Raumbelegung: kalenderbasierte, lernende Heizungssteuerung."""

from __future__ import annotations

import re

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util

from .const import DOMAIN, REMOVED_SETTINGS
from .controller import HeatingController
from .coordinator import RoomCoordinator, RuntimeData

PLATFORMS = [Platform.NUMBER, Platform.SWITCH, Platform.BINARY_SENSOR, Platform.SENSOR]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

SERVICE_CALIBRATE = "kalibrierlauf"
SERVICE_CALIBRATE_STOP = "kalibrierlauf_abbrechen"
SERVICE_LEARN_HISTORY = "aus_historie_lernen"
SERVICE_OFFSET_LOG = "offset_nachtragen"
OFFSET_LOG_SCHEMA = vol.Schema(
    {
        vol.Required("raum"): cv.string,
        vol.Required("verlauf"): vol.All(
            cv.ensure_list,
            [
                vol.Schema(
                    {
                        vol.Required("ab"): cv.datetime,
                        vol.Required("offset"): vol.All(vol.Coerce(float), vol.Range(min=-3.5, max=3.5)),
                    }
                )
            ],
        ),
    }
)
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

    async def offset_log(call: ServiceCall) -> None:
        local = dt_util.get_default_time_zone()
        entries = [
            (item["ab"] if item["ab"].tzinfo else item["ab"].replace(tzinfo=local), item["offset"])
            for item in call.data["verlauf"]
        ]
        _controller(hass).set_offset_log(call.data["raum"], entries)

    hass.services.async_register(DOMAIN, SERVICE_OFFSET_LOG, offset_log, schema=OFFSET_LOG_SCHEMA)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Eintrag einrichten."""
    _remove_old_entities(hass, entry)
    _tidy_entity_ids(hass, entry)
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


_DOUBLED = re.compile(r"^(?P<room>.+?)_(?P=room)_(?P<rest>.+)$")


def _tidy_entity_id(entity_id: str) -> str | None:
    """Neue Entitaets-ID fuer frueher doppelt benannte Entitaeten, sonst None.

    sensor.saal_saal_warm_um_prognose -> sensor.saal_warm_um_prognose
    sensor.heizungssteuerung_kessel_vorlauf -> sensor.kessel_temperatur (Geraet "Kessel")
    """
    domain, _, object_id = entity_id.partition(".")
    if object_id.startswith("heizungssteuerung_kessel_"):
        rest = object_id.removeprefix("heizungssteuerung_kessel_")
        return f"{domain}.kessel_{'temperatur' if rest == 'vorlauf' else rest}"
    match = _DOUBLED.match(object_id)
    if match:
        return f"{domain}.{match.group('room')}_{match.group('rest')}"
    return None


def _tidy_entity_ids(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Doppelte Raumnamen und alte Kessel-Namen aus den Entitaets-IDs entfernen.

    Der Recorder uebernimmt den Verlauf zur neuen ID. Ist die neue ID schon
    vergeben, bleibt die alte.
    """
    registry = er.async_get(hass)
    for item in list(er.async_entries_for_config_entry(registry, entry.entry_id)):
        new_id = _tidy_entity_id(item.entity_id)
        if new_id is None or new_id == item.entity_id or registry.async_get(new_id) is not None:
            continue
        if hass.states.get(new_id) is not None:
            continue
        registry.async_update_entity(item.entity_id, new_entity_id=new_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Eintrag entladen."""
    controller = entry.runtime_data.controller
    if controller is not None:
        await controller.async_stop()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
