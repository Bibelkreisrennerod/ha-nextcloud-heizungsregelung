"""Regelmaessiger Abruf der Termine aus Nextcloud."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_URL, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .caldav import NextcloudClient, NextcloudError, RoomEvent
from .const import CONF_CALENDARS, DAYS_AHEAD, DAYS_BACK, DOMAIN, UPDATE_INTERVAL

_LOGGER = logging.getLogger(__name__)


@dataclass
class RoomData:
    """Ergebnis eines Abrufs."""

    events: list[RoomEvent] = field(default_factory=list)
    rooms: list[str] = field(default_factory=list)


@dataclass
class RuntimeData:
    """Laufzeitdaten eines Eintrags."""

    coordinator: RoomCoordinator
    # Einstellungen aus den Zahlen- und Schalter-Entitaeten:
    # (Raum oder None fuer global, Schluessel) -> Wert
    settings: dict[tuple[str | None, str], float] = field(default_factory=dict)
    controller: Any = None  # HeatingController, gesetzt in __init__.py


class RoomCoordinator(DataUpdateCoordinator[RoomData]):
    """Holt alle fuenf Minuten die Termine der gewaehlten Kalender."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass, _LOGGER, config_entry=entry, name=DOMAIN, update_interval=UPDATE_INTERVAL
        )
        self._client = NextcloudClient(
            async_get_clientsession(hass),
            entry.data[CONF_URL],
            entry.data[CONF_USERNAME],
            entry.data[CONF_PASSWORD],
        )
        self._calendars: list[dict[str, str]] = entry.data[CONF_CALENDARS]
        self._known_rooms: list[str] = []

    async def _async_update_data(self) -> RoomData:
        now = dt_util.utcnow()
        start = now - timedelta(days=DAYS_BACK)
        end = now + timedelta(days=DAYS_AHEAD)
        events: list[RoomEvent] = []
        try:
            for calendar in self._calendars:
                events += await self._client.async_get_events(
                    calendar["url"], start, end, calendar.get("name", "")
                )
        except NextcloudError as err:
            raise UpdateFailed(str(err)) from err

        # Raumliste nur ergaenzen, nie kuerzen: einmal angelegte Sensoren bleiben.
        for room in await self._client.async_get_room_names():
            if room not in self._known_rooms:
                self._known_rooms.append(room)
        for event in events:
            for room in event.rooms:
                if room not in self._known_rooms:
                    self._known_rooms.append(room)

        events.sort(key=lambda event: event.start)
        return RoomData(events=events, rooms=list(self._known_rooms))
