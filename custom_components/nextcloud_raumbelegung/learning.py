"""Parameter laufend aus Messdaten schaetzen (reine Logik, ohne Home Assistant).

Grundlage ist alle 5 Minuten ein Messpunkt je Raum (Temperatur, mittlere
Ventilstellung) und fuer die Anlage (Aussentemperatur, geschaetzte
Pufferreserve, Kesselleistung, falls bekannt). Ausgewertet werden gleitende
Fenster:

- Auskuehlen (Fenster 60 min, alle Ventile des Raums zu, seit mindestens
  30 min): Temperaturabfall / (Raum - aussen) = a.
- Heizphasen (vom Oeffnen der ersten Ventile bis alle wieder zu sind):
  je Raum der Waermegewinn g = (Anstieg + a * (Raum - aussen)) / Ventil in
  15-Minuten-Fenstern. Am Ende der Phase wird das Kessel-/Puffermodell an
  den Verlauf angepasst: Pufferreserve und Leistungsbedarf w werden so
  gewaehlt, dass der berechnete Versorgungsanteil den gemessenen Gewinn am
  besten erklaert; die Aufheizrate b ergibt sich dabei direkt. Heizt ein Raum
  allein, wird sein w bestimmt; heizen mehrere, ihr gemeinsamer Faktor.
- Vorlauftemperatur (StokerCloud), sobald der Kessel Daten liefert: Sie
  zeigt direkt, welchen Anteil ihres Bedarfs die Raeume bekommen. Ein
  Heizkoerper gibt grob proportional zu (Vorlauf - Raum) ab; faellt der
  Vorlauf von 70 auf 45 Grad, bekommen die Raeume nur noch die Haelfte.
  Dieser gemessene Versorgungsanteil fliesst in dieselbe Anpassung ein und
  macht Pufferreserve und Leistungsbedarf viel genauer bestimmbar als die
  Raumtemperaturen allein.
- Kessel-Anlaufzeit und -Leistung aus StokerCloud.
- Vorhersagegüte: Alle 30 Minuten wird der Verlauf der letzten 2 Stunden aus
  dem Startwert mit den gemessenen Ventilstellungen nachgerechnet und mit
  der Messung verglichen. Ein Raum gilt als kalibriert, wenn genug Ruhe- und
  Heizfenster ausgewertet sind und der mittlere Fehler unter 0,5 K liegt;
  bis dahin bleibt der Sicherheitsfaktor bei mindestens 1,25.
- Ventilkennlinie: Ventilstellung ueber der Abweichung Soll minus Ist, je
  Raum aus den gemeldeten Ventilstellungen (ab 10 min nach einer
  Sollwertaenderung), monoton geglaettet. Die Schwellen fuer "Ventil offen"
  gelten relativ zur Hoechstoeffnung des Raums.
- Totzeit: Zeit vom Oeffnen der Ventile (aus der Ruhe) bis die Temperatur um
  RISE Kelvin gestiegen ist, abzueglich der Zeit, die der Anstieg selbst mit
  der bekannten Aufheizrate braucht.

Jeder Wert startet bei einem Startwert, der wie eine Handvoll Messungen
zaehlt; aeltere Messungen verlieren langsam an Gewicht (Halbwertszeit etwa
zwei Wochen). Unplausible Werte werden verworfen.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
import math
import threading
from typing import Any

from . import estimation

from .physics import (
    CURVE_DEV,
    PRIOR_SHAPE,
    Delay,
    PlantParams,
    PlantState,
    RoomParams,
    b_for_minutes,
    plant_step,
    room_step,
    solo_minutes,
)

SAMPLE = timedelta(minutes=5)
DECAY = 0.9998  # je Messpunkt, Halbwertszeit ~ 12 Tage
COOL_WINDOW = 12  # Messpunkte (60 min)
HEAT_WINDOW = 3  # Messpunkte (15 min)
VALVE_CLOSED = 0.03
# Schwellen fuer "offen" gelten relativ zur Hoechstoeffnung des Raums (Kennlinie):
# die Homematic-Thermostate oeffnen je nach Heizkoerper nur bis 50..90 %.
VALVE_OPEN = 0.7
VALVE_MIN_GAIN = 0.1  # darunter ist der Gewinn je Ventiloeffnung zu unsicher
CURVE_SETTLE = timedelta(minutes=10)  # nach einer Sollwertaenderung erst so lange warten
CURVE_RISE = 1.0  # K: Sollwertsprung, ab dem ein Aufheizvorgang beginnt
CURVE_AFTER = timedelta(minutes=30)  # nach Erreichen des Sollwerts noch so lange mitschreiben
CURVE_MIN = 3.0  # Messpunkte, ab denen eine Stuetzstelle als gemessen gilt
LAMBDA_C = 1.0  # Startwert der Kennlinie zaehlt wie ein Messpunkt

PRIOR_A = 0.05
PRIOR_W_SINGLE = 0.3  # Raum mit einem Thermostat
PRIOR_W_PER_VALVE = 0.3
LAMBDA_A = 10 * 15.0**2  # Startwert zaehlt wie 10 Fenster bei 15 K Differenz
LAMBDA_B = 1.0  # Startwert zaehlt wie ein gemessener Aufheizvorgang
LAMBDA_W = 1.0
LAMBDA_E = 1.0
MIN_EPISODE_SAMPLES = 12  # mindestens 1 h Gewinnmessung
MAX_EPISODE = timedelta(hours=8)
LAMBDA_T = 1.0
PRIOR_D = 0.15  # Totzeit-Startwert (h)
LAMBDA_D = 2.0  # Startwert zaehlt wie zwei gemessene Heizbeginne
DEAD_MAX = 1.5  # groesste plausible Totzeit (h)
RISE = 0.3  # Anstieg (K), ab dem die Waerme als angekommen gilt
CHECK_SPAN = 24  # Messpunkte (2 h) je Vorhersagepruefung
CHECK_EVERY = 6  # alle 30 min pruefen
CHECK_ALPHA = 0.1  # Gewicht einer neuen Pruefung im gleitenden Mittel
CALIBRATED_ERROR = 0.5  # K
CALIBRATED_IDLE = 24  # Ruhefenster (12 h)
CALIBRATED_HEAT = 8  # Heizfenster (4 h)
SAFETY_UNCALIBRATED = 1.25
FLOW_ROOM = 20.0  # Bezug fuer die Heizkoerperleistung (Grad)
FLOW_MIN_SPAN = 15.0  # Vorlauf bei vollem Puffer muss so weit ueber FLOW_ROOM liegen
FLOW_WEIGHT = 2.0  # Gewicht des Vorlaufs gegenueber den Raumtemperaturen
FLOW_DECAY = 0.02  # K je Messpunkt (~6 K am Tag): so schnell vergisst der Bezug "Puffer voll"
FIT_MIN_ROWS = 72  # gemeinsame Anpassung ab 6 h neuer Messpunkte
FIT_EVERY = timedelta(hours=20)
FIT_QUIET = timedelta(hours=2)  # nur in Ruhe, damit keine Heizphase zerschnitten wird
ROWS_MAX = 10 * 288  # hoechstens 10 Tage Messpunkte bis zur naechsten Anpassung
Z_95 = 1.65  # einseitiges 95-%-Quantil der Normalverteilung


def flow_share(flow: float | None, flow_full: float | None) -> float | None:
    """Gemessener Versorgungsanteil aus dem Vorlauf (1 = wie bei vollem Puffer)."""
    if flow is None or flow_full is None or flow_full - FLOW_ROOM < FLOW_MIN_SPAN:
        return None
    return min(max((flow - FLOW_ROOM) / (flow_full - FLOW_ROOM), 0.0), 1.0)


@dataclass
class RoomSample:
    time: datetime
    temp: float | None
    valve: float | None  # 0..1
    setpoint: float | None
    measured: bool = True  # Ventilstellung gemessen (nicht aus Soll/Ist geschaetzt)


@dataclass
class RoomStats:
    """Gesammelte Statistik eines Raums."""

    sxx: float = 0.0
    sxy: float = 0.0
    cool_windows: int = 0
    sw_b: float = 0.0
    swb_b: float = 0.0
    heat_windows: int = 0
    sw_w: float = 0.0
    sww_w: float = 0.0
    ratios: list[float] = field(default_factory=list)  # tatsaechlich / vorhergesagt
    cycles: int = 0
    valves: int = 1  # Anzahl Heizkoerperventile (fuer den Startwert von w)
    sw_d: float = 0.0
    swd_d: float = 0.0
    dead_events: int = 0
    err_idle: float | None = None  # mittlerer Vorhersagefehler (K) ohne Heizen
    err_idle_n: int = 0
    err_heat: float | None = None  # ... mit Heizen
    err_heat_n: int = 0
    # Ventilkennlinie: gewichtete Summen der Ventilstellung je Stuetzstelle CURVE_DEV
    cv: list[float] = field(default_factory=lambda: [0.0] * len(CURVE_DEV))
    cw: list[float] = field(default_factory=lambda: [0.0] * len(CURVE_DEV))

    def to_dict(self) -> dict:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, data: dict) -> RoomStats:
        stats = cls()
        for key, value in data.items():
            if hasattr(stats, key):
                setattr(stats, key, value)
        if len(stats.cv) != len(CURVE_DEV) or len(stats.cw) != len(CURVE_DEV):
            stats.cv, stats.cw = [0.0] * len(CURVE_DEV), [0.0] * len(CURVE_DEV)
        return stats


def top_opening(curve: tuple[float, ...] | None) -> float:
    """Hoechstoeffnung laut Kennlinie (Bezug fuer die Schwellen "offen")."""
    return max(max(curve or PRIOR_SHAPE), 0.1)


def _isotonic(values: list[float], weights: list[float]) -> list[float]:
    """Gewichtete monotone (nicht fallende) Glaettung, Pool-Adjacent-Violators."""
    blocks: list[list[float]] = []  # [Mittel, Gewicht, Anzahl]
    for value, weight in zip(values, weights):
        blocks.append([value, weight, 1])
        while len(blocks) > 1 and blocks[-2][0] > blocks[-1][0]:
            v2, w2, n2 = blocks.pop()
            v1, w1, n1 = blocks.pop()
            blocks.append([(v1 * w1 + v2 * w2) / (w1 + w2), w1 + w2, n1 + n2])
    result: list[float] = []
    for value, _, count in blocks:
        result += [value] * count
    return result


@dataclass
class PlantStats:
    """Gesammelte Statistik von Kessel und Puffer."""

    e_sum: float = 0.0
    e_w: float = 0.0
    t_sum: float = 0.0
    t_w: float = 0.0
    p_max_kw: float | None = None
    episodes: int = 0  # ausgewertete Heizphasen
    last_fit: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, data: dict) -> PlantStats:
        stats = cls()
        for key, value in data.items():
            if hasattr(stats, key):
                setattr(stats, key, value)
        return stats


class Learner:
    """Haelt Statistik und liefert die aktuellen Parameter."""

    def __init__(self, data: dict | None = None) -> None:
        data = data or {}
        self.rooms: dict[str, RoomStats] = {
            name: RoomStats.from_dict(value) for name, value in data.get("rooms", {}).items()
        }
        self.plant = PlantStats.from_dict(data.get("plant", {}))
        self._windows: dict[str, deque[RoomSample]] = {}
        self._outside: deque[tuple[datetime, float | None, float]] = deque(maxlen=COOL_WINDOW + 1)
        self._closed_since: dict[str, datetime | None] = {}
        self._open_since: dict[str, datetime | None] = {}
        self._dead: dict[str, tuple[datetime, float]] = {}  # laufende Totzeitmessung: (Oeffnung, Temperatur)
        # Verlauf je Raum fuer die Vorhersagepruefung: (Temp, Ventil, aussen, Versorgungsanteil)
        self._track: dict[str, deque[tuple[float | None, float | None, float | None, float]]] = {}
        self._track_count = 0
        self._episode: dict[str, Any] | None = None
        self._last_episode_end: datetime | None = None
        self._flow_full: float | None = data.get("flow_full")
        # Messpunkte seit der letzten gemeinsamen Anpassung und deren Ergebnis
        self._rows: list[dict[str, Any]] = list(data.get("rows") or [])
        self.fit_state: dict[str, Any] | None = data.get("fit")
        self._rows_lock = threading.Lock()
        self._quiet_since: datetime | None = None  # seit wann kein Raum aufheizt (Ventile < VALVE_OPEN)
        # Sollwert, seit wann, ob es ein Aufheizvorgang ist, wann der Sollwert erreicht war
        self._setpoint: dict[str, tuple[float, datetime, bool, datetime | None]] = {}

    def to_dict(self) -> dict:
        with self._rows_lock:
            rows = list(self._rows)
        return {
            "rooms": {name: stats.to_dict() for name, stats in self.rooms.items()},
            "plant": self.plant.to_dict(),
            "flow_full": self._flow_full,
            "rows": rows,
            "fit": self.fit_state,
        }

    # ------------------------------------------------------------ Parameter

    def room_stats(self, room: str) -> RoomStats:
        return self.rooms.setdefault(room, RoomStats())

    def room_params(self, room: str, prior_minutes: float) -> RoomParams:
        fitted = self._fitted(room)
        if fitted is not None:
            return replace(fitted, curve=self.curve(room))
        return self._single_params(room, prior_minutes)

    def _fitted(self, room: str) -> RoomParams | None:
        """Werte aus der gemeinsamen Anpassung, falls der Raum dabei war."""
        values = (self.fit_state or {}).get("values") or {}
        keys = [f"{room}|{k}" for k in ("a", "b", "d", "w")]
        if not all(k in values for k in keys):
            return None
        a, b, d, w = (values[k] for k in keys)
        return RoomParams(a=a, b=b, w=w, d=d)

    def _single_params(self, room: str, prior_minutes: float) -> RoomParams:
        """Werte aus den Einzelschaetzungen (Fenster je Groesse); Vorwissen der ersten Anpassung."""
        stats = self.room_stats(room)
        a = (stats.sxy + LAMBDA_A * PRIOR_A) / (stats.sxx + LAMBDA_A)
        a = min(max(a, 0.005), 0.5)
        w = (stats.sww_w + LAMBDA_W * self._w_prior(room)) / (stats.sw_w + LAMBDA_W)
        w = min(max(w, 0.02), 5.0)
        d = (stats.swd_d + LAMBDA_D * PRIOR_D) / (stats.sw_d + LAMBDA_D)
        d = min(max(d, 0.0), DEAD_MAX)
        curve = self.curve(room)
        b0 = b_for_minutes(prior_minutes, a, w, self.plant_params(PlantParams()), d, curve)
        b = (stats.swb_b + LAMBDA_B * b0) / (stats.sw_b + LAMBDA_B)
        b = min(max(b, 0.1), 60.0)
        return RoomParams(a=a, b=b, w=w, d=d, curve=curve)

    def curve(self, room: str) -> tuple[float, ...]:
        """Gelernte Ventilkennlinie (Ventilstellung 0..1 an den Stuetzstellen CURVE_DEV).

        Stuetzstellen ohne Messung folgen der Startform, skaliert auf die
        hoechste gemessene Oeffnung beim Aufheizen (ab 1,5 K unter Soll). Die
        Kennlinie wird monoton geglaettet: mehr Abstand zum Sollwert heisst nie
        weniger Oeffnung.
        """
        stats = self.room_stats(room)
        raw = [cv / cw if cw >= CURVE_MIN else None for cv, cw in zip(stats.cv, stats.cw)]
        high = [v for x, v in zip(CURVE_DEV, raw) if v is not None and x >= 1.5]
        scale = max(high) if high else 1.0
        values = [
            (cv + LAMBDA_C * scale * prior) / (cw + LAMBDA_C)
            for cv, cw, prior in zip(stats.cv, stats.cw, PRIOR_SHAPE)
        ]
        weights = [cw + LAMBDA_C for cw in stats.cw]
        return tuple(round(min(max(v, 0.0), 1.0), 3) for v in _isotonic(values, weights))

    def curve_points(self, room: str) -> int:
        """Anzahl der Stuetzstellen mit eigenen Messungen."""
        return sum(1 for cw in self.room_stats(room).cw if cw >= CURVE_MIN)

    def _w_prior(self, room: str) -> float:
        """Startwert fuer w je Einheit Ventilstellung: bei der Hoechstoeffnung des
        Raums zieht jeder Heizkoerper etwa PRIOR_W_PER_VALVE der Kesselleistung."""
        valves = self.room_stats(room).valves
        at_top = PRIOR_W_SINGLE if valves <= 1 else PRIOR_W_PER_VALVE * valves
        return at_top / top_opening(self.curve(room))

    def plant_params(self, base: PlantParams) -> PlantParams:
        e_buf = (self.plant.e_sum + LAMBDA_E * base.e_buf) / (self.plant.e_w + LAMBDA_E)
        fitted = ((self.fit_state or {}).get("values") or {}).get("|e_buf")
        if fitted is not None:
            e_buf = fitted
        t_start = (self.plant.t_sum + LAMBDA_T * base.t_start) / (self.plant.t_w + LAMBDA_T)
        return PlantParams(
            e_buf=min(max(e_buf, 0.05), 5.0),
            t_start=min(max(t_start, 0.0), 2.0),
            p_max_kw=self.plant.p_max_kw or base.p_max_kw,
        )

    def safety(self, room: str) -> float:
        """Sicherheitsfaktor auf die Aufheizdauer.

        Mit gemeinsamer Anpassung: das Groessere aus der Unsicherheit der
        Aufheizdauer (95 %, aus der Kovarianz von a, b, d) und dem
        95-%-Quantil von tatsaechlich/vorhergesagt der Heizvorgaenge.
        Vor der ersten Anpassung: das Quantil, solange das Modell nicht
        kalibriert ist mindestens 1,25.
        """
        ratios = self.room_stats(room).ratios
        empirical = None
        if len(ratios) >= 5:
            ordered = sorted(ratios)
            index = min(len(ordered) - 1, math.ceil(0.95 * len(ordered)) - 1)
            empirical = ordered[index]
        spread = ((self.fit_state or {}).get("duration_sigma") or {}).get(room)
        if spread is not None:
            value = max(empirical or 1.0, 1.0 + Z_95 * spread)
            return min(max(value, 1.0), 2.0)
        value = SAFETY_UNCALIBRATED if empirical is None else min(max(empirical, 1.0), 2.0)
        if not self.calibrated(room):
            value = max(value, SAFETY_UNCALIBRATED)
        return value

    def calibrated(self, room: str) -> bool:
        """Sagt das Modell den Raum genau genug voraus?"""
        stats = self.room_stats(room)
        return (
            stats.err_idle_n >= CALIBRATED_IDLE
            and stats.err_heat_n >= CALIBRATED_HEAT
            and stats.err_idle is not None and stats.err_idle < CALIBRATED_ERROR
            and stats.err_heat is not None and stats.err_heat < CALIBRATED_ERROR
        )

    def add_ratio(self, room: str, actual_minutes: float, predicted_minutes: float) -> None:
        if predicted_minutes < 10 or actual_minutes <= 0:
            return
        stats = self.room_stats(room)
        stats.ratios.append(round(actual_minutes / predicted_minutes, 3))
        del stats.ratios[:-30]
        stats.cycles += 1

    def quiet_for(self, now: datetime) -> timedelta:
        """Wie lange schon kein Raum aufheizt (0, solange einer aufheizt).

        Warmhalten auf der Absenkung zaehlt als Ruhe: dabei laedt der Kessel den
        Puffer wieder voll.
        """
        if self._quiet_since is None or self._episode is not None:
            return timedelta(0)
        return now - self._quiet_since

    @property
    def flow_full(self) -> float | None:
        """Vorlauf bei vollem Puffer (Bezug fuer den gemessenen Versorgungsanteil)."""
        return self._flow_full

    @property
    def episode_running(self) -> bool:
        return self._episode is not None

    # -------------------------------------------------------------- Messen

    def add_sample(
        self,
        now: datetime,
        rooms: dict[str, RoomSample],
        outside: float | None,
        buffer_fraction: float,
        prior_minutes: dict[str, float],
        plant: PlantParams,
        boiler_kw: float | None = None,
        calibration: bool = False,
        flow: float | None = None,
        share: float | None = None,
    ) -> None:
        """Einen 5-Minuten-Messpunkt verarbeiten.

        share: geschaetzter Versorgungsanteil 0..1 in diesem Messintervall
        (aus der Pufferschaetzung der Steuerung), fuer die Vorhersagepruefung.

        calibration: Kalibrierlauf; er beginnt aus der Ruhe, der Puffer gilt
        beim Start als voll.
        """
        self._outside.append((now, outside, buffer_fraction))
        tops = {name: top_opening(self.curve(name)) for name in rooms}
        if all((sample.valve or 0.0) < VALVE_OPEN * tops[name] for name, sample in rooms.items()):
            self._quiet_since = self._quiet_since or now
        else:
            self._quiet_since = None
        if boiler_kw is not None and boiler_kw > 0:
            current = self.plant.p_max_kw or 0.0
            self.plant.p_max_kw = max(current * 0.999, boiler_kw)  # langsam vergessende Spitze
        gains: dict[str, float | None] = {}
        for name, sample in rooms.items():
            stats = self.room_stats(name)
            for key in ("sxx", "sxy", "sw_b", "swb_b", "sw_w", "sww_w", "sw_d", "swd_d"):
                setattr(stats, key, getattr(stats, key) * DECAY)
            stats.cv = [v * DECAY for v in stats.cv]
            stats.cw = [v * DECAY for v in stats.cw]
            window = self._windows.setdefault(name, deque(maxlen=COOL_WINDOW + 1))
            window.append(sample)
            self._curve_sample(name, stats, sample, now)
            if sample.valve is not None:
                if sample.valve < VALVE_CLOSED:
                    if self._closed_since.get(name) is None:
                        self._closed_since[name] = now
                else:
                    self._closed_since[name] = None
                if sample.valve >= VALVE_OPEN * tops[name]:
                    if self._open_since.get(name) is None:
                        self._open_since[name] = now
                else:
                    self._open_since[name] = None
            params = self.room_params(name, prior_minutes.get(name, 120.0))
            track = self._track.setdefault(name, deque(maxlen=CHECK_SPAN + 1))
            track.append((sample.temp, sample.valve, outside, 1.0 if share is None else share))
            if self._track_count % CHECK_EVERY == 0:
                self._check(stats, track, params)
            self._cooling(name, stats, window)
            self._deadtime(name, stats, window, params, outside)
            gains[name] = self._gain(window, params, self._open_since.get(name))
        self._track_count += 1
        self._store_row(now, rooms, outside, flow)
        self._episode_step(
            now, rooms, gains, buffer_fraction, boiler_kw, plant, prior_minutes, calibration, flow, tops
        )

    # ------------------------------------------------- gemeinsame Anpassung

    def _store_row(
        self, now: datetime, rooms: dict[str, RoomSample], outside: float | None, flow: float | None
    ) -> None:
        row = {
            "t": now.timestamp(),
            "o": None if outside is None else round(outside, 1),
            "f": None if flow is None else round(flow, 1),
            "r": {
                name: [
                    round(s.temp, 2),
                    None if s.valve is None or not s.measured else round(s.valve, 3),
                    None if s.setpoint is None else round(s.setpoint, 1),
                ]
                for name, s in rooms.items()
                if s.temp is not None
            },
        }
        with self._rows_lock:
            self._rows.append(row)
            del self._rows[:-ROWS_MAX]

    def sigma(self, room: str) -> dict[str, float] | None:
        """Unsicherheit der angepassten Werte eines Raums (log bzw. Stunden bei d)."""
        values = (self.fit_state or {}).get("sigma") or {}
        result = {k: values[f"{room}|{k}"] for k in ("a", "b", "d", "w") if f"{room}|{k}" in values}
        return result or None

    @property
    def rows_pending(self) -> int:
        return len(self._rows)

    def fit_due(self, now: datetime) -> bool:
        """Ist eine gemeinsame Anpassung faellig (genug neue Messpunkte, Ruhe)?"""
        if len(self._rows) < FIT_MIN_ROWS or self._episode is not None:
            return False
        if self.quiet_for(now) < FIT_QUIET:
            return False
        last = (self.fit_state or {}).get("time")
        return last is None or now.timestamp() - last >= FIT_EVERY.total_seconds()

    def prepare_fit(self, now: datetime, plant_base: PlantParams, prior_minutes: dict[str, float]) -> estimation.FitInput:
        """Momentaufnahme fuer die Anpassung (kann dann im Hintergrund laufen)."""
        with self._rows_lock:
            rows = list(self._rows)
        names = sorted({room for row in rows for room in row["r"]})
        prior_mean: dict[estimation.Name, float] = {}
        for room in names:
            params = self.room_params(room, prior_minutes.get(room, 120.0))
            for key in ("a", "b", "d", "w"):
                prior_mean[(room, key)] = getattr(params, key)
        prior_mean[(None, "e_buf")] = self.plant_params(plant_base).e_buf
        prior_info = None
        if self.fit_state and self.fit_state.get("info"):
            days = (now.timestamp() - self.fit_state["time"]) / 86400
            prior_info = estimation.forget(_info_from_json(self.fit_state["info"]), days)
        return estimation.FitInput(
            rows=rows,
            prior_mean=prior_mean,
            prior_info=prior_info,
            t_start=self.plant_params(plant_base).t_start,
            flow_full=self._flow_full,
            flow_room=FLOW_ROOM,
            flow_min_span=FLOW_MIN_SPAN,
        )

    def run_fit(
        self, data: estimation.FitInput, now: datetime, plant_base: PlantParams, outside: float | None
    ) -> dict[str, Any] | None:
        """Anpassung rechnen (ohne den Learner zu veraendern); Ergebnis fuer apply_fit."""
        result = estimation.fit(data)
        if result is None:
            return None
        curves = {room: self.curve(room) for room, _ in result.names if room is not None}
        plant = PlantParams(e_buf=result.values[(None, "e_buf")], t_start=data.t_start)
        spread = {}
        for room in curves:
            params = RoomParams(
                a=result.values[(room, "a")],
                b=result.values[(room, "b")],
                w=result.values[(room, "w")],
                d=result.values[(room, "d")],
                curve=curves[room],
            )
            spread[room] = round(
                estimation.duration_uncertainty(
                    result, room, params, plant, lambda p: solo_minutes(p, plant, limit=12 * 60)
                ),
                3,
            )
        fitted_rooms = {
            room: RoomParams(
                a=result.values[(room, "a")],
                b=result.values[(room, "b")],
                w=result.values[(room, "w")],
                d=result.values[(room, "d")],
                curve=curves[room],
            )
            for room in curves
        }
        experiments = estimation.rank_experiments(
            result,
            curves,
            data.t_start,
            estimation.candidates_for(fitted_rooms),
            outside if outside is not None else 0.0,
        )
        return {
            "time": now.timestamp(),
            "values": {_key(name): round(value, 5) for name, value in result.values.items()},
            "sigma": {_key(name): round(value, 4) for name, value in result.sigma.items()},
            "info": _info_to_json(result.info),
            "rms": round(result.rms, 3),
            "rms_rooms": {room: round(v, 3) for room, v in result.rms_rooms.items()},
            "residuals": result.residuals,
            "iterations": result.iterations,
            "rows": len(data.rows),
            "start": result.start,
            "end": result.end,
            "duration_sigma": spread,
            "outside": outside,
            "experiments": experiments,
        }

    def apply_fit(self, state: dict[str, Any]) -> None:
        """Ergebnis uebernehmen; die verwendeten Messpunkte sind damit verbraucht."""
        self.fit_state = state
        with self._rows_lock:
            self._rows = [row for row in self._rows if row["t"] > state["end"]]

    def _curve_sample(self, name: str, stats: RoomStats, sample: RoomSample, now: datetime) -> None:
        """Ventilstellung zur Abweichung Soll minus Ist in die Kennlinie eintragen.

        Nur beim Aufheizen: nach einem Sollwertsprung um mindestens CURVE_RISE
        bis CURVE_AFTER nach dem Erreichen des Sollwerts. Beim Halten haengt die
        Ventilstellung vom Waermebedarf ab (I-Anteil), das beschreibt die
        Halte-Oeffnung im Modell. Nur gemessene Ventilstellungen und erst
        CURVE_SETTLE nach der Aenderung (Stellmotoren und Meldungen sind langsam).
        """
        if sample.setpoint is None or sample.temp is None or sample.valve is None:
            self._setpoint.pop(name, None)
            return
        last = self._setpoint.get(name)
        if last is None or abs(last[0] - sample.setpoint) > 0.05:
            rising = last is not None and sample.setpoint - last[0] >= CURVE_RISE
            self._setpoint[name] = (sample.setpoint, now, rising, None)
            return
        setpoint, since, rising, reached = last
        if reached is None and sample.temp >= setpoint:
            reached = now
            self._setpoint[name] = (setpoint, since, rising, reached)
        if not rising or not sample.measured or now - since < CURVE_SETTLE:
            return
        if reached is not None and now - reached > CURVE_AFTER:
            return
        dev = sample.setpoint - sample.temp
        if dev <= CURVE_DEV[0]:
            parts = [(0, 1.0)]
        elif dev >= CURVE_DEV[-1]:
            parts = [(len(CURVE_DEV) - 1, 1.0)]
        else:
            i = next(k for k in range(len(CURVE_DEV) - 1) if CURVE_DEV[k] <= dev < CURVE_DEV[k + 1])
            f = (dev - CURVE_DEV[i]) / (CURVE_DEV[i + 1] - CURVE_DEV[i])
            parts = [(i, 1.0 - f), (i + 1, f)]
        for index, weight in parts:
            stats.cv[index] += weight * sample.valve
            stats.cw[index] += weight

    def _check(self, stats: RoomStats, track: deque, params: RoomParams) -> None:
        """Die letzten 2 Stunden aus dem Startwert nachrechnen und mit der Messung vergleichen."""
        if len(track) < CHECK_SPAN + 1:
            return
        if any(t is None or v is None or o is None for t, v, o, _ in track):
            return
        hours = SAMPLE.total_seconds() / 3600
        first = track[0]
        temp = first[0]
        delay = Delay(params.d, hours, first[1] * first[3])
        for t, v, o, s in list(track)[:-1]:
            temp = room_step(temp, params, delay.push(v * s), o, hours)
        error = abs(temp - track[-1][0])
        if any(v >= 0.3 * top_opening(params.curve) for _, v, _, _ in track):
            stats.err_heat = error if stats.err_heat is None else (1 - CHECK_ALPHA) * stats.err_heat + CHECK_ALPHA * error
            stats.err_heat_n += 1
        else:
            stats.err_idle = error if stats.err_idle is None else (1 - CHECK_ALPHA) * stats.err_idle + CHECK_ALPHA * error
            stats.err_idle_n += 1

    def _outside_mean(self, start: datetime, end: datetime) -> float | None:
        values = [o for t, o, _ in self._outside if start <= t <= end and o is not None]
        return sum(values) / len(values) if values else None

    def _buffer_min(self, start: datetime, end: datetime) -> float:
        values = [b for t, _, b in self._outside if start <= t <= end]
        return min(values) if values else 0.0

    def _cooling(self, name: str, stats: RoomStats, window: deque[RoomSample]) -> None:
        if len(window) < COOL_WINDOW + 1:
            return
        first, last = window[0], window[-1]
        closed = self._closed_since.get(name)
        if closed is None or closed > first.time - timedelta(minutes=30):
            return
        if any(s.temp is None or s.valve is None or s.valve >= VALVE_CLOSED for s in window):
            return
        out = self._outside_mean(first.time, last.time)
        if out is None:
            return
        mean_temp = sum(s.temp for s in window) / len(window)
        x = mean_temp - out
        if x < 3.0:
            return
        hours = (last.time - first.time).total_seconds() / 3600
        y = -(last.temp - first.temp) / hours
        if not (-0.5 < y / x < 0.6):
            return
        stats.sxx += x * x
        stats.sxy += x * y
        stats.cool_windows += 1

    def _deadtime(
        self, name: str, stats: RoomStats, window: deque[RoomSample], params: RoomParams, outside: float | None
    ) -> None:
        """Totzeit messen: Ventile oeffnen aus der Ruhe, bis die Temperatur steigt."""
        if len(window) < 2:
            return
        previous, current = window[-2], window[-1]
        if current.temp is None or current.valve is None:
            self._dead.pop(name, None)
            return
        pending = self._dead.get(name)
        top = top_opening(params.curve)
        if pending is None:
            # Heizbeginn: Ventile oeffnen aus der Ruhe, weil der Sollwert deutlich
            # ueber der Raumtemperatur liegt. Die Stellmotoren oeffnen langsam;
            # die Zeit bis zur vollen Oeffnung gehoert zur Totzeit.
            if (
                previous.valve is not None and previous.valve < 0.3 * top
                and previous.temp is not None and current.valve >= 0.3 * top
                and current.setpoint is not None and current.setpoint - current.temp >= 1.0
            ):
                self._dead[name] = (current.time, previous.temp)
            return
        opened, start_temp = pending
        elapsed = (current.time - opened).total_seconds() / 3600
        if current.valve < 0.3 * top or elapsed > DEAD_MAX + 1.0:
            self._dead.pop(name, None)  # abgebrochen oder unklar
            return
        if current.temp < start_temp + RISE:
            return
        self._dead.pop(name, None)
        loss = params.a * (current.temp - outside) if outside is not None else 0.0
        rate = max(params.b - loss, 0.3)
        dead = elapsed - RISE / rate
        if -0.1 <= dead <= DEAD_MAX:
            stats.sw_d += 1.0
            stats.swd_d += max(dead, 0.0)
            stats.dead_events += 1

    def _gain(
        self, window: deque[RoomSample], params: RoomParams, open_since: datetime | None = None
    ) -> float | None:
        """Waermegewinn je Ventiloeffnung (K/h) im letzten 15-Minuten-Fenster.

        Waehrend der Totzeit nach dem Oeffnen kommt noch keine Waerme an; diese
        Fenster zaehlen nicht.
        """
        recent = list(window)[-(HEAT_WINDOW + 1):]
        if len(recent) < HEAT_WINDOW + 1:
            return None
        if open_since is not None and recent[0].time < open_since + timedelta(hours=params.d):
            return None
        threshold = max(VALVE_OPEN * top_opening(params.curve), VALVE_MIN_GAIN)
        if any(s.temp is None or s.valve is None or s.valve < threshold for s in recent):
            return None
        last = recent[-1]
        first = recent[0]
        out = self._outside_mean(first.time, last.time)
        if out is None:
            return None
        hours = (last.time - first.time).total_seconds() / 3600
        rate = (last.temp - first.temp) / hours
        mean_temp = sum(s.temp for s in recent) / len(recent)
        mean_valve = sum(s.valve for s in recent) / len(recent)
        gain = (rate + params.a * (mean_temp - out)) / mean_valve
        return gain if -2.0 < gain < 60.0 else None

    # ----------------------------------------------------------- Heizphasen

    def _episode_step(
        self,
        now: datetime,
        rooms: dict[str, RoomSample],
        gains: dict[str, float | None],
        buffer_fraction: float,
        boiler_kw: float | None,
        plant: PlantParams,
        prior_minutes: dict[str, float],
        calibration: bool = False,
        flow: float | None = None,
        tops: dict[str, float] | None = None,
    ) -> None:
        valves = {name: (s.valve or 0.0) for name, s in rooms.items()}
        tops = tops or {}
        relative = {name: v / tops.get(name, 1.0) for name, v in valves.items()}
        if flow is not None:
            # Bezug "Puffer voll": langsam vergessendes Maximum des Vorlaufs
            previous = self._flow_full if self._flow_full is not None else flow
            self._flow_full = max(flow, previous - FLOW_DECAY)
        any_open = any(v >= VALVE_OPEN for v in relative.values())
        episode = self._episode
        if episode is None:
            if not any_open:
                return
            # Nach mindestens 90 Minuten Ruhe ist der Puffer sicher voll.
            rested = self._last_episode_end is None or now - self._last_episode_end >= timedelta(minutes=90)
            episode = self._episode = {
                "start": now,
                "rested": calibration or rested,
                "buffer": 1.0 if calibration or rested else buffer_fraction,
                "calibration": calibration,
                "rows": [],
                "flow_full": self._flow_full,
                "boiler_on": None if boiler_kw is None else boiler_kw > 0.5,
                "boiler_at": None,
                "quiet": 0,
            }
        since = (now - episode["start"]).total_seconds() / 3600
        episode["rows"].append({"valves": valves, "gains": gains, "flow": flow})
        if boiler_kw is not None and boiler_kw > 0.5 and episode["boiler_at"] is None:
            episode["boiler_at"] = since
            if 0 < since < 2:
                self.plant.t_sum += since
                self.plant.t_w += 1.0
        episode["quiet"] = 0 if any(v >= 0.5 for v in relative.values()) else episode["quiet"] + 1
        if episode["quiet"] >= 6 or now - episode["start"] >= MAX_EPISODE:
            self._episode = None
            self._last_episode_end = now
            t_start = episode["boiler_at"] if episode["boiler_at"] is not None else plant.t_start
            self.fit_episode(episode, plant, t_start, prior_minutes)

    def fit_episode(
        self, episode: dict[str, Any], plant: PlantParams, t_start: float, prior_minutes: dict[str, float]
    ) -> dict[str, Any] | None:
        """Kessel-/Puffermodell an eine abgeschlossene Heizphase anpassen."""
        rows = episode["rows"]
        # Raeume koennen in einzelnen Messpunkten fehlen (Temperatur unbekannt)
        names = sorted({name for row in rows for name in row["valves"]})
        heating = [
            name for name in names
            if sum(1 for row in rows if row["gains"].get(name) is not None) >= MIN_EPISODE_SAMPLES
        ]
        if not heating:
            return None
        params = {name: self.room_params(name, prior_minutes.get(name, 120.0)) for name in names}
        hours = SAMPLE.total_seconds() / 3600
        solo = len(heating) == 1
        rested = episode.get("rested", False)
        # Heizt ein Raum allein, werden sein w und die Pufferreserve bestimmt.
        # Heizen mehrere, wird nur das w der Raeume angepasst, die noch keine
        # eigene Messung haben (gemeinsamer Faktor); eine schon gemessene
        # Pufferreserve bleibt dann fest, sonst waeren beide nicht trennbar.
        # Ohne Ruhephase davor ist der Pufferstand zu Beginn unsicher: dann
        # werden nur die Aufheizraten angepasst.
        unknown = {n for n in heating if self.room_stats(n).sw_w < 0.8}
        if not rested:
            scaled = set()
        elif solo:
            scaled = set(heating)
        elif episode.get("calibration") or len(unknown) < len(heating):
            # gemeinsamer Faktor nur im geplanten Versuch oder wenn ein Raum
            # mit bekanntem w die Groessenordnung festlegt
            scaled = unknown
        else:
            scaled = set()
        current = self.plant_params(plant)
        # Pufferreserve nur frei, wenn sie sich von w trennen laesst: allein
        # heizender Raum, oder alle beteiligten w schon gemessen.
        fixed_buffer = not rested or (not solo and (self.plant.e_w >= 1.0 or bool(unknown)))

        # Lief der Kessel zu Beginn schon (z. B. vom Warmhalten)? Aus StokerCloud
        # bekannt, sonst werden beide Moeglichkeiten geprueft.
        boiler_start = episode.get("boiler_on")
        boiler_options = [boiler_start] if boiler_start is not None else [False, True]

        def shares(scale: float, e_buf: float, boiler_on: bool = False) -> list[float]:
            weights = {
                name: params[name].w * (scale if name in scaled else 1.0) for name in params
            }
            state_params = PlantParams(e_buf=e_buf, t_start=t_start)
            state = PlantState(e_buf * min(max(episode["buffer"], 0.0), 1.0))
            if boiler_on:
                state.boiler_on, state.on_for = True, t_start
            result = []
            for row in rows:
                demand = sum(weights[n] * v for n, v in row["valves"].items())
                _, share = plant_step(demand, state_params, state, hours)
                result.append(share)
            return result

        # Waerme kommt um die Totzeit verzoegert an: der Gewinn in Zeile i
        # gehoert zum Versorgungsanteil in Zeile i - lag.
        lags = {name: round(params[name].d / hours) for name in heating}

        def residual(share_list: list[float]) -> tuple[float, dict[str, float]]:
            total = 0.0
            b_values = {}
            for name in heating:
                lag = lags[name]
                pairs = [
                    (row["gains"][name], share_list[max(0, i - lag)]) for i, row in enumerate(rows)
                    if row["gains"].get(name) is not None
                ]
                ss = sum(s * s for _, s in pairs)
                b = sum(g * s for g, s in pairs) / ss if ss > 0 else params[name].b
                b_values[name] = b
                scale_g = sum(g * g for g, _ in pairs) or 1.0
                total += sum((g - b * s) ** 2 for g, s in pairs) / scale_g
            return total, b_values

        measured = [flow_share(row.get("flow"), episode.get("flow_full")) for row in rows]
        use_flow = sum(1 for m in measured if m is not None) >= len(rows) / 2

        def flow_residual(share_list: list[float]) -> float:
            if not use_flow:
                return 0.0
            pairs = [(s, m) for s, m in zip(share_list, measured) if m is not None]
            return FLOW_WEIGHT * sum((s - m) ** 2 for s, m in pairs) / len(pairs)

        def search(e_grid: list[float], scale_grid: list[float]):
            best = None
            for e_buf in e_grid:
                for scale in scale_grid:
                    for boiler_on in boiler_options:
                        share_list = shares(scale, e_buf, boiler_on)
                        total, b_values = residual(share_list)
                        total += flow_residual(share_list)
                        if best is None or total < best[0]:
                            best = (total, e_buf, scale, b_values, min(share_list))
            return best

        flat_total, flat_b = residual([1.0] * len(rows))
        flat_total += flow_residual([1.0] * len(rows))
        e_free = [0.05 * (1.15**i) for i in range(30)]  # 0.05 .. ~2.9 Kessel-Stunden
        scale_free = [0.25 * (1.12**i) for i in range(30)]  # 0.25 .. ~6.6
        # Grundlage: bisherige w, Puffer frei (sofern erlaubt)
        best = search([current.e_buf] if fixed_buffer else e_free, [1.0])
        base = best
        if scaled:
            trial = search([current.e_buf] if fixed_buffer else e_free, scale_free)
            # Allein heizender Raum: genau dafuer gedacht, das Ergebnis gilt.
            # Sonst w nur anpassen, wenn der Verlauf das deutlich besser erklaert.
            if (solo and trial[0] <= best[0]) or (
                trial[4] < 0.85 and trial[0] < 0.9 * best[0] and trial[0] < 0.8 * flat_total
            ):
                best = trial
        total, e_buf, scale, b_values, min_share = best
        adjustable = set(scaled)
        if best[2] == 1.0:
            scaled = set()
        # Knapp = der Puffer war leer. Mit Vorlauf direkt gemessen, sonst nur,
        # wenn das Modell mit Knappheit den Verlauf deutlich besser erklaert.
        flow_short = use_flow and min(m for m in measured if m is not None) < 0.9
        # Heizt ein Raum allein, reicht schon ein kleiner Einbruch, wenn er den
        # Verlauf deutlich besser erklaert.
        dip = 0.95 if solo else 0.85
        shortage = (min_share < dip or flow_short) and total < 0.8 * flat_total
        fit = {
            "raeume": heating, "allein": solo, "knapp": shortage,
            "fehler": round(total / len(heating), 4), "vorlauf": use_flow,
            "fehler_ohne_engpass": round(flat_total / len(heating), 4),
            "kleinster_anteil": round(min_share, 2),
        }
        if total / len(heating) > 0.05:
            # Modell erklaert den Verlauf nicht (z. B. Fenster offen, Stoerung): verwerfen
            fit["verworfen"] = True
            self.plant.last_fit = fit
            return fit
        # Erklaert das Modell den Verlauf mindestens so gut wie "immer volle
        # Versorgung", gelten seine Aufheizraten; sonst die einfachen.
        trust_model = total <= 1.05 * flat_total
        fit["modell"] = trust_model
        # Kalibrierlaeufe sind sauber und zaehlen dreifach.
        weight = 3.0 if episode.get("calibration") else (1.0 if rested else 0.5)
        for name in heating:
            stats = self.room_stats(name)
            b = b_values[name] if trust_model else flat_b[name]
            if 0.1 < b < 60.0:
                stats.sw_b += weight
                stats.swb_b += weight * b
                stats.heat_windows += 1
            if shortage and name in scaled:
                w = params[name].w * scale
                w_weight = weight if solo else weight / 2
                stats.sw_w += w_weight
                stats.sww_w += w_weight * w
            fit[name] = {
                "b": round(b, 2),
                "w": round(params[name].w * scale, 2) if shortage and name in scaled else None,
            }
        # Engpass erwartet, aber nicht eingetreten: die Raeume ziehen weniger
        # als angenommen. Ihr w wird hoechstens so gross gesetzt, dass das
        # Modell keinen Engpass mehr zeigt (vorsichtige Obergrenze).
        if (
            adjustable and not shortage and not use_flow
            and base[4] < 0.85 and base[0] > 1.5 * flat_total
        ):
            upper = None
            scaled = adjustable  # shares() skaliert die Raeume in 'scaled'
            for factor in sorted((f for f in scale_free if f < 1.0), reverse=True):
                if min(shares(factor, current.e_buf, True)) >= 0.95:
                    upper = factor
                    break
            if upper is None:
                scaled = set()
            else:
                w_weight = (weight if solo else weight / 2) / 2
                for name in scaled:
                    stats = self.room_stats(name)
                    stats.sw_w += w_weight
                    stats.sww_w += w_weight * params[name].w * upper
                    fit.setdefault(name, {})["w_hoechstens"] = round(params[name].w * upper, 2)
        if shortage and not fixed_buffer:
            self.plant.e_sum += weight * e_buf
            self.plant.e_w += weight
            fit["puffer"] = round(e_buf, 2)
        self.plant.episodes += 1
        self.plant.last_fit = fit
        return fit


def _key(name: tuple[str | None, str]) -> str:
    return f"{name[0] or ''}|{name[1]}"


def _name(key: str) -> tuple[str | None, str]:
    room, _, quantity = key.rpartition("|")
    return (room or None, quantity)


def _info_to_json(info: dict) -> dict[str, dict[str, float]]:
    return {_key(a): {_key(b): round(v, 6) for b, v in row.items()} for a, row in info.items()}


def _info_from_json(info: dict[str, dict[str, float]]) -> dict:
    return {_name(a): {_name(b): v for b, v in row.items()} for a, row in info.items()}
