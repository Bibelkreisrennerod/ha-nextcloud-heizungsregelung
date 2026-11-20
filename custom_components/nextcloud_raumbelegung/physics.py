"""Physikalisches Modell der Anlage (reine Logik, ohne Home Assistant).

Raeume (je Raum r, Zeit in Stunden):

    dT_r/dt = b_r * u_r(t - d_r) - a_r * (T_r - T_aussen)

    a_r  Auskuehlkonstante (1/h): Waermeverlust / Waermespeicher des Raums
    b_r  Aufheizrate bei offenen Ventilen und voller Versorgung (K/h)
    u_r  tatsaechliche Versorgung, 0..1 = Ventilstellung x Versorgungsanteil
    d_r  Totzeit (h): so lange dauert es, bis Waerme aus geoeffneten Ventilen
         am Temperaturfuehler ankommt (Heizkoerper aufwaermen, Luft umwaelzen)

Kessel und Puffer (Leistungen in Vielfachen der Kessel-Dauerleistung):

    w_r  Leistungsbedarf des Raums bei offenen Ventilen (1.0 = genau die
         Dauerleistung des Kessels; der Saal liegt darueber)
    D    = sum(w_r * Ventil_r), gesamter Bedarf
    E    Pufferreserve in Kessel-Stunden (Energie, die der Puffer ueber dem
         nutzbaren Mindestniveau abgeben kann)

Ist der Puffer voll, bekommt jeder Raum, was er braucht; die Luecke zum
Kessel deckt der Puffer. Wird er leer, sinkt der Vorlauf und alle offenen
Raeume teilen sich die Kesselleistung. Der Kessel springt an, wenn der
Puffer unter 90 % faellt, und liefert erst nach seiner Anlaufzeit.

Ventilkennlinie: Die Homematic-Thermostate regeln selbst (PI-Regler, der
aus frueheren Heizvorgaengen lernt) und oeffnen das Ventil fast nie ganz;
jeder Heizkoerper hat seine eigene Hoechstoeffnung (gemessen am 19.11.2026:
Saal 48 %, Nebenraum gross 83 %). Das Modell beschreibt das je Raum mit einer
gelernten Kennlinie: Ventilstellung beim Aufheizen an Stuetzstellen der
Abweichung Soll minus Ist (CURVE_DEV), dazwischen linear. Weil der Regler
einen I-Anteil hat, haelt er einen Raum auf Dauer genau am Sollwert, solange
die Hoechstoeffnung reicht. Im Modell oeffnet das Ventil deshalb um die
Halte-Oeffnung (a * (Raum - aussen) / b) herum: unter dem Sollwert
mindestens so weit wie die Halte-Oeffnung plus den Anstieg der Kennlinie
ueber ihren Wert am Sollwert, darueber entsprechend weniger (siehe valve). Ohne Messwerte gilt
PRIOR_SHAPE, ebenfalls aus diesen Messungen: am Sollwert halb offen, voll ab
2,5 K darunter, zu ab 0,5 K darueber. b und w gelten je Einheit
Ventilstellung (1.0 = 100 %), wie sie die Thermostate melden.
"""

from __future__ import annotations

from bisect import bisect_right
import math
from collections import deque
from dataclasses import dataclass
from functools import lru_cache

REACH_TOLERANCE = 0.5  # "warm" = Sollwert minus diese Kelvin

# Stuetzstellen der Ventilkennlinie: Abweichung Soll minus Ist (K)
CURVE_DEV = (-1.0, -0.5, 0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0)


def _prior_shape(dev: float) -> float:
    if dev <= -0.5:
        return 0.0
    if dev < 0.0:
        return 0.5 + dev
    return min(0.5 + 0.2 * dev, 1.0)


# Startform relativ zur Hoechstoeffnung (Messungen Saal und Nebenraum gross)
PRIOR_SHAPE = tuple(_prior_shape(x) for x in CURVE_DEV)


