"""Binaersensoren je Raum: Heizbedarf und Belegt."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .caldav import RoomEvent
from .const import DEFAULT_PREHEAT_MINUTES
from .coordinator import RoomCoordinator, RuntimeData
from .entity import room_device_info, room_unique_id, signal_preheat


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Sensoren anlegen, auch fuer spaeter gefundene Raeume."""
    runtime: RuntimeData = entry.runtime_data
    known: set[str] = set()

    @callback
    def _add_new_rooms() -> None:
        new = [room for room in runtime.coordinator.data.rooms if room not in known]
        if new:
            known.update(new)
            entities: list[BinarySensorEntity] = []
            for room in new:
                entities.append(RoomSensor(entry, room, use_preheat=True))
                entities.append(RoomSensor(entry, room, use_preheat=False))
            async_add_entities(entities)

    _add_new_rooms()
    entry.async_on_unload(runtime.coordinator.async_add_listener(_add_new_rooms))


class RoomSensor(CoordinatorEntity[RoomCoordinator], BinarySensorEntity):
    """An, solange ein Termin im Raum laeuft (Heizbedarf: schon ab Vorheizzeit).

    Ganztaegige Termine werden nicht beruecksichtigt. Faellt der Abruf aus,
    wird der Sensor "nicht verfuegbar" statt an oder aus zu bleiben.
    """

    _attr_has_entity_name = True

    def __init__(self, entry: ConfigEntry, room: str, use_preheat: bool) -> None:
        runtime: RuntimeData = entry.runtime_data
        super().__init__(runtime.coordinator)
        self._entry = entry
        self._room = room
        self._use_preheat = use_preheat
        key = "heating_demand" if use_preheat else "occupied"
        self._attr_translation_key = key
        self._attr_unique_id = room_unique_id(entry, room, key)
        self._attr_device_info = room_device_info(entry, room)
        self._attr_device_class = (
            BinarySensorDeviceClass.HEAT if use_preheat else BinarySensorDeviceClass.OCCUPANCY
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        @callback
        def _refresh(_now: datetime | None = None) -> None:
            self.async_write_ha_state()

        # Der Zustand haengt von der Uhrzeit ab, nicht nur vom Abruf.
        self.async_on_remove(async_track_time_interval(self.hass, _refresh, timedelta(minutes=1)))
        self.async_on_remove(
            async_dispatcher_connect(self.hass, signal_preheat(self._entry), _refresh)
        )

    def _lead(self) -> timedelta:
        if not self._use_preheat:
            return timedelta(0)
        runtime: RuntimeData = self._entry.runtime_data
        return timedelta(minutes=runtime.preheat.get(self._room, DEFAULT_PREHEAT_MINUTES))

    def _room_events(self) -> list[RoomEvent]:
        now = dt_util.utcnow()
        return [
            event
            for event in self.coordinator.data.events
            if self._room in event.rooms and not event.all_day and event.end > now
        ]

    @property
    def is_on(self) -> bool:
        now = dt_util.utcnow()
        lead = self._lead()
        return any(event.start - lead <= now for event in self._room_events())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        events = self._room_events()
        if not events:
            return {"event": None, "start": None, "end": None, "calendar": None}
        event = events[0]
        return {
            "event": event.summary,
            "start": dt_util.as_local(event.start).isoformat(),
            "end": dt_util.as_local(event.end).isoformat(),
            "calendar": event.calendar,
        }
