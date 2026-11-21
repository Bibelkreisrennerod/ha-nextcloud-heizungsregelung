"""Binaersensoren je Raum: Heizbedarf (Steuerung), Belegt (Kalender), Modell kalibriert."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .entity import StatusEntity, add_room_entities


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    add_room_entities(
        entry,
        async_add_entities,
        lambda room: [HeatingDemand(entry, room), Occupied(entry, room), ModelCalibrated(entry, room)],
    )


def _iso(value):
    return dt_util.as_local(value).isoformat(timespec="minutes") if value else None


class HeatingDemand(StatusEntity, BinarySensorEntity):
    """An, wenn die Steuerung den Raum gerade auf Komforttemperatur haelt oder aufheizt.

    Bewusst ohne Geraeteklasse: "heat" wuerde als "Heiss/Normal" angezeigt.
    Die Zustandsnamen kommen aus den Uebersetzungen (Heizen/Abgesenkt).
    """

    _attr_icon = "mdi:radiator"

    def __init__(self, entry: ConfigEntry, room: str) -> None:
        super().__init__(entry, room, "heating_demand")

    @property
    def is_on(self) -> bool | None:
        status = self._status
        return status.get("demand") if status else None

    def _extra(self) -> dict[str, Any] | None:
        status = self._status
        return {
            "betriebsart": status.get("mode"),
            "sollwert": status.get("desired"),
            "zieltemperatur": status.get("target"),
            "ziel_grund": status.get("target_source"),
            "ziel_hinweis": status.get("target_hint") or None,
            "naechster_termin": status.get("next_event"),
            "termin_beginn": _iso(status.get("next_start")),
            "geplanter_heizstart": _iso(status.get("planned_start")),
            "warm_um": _iso(status.get("reach")),
            "thermostate": status.get("climates"),
        }


class Occupied(StatusEntity, BinarySensorEntity):
    """An, solange laut Kalender ein Termin im Raum laeuft."""

    _attr_device_class = BinarySensorDeviceClass.OCCUPANCY

    def __init__(self, entry: ConfigEntry, room: str) -> None:
        super().__init__(entry, room, "occupied")

    @property
    def is_on(self) -> bool:
        now = dt_util.utcnow()
        data = self._entry.runtime_data.coordinator.data
        return any(
            self._room in event.rooms and not event.all_day and event.start <= now < event.end
            for event in (data.events if data else [])
        )


class ModelCalibrated(StatusEntity, BinarySensorEntity):
    """An, wenn das Modell den Raum genau genug vorhersagt (Fehler unter 0,5 K)."""

    _attr_icon = "mdi:check-decagram"

    def __init__(self, entry: ConfigEntry, room: str) -> None:
        super().__init__(entry, room, "calibrated")

    @property
    def is_on(self) -> bool | None:
        if not self._status:
            return None
        return bool(self._status.get("calibrated"))

    def _extra(self) -> dict[str, Any] | None:
        status = self._status
        return {
            "sicherheitsfaktor": round(status["safety"], 2) if status.get("safety") else None,
            "fenster_ohne_heizen": status.get("err_idle_n"),
            "fenster_beim_heizen": status.get("err_heat_n"),
        }
