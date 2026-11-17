"""Heizungssteuerung: plant, schaltet die Thermostate, misst und lernt.

Ablauf je Takt (jede Minute):
 1. Zustand lesen: Termine, Raumtemperaturen, Sollwerte, Aussen- und
    Vorlauftemperatur, Duty Cycle des Access Points.
 2. Je Raum die noetige Vorheizzeit aus dem gelernten Modell bestimmen.
 3. Aufheizphasen mit dem Lastbudget planen (planner.py).
 4. Je Raum den gewuenschten Sollwert bestimmen und nur bei Abweichung senden.
 5. Aufheizvorgaenge protokollieren und daraus lernen.

Die Thermostate laufen im Homematic-Automodus mit flachem Absenkprofil.
Ein Sollwert von hier gilt dort nur bis zum naechsten Profil-Schaltpunkt.
Faellt Home Assistant aus, kehren die Raeume so von selbst zur
Absenktemperatur zurueck. Setzt das Profil den Sollwert waehrend einer
Heizphase zurueck, sendet die Steuerung ihn erneut.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import logging
from typing import Any, Callable

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .caldav import RoomEvent
from .const import (
    BOILER_ALERT_AFTER,
    BOILER_INTERVAL,
    CONF_STOKERCLOUD,
    CONF_DUTY_CYCLE_SENSOR,
    CONF_FLOW_SENSOR,
    CONF_OUTSIDE_SENSOR,
    CONF_WEATHER,
    CONTROL_INTERVAL,
    DEFAULT_WEATHER,
    DOMAIN,
    DUTY_CYCLE_ALERT,
    DUTY_CYCLE_LIMIT,
    EARLY_MINUTES,
    FLOW_LOW_MINUTES,
    FORECAST_INTERVAL,
    GLOBAL_DEFAULTS,
    HISTORY_LENGTH,
    MANUAL_HOLD_OUTSIDE_EVENT,
    MAX_COMMANDS_PER_HOUR,
    MIN_COMMAND_GAP,
    COMMAND_RETRY_WINDOW,
    PAUSE_MINUTES,
    REACHED_TOLERANCE,
    ROOM_DEFAULTS,
    SAMPLE_WINDOW,
    SETPOINT_TOLERANCE,
    SOURCE_ALERT_AFTER,
)
from .model import RoomModel
from .planner import Job, Plan, plan
from .stokercloud import StokerCloudClient, StokerCloudError, StokerData

_LOGGER = logging.getLogger(__name__)
STORE_VERSION = 1


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
    predicted_minutes: float
    lead_minutes: float
    counts: bool = True  # zaehlt fuers Lernen (nicht bei Handverstellung/Neustart)
    reached_at: datetime | None = None
    paused_minutes: float = 0.0
    observed: bool = False  # Aufheizen durch das Homematic-Profil, nur beobachtet
    window: list[tuple[datetime, float, float]] = field(default_factory=list)

    def record(self, reason: str) -> dict[str, Any]:
        heat_minutes = None
        late = None
        if self.reached_at is not None:
            heat_minutes = round(
                (self.reached_at - self.heat_start).total_seconds() / 60 - self.paused_minutes
            )
            late = round((self.reached_at - self.deadline).total_seconds() / 60)
        if self.observed:
            late = None
        return {
            "termin": self.event,
            "kalender": self.calendar,
            "beginn": dt_util.as_local(self.event_start).isoformat(timespec="minutes"),
            "frist": None if self.observed else dt_util.as_local(self.deadline).isoformat(timespec="minutes"),
            "geplanter_start": dt_util.as_local(self.planned_start).isoformat(timespec="minutes"),
            "heizstart": dt_util.as_local(self.heat_start).isoformat(timespec="minutes"),
            "vorheizzeit_geplant": round(self.lead_minutes),
            "temp_start": round(self.temp_start, 1),
            "aussen_start": None if self.outside_start is None else round(self.outside_start, 1),
            "ziel": self.target,
            "erreicht_um": None
            if self.reached_at is None
            else dt_util.as_local(self.reached_at).isoformat(timespec="minutes"),
            "aufheizdauer": heat_minutes,
            "abweichung_frist": late,  # negativ = frueher warm als noetig
            "pausen_min": round(self.paused_minutes),
            "gewertet": self.counts,
            "abschluss": reason,
        }


@dataclass
class RoomState:
    """Laufzeitzustand eines Raums (nicht gespeichert)."""

    last_command: float | None = None
    last_command_at: datetime | None = None
    before_command: float | None = None  # Sollwert vor unserem letzten Befehl
    manual_until: datetime | None = None
    paused_until: datetime | None = None
    cycle: Cycle | None = None
    status: dict[str, Any] = field(default_factory=dict)


class HeatingController:
    """Steuert alle Raeume eines Eintrags."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, runtime: Any) -> None:
        self.hass = hass
        self.entry = entry
        self.runtime = runtime
        self._store: Store = Store(hass, STORE_VERSION, f"{DOMAIN}.{entry.entry_id}")
        self.models: dict[str, RoomModel] = {}
        self.history: dict[str, list[dict[str, Any]]] = {}
        self.rooms: dict[str, RoomState] = {}
        self.status: dict[str, Any] = {}
        self._forecast: list[tuple[datetime, float]] = []
        self._forecast_at: datetime | None = None
        self._flow_low_since: datetime | None = None
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

    # ------------------------------------------------------------ Lebenszyklus

    async def async_start(self) -> None:
        data = await self._store.async_load() or {}
        self._stored_models = data.get("models", {})
        self.history = data.get("history", {})
        self._unsub = async_track_time_interval(self.hass, self._async_tick, CONTROL_INTERVAL)

    async def async_stop(self) -> None:
        if self._unsub:
            self._unsub()
            self._unsub = None
        await self._store.async_save(self._data_to_store())

    def _data_to_store(self) -> dict[str, Any]:
        return {
            "models": {room: model.to_dict() for room, model in self.models.items()},
            "history": self.history,
        }

    def _save(self) -> None:
        self._store.async_delay_save(self._data_to_store, 30)

    # ------------------------------------------------------------ Einstellungen

    def setting(self, room: str | None, key: str) -> float:
        default = ROOM_DEFAULTS.get(key) if room is not None else GLOBAL_DEFAULTS.get(key)
        return self.runtime.settings.get((room, key), default)

    def enabled(self, room: str | None) -> bool:
        return bool(self.runtime.settings.get((room, "automatic"), 1.0))

    def model(self, room: str) -> RoomModel:
        prior = self.setting(room, "preheat")
        if room not in self.models:
            self.models[room] = RoomModel.from_dict(self._stored_models.get(room, {}), prior)
        self.models[room].prior_minutes = prior
        return self.models[room]

    @callback
    def resume(self, room: str | None) -> None:
        """Automatik wieder an: vorhandene Sollwerte nicht als Handverstellung werten."""
        for name, state in self.rooms.items():
            if room is None or name == room:
                state.manual_until = None
                state.last_command = None
                state.before_command = None
                if state.cycle is not None:
                    state.cycle.counts = False
        self._first_tick = True

    async def async_suspend(self, room: str | None) -> None:
        """Automatik aus: Thermostate sofort wieder ihrem Homematic-Profil ueberlassen.

        Ein Sollwert dieser Steuerung gilt im Automodus nur voruebergehend.
        Das erneute Setzen des Automodus beendet ihn sofort.
        """
        for name, state in self.rooms.items():
            if room is not None and name != room:
                continue
            if state.cycle is not None and not state.cycle.observed:
                self._close_cycle(dt_util.utcnow(), name, state, "Automatik aus")
            climates, _ = self._climates(name)
            if not climates:
                continue
            try:
                await self.hass.services.async_call(
                    "climate", "set_hvac_mode", {"entity_id": climates, "hvac_mode": "auto"}, blocking=True
                )
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("%s: Rueckgabe an das Homematic-Profil fehlgeschlagen: %s", name, err)
            state.last_command = None
            state.before_command = None
            state.manual_until = None

    @callback
    def request_update(self) -> None:
        """Nach Aenderung einer Einstellung sofort neu planen."""
        self.hass.async_create_task(self._async_tick(dt_util.utcnow()))

    # --------------------------------------------------------------- Eingaben

    def _climates(self, room: str) -> tuple[list[str], str | None]:
        """Thermostat-Gruppen im HA-Bereich mit dem Namen des Raums."""
        areas = ar.async_get(self.hass)
        area = areas.async_get_area_by_name(room)
        if area is None:
            return [], None
        entities = er.async_get(self.hass)
        devices = dr.async_get(self.hass)
        result = []
        for entry in entities.entities.values():
            if entry.domain != "climate" or entry.disabled_by is not None:
                continue
            area_id = entry.area_id
            if area_id is None and entry.device_id:
                device = devices.async_get(entry.device_id)
                area_id = device.area_id if device else None
            if area_id == area.id:
                result.append(entry.entity_id)
        return sorted(result), area.id

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

    async def _refresh_boiler(self, now: datetime) -> None:
        """Kesselwerte aus StokerCloud lesen (nur lesend, alle 5 Minuten)."""
        if self._boiler_client is None:
            return
        if self._boiler_at and now - self._boiler_at < BOILER_INTERVAL:
            return
        self._boiler_at = now
        try:
            self.boiler = await self._boiler_client.async_read(
                now, dt_util.get_default_time_zone()
            )
            self._boiler_error = None
        except StokerCloudError as err:
            self._boiler_error = str(err)
            _LOGGER.debug("StokerCloud: %s", err)
        boiler = self.boiler
        last = boiler.updated if boiler else None
        if self._boiler_error or (boiler is not None and boiler.stale):
            since = last or now
            if now - since >= BOILER_ALERT_AFTER or (self._boiler_error and boiler is None):
                when = dt_util.as_local(last).strftime("%d.%m. %H:%M") if last else "unbekannt"
                self._notify(
                    "boiler",
                    "Kessel liefert keine Daten",
                    f"StokerCloud meldet keine aktuellen Werte (letzte Meldung: {when}). "
                    "Die Steuerung plant ohne Vorlauftemperatur nur mit dem Lastbudget.",
                )
        else:
            self._dismiss("boiler")

    def _outside_function(self, start: datetime, fallback: float) -> Callable[[float], float]:
        """Aussentemperatur in 'Minuten ab start' laut Prognose."""
        points = self._forecast

        def at(minutes: float) -> float:
            moment = start + timedelta(minutes=minutes)
            if not points:
                return fallback
            if moment <= points[0][0]:
                return points[0][1] if moment >= points[0][0] - timedelta(hours=1) else fallback
            for (t1, v1), (t2, v2) in zip(points, points[1:]):
                if t1 <= moment <= t2:
                    share = (moment - t1) / (t2 - t1)
                    return v1 + (v2 - v1) * share
            return points[-1][1]

        return at

    # ------------------------------------------------------------------ Takt

    async def _async_tick(self, now: datetime) -> None:
        if self._lock.locked():
            return  # ein Takt laeuft noch
        try:
            async with self._lock:
                await self._tick(dt_util.utcnow())
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Fehler im Steuerungstakt")

    async def _tick(self, now: datetime) -> None:
        coordinator = self.runtime.coordinator
        data = coordinator.data
        if data is None:
            return
        await self._refresh_forecast(now)
        await self._refresh_boiler(now)
        self._check_sources(now)

        global_on = self.enabled(None)
        setback = self.setting(None, "setback")
        budget = self.setting(None, "budget")
        outside = self._outside_now()
        outside_value = outside if outside is not None else 0.0
        flow = _float(self.hass, self.entry.options.get(CONF_FLOW_SENSOR))
        if flow is None and self.boiler is not None and not self.boiler.stale:
            flow = self.boiler.flow
        duty = self._duty_cycle()
        flow_low = self._update_flow(now, flow)

        # ---- 1+2: Zustand je Raum und benoetigte Vorheizzeit
        infos: dict[str, dict[str, Any]] = {}
        jobs: list[Job] = []
        for room in data.rooms:
            state = self.rooms.setdefault(room, RoomState())
            climates, area_id = self._climates(room)
            temp, target_now = self._room_reading(climates)
            comfort = self.setting(room, "comfort")
            events = [
                e for e in data.events if room in e.rooms and not e.all_day and e.end > now
            ]
            current = next((e for e in events if e.start <= now), None)
            upcoming = next((e for e in events if e.start > now), None)
            model = self.model(room)
            lead = None
            predicted = None
            if upcoming is not None and temp is not None:
                deadline = upcoming.start - timedelta(minutes=EARLY_MINUTES)
                guess = model.lead_minutes(temp, comfort, outside_value)
                start_guess = max(now, deadline - timedelta(minutes=guess))
                func = self._outside_function(start_guess, outside_value)
                lead = model.lead_minutes(temp, comfort, func)
                predicted = model.predict_minutes(temp, comfort, func)
            info = {
                "climates": climates,
                "area": area_id,
                "temp": temp,
                "target_now": target_now,
                "comfort": comfort,
                "current": current,
                "upcoming": upcoming,
                "lead": lead,
                "predicted": predicted,
            }
            infos[room] = info

            if not (global_on and self.enabled(room)) or not climates:
                continue
            if upcoming is None or current is not None or temp is None or lead is None:
                continue
            warm = temp >= comfort - REACHED_TOLERANCE
            heating = (
                state.cycle is not None and state.cycle.reached_at is None
            ) or state.status.get("mode") == "aufheizen"
            if warm and not heating:
                continue  # schon warm, wird nur gehalten
            paused = state.paused_until is not None and state.paused_until > now
            jobs.append(
                Job(
                    room=room,
                    deadline=upcoming.start - timedelta(minutes=EARLY_MINUTES),
                    duration=timedelta(minutes=max(lead, 5)),
                    weight=self.setting(room, "weight"),
                    priority=int(self.setting(room, "priority")),
                    running=heating and not paused,
                )
            )

        # ---- Lastpause bei zu niedrigem Vorlauf
        if flow_low:
            self._pause_one(now, jobs)

        # ---- 3: planen
        plans = plan(jobs, now, budget)

        # ---- 4: Sollwerte bestimmen und senden
        for room, info in infos.items():
            state = self.rooms[room]
            mode, desired = self._desired(now, room, state, info, plans.get(room), setback)
            info["mode"] = mode
            if desired is not None and info["climates"]:
                await self._apply(now, room, state, info, desired, setback, duty)
            if mode == "aus" and info["climates"]:
                self._observe(now, room, state, info, setback, outside)
            else:
                self._track_cycle(now, room, state, info, plans.get(room), desired, outside)
            state.status = self._room_status(room, state, info, plans.get(room), desired)

        self._first_tick = False
        self.status = {
            "automatik": global_on,
            "aussen": outside,
            "vorlauf": flow,
            "kessel_aktuell": self.boiler is not None and not self.boiler.stale,
            "vorlauf_niedrig": flow_low,
            "duty_cycle": duty,
            "befehle_letzte_stunde": len(self._sent),
            "budget": budget,
            "plan": [
                {
                    "raum": p.room,
                    "start": dt_util.as_local(p.start).isoformat(timespec="minutes"),
                    "ende": dt_util.as_local(p.end).isoformat(timespec="minutes"),
                    "frist": dt_util.as_local(p.deadline).isoformat(timespec="minutes"),
                    "knapp": p.late,
                }
                for p in sorted(plans.values(), key=lambda p: p.start)
            ],
        }
        async_dispatcher_send(self.hass, signal_status(self.entry))

    # --------------------------------------------------------- Entscheidungen

    def _desired(
        self,
        now: datetime,
        room: str,
        state: RoomState,
        info: dict[str, Any],
        planned: Plan | None,
        setback: float,
    ) -> tuple[str, float | None]:
        """(Betriebsart, gewuenschter Sollwert oder None = nicht eingreifen)."""
        if not (self.enabled(None) and self.enabled(room)):
            return "aus", None
        if not info["climates"]:
            return "kein Thermostat", None
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
        if state.paused_until is not None:
            if state.paused_until > now:
                return "Lastpause", setback
            state.paused_until = None
        cycle = state.cycle
        if cycle is not None and cycle.reached_at is not None and info["upcoming"] is not None:
            return "halten", comfort
        if planned is not None and planned.start <= now:
            return "aufheizen", comfort
        return "abgesenkt", setback

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
        current = info["target_now"]
        if current is None:
            return
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
        if not ours and not profile_reset and not not_arrived and not self._first_tick:
            # Jemand hat am Thermostat oder in der App verstellt.
            event = info["current"] or (info["upcoming"] if info.get("mode") in ("aufheizen", "halten") else None)
            state.manual_until = event.end if event is not None else now + MANUAL_HOLD_OUTSIDE_EVENT
            if state.cycle is not None:
                state.cycle.counts = False
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

    def _pause_one(self, now: datetime, jobs: list[Job]) -> None:
        """Den laufenden Aufheizvorgang mit dem groessten Zeitpuffer pausieren."""
        running = [job for job in jobs if job.running]
        if len(running) < 2:
            return
        slack = lambda job: job.deadline - now - job.duration  # noqa: E731
        job = max(running, key=slack)
        state = self.rooms[job.room]
        state.paused_until = now + timedelta(minutes=PAUSE_MINUTES)
        job.running = False
        self._flow_low_since = now  # naechste Pause fruehestens nach erneuter Wartezeit
        _LOGGER.info("Vorlauf zu niedrig: %s pausiert bis %s", job.room, state.paused_until)

    def _update_flow(self, now: datetime, flow: float | None) -> bool:
        if flow is None or flow >= self.setting(None, "flow_threshold"):
            self._flow_low_since = None
            return False
        if self._flow_low_since is None:
            self._flow_low_since = now
        return now - self._flow_low_since >= timedelta(minutes=FLOW_LOW_MINUTES)

    def _duty_cycle(self) -> float | None:
        entity_id = self.entry.options.get(CONF_DUTY_CYCLE_SENSOR)
        if not entity_id:
            for entry in er.async_get(self.hass).entities.values():
                if entry.platform == "homematicip_cloud" and "duty_cycle" in entry.entity_id:
                    entity_id = entry.entity_id
                    break
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

    # ------------------------------------------------------- Messen und Lernen

    def _track_cycle(
        self,
        now: datetime,
        room: str,
        state: RoomState,
        info: dict[str, Any],
        planned: Plan | None,
        desired: float | None,
        outside: float | None,
    ) -> None:
        temp = info["temp"]
        comfort = info["comfort"]
        cycle = state.cycle
        heating = desired is not None and desired >= comfort - SETPOINT_TOLERANCE
        event = info["current"] or info["upcoming"]

        # Ende: Termin vorbei oder keine Komfortphase mehr
        if cycle is not None and (now >= cycle.event_end or (not heating and info["mode"] != "Lastpause" and info["mode"] != "Handverstellung")):
            self._close_cycle(now, room, state, "Terminende" if now >= cycle.event_end else "abgebrochen")
            cycle = None

        # Beginn eines neuen Aufheizvorgangs
        if cycle is None and heating and temp is not None and event is not None:
            if temp < comfort - REACHED_TOLERANCE - 0.3:
                predicted = info["predicted"] or self.model(room).predict_minutes(temp, comfort, outside or 0.0)
                lead = info["lead"] or predicted
                state.cycle = Cycle(
                    event=event.summary,
                    calendar=event.calendar,
                    event_start=event.start,
                    event_end=event.end,
                    deadline=event.start - timedelta(minutes=EARLY_MINUTES),
                    planned_start=planned.start if planned else now,
                    heat_start=now,
                    temp_start=temp,
                    outside_start=outside,
                    target=comfort,
                    predicted_minutes=predicted,
                    lead_minutes=lead,
                    counts=not self._first_tick and state.manual_until is None,
                )
            cycle = state.cycle
        if cycle is None or temp is None:
            return

        if info["mode"] == "Lastpause":
            cycle.paused_minutes += CONTROL_INTERVAL.total_seconds() / 60
            cycle.window.clear()
            return

        # Messfenster fuer die Aufheizrate, solange voll geheizt wird
        if cycle.reached_at is None and outside is not None and temp < comfort - 1.0:
            cycle.window.append((now, temp, outside))
            first = cycle.window[0]
            if now - first[0] >= SAMPLE_WINDOW:
                hours = (now - first[0]).total_seconds() / 3600
                rate = (temp - first[1]) / hours
                mean_temp = sum(p[1] for p in cycle.window) / len(cycle.window)
                mean_out = sum(p[2] for p in cycle.window) / len(cycle.window)
                if cycle.counts:
                    self.model(room).add_sample(mean_temp - mean_out, rate)
                cycle.window = [cycle.window[-1]]
        elif cycle.window:
            cycle.window.clear()

        # Ziel erreicht
        if cycle.reached_at is None and temp >= cycle.target - REACHED_TOLERANCE:
            cycle.reached_at = now
            minutes = (now - cycle.heat_start).total_seconds() / 60 - cycle.paused_minutes
            if cycle.counts:
                self.model(room).add_ratio(minutes, cycle.predicted_minutes)
            self._save()
            if now > cycle.deadline + timedelta(minutes=5):
                late = round((now - cycle.deadline).total_seconds() / 60)
                self._notify(
                    f"late_{room}",
                    f"{room} zu spät warm",
                    f"{room} hat die Zieltemperatur für „{cycle.event}“ erst "
                    f"{late} min nach der Frist erreicht. Die Vorheizzeit wird angepasst.",
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
        """Automatik aus: Aufheizvorgaenge des Homematic-Profils beobachten und daraus lernen."""
        temp = info["temp"]
        target = info["target_now"]
        cycle = state.cycle
        if temp is None or target is None:
            return
        # Ende: Profil senkt wieder ab oder aendert das Ziel
        if cycle is not None and (
            not cycle.observed or abs(target - cycle.target) >= SETPOINT_TOLERANCE
            or now - cycle.heat_start > timedelta(hours=12)
        ):
            self._close_cycle(now, room, state, "beobachtet" if cycle.observed else "Automatik aus")
            cycle = None
        # Beginn: Profil verlangt deutlich mehr als gemessen
        if cycle is None and target > setback + 0.5 and target - temp >= 1.5:
            model = self.model(room)
            func = self._outside_function(now, outside if outside is not None else 0.0)
            predicted = model.predict_minutes(temp, target, func)
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
                predicted_minutes=predicted,
                lead_minutes=predicted,
                counts=not self._first_tick,
                observed=True,
            )
        if cycle is None:
            return
        if cycle.reached_at is None and outside is not None and temp < cycle.target - 1.0:
            cycle.window.append((now, temp, outside))
            first = cycle.window[0]
            if now - first[0] >= SAMPLE_WINDOW:
                hours = (now - first[0]).total_seconds() / 3600
                rate = (temp - first[1]) / hours
                mean_temp = sum(p[1] for p in cycle.window) / len(cycle.window)
                mean_out = sum(p[2] for p in cycle.window) / len(cycle.window)
                if cycle.counts:
                    self.model(room).add_sample(mean_temp - mean_out, rate)
                cycle.window = [cycle.window[-1]]
        if cycle.reached_at is None and temp >= cycle.target - REACHED_TOLERANCE:
            cycle.reached_at = now
            cycle.window.clear()
            if cycle.counts:
                minutes = (now - cycle.heat_start).total_seconds() / 60
                self.model(room).add_ratio(minutes, cycle.predicted_minutes)
            self._save()

    def _close_cycle(self, now: datetime, room: str, state: RoomState, reason: str) -> None:
        cycle = state.cycle
        state.cycle = None
        if cycle is None:
            return
        if cycle.reached_at is None and cycle.counts and reason == "Terminende" and not cycle.observed:
            # Nicht erreicht: die tatsaechliche Dauer ist mindestens so lang.
            elapsed = (now - cycle.heat_start).total_seconds() / 60 - cycle.paused_minutes
            if elapsed > cycle.predicted_minutes:
                self.model(room).add_ratio(elapsed, cycle.predicted_minutes)
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

    def _room_status(
        self,
        room: str,
        state: RoomState,
        info: dict[str, Any],
        planned: Plan | None,
        desired: float | None,
    ) -> dict[str, Any]:
        model = self.model(room)
        power, loss = model.parameters()
        upcoming: RoomEvent | None = info["upcoming"]
        return {
            "mode": info.get("mode"),
            "demand": desired is not None and desired >= info["comfort"] - SETPOINT_TOLERANCE,
            "temp": info["temp"],
            "desired": desired,
            "lead": info["lead"],
            "planned_start": planned.start if planned else None,
            "planned_late": planned.late if planned else False,
            "next_event": upcoming.summary if upcoming else None,
            "next_start": upcoming.start if upcoming else None,
            "climates": info["climates"],
            "area": info["area"],
            "learned_cycles": model.cycles,
            "margin": round(model.margin_factor(), 2),
            "heat_rate": round(power - loss * 15.0, 2),  # K/h bei 15 K Abstand nach aussen
            "loss": round(loss, 3),
            "manual_until": state.manual_until,
            "history": self.history.get(room, []),
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
