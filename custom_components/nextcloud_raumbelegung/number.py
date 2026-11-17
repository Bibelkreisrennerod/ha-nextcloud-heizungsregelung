"""Vorheizzeit je Raum."""

from __future__ import annotations

from homeassistant.components.number import NumberDeviceClass, NumberMode, RestoreNumber
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DEFAULT_PREHEAT_MINUTES
from .coordinator import RuntimeData
from .entity import room_device_info, room_unique_id, signal_preheat


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Zahlen-Entitaeten anlegen, auch fuer spaeter gefundene Raeume."""
    runtime: RuntimeData = entry.runtime_data
    known: set[str] = set()

    @callback
    def _add_new_rooms() -> None:
        new = [room for room in runtime.coordinator.data.rooms if room not in known]
        if new:
            known.update(new)
            async_add_entities(PreheatNumber(entry, room) for room in new)

    _add_new_rooms()
    entry.async_on_unload(runtime.coordinator.async_add_listener(_add_new_rooms))


class PreheatNumber(RestoreNumber):
    """Minuten, die vor Terminbeginn schon geheizt wird."""

    _attr_has_entity_name = True
    _attr_translation_key = "preheat"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_device_class = NumberDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_native_min_value = 0
    _attr_native_max_value = 240
    _attr_native_step = 5
    _attr_mode = NumberMode.BOX
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry, room: str) -> None:
        self._entry = entry
        self._room = room
        self._attr_unique_id = room_unique_id(entry, room, "preheat")
        self._attr_device_info = room_device_info(entry, room)
        self._attr_native_value = DEFAULT_PREHEAT_MINUTES

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_number_data()
        if last is not None and last.native_value is not None:
            self._attr_native_value = last.native_value
        self._publish()

    async def async_set_native_value(self, value: float) -> None:
        self._attr_native_value = value
        self._publish()
        self.async_write_ha_state()

    def _publish(self) -> None:
        runtime: RuntimeData = self._entry.runtime_data
        runtime.preheat[self._room] = float(self._attr_native_value)
        async_dispatcher_send(self.hass, signal_preheat(self._entry))
