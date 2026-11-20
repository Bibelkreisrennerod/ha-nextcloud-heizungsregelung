"""Sensoren je Raum (Plan, Prognose, gelernte Groessen, Protokoll) und fuer die Anlage."""

from __future__ import annotations

import math

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfEnergy, UnitOfPower, UnitOfTemperature, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import CONF_STOKERCLOUD
from .entity import StatusEntity, add_room_entities
from .physics import CURVE_DEV

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
            WarmAtSensor(entry, room),
            SetpointSensor(entry, room),
            RoomTemperatureSensor(entry, room),
            ValveSensor(entry, room),
            CoolingRateSensor(entry, room),
            HeatingRateSensor(entry, room),
            PowerDemandSensor(entry, room),
            DeadTimeSensor(entry, room),
            PredictionErrorSensor(entry, room),
            ProtocolSensor(entry, room),
        ],
        [
            ControlStatusSensor(entry, None, "status"),
            BufferReserveSensor(entry, None, "buffer_reserve"),
            BufferCapacitySensor(entry, None, "buffer_capacity"),
            BoilerRatedSensor(entry, None, "boiler_rated"),
            BoilerStartupSensor(entry, None, "boiler_startup"),
            CalibrationSensor(entry, None, "calibration"),
            ModelFitSensor(entry, None, "model_fit"),
            TestRunSensor(entry, None, "test_run"),
        ]
        + (
            [
                BoilerSensor(entry, "boiler_flow", "flow"),
                BoilerSensor(entry, "boiler_dhw", "dhw"),
                BoilerSensor(entry, "boiler_power", "power"),
                BoilerUpdatedSensor(entry, None, "boiler_updated"),
                BoilerStateSensor(entry, None, "boiler_state"),
            ]
            if entry.options.get(CONF_STOKERCLOUD)
            else []
        ),
    )


def _iso(value: datetime | None) -> str | None:
    return dt_util.as_local(value).isoformat(timespec="minutes") if value else None


# ---------------------------------------------------------------- je Raum


class LeadSensor(StatusEntity, SensorEntity):
    """Vorheizzeit fuer den naechsten Termin laut Plan (Frist minus Heizbeginn)."""

    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_suggested_display_precision = 0

    def __init__(self, entry: ConfigEntry, room: str) -> None:
        super().__init__(entry, room, "lead")

    @property
    def native_value(self) -> float | None:
        lead = self._status.get("lead")
        return round(lead) if lead is not None else None

    def _extra(self) -> dict[str, Any] | None:
        status = self._status
        return {
            "allein_bestfall_min": status.get("solo"),
            "sicherheitsfaktor": round(status["safety"], 2) if status.get("safety") else None,
            "gewertete_vorgaenge": status.get("learned_cycles"),
        }


class PlannedStartSensor(StatusEntity, SensorEntity):
    """Geplanter Beginn des naechsten Aufheizens (Vorstufe oder Komfort)."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, entry: ConfigEntry, room: str) -> None:
        super().__init__(entry, room, "planned_start")

    @property
    def native_value(self) -> datetime | None:
        return self._status.get("planned_start")

    def _extra(self) -> dict[str, Any] | None:
        status = self._status
        return {
            "vorstufe_ab": _iso(status.get("pre_start")),
            "vorstufe_sollwert": status.get("pre_level"),
            "komfort_ab": _iso(status.get("main_start")),
            "frist": _iso(status.get("deadline")),
            "knapp": status.get("planned_late", False),
            "zieltemperatur": status.get("next_target"),
            "ziel_grund": status.get("next_target_source"),
        }


class WarmAtSensor(StatusEntity, SensorEntity):
    """Prognose: wann der Raum warm ist (vorsichtig gerechnet)."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, entry: ConfigEntry, room: str) -> None:
        super().__init__(entry, room, "warm_at")

    @property
    def native_value(self) -> datetime | None:
        return self._status.get("reach")

    def _extra(self) -> dict[str, Any] | None:
        status = self._status
        return {
            "ohne_sicherheitszuschlag": _iso(status.get("reach_nominal")),
            "frist": _iso(status.get("deadline")),
            "knapp": status.get("planned_late", False),
        }


class SetpointSensor(StatusEntity, SensorEntity):
    """Sollwert, den die Steuerung gerade vorgibt (auch Zwischenwerte)."""

    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, entry: ConfigEntry, room: str) -> None:
        super().__init__(entry, room, "setpoint")

    @property
    def native_value(self) -> float | None:
        return self._status.get("desired")

    def _extra(self) -> dict[str, Any] | None:
        return {"betriebsart": self._status.get("mode")}


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


