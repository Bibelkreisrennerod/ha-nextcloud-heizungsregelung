"""Gemeinsame Basis: ein Geraet je Raum und ein Geraet fuer die Anlage."""

from __future__ import annotations

from collections.abc import Callable, Iterable

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import slugify

from .const import DOMAIN
from .explanations import Explained


def room_unique_id(entry: ConfigEntry, room: str | None, key: str) -> str:
    """Eindeutige Kennung einer Entitaet (room=None: Anlage)."""
    if room is None:
        return f"{entry.entry_id}_global_{key}"
    return f"{entry.entry_id}_{slugify(room)}_{key}"


def room_device_info(entry: ConfigEntry, room: str | None) -> DeviceInfo:
    """Geraet eines Raums oder der Anlage (room=None)."""
    if room is None:
        return DeviceInfo(
            identifiers={(DOMAIN, f"{entry.entry_id}_global")},
            name="Heizungssteuerung",
            manufacturer="Nextcloud Raumbelegung",
            model="Steuerung",
            entry_type=DeviceEntryType.SERVICE,
        )
    return DeviceInfo(
        identifiers={(DOMAIN, f"{entry.entry_id}_{slugify(room)}")},
        name=room,
        manufacturer="Nextcloud",
        model="Raum",
        entry_type=DeviceEntryType.SERVICE,
    )


def add_room_entities(
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
    factory: Callable[[str], Iterable[Entity]],
    global_entities: Iterable[Entity] = (),
) -> None:
    """Entitaeten fuer alle bekannten und spaeter gefundenen Raeume anlegen."""
    runtime = entry.runtime_data
    known: set[str] = set()
    first = list(global_entities)

    @callback
    def _add_new_rooms() -> None:
        new = [room for room in runtime.coordinator.data.rooms if room not in known]
        entities: list[Entity] = []
        if first:
            entities.extend(first)
            first.clear()
        for room in new:
            known.add(room)
            entities.extend(factory(room))
        if entities:
            async_add_entities(entities)

    _add_new_rooms()
    entry.async_on_unload(runtime.coordinator.async_add_listener(_add_new_rooms))


class StatusEntity(Explained, Entity):
    """Entitaet, die sich nach jedem Steuerungstakt aktualisiert."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry, room: str | None, key: str) -> None:
        self._entry = entry
        self._room = room
        self._attr_translation_key = key if room is not None or key.startswith("global_") else f"global_{key}"
        self._attr_unique_id = room_unique_id(entry, room, key)
        self._attr_device_info = room_device_info(entry, room)

    @property
    def _controller(self):
        return self._entry.runtime_data.controller

    @property
    def _status(self) -> dict:
        controller = self._controller
        if controller is None:
            return {}
        if self._room is None:
            return controller.status
        state = controller.rooms.get(self._room)
        return state.status if state else {}

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        from .controller import signal_status

        self.async_on_remove(
            async_dispatcher_connect(self.hass, signal_status(self._entry), self.async_write_ha_state)
        )
