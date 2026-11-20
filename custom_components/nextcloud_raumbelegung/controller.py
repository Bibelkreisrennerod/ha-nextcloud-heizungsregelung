"""Heizungssteuerung: misst, lernt, plant und schaltet die Thermostate.

Ablauf je Takt (jede Minute):
 1. Zustand lesen: Termine, Raumtemperaturen, Sollwerte, Ventilstellungen,
    Aussen- und Vorlauftemperatur, Duty Cycle des Access Points.
 2. Alle 5 Minuten ein Messpunkt fuers Lernen (learning.py) und die
    geschaetzte Pufferreserve fortschreiben - auch bei ausgeschalteter
    Automatik, dann lernt die Steuerung aus dem Homematic-Profil.
 3. Planen (planning.py): nur wenn sich Termine oder Einstellungen aendern,
    alle 10 Minuten wird mit einem Simulationslauf geprueft, ob der Plan noch
    aufgeht. Die Rechnung laeuft ausserhalb der Ereignisschleife.
 4. Je Raum den Sollwert laut Plan bestimmen (Absenkung, Vorstufe, Komfort)
    und nur bei Abweichung senden.
 5. Aufheizvorgaenge protokollieren; Abweichung zur Vorhersage ergibt den
    Sicherheitsfaktor.

Solange die Automatik an ist, stehen die gesteuerten Heizgruppen im
Homematic-Modus "Manuell" (hvac_mode "heat"): das Homematic-Profil greift
dann nicht ein. Wird die Automatik ausgeschaltet, setzt die Steuerung die
Gruppen zurueck in den Automodus, und das bisherige Homematic-Profil gilt
sofort wieder. Faellt Home Assistant aus, bleibt der letzte Sollwert stehen.
"""

from __future__ import annotations

import asyncio
from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import partial
import logging
import math
from typing import Any, Callable

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    BOILER_ALERT_AFTER,
    BOILER_INTERVAL,
    BOILER_NOMINAL_KW,
    CALIBRATION_HOURS,
    CALIBRATION_OTHERS_DROP,
    CALIBRATION_REST,
    CALIBRATION_WAIT,
    COMMAND_RETRY_WINDOW,
    CONF_DUTY_CYCLE_SENSOR,
    CONF_FLOW_SENSOR,
    CONF_OUTSIDE_SENSOR,
    CONF_STOKERCLOUD,
    CONF_WEATHER,
    CONTROL_INTERVAL,
    DEFAULT_WEATHER,
    DOMAIN,
    DUTY_CYCLE_ALERT,
    DUTY_CYCLE_LIMIT,
    EARLY_MINUTES,
    FORECAST_INTERVAL,
    GLOBAL_DEFAULTS,
    HISTORY_LENGTH,
    MANUAL_HOLD_OUTSIDE_EVENT,
    MAX_COMMANDS_PER_HOUR,
    MIN_COMMAND_GAP,
    PLAN_CHECK_INTERVAL,
    PLAN_MAX_AGE,
    REACHED_TOLERANCE,
    ROOM_DEFAULTS,
    SAMPLE_INTERVAL,
    SETPOINT_TOLERANCE,
    SOURCE_ALERT_AFTER,
)
from .history import ReplayResult, RoomSeries, Series, replay
from .learning import Learner, RoomSample, flow_share
from .physics import PlantParams, PlantState, RoomParams, plant_step, solo_minutes, valve
from .planning import (
    PlanResult,
    RoomInput,
    RoomPlan,
    plan_heating,
    plan_signature,
    plan_still_valid,
    setpoint_at,
)
from .stokercloud import StokerCloudClient, StokerCloudError, StokerData

_LOGGER = logging.getLogger(__name__)
STORE_VERSION = 1
SAMPLE_HOURS = SAMPLE_INTERVAL.total_seconds() / 3600
HISTORY_DAYS = 14  # so weit zurueck aus dem Recorder lernen (er haelt meist 10 Tage)
BOOTSTRAP_DELAY = 120  # s nach dem Start, damit alle Entitaeten geladen sind


def signal_status(entry: ConfigEntry) -> str:
    """Signal: neuer Zustand der Steuerung liegt vor."""
    return f"{DOMAIN}_{entry.entry_id}_status"


def _float(hass: HomeAssistant, entity_id: str | None) -> float | None:
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
        return None
    try:
        return float(state.state)
    except ValueError:
        return None


def _local(moment: datetime | None) -> str | None:
    return dt_util.as_local(moment).isoformat(timespec="minutes") if moment else None


@dataclass
class Cycle:
    """Ein laufender Aufheizvorgang eines Raums."""

    event: str
    calendar: str
    event_start: datetime
    event_end: datetime
    deadline: datetime
    planned_start: datetime
    heat_start: datetime
    temp_start: float
    outside_start: float | None
    target: float
    predicted_minutes: float | None  # Dauer laut Modell ohne Sicherheitsfaktor
    lead_minutes: float
    counts: bool = True  # zaehlt fuer den Sicherheitsfaktor (nicht bei Handverstellung/Neustart)
    reached_at: datetime | None = None
    observed: bool = False  # Aufheizen durch das Homematic-Profil, nur beobachtet

    def record(self, reason: str) -> dict[str, Any]:
        heat_minutes = None
        late = None
        if self.reached_at is not None:
            heat_minutes = round((self.reached_at - self.heat_start).total_seconds() / 60)
            late = round((self.reached_at - self.deadline).total_seconds() / 60)
        if self.observed:
            late = None
        return {
            "termin": self.event,
            "kalender": self.calendar,
            "beginn": _local(self.event_start),
            "frist": None if self.observed else _local(self.deadline),
            "geplanter_start": _local(self.planned_start),
            "heizstart": _local(self.heat_start),
            "vorheizzeit_geplant": round(self.lead_minutes),
            "temp_start": round(self.temp_start, 1),
            "aussen_start": None if self.outside_start is None else round(self.outside_start, 1),
            "ziel": self.target,
            "erreicht_um": _local(self.reached_at),
            "aufheizdauer": heat_minutes,
            "vorhergesagt": None if self.predicted_minutes is None else round(self.predicted_minutes),
            "abweichung_frist": late,  # negativ = frueher warm als noetig
            "gewertet": self.counts,
            "abschluss": reason,
        }


@dataclass
class RoomState:
    """Laufzeitzustand eines Raums (nicht gespeichert)."""

    last_command: float | None = None
    last_command_at: datetime | None = None
    before_command: float | None = None  # Sollwert vor unserem letzten Befehl
    mode_command_at: datetime | None = None  # letztes Umschalten auf Manuell
    manual_until: datetime | None = None
    desired: float | None = None  # zuletzt gewuenschter Sollwert
    mode: str | None = None  # zuletzt bestimmte Betriebsart
    # Seit dem Start schon einen gueltigen Sollwert gelesen? Nach einem Neustart
    # kann Homematic IP noch einige Minuten "nicht verfuegbar" melden.
    seen: bool = False
    cycle: Cycle | None = None
    status: dict[str, Any] = field(default_factory=dict)


@dataclass
class Calibration:
    """Ein angeforderter Kalibrierlauf."""

    rooms: list[str]
    target: float
    others: float
    hours: float
    requested: datetime
    started: datetime | None = None
    reached: dict[str, datetime] = field(default_factory=dict)

    @property
    def running(self) -> bool:
        return self.started is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "rooms": self.rooms,
            "target": self.target,
            "others": self.others,
            "hours": self.hours,
            "requested": self.requested.isoformat(),
            "started": self.started.isoformat() if self.started else None,
            "reached": {room: at.isoformat() for room, at in self.reached.items()},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Calibration:
        def parse(value: str | None) -> datetime | None:
            return dt_util.parse_datetime(value) if value else None

        return cls(
            rooms=list(data["rooms"]),
            target=float(data["target"]),
            others=float(data["others"]),
            hours=float(data["hours"]),
            requested=parse(data["requested"]),
            started=parse(data.get("started")),
            reached={room: parse(at) for room, at in (data.get("reached") or {}).items()},
        )

    def describe(self) -> dict[str, Any]:
        return {
            "raeume": self.rooms,
            "ziel": self.target,
            "andere_raeume": self.others,
            "hoechstens_stunden": self.hours,
            "angefordert": _local(self.requested),
            "gestartet": _local(self.started),
            "erreicht": {room: _local(at) for room, at in self.reached.items()},
        }


