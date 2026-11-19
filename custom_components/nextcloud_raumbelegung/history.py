"""Aus der Home-Assistant-Historie lernen (reine Logik, ohne Home Assistant).

Der Recorder speichert Raumtemperaturen, Sollwerte und Ventilstellungen
ohnehin. Beim ersten Start (und auf Wunsch per Dienst) liest die Steuerung
diese Historie, bringt sie auf das 5-Minuten-Raster der Live-Messung und
spielt sie dem Lerner in zeitlicher Reihenfolge vor. So kennt das Modell
Auskuehlraten, Aufheizvorgaenge der Homematic-Profile und Totzeiten schon
am ersten Tag, statt erst Wochen zu sammeln.

Eingabe sind einfache Zeitreihen (Zeitpunkt, Wert), die die Steuerung aus
den Recorder-Zustaenden zieht; hier wird nur gerastert und vorgespielt.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .learning import SAMPLE, Learner, RoomSample
from .physics import PlantParams, PlantState, plant_step, valve

Series = list[tuple[datetime, float]]  # (Zeitpunkt, Wert), aufsteigend
MAX_AGE = timedelta(hours=2)  # aelterer letzter Wert gilt als unbekannt


@dataclass
class RoomSeries:
    """Zeitreihen eines Raums."""

    temps: list[Series] = field(default_factory=list)  # je Heizgruppe: Raumtemperatur
    targets: list[Series] = field(default_factory=list)  # je Heizgruppe: Sollwert
    valves: list[Series] = field(default_factory=list)  # je Ventilsensor: Stellung in %


@dataclass
class ReplayResult:
    samples: int = 0
    days: float = 0.0
    start: datetime | None = None
    end: datetime | None = None


class _Cursor:
    """Liest eine Zeitreihe als Treppenfunktion (letzter Wert bis zum Zeitpunkt)."""

    def __init__(self, series: Series) -> None:
        self._series = series
        self._index = -1

    def at(self, moment: datetime) -> float | None:
        series = self._series
        while self._index + 1 < len(series) and series[self._index + 1][0] <= moment:
            self._index += 1
        if self._index < 0:
            return None
        when, value = series[self._index]
        return value if moment - when <= MAX_AGE else None


def _mean(values: list[float | None]) -> float | None:
    known = [v for v in values if v is not None]
    return sum(known) / len(known) if known else None


def _floor(moment: datetime) -> datetime:
    minutes = moment.minute - moment.minute % 5
    return moment.replace(minute=minutes, second=0, microsecond=0)


def replay(
    learner: Learner,
    rooms: dict[str, RoomSeries],
    outside: Series,
    flow: Series,
    start: datetime,
    end: datetime,
    prior_minutes: dict[str, float],
    plant_base: PlantParams,
) -> ReplayResult:
    """Historie im 5-Minuten-Raster an den Lerner geben.

    Die Pufferreserve wird dabei wie im Live-Betrieb aus den Ventilstellungen
    fortgeschrieben (ohne Vorlauf ist sie nur geschaetzt).
    """
    cursors = {
        name: (
            [_Cursor(s) for s in series.temps],
            [_Cursor(s) for s in series.targets],
            [_Cursor(s) for s in series.valves],
        )
        for name, series in rooms.items()
    }
    outside_cursor = _Cursor(outside)
    flow_cursor = _Cursor(flow)
    plant = learner.plant_params(plant_base)
    gauge = PlantState(plant.e_buf)
    hours = SAMPLE.total_seconds() / 3600
    result = ReplayResult(start=start, end=end)
    moment = _floor(start) + SAMPLE
    while moment <= end:
        samples: dict[str, RoomSample] = {}
        demand = 0.0
        for name, (temps, targets, valves) in cursors.items():
            temp = _mean([c.at(moment) for c in temps])
            target = _mean([c.at(moment) for c in targets])
            if temp is None:
                continue
            opening = _mean([c.at(moment) for c in valves])
            if opening is not None:
                opening = min(max(opening / 100.0, 0.0), 1.0)
            elif target is not None:
                opening = valve(target, temp)
            samples[name] = RoomSample(moment, temp, opening, target)
            demand += learner.room_params(name, prior_minutes.get(name, 120.0)).w * (opening or 0.0)
        gauge.e = min(gauge.e, plant.e_buf)
        _, share = plant_step(demand, plant, gauge, hours)
        if samples:
            fraction = gauge.e / plant.e_buf if plant.e_buf > 0 else 0.0
            learner.add_sample(
                moment,
                samples,
                outside_cursor.at(moment),
                fraction,
                prior_minutes,
                plant,
                None,
                False,
                flow_cursor.at(moment),
                share,
            )
            result.samples += 1
            plant = learner.plant_params(plant_base)
        moment += SAMPLE
    result.days = result.samples * hours / 24
    return result
