"""Schalter "Automatik" je Raum und fuer die ganze Anlage."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_OFF
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .entity import add_room_entities, room_device_info, room_unique_id


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    add_room_entities(
        entry,
        async_add_entities,
        lambda room: [AutomaticSwitch(entry, room)],
        [AutomaticSwitch(entry, None)],
    )


class AutomaticSwitch(SwitchEntity, RestoreEntity):
    """Aus: die Steuerung laesst die Thermostate in Ruhe."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_icon = "mdi:radiator"

    def __init__(self, entry: ConfigEntry, room: str | None) -> None:
        self._entry = entry
        self._room = room
        self._attr_translation_key = "automatic" if room is not None else "global_automatic"
        self._attr_unique_id = room_unique_id(entry, room, "automatic")
        self._attr_device_info = room_device_info(entry, room)
        self._attr_is_on = True

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None:
            self._attr_is_on = last.state != STATE_OFF
        self._publish()

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)

    async def _set(self, value: bool) -> None:
        self._attr_is_on = value
        self._publish()
        self.async_write_ha_state()
        controller = self._entry.runtime_data.controller
        if controller is not None:
            controller.request_update()

    def _publish(self) -> None:
        self._entry.runtime_data.settings[(self._room, "automatic")] = 1.0 if self._attr_is_on else 0.0