class ValveSensor(StatusEntity, SensorEntity):
    """Mittlere Ventilstellung der Heizkoerper im Raum."""

    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:valve"

    def __init__(self, entry: ConfigEntry, room: str) -> None:
        super().__init__(entry, room, "valve")

    @property
    def native_value(self) -> float | None:
        return self._status.get("valve")

    def _extra(self) -> dict[str, Any] | None:
        count = self._status.get("valve_count")
        curve = self._status.get("curve")
        attrs: dict[str, Any] = {"ventile": count, "geschaetzt": not count}
        if curve:
            # Gelernte Kennlinie: Ventilstellung (%) je Abweichung Soll minus Ist
            attrs["hoechstoeffnung"] = round(max(curve) * 100)
            attrs["kennlinie"] = {f"{x:+.1f} K": round(v * 100) for x, v in zip(CURVE_DEV, curve)}
            attrs["gemessene_stuetzstellen"] = self._status.get("curve_points")
        return attrs


class CoolingRateSensor(StatusEntity, SensorEntity):
    """Waermeverlust als Temperaturabfall: K/h bei 20 K Unterschied nach aussen."""

    _attr_native_unit_of_measurement = "K/h"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 2
    _attr_icon = "mdi:thermometer-chevron-down"

    def __init__(self, entry: ConfigEntry, room: str) -> None:
        super().__init__(entry, room, "cooling_rate")

    @property
    def native_value(self) -> float | None:
        a = self._status.get("a")
        return round(a * 20.0, 2) if a is not None else None

    def _extra(self) -> dict[str, Any] | None:
        a = self._status.get("a")
        return {
            "auskuehlkonstante_pro_h": round(a, 4) if a else None,
            "zeitkonstante_h": round(1 / a, 1) if a else None,
            "messfenster": self._status.get("cool_windows"),
            "unsicherheit_prozent": _spread(self._status, "a"),
        }


class HeatingRateSensor(StatusEntity, SensorEntity):
    """Groesster Waermegewinn: K/h bei offenen Ventilen und vollem Puffer."""

    _attr_native_unit_of_measurement = "K/h"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 2
    _attr_icon = "mdi:thermometer-chevron-up"

    def __init__(self, entry: ConfigEntry, room: str) -> None:
        super().__init__(entry, room, "heating_rate")

    @property
    def native_value(self) -> float | None:
        b = self._status.get("b")
        return round(b, 2) if b is not None else None

    def _extra(self) -> dict[str, Any] | None:
        status = self._status
        a, b = status.get("a"), status.get("b")
        return {
            "netto_bei_20_k": round(b - 20 * a, 2) if a is not None and b is not None else None,
            "aufheizvorgaenge": status.get("heat_windows"),
            "unsicherheit_prozent": _spread(status, "b"),
        }


class PowerDemandSensor(StatusEntity, SensorEntity):
    """Leistungsbedarf bei offenen Ventilen, in Prozent der Kessel-Dauerleistung."""

    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 0
    _attr_icon = "mdi:radiator"

    def __init__(self, entry: ConfigEntry, room: str) -> None:
        super().__init__(entry, room, "power_demand")

    @property
    def native_value(self) -> float | None:
        w = self._status.get("w")
        return round(w * 100) if w is not None else None

    def _extra(self) -> dict[str, Any] | None:
        status = self._status
        w, kw = status.get("w"), status.get("boiler_kw")
        return {
            "kw": round(w * kw, 1) if w is not None and kw else None,
            "gemessen": status.get("w_measured", False),
            "unsicherheit_prozent": _spread(status, "w"),
        }


class DeadTimeSensor(StatusEntity, SensorEntity):
    """Totzeit: vom Oeffnen der Ventile, bis die Waerme am Fuehler ankommt."""

    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_suggested_display_precision = 0
    _attr_icon = "mdi:timer-sand"

    def __init__(self, entry: ConfigEntry, room: str) -> None:
        super().__init__(entry, room, "dead_time")

    @property
    def native_value(self) -> float | None:
        d = self._status.get("d")
        return round(d * 60) if d is not None else None

    def _extra(self) -> dict[str, Any] | None:
        sigma = (self._status.get("sigma") or {}).get("d")
        tau = self._status.get("tau")
        return {
            "gemessene_heizbeginne": self._status.get("dead_events"),
            "unsicherheit_min": None if sigma is None else round(sigma * 60),
            # Traegheit der Heizkoerper: Waerme kommt danach verzoegert an und wirkt nach
            "traegheit_min": None if tau is None else round(tau * 60),
            "traegheit_unsicherheit_prozent": _spread(self._status, "tau"),
        }


