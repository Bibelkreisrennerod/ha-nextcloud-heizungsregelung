"""Physikalisches Modell der Anlage (reine Logik, ohne Home Assistant).

Raeume (je Raum r, Zeit in Stunden):

    dT_r/dt = b_r * u_r - a_r * (T_r - T_aussen)

    a_r  Auskuehlkonstante (1/h): Waermeverlust / Waermespeicher des Raums
    b_r  Aufheizrate bei offenen Ventilen und voller Versorgung (K/h)
    u_r  tatsaechliche Versorgung, 0..1 = Ventilstellung x Versorgungsanteil

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

Die Thermostate regeln proportional: das Ventil ist voll offen, solange der
Raum mehr als BAND Kelvin unter dem Sollwert liegt, und schliesst linear bis
zum Sollwert. So bildet das Modell auch Zwischen-Sollwerte ab.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

BAND = 0.8  # Proportionalband der Thermostate (K)
REACH_TOLERANCE = 0.5  # "warm" = Sollwert minus diese Kelvin


@dataclass
class RoomParams:
    """Gelernte Eigenschaften eines Raums."""

    a: float = 0.05  # 1/h  (Auskuehlzeitkonstante 1/a = 20 h)
    b: float = 2.0  # K/h
    w: float = 0.3  # Anteil der Kessel-Dauerleistung

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


def valve(setpoint: float | None, temp: float) -> float:
    """Ventilstellung 0..1 eines proportional regelnden Thermostats."""
    if setpoint is None:
        return 0.0
    return min(max((setpoint - temp) / BAND, 0.0), 1.0)


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
    while temp < t_to - REACH_TOLERANCE and minutes < limit:
        opening = valve(t_to, temp)
        _, share = plant_step(params.w * opening, plant, state, dt)
        temp = room_step(temp, params, opening * share, outside, dt)
        minutes += 5
    return minutes


@lru_cache(maxsize=256)
def _b_for(minutes: float, a: float, w: float, e_buf: float, t_start: float) -> float:
    plant = PlantParams(e_buf=e_buf, t_start=t_start)
    low, high = 0.05, 60.0
    for _ in range(40):
        mid = (low + high) / 2
        if solo_minutes(RoomParams(a=a, b=mid, w=w), plant, limit=minutes + 60) > minutes:
            low = mid
        else:
            high = mid
    return high


def b_for_minutes(minutes: float, a: float, w: float = 0.0, plant: PlantParams | None = None) -> float:
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
    )
