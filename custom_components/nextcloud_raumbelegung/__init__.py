"""Nextcloud Raumbelegung: kalenderbasierte, lernende Heizungssteuerung."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .controller import HeatingController
from .coordinator import RoomCoordinator, RuntimeData

PLATFORMS = [Platform.NUMBER, Platform.SWITCH, Platform.BINARY_SENSOR, Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Eintrag einrichten."""
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


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Eintrag entladen."""
    controller = entry.runtime_data.controller
    if controller is not None:
        await controller.async_stop()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