class PredictionErrorSensor(StatusEntity, SensorEntity):
    """Mittlerer Fehler der 2-Stunden-Vorhersage beim Heizen (K)."""

    _attr_native_unit_of_measurement = UnitOfTemperature.KELVIN
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 2
    _attr_icon = "mdi:target"

    def __init__(self, entry: ConfigEntry, room: str) -> None:
        super().__init__(entry, room, "prediction_error")

    @property
    def native_value(self) -> float | None:
        status = self._status
        value = status.get("err_heat") if status.get("err_heat") is not None else status.get("err_idle")
        return round(value, 2) if value is not None else None

    def _extra(self) -> dict[str, Any] | None:
        status = self._status
        idle, heat = status.get("err_idle"), status.get("err_heat")
        return {
            "kalibriert": status.get("calibrated", False),
            "fehler_ohne_heizen": round(idle, 2) if idle is not None else None,
            "fehler_beim_heizen": round(heat, 2) if heat is not None else None,
            "fenster_ohne_heizen": status.get("err_idle_n"),
            "fenster_beim_heizen": status.get("err_heat_n"),
        }


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

    def _extra(self) -> dict[str, Any] | None:
        return {"vorgaenge": (self._status.get("history") or [])[:PROTOCOL_ROWS]}


# ---------------------------------------------------------------- Anlage


class ControlStatusSensor(StatusEntity, SensorEntity):
    """Zusammenfassung der Anlage: Plan, Vorlauf, Aussentemperatur."""

    _attr_icon = "mdi:home-thermometer"
    _unrecorded_attributes = frozenset({"plan", "letzte_auswertung"})

    @property
    def native_value(self) -> str | None:
        status = self._status
        if not status:
            return None
        if status.get("kalibrierung_status") not in (None, "kein Lauf"):
            return f"Kalibrierlauf {status['kalibrierung_status']}"
        if not status.get("automatik"):
            return "Automatik aus"
        count = status.get("heizen", 0)
        return f"{count} Raum heizt" if count == 1 else f"{count} Räume heizen"

    def _extra(self) -> dict[str, Any] | None:
        status = self._status
        return {
            "plan": status.get("plan"),
            "geplant_um": status.get("plan_um"),
            "absenkfenster": status.get("absenkfenster"),
            "aussentemperatur": status.get("aussen"),
            "vorlauf": status.get("vorlauf"),
            "duty_cycle": status.get("duty_cycle"),
        }


class BufferReserveSensor(StatusEntity, SensorEntity):
    """Geschaetzte Pufferreserve in Prozent (aus Ventilstellungen, Vorlauf korrigiert)."""

    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 0
    _attr_icon = "mdi:storage-tank"

    @property
    def native_value(self) -> float | None:
        value = self._status.get("puffer_reserve")
        return round(value * 100) if value is not None else None


class BufferCapacitySensor(StatusEntity, SensorEntity):
    """Gelernte nutzbare Pufferkapazitaet in kWh."""

    _attr_device_class = SensorDeviceClass.ENERGY_STORAGE
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_suggested_display_precision = 0

    @property
    def native_value(self) -> float | None:
        status = self._status
        hours, kw = status.get("puffer_kapazitaet_h"), status.get("kessel_kw")
        return round(hours * kw, 1) if hours is not None and kw else None

    def _extra(self) -> dict[str, Any] | None:
        status = self._status
        hours = status.get("puffer_kapazitaet_h")
        return {
            "kessel_stunden": round(hours, 2) if hours is not None else None,
            "gemessen": status.get("puffer_gemessen", False),
        }


class BoilerRatedSensor(StatusEntity, SensorEntity):
    """Dauerleistung des Kessels (StokerCloud, sonst Nennwert)."""

    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.KILO_WATT
    _attr_suggested_display_precision = 0

    @property
    def native_value(self) -> float | None:
        return self._status.get("kessel_kw")


class BoilerStartupSensor(StatusEntity, SensorEntity):
    """Anlaufzeit des Kessels bis zur vollen Leistung."""

    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_suggested_display_precision = 0

    @property
    def native_value(self) -> float | None:
        hours = self._status.get("anlaufzeit_h")
        return round(hours * 60) if hours is not None else None


class CalibrationSensor(StatusEntity, SensorEntity):
    """Kalibrierlauf und letzte Auswertung einer Heizphase."""

    _attr_icon = "mdi:tune-vertical"
    _unrecorded_attributes = frozenset({"letzte_auswertung", "lauf"})

    @property
    def native_value(self) -> str | None:
        return self._status.get("kalibrierung_status")

    def _extra(self) -> dict[str, Any] | None:
        status = self._status
        return {
            "lauf": status.get("kalibrierung"),
            "ausgewertete_heizphasen": status.get("auswertungen"),
            "letzte_auswertung": status.get("letzte_auswertung"),
        }


