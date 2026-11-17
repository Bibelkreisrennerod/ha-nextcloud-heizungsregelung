"""Sensoren je Raum (Vorheizzeit, Heizstart, Temperatur, Protokoll) und fuer die Anlage."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .entity import StatusEntity, add_room_entities

PROTOCOL_ROWS = 20


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    add_room_entities(
        entry,
        async_add_entities,
        lambda room: [
            LeadSensor(entry, room),
            PlannedStartSensor(entry, room),
            RoomTemperatureSensor(entry, room),
            ProtocolSensor(entry, room),
        ],
        [ControlStatusSensor(entry, None, "status")],
    )


class LeadSensor(StatusEntity, SensorEntity):
    """Benoetigte Vorheizzeit fuer den naechsten Termin (gelernt, mit Zuschlag)."""

    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 0

    def __init__(self, entry: ConfigEntry, room: str) -> None:
        super().__init__(entry, room, "lead")

    @property
    def native_value(self) -> float | None:
        lead = self._status.get("lead")
        return round(lead) if lead is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        status = self._status
        return {
            "gelernte_vorgaenge": status.get("learned_cycles"),
            "sicherheitsfaktor": status.get("margin"),
            "aufheizrate_k_pro_h": status.get("heat_rate"),
            "waermeverlust": status.get("loss"),
        }


class PlannedStartSensor(StatusEntity, SensorEntity):
    """Geplanter Beginn des naechsten Aufheizens."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, entry: ConfigEntry, room: str) -> None:
        super().__init__(entry, room, "planned_start")

    @property
    def native_value(self) -> datetime | None:
        return self._status.get("planned_start")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"knapp": self._status.get("planned_late", False)}


class RoomTemperatureSensor(StatusEntity, SensorEntity):
    """Raumtemperatur, wie die Steuerung sie verwendet."""

    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, entry: ConfigEntry, room: str) -> None:
        super().__init__(entry, room, "room_temperature")

    @property
    def native_value(self) -> float | None:
        return self._status.get("temp")


class ProtocolSensor(StatusEntity, SensorEntity):
    """Heizprotokoll: Zustand = Abweichung des letzten Vorgangs von der Frist (min)."""

    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_icon = "mdi:history"
    _unrecorded_attributes = frozenset({"vorgaenge"})

    def __init__(self, entry: ConfigEntry, room: str) -> None:
        super().__init__(entry, room, "protocol")

    @property
    def native_value(self) -> int | None:
        history = self._status.get("history") or []
        for row in history:
            if row.get("abweichung_frist") is not None:
                return row["abweichung_frist"]
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"vorgaenge": (self._status.get("history") or [])[:PROTOCOL_ROWS]}


class ControlStatusSensor(StatusEntity, SensorEntity):
    """Zusammenfassung der Anlage: Plan, Vorlauf, Aussentemperatur."""

    _attr_icon = "mdi:home-thermometer"

    @property
    def native_value(self) -> str | None:
        status = self._status
        if not status:
            return None
        if not status.get("automatik"):
            return "aus"
        if status.get("vorlauf_niedrig"):
            return "Vorlauf niedrig"
        now = dt_util.now().isoformat(timespec="minutes")
        active = [p for p in status.get("plan", []) if p["start"] <= now]
        return f"{len(active)} Raum heizt auf" if len(active) == 1 else f"{len(active)} Räume heizen auf"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        status = self._status
        return {
            "plan": status.get("plan"),
            "aussentemperatur": status.get("aussen"),
            "vorlauf": status.get("vorlauf"),
            "duty_cycle": status.get("duty_cycle"),
            "lastbudget": status.get("budget"),
        }