class HeatingController:
    """Steuert alle Raeume eines Eintrags."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, runtime: Any) -> None:
        self.hass = hass
        self.entry = entry
        self.runtime = runtime
        self._store: Store = Store(hass, STORE_VERSION, f"{DOMAIN}.{entry.entry_id}")
        self.learner = Learner()
        self.history: dict[str, list[dict[str, Any]]] = {}
        self.rooms: dict[str, RoomState] = {}
        self.status: dict[str, Any] = {}
        self.plan: PlanResult | None = None
        self.gauge: PlantState | None = None  # geschaetzte Pufferreserve
        self.calibration: Calibration | None = None
        self._inputs: dict[str, RoomInput] = {}
        self._plan_signature: tuple | None = None
        self._plan_at: datetime | None = None
        self._plan_checked_at: datetime | None = None
        self._force_plan = True
        self._sample_at: datetime | None = None
        self._forecast: list[tuple[datetime, float]] = []
        self._forecast_at: datetime | None = None
        self._source_failed_since: datetime | None = None
        self._first_tick = True
        name = (entry.options.get(CONF_STOKERCLOUD) or "").strip()
        self._boiler_client = (
            StokerCloudClient(async_get_clientsession(hass), name) if name else None
        )
        self.boiler: StokerData | None = None
        self._boiler_at: datetime | None = None
        self._boiler_error: str | None = None
        self._unsub: Callable[[], None] | None = None
        self._lock = asyncio.Lock()
        self._sent: list[datetime] = []  # eigene Befehle der letzten Stunde
        self._bootstrapped = False  # schon einmal aus der Historie gelernt
        self._learning_history = False  # Live-Messung pausiert (Historie wird gelesen)
        self._history_running = False
        self._fit_running = False
        self._fit_tried_at: datetime | None = None  # letzter Versuch (nach Fehlschlag nicht jede 5 min)
        self._unsub_bootstrap: Callable[[], None] | None = None
        self._late_notified: dict[str, datetime] = {}  # Raum -> gemeldete Uhrzeit "warm um"
        self._area_map: dict[tuple[str, str], list[str]] = {}  # (Bereich, Domain) -> Entitaeten, je Takt
        self._duty_entity: str | None = None
        self._duty_lookup_at: datetime | None = None

    # ------------------------------------------------------------ Lebenszyklus

    async def async_start(self) -> None:
        data = await self._store.async_load() or {}
        self.history = data.get("history", {})
        try:
            self.learner = Learner(data.get("learner"))
        except (TypeError, ValueError, KeyError) as err:
            _LOGGER.warning("Gelernte Werte nicht lesbar, beginne neu: %s", err)
            self.learner = Learner()
        self._bootstrapped = bool(data.get("bootstrapped"))
        if data.get("calibration"):
            # Ein Kalibrierlauf ueberlebt einen Neustart; sonst blieben seine
            # Sollwerte im Modus Manuell stehen.
            try:
                self.calibration = Calibration.from_dict(data["calibration"])
            except (KeyError, TypeError, ValueError):
                self.calibration = None
        self._unsub = async_track_time_interval(self.hass, self._async_tick, CONTROL_INTERVAL)
        if not self._bootstrapped:
            # Beim ersten Start aus der Historie lernen; bis dahin nicht live messen,
            # damit die Messpunkte in zeitlicher Reihenfolge ankommen.
            self._learning_history = True
            self._unsub_bootstrap = async_call_later(self.hass, BOOTSTRAP_DELAY, self._async_bootstrap)

    async def _async_bootstrap(self, _now: datetime) -> None:
        self._unsub_bootstrap = None
        try:
            await self.async_learn_from_history(reset=False)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Lernen aus der Historie fehlgeschlagen")
            self._learning_history = False

    async def async_stop(self) -> None:
        if self._unsub:
            self._unsub()
            self._unsub = None
        if self._unsub_bootstrap:
            self._unsub_bootstrap()
            self._unsub_bootstrap = None
        await self._store.async_save(self._data_to_store())

    def _data_to_store(self) -> dict[str, Any]:
        return {
            "history": self.history,
            "learner": self.learner.to_dict(),
            "bootstrapped": self._bootstrapped,
            "calibration": self.calibration.to_dict() if self.calibration else None,
        }

    def _save(self) -> None:
        self._store.async_delay_save(self._data_to_store, 60)

    # ------------------------------------------------------------ Einstellungen

    def setting(self, room: str | None, key: str) -> float:
        default = ROOM_DEFAULTS.get(key) if room is not None else GLOBAL_DEFAULTS.get(key)
        return self.runtime.settings.get((room, key), default)

    def enabled(self, room: str | None) -> bool:
        # Ohne gespeicherten Schalterstand: global aus (nichts steuern, bis
        # jemand einschaltet), je Raum an.
        return bool(self.runtime.settings.get((room, "automatic"), 1.0 if room is not None else 0.0))

    def controlled(self, room: str) -> bool:
        return self.enabled(None) and self.enabled(room)

    def room_params(self, room: str) -> RoomParams:
        return self.learner.room_params(room, self.setting(room, "preheat"))

    def plant_params(self) -> PlantParams:
        nominal = self._boiler_nominal_kw()
        return self.learner.plant_params(PlantParams(p_max_kw=nominal))

    @callback
    def resume(self, room: str | None) -> None:
        """Automatik wieder an: vorhandene Sollwerte nicht als Handverstellung werten."""
        for name, state in self.rooms.items():
            if room is None or name == room:
                state.manual_until = None
                state.last_command = None
                state.before_command = None
                state.desired = None
                if state.cycle is not None:
                    state.cycle.counts = False
        self._first_tick = True
        self._force_plan = True

    async def async_suspend(self, room: str | None) -> None:
        """Automatik aus: Heizgruppen zurueck in den Automodus, das Homematic-Profil gilt sofort."""
        for name in list(self.rooms):
            if room is not None and name != room:
                continue
            if room is None and self.calibration is not None and self.calibration.running:
                continue  # der Kalibrierlauf laeuft auch ohne Automatik weiter
            await self._release(name, "Automatik aus")
        self._force_plan = True

    async def _release(self, room: str, reason: str) -> None:
        """Raum dem Homematic-Profil zurueckgeben."""
        state = self.rooms.setdefault(room, RoomState())
        if state.cycle is not None and not state.cycle.observed:
            self._close_cycle(dt_util.utcnow(), room, state, reason)
        climates, _ = self._climates(room)
        # Nur Gruppen im Modus Manuell umschalten: jeder Befehl kostet Funkkontingent.
        manual = [c for c in climates if (current := self.hass.states.get(c)) is not None and current.state == "heat"]
        if manual:
            try:
                await self.hass.services.async_call(
                    "climate", "set_hvac_mode", {"entity_id": manual, "hvac_mode": "auto"}, blocking=True
                )
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("%s: Rueckgabe an das Homematic-Profil fehlgeschlagen: %s", room, err)
        state.last_command = None
        state.before_command = None
        state.manual_until = None
        state.mode_command_at = None
        state.desired = None

    @callback
    def request_update(self) -> None:
        """Nach Aenderung einer Einstellung sofort neu planen."""
        self._force_plan = True
        self.hass.async_create_task(self._async_tick(dt_util.utcnow()))

    # ------------------------------------------------------------ Kalibrierlauf

    def _calibration_rooms(self) -> list[str]:
        return self.calibration.rooms if self.calibration else []

    async def async_start_calibration(
        self, rooms: list[str], target: float | None, others: float | None, hours: float | None
    ) -> None:
        """Kalibrierlauf anfordern; er beginnt, sobald seit 90 Minuten kein Raum heizt."""
        data = self.runtime.coordinator.data
        known = data.rooms if data else []
        unknown = [room for room in rooms if room not in known]
        if unknown:
            raise HomeAssistantError(f"Unbekannte Räume: {', '.join(unknown)}")
        if not rooms:
            raise HomeAssistantError("Bitte mindestens einen Raum angeben.")
        missing = [room for room in rooms if not self._climates(room)[0]]
        if missing:
            raise HomeAssistantError(f"Ohne Thermostat: {', '.join(missing)}")
        hours = hours or CALIBRATION_HOURS
        now = dt_util.utcnow()
        horizon = now + CALIBRATION_WAIT + timedelta(hours=hours + 1)
        busy = sorted(
            {
                room
                for event in (data.events if data else [])
                if not event.all_day and event.start < horizon and event.end > now
                for room in event.rooms
            }
        )
        if busy:
            raise HomeAssistantError(
                "In den nächsten Stunden sind Räume gebucht: " + ", ".join(busy)
                + ". Kalibrierläufe bitte nur an freien Tagen."
            )
        setback = self.setting(None, "setback")
        self.calibration = Calibration(
            rooms=list(rooms),
            target=target if target is not None else max(self.setting(r, "comfort") for r in rooms),
            others=others if others is not None else setback - CALIBRATION_OTHERS_DROP,
            hours=hours,
            requested=now,
        )
        self._force_plan = True
        _LOGGER.info("Kalibrierlauf angefordert: %s", self.calibration.describe())
        self._save()
        await self._async_tick(now)

    async def async_stop_calibration(self, reason: str = "abgebrochen") -> None:
        calibration = self.calibration
        if calibration is None:
            return
        self.calibration = None
        self._force_plan = True
        self._save()
        self._notify(
            "calibration",
            f"Kalibrierlauf {reason}",
            self._calibration_summary(calibration, reason),
        )
        # Raeume ohne Automatik wieder dem Homematic-Profil geben
        for room in list(self.rooms):
            if not self.controlled(room):
                await self._release(room, f"Kalibrierlauf {reason}")

    def _calibration_summary(self, calibration: Calibration, reason: str) -> str:
        lines = [f"Räume: {', '.join(calibration.rooms)}, Ziel {calibration.target:.1f} °C."]
        for room in calibration.rooms:
            at = calibration.reached.get(room)
            if at and calibration.started:
                minutes = round((at - calibration.started).total_seconds() / 60)
                lines.append(f"{room}: Ziel nach {minutes} min erreicht.")
            else:
                lines.append(f"{room}: Ziel nicht erreicht.")
        if reason == "beendet":
            lines.append(
                "Die Auswertung (Pufferreserve, Leistungsbedarf) folgt, sobald alle Ventile "
                "wieder zu sind; sie steht dann im Sensor „Kalibrierung“."
            )
        return "\n".join(lines)

    def _calibration_step(self, now: datetime, infos: dict[str, dict[str, Any]]) -> None:
        calibration = self.calibration
        if calibration is None:
            return
        if not calibration.running:
            if self.learner.quiet_for(now) >= CALIBRATION_REST:
                calibration.started = now
                self._save()
                _LOGGER.info("Kalibrierlauf gestartet")
            elif now - calibration.requested > CALIBRATION_WAIT:
                self.hass.async_create_task(self.async_stop_calibration("abgebrochen (keine Ruhephase)"))
            return
        for room in calibration.rooms:
            temp = infos.get(room, {}).get("temp")
            if room not in calibration.reached and temp is not None and temp >= calibration.target - REACHED_TOLERANCE:
                calibration.reached[room] = now
                self._save()
        if len(calibration.reached) == len(calibration.rooms):
            self.hass.async_create_task(self.async_stop_calibration("beendet"))
        elif now - calibration.started > timedelta(hours=calibration.hours):
            self.hass.async_create_task(self.async_stop_calibration("abgebrochen (Zeit überschritten)"))

    # --------------------------------------------------------------- Eingaben

    def _climates(self, room: str) -> tuple[list[str], str | None]:
        """Thermostat-Gruppen im HA-Bereich mit dem Namen des Raums."""
        area = ar.async_get(self.hass).async_get_area_by_name(room)
        if area is None:
            return [], None
        return self._area_entities(area.id, "climate"), area.id

    def _area_entities(self, area_id: str, domain: str) -> list[str]:
        if not self._area_map:
            self._build_area_map()
        return self._area_map.get((area_id, domain), [])

    def _build_area_map(self) -> None:
        """Einmal je Takt: Klima- und Sensor-Entitaeten nach Bereich (eigener oder des Geraets)."""
        devices = dr.async_get(self.hass)
        result: dict[tuple[str, str], list[str]] = {}
        for entry in er.async_get(self.hass).entities.values():
            if entry.domain not in ("climate", "sensor") or entry.disabled_by is not None:
                continue
            area = entry.area_id
            if area is None and entry.device_id:
                device = devices.async_get(entry.device_id)
                area = device.area_id if device else None
            if area is not None:
                result.setdefault((area, entry.domain), []).append(entry.entity_id)
        for value in result.values():
            value.sort()
        self._area_map = result

    def _valve_sensors(self, area_id: str | None) -> list[str]:
        """Ventilstellungen (%) der Heizkoerperthermostate im Bereich."""
        if area_id is None:
            return []
        registry = er.async_get(self.hass)
        result = []
        for entity_id in self._area_entities(area_id, "sensor"):
            entry = registry.async_get(entity_id)
            if entry is None or entry.platform != "homematicip_cloud":
                continue
            state = self.hass.states.get(entity_id)
            unit = state.attributes.get("unit_of_measurement") if state else None
            device_class = state.attributes.get("device_class") if state else None
            if unit != "%" or device_class in ("humidity", "battery"):
                continue
            key = f"{entry.translation_key or ''} {entry.original_name or ''} {entity_id}".lower()
            if "valve" in key or "ventil" in key or entity_id.endswith("_heizen"):
                result.append(entity_id)
        return result

    def _room_reading(self, climates: list[str]) -> tuple[float | None, float | None]:
        """(Raumtemperatur, aktueller Sollwert) gemittelt ueber die Thermostat-Gruppen."""
        temps, targets = [], []
        for entity_id in climates:
            state = self.hass.states.get(entity_id)
            if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
                continue
            current = state.attributes.get("current_temperature")
            target = state.attributes.get("temperature")
            if isinstance(current, (int, float)):
                temps.append(float(current))
            if isinstance(target, (int, float)):
                targets.append(float(target))
        temp = sum(temps) / len(temps) if temps else None
        target = sum(targets) / len(targets) if targets else None
        return temp, target

    def _valve_reading(
        self, room: str, sensors: list[str], target: float | None, temp: float | None
    ) -> float | None:
        """Mittlere Ventilstellung 0..1; ohne Sensoren aus Soll/Ist und Kennlinie geschaetzt."""
        values = [v for v in (_float(self.hass, s) for s in sensors) if v is not None]
        if values:
            return min(max(sum(values) / len(values) / 100.0, 0.0), 1.0)
        if target is None or temp is None:
            return None
        return valve(target, temp, self.learner.curve(room))

    def _outside_now(self) -> float | None:
        value = _float(self.hass, self.entry.options.get(CONF_OUTSIDE_SENSOR))
        if value is not None:
            return value
        weather = self.hass.states.get(self.entry.options.get(CONF_WEATHER, DEFAULT_WEATHER))
        if weather is not None:
            temp = weather.attributes.get("temperature")
            if isinstance(temp, (int, float)):
                return float(temp)
        return None

    async def _refresh_forecast(self, now: datetime) -> None:
        if self._forecast_at and now - self._forecast_at < FORECAST_INTERVAL:
            return
        self._forecast_at = now
        weather = self.entry.options.get(CONF_WEATHER, DEFAULT_WEATHER)
        if self.hass.states.get(weather) is None:
            return
        try:
            response = await self.hass.services.async_call(
                "weather",
                "get_forecasts",
                {"type": "hourly"},
                target={"entity_id": weather},
                blocking=True,
                return_response=True,
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Wetterprognose nicht verfuegbar: %s", err)
            return
        points = []
        for item in (response or {}).get(weather, {}).get("forecast", []):
            moment = dt_util.parse_datetime(str(item.get("datetime", "")))
            temp = item.get("temperature")
            if moment is not None and isinstance(temp, (int, float)):
                points.append((dt_util.as_utc(moment), float(temp)))
        if points:
            self._forecast = sorted(points)

    def _outside_function(self, fallback: float) -> Callable[[datetime], float]:
        """Aussentemperatur zu einem Zeitpunkt laut Prognose (sonst der aktuelle Wert)."""
        points = list(self._forecast)
        times = [t for t, _ in points]
        cache: dict[datetime, float] = {}  # die Planung fragt dieselben Zeitpunkte oft ab

        def at(moment: datetime) -> float:
            value = cache.get(moment)
            if value is not None:
                return value
            if not points or moment < times[0] - timedelta(hours=1):
                value = fallback
            elif moment <= times[0]:
                value = points[0][1]
            elif moment >= times[-1]:
                value = points[-1][1]
            else:
                index = bisect_right(times, moment)
                (t1, v1), (t2, v2) = points[index - 1], points[index]
                value = v1 + (v2 - v1) * ((moment - t1) / (t2 - t1))
            cache[moment] = value
            return value

        return at

    async def _refresh_boiler(self, now: datetime) -> None:
        """Kesselwerte aus StokerCloud lesen (nur lesend, alle 5 Minuten)."""
        if self._boiler_client is None:
            return
        if self._boiler_at and now - self._boiler_at < BOILER_INTERVAL:
            return
        self._boiler_at = now
        try:
            self.boiler = await self._boiler_client.async_read(now, dt_util.get_default_time_zone())
            self._boiler_error = None
        except StokerCloudError as err:
            self._boiler_error = str(err)
            _LOGGER.debug("StokerCloud: %s", err)
        boiler = self.boiler
        last = boiler.updated if boiler else None
        if self._boiler_error or (boiler is not None and boiler.stale):
            since = last or now
            if now - since >= BOILER_ALERT_AFTER or (self._boiler_error and boiler is None):
                self._notify(
                    "boiler",
                    "Kessel liefert keine Daten",
                    "StokerCloud meldet keine aktuellen Werte. Die Steuerung schätzt die "
                    "Pufferreserve ohne Vorlauftemperatur nur aus den Ventilstellungen.",
                )
        else:
            self._dismiss("boiler")

    def _boiler_fresh(self) -> StokerData | None:
        return self.boiler if self.boiler is not None and not self.boiler.stale else None

    def _boiler_nominal_kw(self) -> float:
        boiler = self.boiler
        if boiler is not None:
            item = boiler.values.get("8") or {}
            value = item.get("value")
            if isinstance(value, (int, float)) and 3 <= float(value) <= 500:
                return float(value)
        return BOILER_NOMINAL_KW

    def _boiler_kw(self) -> float | None:
        boiler = self._boiler_fresh()
        if boiler is None or boiler.power is None:
            return None
        return boiler.power / 100.0 * self._boiler_nominal_kw()

    def _flow(self) -> float | None:
        value = _float(self.hass, self.entry.options.get(CONF_FLOW_SENSOR))
        if value is None and self._boiler_fresh() is not None:
            value = self.boiler.flow
        return value

    # ------------------------------------------------------------------ Takt

    async def _async_tick(self, now: datetime) -> None:
        if self._lock.locked():
            return  # ein Takt laeuft noch
        try:
            async with self._lock:
                await self._tick(dt_util.utcnow().replace(second=0, microsecond=0))
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Fehler im Steuerungstakt")

    async def _tick(self, now: datetime) -> None:
        data = self.runtime.coordinator.data
        if data is None:
            return
        self._area_map = {}  # Zuordnung zu Bereichen in diesem Takt neu lesen
        await self._refresh_forecast(now)
        await self._refresh_boiler(now)
        self._check_sources(now)

        setback = self.setting(None, "setback")
        outside = self._outside_now()
        flow = self._flow()
        duty = self._duty_cycle()

        # ---- 1: Zustand je Raum
        infos: dict[str, dict[str, Any]] = {}
        for room in data.rooms:
            self.rooms.setdefault(room, RoomState())
            climates, area_id = self._climates(room)
            temp, target_now = self._room_reading(climates)
            valve_sensors = self._valve_sensors(area_id)
            events = sorted(
                (e for e in data.events if room in e.rooms and not e.all_day and e.end > now),
                key=lambda e: e.start,
            )
            infos[room] = {
                "climates": climates,
                "area": area_id,
                "temp": temp,
                "target_now": target_now,
                "valve": self._valve_reading(room, valve_sensors, target_now, temp),
                "valve_count": len(valve_sensors),
                # Boost am Thermostat: Ventil fest offen (meist 80 %), Sollwert unveraendert
                "boost": any(
                    (s := self.hass.states.get(c)) is not None and s.attributes.get("preset_mode") == "boost"
                    for c in climates
                ),
                "comfort": self.setting(room, "comfort"),
                "events": events,
                "current": next((e for e in events if e.start <= now), None),
                "upcoming": next((e for e in events if e.start > now), None),
            }

        # ---- 2: Messpunkt fuers Lernen, Pufferreserve
        if not self._learning_history and (self._sample_at is None or now - self._sample_at >= SAMPLE_INTERVAL):
            self._sample_at = now
            await self._sample(now, infos, outside, flow)

        self._calibration_step(now, infos)

        # ---- 3: planen
        await self._update_plan(now, infos, setback, outside)

        # ---- 4: Sollwerte bestimmen und senden
        for room, info in infos.items():
            state = self.rooms[room]
            mode, desired = self._desired(now, room, state, info, setback)
            info["mode"] = mode
            state.desired, state.mode = desired, mode
            if desired is not None and info["climates"]:
                await self._ensure_manual(now, room, state, info)
                await self._apply(now, room, state, info, desired, setback, duty)
            if mode == "aus" and info["climates"]:
                self._observe(now, room, state, info, setback, outside)
            else:
                self._track_cycle(now, room, state, info, desired, outside)
            state.status = self._room_status(room, state, info, desired)
            if info["target_now"] is not None and info["temp"] is not None:
                state.seen = True

        self._first_tick = False
        self.status = self._global_status(now, outside, flow, duty)
        async_dispatcher_send(self.hass, signal_status(self.entry))

    # ------------------------------------------------------- Messen und Lernen

    async def _sample(
        self, now: datetime, infos: dict[str, dict[str, Any]], outside: float | None, flow: float | None
    ) -> None:
        plant = self.plant_params()
        if self.gauge is None:
            self.gauge = PlantState(plant.e_buf)
        samples: dict[str, RoomSample] = {}
        prior: dict[str, float] = {}
        demand = 0.0
        for room, info in infos.items():
            if not info["climates"] or info["temp"] is None:
                continue
            stats = self.learner.room_stats(room)
            stats.valves = max(1, info["valve_count"])
            samples[room] = RoomSample(
                now,
                info["temp"],
                info["valve"],
                info["target_now"],
                measured=info["valve_count"] > 0,
                forced=info["boost"],
            )
            prior[room] = self.setting(room, "preheat")
            demand += self.room_params(room).w * (info["valve"] or 0.0)
        # Pufferreserve fortschreiben; der Vorlauf korrigiert, wenn bekannt
        self.gauge.e = min(self.gauge.e, plant.e_buf)
        _, share = plant_step(demand, plant, self.gauge, SAMPLE_HOURS)
        measured = flow_share(flow, self.learner.flow_full)
        if measured is not None and measured < 0.95:
            self.gauge.e = min(self.gauge.e, 0.25 * plant.e_buf * measured)
        if not samples:
            return
        fraction = self.gauge.e / plant.e_buf if plant.e_buf > 0 else 0.0
        calibration = self.calibration is not None and self.calibration.running
        episodes = self.learner.plant.episodes
        await self.hass.async_add_executor_job(
            self.learner.add_sample,
            now,
            samples,
            outside,
            fraction,
            prior,
            plant,
            self._boiler_kw(),
            calibration,
            flow,
            share,
        )
        if self.learner.plant.episodes != episodes:
            self._force_plan = True  # neue Parameter
            fit = self.learner.plant.last_fit or {}
            _LOGGER.info("Heizphase ausgewertet: %s", fit)
        retry = self._fit_tried_at is None or now - self._fit_tried_at >= timedelta(hours=1)
        if self.learner.fit_due(now) and not self._fit_running and retry:
            self.hass.async_create_task(self._async_fit(now, outside))
        self._save()

    async def _async_fit(self, now: datetime, outside: float | None) -> None:
        """Gemeinsame Anpassung im Hintergrund rechnen und uebernehmen."""
        if self._fit_running:
            return
        self._fit_running = True
        self._fit_tried_at = now
        learner = self.learner
        try:
            plant = PlantParams(p_max_kw=self._boiler_nominal_kw())
            prior = {room: self.setting(room, "preheat") for room in learner.rooms}
            data = learner.prepare_fit(now, plant, prior)
            # Testlaeufe fuer die kaelteste Stunde der naechsten 24 h bewerten
            ahead = [v for t, v in self._forecast if now <= t <= now + timedelta(hours=24)]
            cold = min(ahead) if ahead else outside
            state = await self.hass.async_add_executor_job(learner.run_fit, data, now, plant, cold)
            if state is None or learner is not self.learner:
                return
            learner.apply_fit(state)
            self._force_plan = True
            self._save()
            _LOGGER.info(
                "Gemeinsame Anpassung: %s Messpunkte, Fehler %.2f K, empfohlener Testlauf %s",
                state["rows"],
                state["rms"],
                (state["experiments"] or [{}])[0].get("label"),
            )
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Gemeinsame Anpassung fehlgeschlagen")
        finally:
            self._fit_running = False

    # ------------------------------------------------- Lernen aus der Historie

    async def async_learn_from_history(self, reset: bool = True) -> ReplayResult | None:
        """Raumtemperaturen, Sollwerte und Ventile aus dem Recorder lesen und lernen.

        reset=True (Dienst): gelernte Raum- und Anlagenwerte verwerfen und neu
        aus der Historie lernen; Sicherheitsfaktoren aus dem Heizprotokoll
        bleiben erhalten. reset=False (erster Start): nur, wenn noch nichts
        gelernt ist.
        """
        from homeassistant.components.recorder import get_instance, history  # noqa: PLC0415

        if not reset and (self.learner.plant.episodes or any(
            stats.cool_windows or stats.heat_windows for stats in self.learner.rooms.values()
        )):
            self._bootstrapped = True
            self._learning_history = False
            self._save()
            return None
        if self._history_running:
            raise HomeAssistantError("Das Lernen aus der Historie läuft bereits.")
        self._history_running = True
        self._learning_history = True
        try:
            data = self.runtime.coordinator.data
            room_names = list(data.rooms) if data else list(self.rooms)
            end = dt_util.utcnow().replace(second=0, microsecond=0)
            start = end - timedelta(days=HISTORY_DAYS)
            sources: dict[str, tuple[list[str], list[str]]] = {}
            for room in room_names:
                climates, area_id = self._climates(room)
                if climates:
                    sources[room] = (climates, self._valve_sensors(area_id))
            outside_id = self.entry.options.get(CONF_OUTSIDE_SENSOR) or self.entry.options.get(
                CONF_WEATHER, DEFAULT_WEATHER
            )
            flow_id = self.entry.options.get(CONF_FLOW_SENSOR) or er.async_get(self.hass).async_get_entity_id(
                "sensor", DOMAIN, f"{self.entry.entry_id}_global_boiler_flow"
            )
            entity_ids = sorted(
                {e for climates, valves in sources.values() for e in climates + valves}
                | {e for e in (outside_id, flow_id) if e}
            )
            if not entity_ids:
                return None
            states = await get_instance(self.hass).async_add_executor_job(
                partial(
                    history.get_significant_states,
                    self.hass,
                    start,
                    end,
                    entity_ids=entity_ids,
                    include_start_time_state=True,
                    significant_changes_only=False,
                    minimal_response=False,
                    no_attributes=False,
                )
            )

            def series(entity_id: str | None, attribute: str | None = None) -> Series:
                result: Series = []
                for state in (states or {}).get(entity_id, []) if entity_id else []:
                    raw = state.attributes.get(attribute) if attribute else state.state
                    try:
                        value = float(raw)
                    except (TypeError, ValueError):
                        continue
                    result.append((state.last_updated, value))
                return result

            def boost_series(entity_id: str) -> Series:
                return [
                    (state.last_updated, 1.0 if state.attributes.get("preset_mode") == "boost" else 0.0)
                    for state in (states or {}).get(entity_id, [])
                ]

            rooms = {
                room: RoomSeries(
                    temps=[series(c, "current_temperature") for c in climates],
                    targets=[series(c, "temperature") for c in climates],
                    valves=[series(v) for v in valves],
                    boosts=[boost_series(c) for c in climates],
                )
                for room, (climates, valves) in sources.items()
            }
            outside = series(outside_id, None if outside_id.startswith("sensor.") else "temperature")
            flow = series(flow_id)
            fresh = Learner()
            old = self.learner
            for room, (_climates, valves) in sources.items():
                stats = fresh.room_stats(room)
                stats.valves = max(1, len(valves))
                previous = old.rooms.get(room)
                if previous is not None:
                    stats.ratios, stats.cycles = list(previous.ratios), previous.cycles
            prior = {room: self.setting(room, "preheat") for room in sources}
            result = await self.hass.async_add_executor_job(
                replay, fresh, rooms, outside, flow, start, end, prior, PlantParams(p_max_kw=self._boiler_nominal_kw())
            )
            self.learner = fresh
            self.gauge = None
            self._sample_at = None
            self._bootstrapped = True
            self._force_plan = True
            if result.samples:
                # alle Werte gemeinsam an die gelesene Historie anpassen
                await self._async_fit(end, None)
            self._save()
            cool = sum(s.cool_windows for s in fresh.rooms.values())
            heat = fresh.plant.episodes
            first = min((p[0][0] for r in rooms.values() for p in r.temps if p), default=None)
            if not result.samples:
                _LOGGER.info("Keine Historie zum Lernen gefunden")
                return result
            self._notify(
                "history",
                "Aus der Historie gelernt",
                f"Ausgewertet: {result.days:.1f} Tage"
                + (f" ab {dt_util.as_local(first).strftime('%d.%m. %H:%M')}" if first else "")
                + f", {cool} Auskühlfenster, {heat} Heizphasen. Die Werte stehen in den Sensoren der Räume.",
            )
            _LOGGER.info("Aus der Historie gelernt: %s Messpunkte, %s Heizphasen", result.samples, heat)
            return result
        finally:
            self._learning_history = False
            self._history_running = False

    # ------------------------------------------------------------ Planen

    def _plan_inputs(self, now: datetime, infos: dict[str, dict[str, Any]], setback: float) -> list[RoomInput]:
        inputs = []
        calibration_rooms = self._calibration_rooms()
        for room, info in infos.items():
            if not info["climates"] or info["temp"] is None:
                continue
            state = self.rooms[room]
            comfort = info["comfort"]
            fixed = None
            calibration = self.calibration
            # gleiche Reihenfolge wie in _desired
            if calibration is not None and calibration.running and self.enabled(room):
                fixed = calibration.target if room in calibration_rooms else calibration.others
            elif not self.controlled(room):
                fixed = info["target_now"] if info["target_now"] is not None else setback
            elif calibration is not None:
                fixed = setback  # wartet auf die Ruhephase vor dem Kalibrierlauf
            elif state.manual_until is not None and state.manual_until > now:
                fixed = info["target_now"]
            # Laeuft das Aufheizen schon, wird es nicht wieder verschoben. Ist
            # der Raum schon warm und der Termin noch fern, darf der Plan ihn
            # wieder abkuehlen lassen (Auskuehlen kostet weniger als Halten).
            committed = None
            if fixed is None and info["current"] is None and info["upcoming"] is not None:
                committed = {"aufheizen": "main", "halten": "warm", "Vorstufe": "pre"}.get(state.mode or "")
                if not state.seen:
                    committed = self._committed_after_restart(info, comfort, setback)
            inputs.append(
                RoomInput(
                    name=room,
                    params=self.room_params(room),
                    temp=info["temp"],
                    comfort=comfort,
                    setback=setback,
                    priority=int(self.setting(room, "priority")),
                    safety=self.learner.safety(room),
                    events=[(e.start, e.end) for e in info["events"]],
                    fixed=fixed,
                    committed=committed,
                    valve=info["valve"],
                )
            )
        return inputs

    def _committed_after_restart(self, info: dict[str, Any], comfort: float, setback: float) -> str | None:
        """Nach einem Neustart: stehen die Gruppen noch auf unserem Sollwert (Modus Manuell)?

        Die Steuerung sendet nur wenige Sollwerte: Komfort (Aufheizen, Halten),
        Komfort minus 3 K (Vorstufe), Absenkung und Absenkung minus 2 K (wartend).
        Daraus laesst sich ablesen, was vor dem Neustart lief. Im Automodus
        stammen die Sollwerte vom Homematic-Profil und zaehlen nicht.
        """
        target = info["target_now"]
        manual = all(
            (state := self.hass.states.get(c)) is not None and state.state == "heat" for c in info["climates"]
        )
        if target is None or not manual:
            return None
        if target >= comfort - SETPOINT_TOLERANCE:
            warm = info["temp"] is not None and info["temp"] >= comfort - REACHED_TOLERANCE
            return "warm" if warm else "main"
        if target > setback + SETPOINT_TOLERANCE:
            return "pre"
        return None

    async def _update_plan(
        self, now: datetime, infos: dict[str, dict[str, Any]], setback: float, outside: float | None
    ) -> None:
        inputs = self._plan_inputs(now, infos, setback)
        self._inputs = {i.name: i for i in inputs}
        plant = self.plant_params()
        state = (self.gauge or PlantState(plant.e_buf)).copy()
        outside_at = self._outside_function(outside if outside is not None else 0.0)
        signature = (
            plan_signature(inputs, now),
            tuple((i.name, round(i.params.b, 1), round(i.params.w, 2), round(i.safety, 2)) for i in inputs),
            round(plant.e_buf, 2),
        )
        replan = (
            self._force_plan
            or self.plan is None
            or signature != self._plan_signature
            or self._plan_at is None
            or now - self._plan_at >= PLAN_MAX_AGE
        )
        if not replan and (self._plan_checked_at is None or now - self._plan_checked_at >= PLAN_CHECK_INTERVAL):
            self._plan_checked_at = now
            valid = await self.hass.async_add_executor_job(
                plan_still_valid, inputs, self.plan, plant, state, outside_at, now
            )
            replan = not valid
            if not valid:
                _LOGGER.debug("Plan geht nicht mehr auf, neu planen")
        if not replan:
            return
        self.plan = await self.hass.async_add_executor_job(plan_heating, inputs, plant, state, outside_at, now)
        self._plan_signature = signature
        self._plan_at = self._plan_checked_at = now
        self._force_plan = False
        for room, plan in self.plan.plans.items():
            if plan.late and self.controlled(room) and self.calibration is None:
                # Nur neu melden, wenn sich die erwartete Zeit um mehr als 15 min aendert
                previous = self._late_notified.get(room)
                reach = plan.reach or plan.deadline + timedelta(days=1)
                if previous is None or abs(reach - previous) > timedelta(minutes=15):
                    self._late_notified[room] = reach
                    self._notify(
                        f"plan_{room}",
                        f"{room} wird voraussichtlich nicht rechtzeitig warm",
                        f"Laut Plan ist {room} erst um {self._clock(plan.reach)} warm "
                        f"(Frist {self._clock(plan.deadline)}). höhere Priorität anderer Räume oder "
                        "die Außentemperatur lassen nicht mehr zu.",
                    )
            elif self._late_notified.pop(room, None) is not None:
                self._dismiss(f"plan_{room}")

    @staticmethod
    def _clock(moment: datetime | None) -> str:
        return dt_util.as_local(moment).strftime("%H:%M") if moment else "nie"

    def _room_plan(self, room: str) -> RoomPlan | None:
        if self.plan is None:
            return None
        return self.plan.plans.get(room)

    # --------------------------------------------------------- Entscheidungen

    def _desired(
        self, now: datetime, room: str, state: RoomState, info: dict[str, Any], setback: float
    ) -> tuple[str, float | None]:
        """(Betriebsart, gewuenschter Sollwert oder None = nicht eingreifen)."""
        if not info["climates"]:
            return "kein Thermostat", None
        calibration = self.calibration
        if calibration is not None and calibration.running and self.enabled(room):
            if room in calibration.rooms:
                return "Kalibrierung", calibration.target
            return "Kalibrierung (andere Räume)", calibration.others
        if not self.controlled(room):
            return "aus", None
        if calibration is not None:
            return "wartet auf Kalibrierung", setback
        if state.manual_until is not None:
            if state.manual_until > now:
                return "Handverstellung", None
            # Handverstellung abgelaufen: den verstellten Wert als bekannt
            # uebernehmen, damit er nicht erneut als Handverstellung gilt.
            state.manual_until = None
            state.last_command = info["target_now"]
            state.last_command_at = None
        comfort = info["comfort"]
        if info["current"] is not None:
            return "Termin", comfort
        room_input = self._inputs.get(room)
        plan = self._room_plan(room)
        if room_input is None or plan is None or self.plan is None:
            return "abgesenkt", setback
        desired = setpoint_at(room_input, plan, now, self.plan.idle)
        if desired >= comfort - SETPOINT_TOLERANCE:
            warm = info["temp"] is not None and info["temp"] >= comfort - REACHED_TOLERANCE
            return ("halten" if warm else "aufheizen"), desired
        if desired > setback + SETPOINT_TOLERANCE:
            return "Vorstufe", desired
        if desired < setback - SETPOINT_TOLERANCE:
            return "abgesenkt (Priorität)", desired
        return "abgesenkt", desired

    async def _ensure_manual(self, now: datetime, room: str, state: RoomState, info: dict[str, Any]) -> None:
        """Gesteuerte Heizgruppen in den Modus "Manuell" bringen (Profil greift nicht)."""
        auto = []
        for entity_id in info["climates"]:
            current = self.hass.states.get(entity_id)
            if current is not None and current.state not in ("heat", STATE_UNAVAILABLE, STATE_UNKNOWN):
                auto.append(entity_id)
        if not auto:
            return
        if state.mode_command_at is not None and now - state.mode_command_at < MIN_COMMAND_GAP:
            return
        state.mode_command_at = now
        try:
            await self.hass.services.async_call(
                "climate", "set_hvac_mode", {"entity_id": auto, "hvac_mode": "heat"}, blocking=True
            )
            self._sent.append(now)
            _LOGGER.info("%s: Heizgruppe auf Manuell gestellt", room)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("%s: Umschalten auf Manuell fehlgeschlagen: %s", room, err)

    async def _apply(
        self,
        now: datetime,
        room: str,
        state: RoomState,
        info: dict[str, Any],
        desired: float,
        setback: float,
        duty: float | None,
    ) -> None:
        """Sollwert senden, wenn noetig. Erkennt Handverstellungen."""
        desired = round(desired * 2) / 2  # Homematic kennt nur halbe Grad
        current = info["target_now"]
        if current is None or info.get("boost"):
            return  # waehrend eines Boosts nichts senden, er endet von selbst
        if abs(current - desired) < SETPOINT_TOLERANCE:
            return
        ours = state.last_command is not None and abs(current - state.last_command) < SETPOINT_TOLERANCE
        profile_reset = abs(current - setback) < SETPOINT_TOLERANCE
        # Unser letzter Befehl ist (noch) nicht angekommen, z. B. weil der
        # Access Point kurz keine Cloud-Verbindung hatte: erneut senden.
        not_arrived = (
            state.before_command is not None
            and state.last_command_at is not None
            and now - state.last_command_at < COMMAND_RETRY_WINDOW
            and abs(current - state.before_command) < SETPOINT_TOLERANCE
        )
        calibrating = self.calibration is not None and self.calibration.running
        # Erster gueltiger Wert seit dem Start oder dem Wiedereinschalten: keine Handverstellung
        fresh = self._first_tick or not state.seen
        if not ours and not profile_reset and not not_arrived and not fresh and not calibrating:
            # Jemand hat am Thermostat oder in der App verstellt.
            event = info["current"] or (info["upcoming"] if info.get("mode") in ("aufheizen", "halten", "Vorstufe") else None)
            state.manual_until = event.end if event is not None else now + MANUAL_HOLD_OUTSIDE_EVENT
            if state.cycle is not None:
                state.cycle.counts = False
            self._force_plan = True
            _LOGGER.info("%s: Handverstellung auf %.1f, gilt bis %s", room, current, state.manual_until)
            return
        if state.last_command_at is not None and now - state.last_command_at < MIN_COMMAND_GAP:
            return
        if duty is not None and duty >= DUTY_CYCLE_LIMIT and desired < current:
            return  # Funkkontingent knapp: Absenken darf warten
        # Eigene Obergrenze, falls der Access Point keinen Duty Cycle meldet.
        self._sent = [t for t in self._sent if now - t < timedelta(hours=1)]
        if len(self._sent) >= MAX_COMMANDS_PER_HOUR and desired < current:
            return
        try:
            await self.hass.services.async_call(
                "climate",
                "set_temperature",
                {"entity_id": info["climates"], "temperature": desired},
                blocking=True,
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("%s: Sollwert %.1f nicht gesendet: %s", room, desired, err)
            state.last_command_at = now  # in MIN_COMMAND_GAP erneut versuchen
            return
        self._sent.append(now)
        state.before_command = current
        state.last_command = desired
        state.last_command_at = now
        _LOGGER.debug("%s: Sollwert %.1f gesendet (%s)", room, desired, info.get("mode"))

    def _duty_cycle(self) -> float | None:
        entity_id = self.entry.options.get(CONF_DUTY_CYCLE_SENSOR)
        if not entity_id:
            # Suche im Register hoechstens stuendlich
            now = dt_util.utcnow()
            if self._duty_lookup_at is None or now - self._duty_lookup_at >= timedelta(hours=1):
                self._duty_lookup_at = now
                self._duty_entity = next(
                    (
                        e.entity_id for e in er.async_get(self.hass).entities.values()
                        if e.platform == "homematicip_cloud" and "duty_cycle" in e.entity_id
                    ),
                    None,
                )
            entity_id = self._duty_entity
        value = _float(self.hass, entity_id)
        if value is not None and value >= DUTY_CYCLE_ALERT:
            self._notify(
                "duty_cycle",
                "Funkkontingent knapp",
                f"Der Duty Cycle des Homematic Access Points liegt bei {value:.0f} %. "
                "Die Steuerung sendet nur noch Aufheiz-Befehle.",
            )
        elif value is not None:
            self._dismiss("duty_cycle")
        return value

    # ------------------------------------------------------- Protokoll

    def _track_cycle(
        self,
        now: datetime,
        room: str,
        state: RoomState,
        info: dict[str, Any],
        desired: float | None,
        outside: float | None,
    ) -> None:
        temp = info["temp"]
        comfort = info["comfort"]
        cycle = state.cycle
        heating = desired is not None and desired >= comfort - SETPOINT_TOLERANCE
        event = info["current"] or info["upcoming"]
        mode = info.get("mode")

        # Ende: Termin vorbei oder keine Komfortphase mehr
        if cycle is not None and (now >= cycle.event_end or (not heating and mode != "Handverstellung")):
            self._close_cycle(now, room, state, "Terminende" if now >= cycle.event_end else "abgebrochen")
            cycle = None

        # Beginn eines neuen Aufheizvorgangs
        if cycle is None and heating and temp is not None and event is not None and mode != "Kalibrierung":
            if temp < comfort - REACHED_TOLERANCE - 0.3:
                plan = self._room_plan(room)
                deadline = event.start - timedelta(minutes=EARLY_MINUTES)
                start = plan.heat_start() if plan and plan.heat_start() else now
                predicted = None
                if plan is not None and plan.reach_nominal is not None:
                    predicted = (plan.reach_nominal - max(start, plan.main or start)).total_seconds() / 60
                state.cycle = Cycle(
                    event=event.summary,
                    calendar=event.calendar,
                    event_start=event.start,
                    event_end=event.end,
                    deadline=deadline,
                    planned_start=start,
                    heat_start=now,
                    temp_start=temp,
                    outside_start=outside,
                    target=comfort,
                    predicted_minutes=predicted,
                    lead_minutes=(deadline - start).total_seconds() / 60,
                    counts=not self._first_tick and state.seen and state.manual_until is None,
                )
            cycle = state.cycle
        if cycle is None or temp is None:
            return

        # Ziel erreicht
        if cycle.reached_at is None and temp >= cycle.target - REACHED_TOLERANCE:
            cycle.reached_at = now
            minutes = (now - cycle.heat_start).total_seconds() / 60
            if cycle.counts and cycle.predicted_minutes:
                self.learner.add_ratio(room, minutes, cycle.predicted_minutes)
            self._save()
            if now > cycle.deadline + timedelta(minutes=5):
                late = round((now - cycle.deadline).total_seconds() / 60)
                self._notify(
                    f"late_{room}",
                    f"{room} zu spät warm",
                    f"{room} hat die Zieltemperatur für „{cycle.event}“ erst "
                    f"{late} min nach der Frist erreicht. Der Sicherheitszuschlag wird angepasst.",
                )

    def _observe(
        self,
        now: datetime,
        room: str,
        state: RoomState,
        info: dict[str, Any],
        setback: float,
        outside: float | None,
    ) -> None:
        """Automatik aus: Aufheizvorgaenge des Homematic-Profils protokollieren.

        Gelernt wird ohnehin aus den 5-Minuten-Messpunkten; hier entsteht nur
        der Eintrag im Heizprotokoll.
        """
        temp = info["temp"]
        target = info["target_now"]
        cycle = state.cycle
        if temp is None or target is None:
            return
        if cycle is not None and (
            not cycle.observed
            or abs(target - cycle.target) >= SETPOINT_TOLERANCE
            or now - cycle.heat_start > timedelta(hours=12)
        ):
            self._close_cycle(now, room, state, "beobachtet" if cycle.observed else "Automatik aus")
            cycle = None
        if cycle is None and target > setback + 0.5 and target - temp >= 1.5:
            state.cycle = cycle = Cycle(
                event="Homematic-Profil",
                calendar="",
                event_start=now,
                event_end=now + timedelta(hours=12),
                deadline=now,
                planned_start=now,
                heat_start=now,
                temp_start=temp,
                outside_start=outside,
                target=target,
                predicted_minutes=None,
                lead_minutes=0,
                counts=False,
                observed=True,
            )
        if cycle is not None and cycle.reached_at is None and temp >= cycle.target - REACHED_TOLERANCE:
            cycle.reached_at = now
            self._save()

    def _close_cycle(self, now: datetime, room: str, state: RoomState, reason: str) -> None:
        cycle = state.cycle
        state.cycle = None
        if cycle is None:
            return
        if cycle.reached_at is None and reason == "Terminende" and not cycle.observed:
            elapsed = (now - cycle.heat_start).total_seconds() / 60
            if cycle.counts and cycle.predicted_minutes and elapsed > cycle.predicted_minutes:
                self.learner.add_ratio(room, elapsed, cycle.predicted_minutes)
            self._notify(
                f"late_{room}",
                f"{room} nicht warm geworden",
                f"{room} hat die Zieltemperatur für „{cycle.event}“ bis zum Terminende nicht erreicht.",
            )
        history = self.history.setdefault(room, [])
        history.insert(0, cycle.record(reason))
        del history[HISTORY_LENGTH:]
        self._save()

    # ----------------------------------------------------------- Ausgaben

    def _room_status(self, room: str, state: RoomState, info: dict[str, Any], desired: float | None) -> dict[str, Any]:
        params = self.room_params(room)
        plant = self.plant_params()
        plan = self._room_plan(room)
        stats = self.learner.room_stats(room)
        upcoming = info["upcoming"]
        lead = None
        if plan is not None and plan.deadline is not None and plan.heat_start() is not None:
            lead = (plan.deadline - plan.heat_start()).total_seconds() / 60
        # Bestfall: der Raum allein, voller Puffer, jetzige Aussentemperatur, ohne Zuschlag
        solo = None
        outside = self._outside_now()
        if info["temp"] is not None and outside is not None:
            solo = solo_minutes(params, plant, t_from=info["temp"], t_to=info["comfort"], outside=outside, limit=24 * 60)
        return {
            "solo": None if solo is None or solo >= 24 * 60 else round(solo),
            "mode": info.get("mode"),
            "demand": desired is not None and desired >= info["comfort"] - SETPOINT_TOLERANCE,
            "temp": info["temp"],
            "desired": desired,
            "valve": None if info["valve"] is None else round(info["valve"] * 100),
            "valve_count": info["valve_count"],
            "lead": lead,
            "planned_start": plan.heat_start() if plan else None,
            "pre_start": plan.pre if plan else None,
            "pre_level": plan.pre_level if plan else None,
            "main_start": plan.main if plan else None,
            "deadline": plan.deadline if plan else None,
            "reach": plan.reach if plan else None,
            "reach_nominal": plan.reach_nominal if plan else None,
            "planned_late": plan.late if plan else False,
            "next_event": upcoming.summary if upcoming else None,
            "next_start": upcoming.start if upcoming else None,
            "climates": info["climates"],
            "area": info["area"],
            "a": params.a,
            "b": params.b,
            "w": params.w,
            "d": params.d,
            "tau": params.tau,
            "boost": info.get("boost", False),
            "sigma": self.learner.sigma(room),
            "curve": params.curve,
            "curve_points": self.learner.curve_points(room),
            "dead_events": stats.dead_events,
            "calibrated": self.learner.calibrated(room),
            "err_idle": stats.err_idle,
            "err_heat": stats.err_heat,
            "err_idle_n": stats.err_idle_n,
            "err_heat_n": stats.err_heat_n,
            "boiler_kw": plant.p_max_kw,
            "safety": self.learner.safety(room),
            "cool_windows": stats.cool_windows,
            "heat_windows": stats.heat_windows,
            "w_measured": stats.sw_w >= 0.8,
            "learned_cycles": stats.cycles,
            "manual_until": state.manual_until,
            "history": self.history.get(room, []),
        }

    def _fit_summary(self) -> dict[str, Any] | None:
        fit = self.learner.fit_state
        if not fit:
            return None
        sigma = fit.get("sigma") or {}

        def percent(key: str) -> int | None:
            value = sigma.get(key)
            return None if value is None else round(100 * (math.exp(value) - 1))

        rooms = {}
        for room, error in (fit.get("rms_rooms") or {}).items():
            d = sigma.get(f"{room}|d")
            rooms[room] = {
                "fehler_k": error,
                "auskuehlrate_prozent": percent(f"{room}|a"),
                "aufheizrate_prozent": percent(f"{room}|b"),
                "totzeit_min": None if d is None else round(d * 60),
                "traegheit_prozent": percent(f"{room}|tau"),
                "leistungsbedarf_prozent": percent(f"{room}|w"),
                "aufheizdauer_prozent": round(100 * (fit.get("duration_sigma") or {}).get(room, 0.0)),
            }
        return {
            "rms": fit.get("rms"),
            "zeitpunkt": _local(datetime.fromtimestamp(fit["time"], tz=dt_util.UTC)) if fit.get("time") else None,
            "rows": fit.get("rows"),
            "residuals": fit.get("residuals"),
            "iterations": fit.get("iterations"),
            "e_buf_spread": percent("|e_buf"),
            "rooms": rooms,
            "experiments": fit.get("experiments"),
            "outside": fit.get("outside"),
        }

    def _global_status(self, now: datetime, outside: float | None, flow: float | None, duty: float | None) -> dict[str, Any]:
        plant = self.plant_params()
        plan_rows = []
        if self.plan is not None:
            for room, plan in sorted(
                self.plan.plans.items(), key=lambda item: item[1].heat_start() or now + timedelta(days=9)
            ):
                if plan.deadline is None:
                    continue
                plan_rows.append(
                    {
                        "raum": room,
                        "vorstufe_ab": _local(plan.pre),
                        "vorstufe_soll": plan.pre_level,
                        "komfort_ab": _local(plan.main),
                        "frist": _local(plan.deadline),
                        "warm_um": _local(plan.reach),
                        "knapp": plan.late,
                    }
                )
        heating = sum(1 for room in self.rooms.values() if room.status.get("demand"))
        return {
            "automatik": self.enabled(None),
            "aussen": outside,
            "vorlauf": flow,
            "kessel_aktuell": self._boiler_fresh() is not None,
            "duty_cycle": duty,
            "heizen": heating,
            "plan": plan_rows,
            "plan_um": _local(self._plan_at),
            "absenkfenster": None
            if self.plan is None or self.plan.idle.start is None
            else [_local(self.plan.idle.start), _local(self.plan.idle.end)],
            "puffer_reserve": None if self.gauge is None or plant.e_buf <= 0 else self.gauge.e / plant.e_buf,
            "puffer_kapazitaet_h": plant.e_buf,
            "kessel_kw": plant.p_max_kw,
            "anlaufzeit_h": plant.t_start,
            "puffer_gemessen": self.learner.plant.e_w >= 1.0,
            "auswertungen": self.learner.plant.episodes,
            "letzte_auswertung": self.learner.plant.last_fit,
            "kalibrierung": None if self.calibration is None else self.calibration.describe(),
            "fit": self._fit_summary(),
            "rows_pending": self.learner.rows_pending,
            "historie_gelernt": self._bootstrapped,
            "lernt_aus_historie": self._learning_history,
            "kalibrierung_status": "kein Lauf"
            if self.calibration is None
            else ("läuft" if self.calibration.running else "wartet auf Ruhe"),
        }

    def _check_sources(self, now: datetime) -> None:
        if self.runtime.coordinator.last_update_success:
            self._source_failed_since = None
            self._dismiss("nextcloud")
            return
        if self._source_failed_since is None:
            self._source_failed_since = now
        if now - self._source_failed_since >= SOURCE_ALERT_AFTER:
            self._notify(
                "nextcloud",
                "Nextcloud nicht erreichbar",
                "Die Raumbuchungen konnten seit über 30 Minuten nicht gelesen werden. "
                "Die Steuerung plant mit dem zuletzt bekannten Stand.",
            )

    def _notify(self, key: str, title: str, message: str) -> None:
        persistent_notification.async_create(
            self.hass, message, title=f"Heizung: {title}", notification_id=f"{DOMAIN}_{key}"
        )

    def _dismiss(self, key: str) -> None:
        persistent_notification.async_dismiss(self.hass, f"{DOMAIN}_{key}")