class BoilerSensor(StatusEntity, SensorEntity):
    """Ein Kesselwert aus StokerCloud (nur aktuelle Werte, sonst unbekannt)."""

    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, entry: ConfigEntry, key: str, field: str) -> None:
        super().__init__(entry, None, key)
        self._field = field
        if field == "power":
            self._attr_native_unit_of_measurement = "%"
            self._attr_icon = "mdi:fire"
        else:
            self._attr_device_class = SensorDeviceClass.TEMPERATURE
            self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    @property
    def native_value(self) -> float | None:
        boiler = self._controller.boiler if self._controller else None
        if boiler is None or boiler.stale:
            return None
        return getattr(boiler, self._field)


class BoilerUpdatedSensor(StatusEntity, SensorEntity):
    """Zeitpunkt der letzten Meldung des Kessels an StokerCloud."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP

    @property
    def native_value(self) -> datetime | None:
        boiler = self._controller.boiler if self._controller else None
        return boiler.updated if boiler else None


class BoilerStateSensor(StatusEntity, SensorEntity):
    """Kesselstatus; Attribute enthalten alle gelesenen Rohwerte zur Pruefung."""

    _attr_icon = "mdi:fire-circle"
    _unrecorded_attributes = frozenset({"rohwerte"})

    @property
    def native_value(self) -> str | None:
        controller = self._controller
        if controller is None:
            return None
        if controller.boiler is None:
            return "keine Verbindung" if controller._boiler_error else None
        if controller.boiler.stale:
            offline = str(controller.boiler.values.get("notconnected", {}).get("value")) == "1"
            return "Kessel offline" if offline else "keine aktuellen Daten"
        return controller.boiler.state or "verbunden"

    def _extra(self) -> dict[str, Any] | None:
        controller = self._controller
        boiler = controller.boiler if controller else None
        return {
            "fehler": controller._boiler_error if controller else None,
            "vorlauf_gelesen": boiler.flow if boiler else None,
            "letzte_uhrzeit": (boiler.values.get("clock") or {}).get("value") if boiler else None,
            "rohwerte": {
                key: f"{item.get('value')} {item.get('unit') or ''}".strip()
                for key, item in (boiler.values.items() if boiler else [])
            },
        }


def _spread(status: dict[str, Any], key: str) -> int | None:
    """Unsicherheit (1 Standardabweichung) eines gelernten Werts in Prozent."""
    sigma = (status.get("sigma") or {}).get(key)
    return None if sigma is None else round(100 * (math.exp(sigma) - 1))


class ModelFitSensor(StatusEntity, SensorEntity):
    """Gemeinsame Anpassung aller Werte: mittlerer Vorhersagefehler (K)."""

    _attr_native_unit_of_measurement = UnitOfTemperature.KELVIN
    _attr_suggested_display_precision = 2
    _attr_icon = "mdi:chart-bell-curve"
    _unrecorded_attributes = frozenset({"raeume", "pufferreserve_unsicherheit_prozent"})

    @property
    def native_value(self) -> float | None:
        fit = self._status.get("fit") or {}
        return fit.get("rms")

    def _extra(self) -> dict[str, Any] | None:
        fit = self._status.get("fit") or {}
        return {
            "zeitpunkt": fit.get("zeitpunkt"),
            "messpunkte": fit.get("rows"),
            "vorhersagen": fit.get("residuals"),
            "schritte": fit.get("iterations"),
            "neue_messpunkte": self._status.get("rows_pending"),
            "pufferreserve_unsicherheit_prozent": fit.get("e_buf_spread"),
            "raeume": fit.get("rooms"),
        }


class TestRunSensor(StatusEntity, SensorEntity):
    """Testlauf, der die Unsicherheit der Werte am staerksten verkleinern wuerde."""

    _attr_icon = "mdi:flask-outline"
    _unrecorded_attributes = frozenset({"alle", "verbessert"})

    @property
    def native_value(self) -> str | None:
        runs = (self._status.get("fit") or {}).get("experiments") or []
        return runs[0]["label"] if runs else None

    def _extra(self) -> dict[str, Any] | None:
        fit = self._status.get("fit") or {}
        runs = fit.get("experiments") or []
        best = runs[0] if runs else {}
        return {
            "raeume": best.get("rooms"),
            "informationsgewinn": best.get("gain"),
            "dauer_h": best.get("hours"),
            "aussentemperatur": fit.get("outside"),
            "verbessert": best.get("improves"),
            "alle": [{"lauf": r["label"], "gewinn": r["gain"], "dauer_h": r["hours"]} for r in runs],
        }

