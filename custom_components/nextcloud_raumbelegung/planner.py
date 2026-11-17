"""Lastplanung: wann welcher Raum vorheizt (reine Logik, ohne Home Assistant).

Jeder Raum hat ein Lastgewicht. Waehrend einer Aufheizphase belegt er sein
Gewicht vom Kesselbudget. Der Planer legt die Aufheizphasen rueckwaerts
von der Frist aus so spaet wie moeglich, ohne das Budget zu ueberschreiten.
Reicht der Platz nicht, rueckt eine Phase frueher (gestaffeltes Vorheizen).

Gehaltene Raeume (schon warm, warten auf den Termin) zaehlen nicht gegen das
Budget: die Thermostate oeffnen dann nur noch wenig.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

SLOT = timedelta(minutes=5)
HORIZON = timedelta(hours=48)


@dataclass
class Job:
    """Eine geplante Aufheizphase."""

    room: str
    deadline: datetime  # bis dahin soll der Raum warm sein
    duration: timedelta  # benoetigte Aufheizdauer (mit Zuschlag)
    weight: float
    priority: int = 5  # 1 = hoechster Vorrang
    running: bool = False  # Aufheizen laeuft schon, Start liegt fest


@dataclass
class Plan:
    """Ergebnis fuer eine Aufheizphase."""

    room: str
    start: datetime
    end: datetime
    deadline: datetime
    late: bool  # Start musste vor 'jetzt' liegen, Frist wird knapp


def _slot(now: datetime, moment: datetime) -> int:
    return int((moment - now) / SLOT)


def _place(
    jobs: list[Job], order: list[Job], now: datetime, budget: float
) -> dict[str, Plan]:
    """Phasen in der gegebenen Reihenfolge rueckwaerts einplanen."""
    slots = int(HORIZON / SLOT) + 1
    usage = [0.0] * slots
    result: dict[str, Plan] = {}

    def fits(start: int, end: int, weight: float) -> bool:
        for i in range(max(start, 0), min(end, slots)):
            if usage[i] > 1e-9 and usage[i] + weight > budget + 1e-9:
                return False
        return True

    def occupy(start: int, end: int, weight: float) -> None:
        for i in range(max(start, 0), min(end, slots)):
            usage[i] += weight

    # Laufende Phasen liegen fest: sie belegen ab jetzt bis zu ihrem Ende.
    for job in jobs:
        if job.running:
            end = _slot(now, now + job.duration) + 1
            occupy(0, end, job.weight)
            result.setdefault(
                job.room,
                Plan(job.room, now, now + job.duration, job.deadline, now + job.duration > job.deadline),
            )

    for job in order:
        length = max(1, -(-int(job.duration.total_seconds()) // int(SLOT.total_seconds())))
        end = min(_slot(now, job.deadline), slots)
        while end - length >= 0 and not fits(end - length, end, job.weight):
            end -= 1
        start = end - length
        late = start < 0
        if late:
            # Vor der Frist ist kein Platz mehr: so frueh wie moeglich,
            # ohne das Budget zu ueberschreiten (notfalls nach der Frist).
            start = 0
            while start + length < slots and not fits(start, start + length, job.weight):
                start += 1
            end = start + length
        occupy(start, end, job.weight)
        planned = Plan(job.room, now + SLOT * start, now + SLOT * end, job.deadline, late)
        existing = result.get(job.room)
        if existing is None or planned.deadline < existing.deadline:
            result[job.room] = planned
    return result


def _cost(jobs: list[Job], result: dict[str, Plan]) -> float:
    """Verspaetung (nach Vorrang gewichtet) ist teuer, Warmhalten kostet wenig."""
    cost = 0.0
    for job in jobs:
        planned = result.get(job.room)
        if planned is None or planned.deadline != job.deadline:
            continue
        minutes = (planned.end - job.deadline).total_seconds() / 60
        if minutes > 0:
            cost += minutes * 1000 * (10 - job.priority)
        else:
            cost += -minutes * job.weight  # Minuten Warmhalten x Gewicht
    return cost


def plan(jobs: list[Job], now: datetime, budget: float) -> dict[str, Plan]:
    """Aufheizphasen planen. Liefert je Raum die Phase mit der fruehesten Frist.

    Probiert zwei Reihenfolgen und nimmt die guenstigere:
    - Vorrang zuerst: der wichtigste Raum bekommt den Platz direkt vor
      seiner Frist, die anderen werden davor gestaffelt.
    - Spaeteste Frist zuerst: klassische Rueckwaertsplanung, haelt
      unnoetiges Warmhalten klein.
    """
    pending = [job for job in jobs if not job.running]
    orders = [
        sorted(pending, key=lambda job: (job.priority, -job.deadline.timestamp())),
        sorted(pending, key=lambda job: (-job.deadline.timestamp(), job.priority)),
    ]
    best: dict[str, Plan] | None = None
    best_cost = 0.0
    for order in orders:
        result = _place(jobs, order, now, budget)
        cost = _cost(jobs, result)
        if best is None or cost < best_cost:
            best, best_cost = result, cost
    return best or {}