@dataclass
class RoomParams:
    """Gelernte Eigenschaften eines Raums."""

    a: float = 0.05  # 1/h  (Auskuehlzeitkonstante 1/a = 20 h)
    b: float = 2.0  # K/h
    w: float = 0.3  # Anteil der Kessel-Dauerleistung
    d: float = 0.15  # Totzeit in h (9 min)
    # Traegheit der Heizkoerper in h: Wasser und Stahl speichern Waerme, sie
    # kommt verzoegert an und wirkt nach dem Schliessen noch nach (Saal am
    # 20.11.2026 gemessen: etwa 35 min, dazu 10 min Totzeit)
    tau: float = 0.0
    # Ventilstellung 0..1 an den Stuetzstellen CURVE_DEV; None = PRIOR_SHAPE
    curve: tuple[float, ...] | None = None

    def heat_loss(self, delta: float = 20.0) -> float:
        """Temperaturverlust in K/h bei 'delta' Kelvin Unterschied nach aussen."""
        return self.a * delta


@dataclass
class PlantParams:
    """Gelernte Eigenschaften von Kessel und Puffer."""

    e_buf: float = 0.6  # Kessel-Stunden
    t_start: float = 0.33  # Anlaufzeit des Kessels in h
    p_max_kw: float = 30.0  # Dauerleistung in kW (nur fuer die Anzeige)


@dataclass
class PlantState:
    """Zustand von Kessel und Puffer."""

    e: float
    boiler_on: bool = False
    on_for: float = 0.0  # h seit Kesselstart

    def copy(self) -> PlantState:
        return PlantState(self.e, self.boiler_on, self.on_for)


def _interp(points: tuple[float, ...], dev: float) -> float:
    if dev <= CURVE_DEV[0]:
        return points[0]
    if dev >= CURVE_DEV[-1]:
        return points[-1]
    i = bisect_right(CURVE_DEV, dev) - 1
    f = (dev - CURVE_DEV[i]) / (CURVE_DEV[i + 1] - CURVE_DEV[i])
    return points[i] + f * (points[i + 1] - points[i])


def valve(
    setpoint: float | None,
    temp: float,
    curve: tuple[float, ...] | None = None,
    hold: float | None = None,
) -> float:
    """Ventilstellung 0..1 laut Kennlinie des Raums.

    hold: Ventilstellung, mit der die aktuelle Temperatur gerade gehalten wird
    (a * (Raum - aussen) / b). Ohne hold gilt die Kennlinie allein.
    Mit hold (I-Anteil des Reglers): unter dem Sollwert die Kennlinie, aber
    mindestens hold plus deren Anstieg ueber den Wert am Sollwert; ueber dem
    Sollwert hold abzueglich des Abfalls der Kennlinie. Am Sollwert also genau
    hold: der Raum bleibt dort stehen. Hoechstens bis zur Hoechstoeffnung.
    """
    if setpoint is None:
        return 0.0
    points = curve or PRIOR_SHAPE
    dev = setpoint - temp
    opening = _interp(points, dev)
    if hold is None:
        return opening
    around_hold = hold + opening - _interp(points, 0.0)
    if dev > 0:
        opening = max(opening, around_hold)
    else:
        opening = around_hold
    return min(max(opening, 0.0), max(points))


def hold_opening(params: RoomParams, temp: float, outside: float) -> float:
    """Ventilstellung, mit der die Temperatur gerade gehalten wird."""
    return params.a * (temp - outside) / params.b if params.b > 0 else 0.0


def plant_step(
    demand: float, plant: PlantParams, state: PlantState, dt: float
) -> tuple[float, float]:
    """Kessel und Puffer um dt Stunden fortschreiben.

    Liefert (gelieferte Leistung, Versorgungsanteil 0..1) und veraendert state.
    """
    if plant.e_buf <= 0:
        state.e = 0.0
    if not state.boiler_on and state.e < 0.9 * plant.e_buf:
        state.boiler_on = True
        state.on_for = 0.0
    boiler = 1.0 if state.boiler_on and state.on_for >= plant.t_start else 0.0
    # Je leerer der Puffer, desto kaelter der Vorlauf: im unteren Viertel
    # deckt er die Luecke zum Kessel nur noch anteilig.
    quality = min(max(state.e / (0.25 * plant.e_buf), 0.0), 1.0) if plant.e_buf > 0 else 0.0
    if demand <= boiler:
        supply = demand
    else:
        supply = boiler + quality * (demand - boiler)
    share = supply / demand if demand > 1e-9 else 1.0
    state.e = min(max(state.e + (boiler - supply) * dt, 0.0), plant.e_buf)
    if state.boiler_on:
        state.on_for += dt
        if state.e >= plant.e_buf - 1e-9 and demand < 1.0:
            state.boiler_on = False
    return supply, share


class Delay:
    """Weg der Waerme vom Ventil in den Raum: Totzeit, dann Traegheit.

    Totzeit: gibt den Wert zurueck, der vor 'dead' Stunden hineinging; liegt
    sie zwischen zwei Schritten, wird interpoliert. Traegheit (lag, Stunden):
    Verzoegerung erster Ordnung der Heizkoerper. Vor dem ersten Schritt gilt
    'initial' als Vorgeschichte (0 = Ventile waren zu).
    """

    def __init__(self, dead: float, dt: float, initial: float = 0.0, lag: float = 0.0) -> None:
        steps = max(dead, 0.0) / dt if dt > 0 else 0.0
        self._whole = int(steps)
        self._part = steps - self._whole
        self._history: deque[float] = deque([initial] * (self._whole + 2), maxlen=self._whole + 2)
        self._factor = 1.0 - math.exp(-dt / lag) if lag > 1e-6 and dt > 0 else 1.0
        self._state = initial

    def push(self, value: float) -> float:
        self._history.append(value)
        newer = self._history[-1 - self._whole]
        older = self._history[-2 - self._whole]
        delayed = (1.0 - self._part) * newer + self._part * older
        self._state += (delayed - self._state) * self._factor
        return self._state


def room_step(temp: float, params: RoomParams, supply: float, outside: float, dt: float) -> float:
    """Raumtemperatur um dt Stunden fortschreiben (supply = u_r, 0..1)."""
    return temp + (params.b * supply - params.a * (temp - outside)) * dt


def solo_minutes(
    params: RoomParams,
    plant: PlantParams,
    t_from: float = 14.0,
    t_to: float = 21.0,
    outside: float = 0.0,
    limit: float = 24 * 60,
) -> float:
    """Aufheizdauer eines Raums, der allein heizt (voller Puffer), in Minuten."""
    state = PlantState(plant.e_buf)
    temp, minutes, dt = t_from, 0.0, 5 / 60
    delay = Delay(params.d, dt, lag=params.tau)
    while temp < t_to - REACH_TOLERANCE and minutes < limit:
        opening = valve(t_to, temp, params.curve, hold_opening(params, temp, outside))
        _, share = plant_step(params.w * opening, plant, state, dt)
        temp = room_step(temp, params, delay.push(opening * share), outside, dt)
        minutes += 5
    return minutes


@lru_cache(maxsize=256)
def _b_for(
    minutes: float,
    a: float,
    w: float,
    e_buf: float,
    t_start: float,
    d: float,
    curve: tuple[float, ...] | None,
    tau: float = 0.0,
) -> float:
    plant = PlantParams(e_buf=e_buf, t_start=t_start)
    low, high = 0.05, 200.0
    for _ in range(40):
        mid = (low + high) / 2
        if solo_minutes(RoomParams(a=a, b=mid, w=w, d=d, curve=curve, tau=tau), plant, limit=minutes + 60) > minutes:
            low = mid
        else:
            high = mid
    return high


def b_for_minutes(
    minutes: float,
    a: float,
    w: float = 0.0,
    plant: PlantParams | None = None,
    d: float = 0.0,
    curve: tuple[float, ...] | None = None,
    tau: float = 0.0,
) -> float:
    """Aufheizrate b, mit der ein Raum allein in 'minutes' von 14 auf 21 Grad
    kommt (bei 0 Grad aussen, voller Puffer, mit Kessel- und Pufferbegrenzung).

    So bedeutet der Startwert "Vorheizzeit" fuer jeden Raum dasselbe wie in
    der Planung: Ein Saal, der mehr zieht als der Kessel liefert, braucht
    die angegebene Zeit einschliesslich des leer werdenden Puffers.
    """
    plant = plant or PlantParams()
    return _b_for(
        round(max(minutes, 5.0) / 5) * 5,
        round(a, 3),
        round(w, 2),
        round(plant.e_buf, 2),
        round(plant.t_start, 2),
        round(min(d, minutes / 60 / 2), 2),
        tuple(round(v * 20) / 20 for v in curve) if curve else None,  # grob, damit der Cache greift
        round(tau, 2),
    )
