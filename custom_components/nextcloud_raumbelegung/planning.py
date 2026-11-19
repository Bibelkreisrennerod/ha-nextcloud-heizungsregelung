"""Gemeinsame Heizplanung aller Raeume (reine Logik, ohne Home Assistant).

Je Raum mit anstehendem Termin wird ein Plan aus hoechstens zwei Stufen
gesucht:

    Absenkung  ->  [Vorstufe auf Zwischen-Sollwert]  ->  Komfort  ->  Termin

Alle Raeume werden zusammen mit Kessel und Puffer simuliert (physics.py),
mit der Wetterprognose fuer die Aussentemperatur. Die Planung hat drei
Schritte:

1. Verlaengern: Jeder Raum beginnt zunaechst so spaet, wie er allein
   brauchen wuerde. Wird ein Raum in der gemeinsamen Simulation zu spaet
   warm, weil andere gleichzeitig heizen, beginnt er um seine Verspaetung
   frueher. Das wiederholt sich, bis alle puenktlich sind oder nicht mehr
   frueher beginnen koennen.
2. Feinsuche: Die Starts werden einzeln verschoben, solange die Kosten
   sinken. Kosten = Energie plus Verspaetung; eine Verspaetungsminute zaehlt
   je Vorrangstufe dreimal so viel wie bei der naechsten. So rutschen Raeume
   nach hinten, wenn sie zu frueh warm waeren, und Raeume mit geringerem
   Vorrang weichen dem Saal aus, wenn die Zeit nicht fuer alle reicht.
3. Vorstufe: Beginnt ein Raum deutlich frueher als allein noetig, wird
   geprueft, ob er frueh auf einen Zwischen-Sollwert und erst spaet auf
   Komfort gehen kann. Die sparsamere Variante gewinnt.

Waehrend Raeume aufheizen und der Bedarf die Kesselleistung uebersteigt,
werden Raeume ohne Termin im Horizont um IDLE_DROP Kelvin unter die Absenkung
gesetzt. Sonst oeffnen deren Ventile, sobald sie unter die Absenkung fallen,
und nehmen den aufheizenden Raeumen Vorlauf weg. Raeume, die spaeter selbst
aufheizen, bleiben auf der Absenkung (jedes Kelvin weniger verlaengert ihr
eigenes Aufheizen).

Ein fertiger Plan wird beibehalten, solange er aufgeht (plan_still_valid).
Neu geplant wird nur, wenn sich Termine oder Einstellungen aendern oder ein
Raum laut Simulation aus dem aktuellen Zustand zu spaet kaeme. Das spart
Rechenzeit und verhindert, dass ein Raum frueh startet, weil andere
gleichzeitig heizen sollten, und die anderen danach doch verschoben werden.

Die Simulation ist vorsichtig: jeder Raum reagiert um seinen
Sicherheitsfaktor langsamer, Aufheizdauern werden also um diesen Faktor
laenger. Rechenaufwand: Schritte von 10 Minuten, Horizont
bis zur spaetesten Frist (hoechstens 24 Stunden) plus Reserve, insgesamt
unter 100 Simulationslaeufe je Planung.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Callable

from .physics import (
    REACH_TOLERANCE,
    Delay,
    PlantParams,
    PlantState,
    RoomParams,
    plant_step,
    room_step,
    valve,
)

STEP = timedelta(minutes=10)
STEP_H = STEP.total_seconds() / 3600
HORIZON = timedelta(hours=24)
EARLY = timedelta(minutes=15)  # warm so viele Minuten vor Terminbeginn
PRESTAGE_DROP = 3.0  # Zwischen-Sollwert = Komfort minus so viele Kelvin
IDLE_DROP = 2.0  # wartende Raeume waehrend knapper Versorgung so viel unter Absenkung
EXTRA = timedelta(hours=3)  # Simulation laeuft so lange ueber die letzte Frist hinaus
MAX_ROUNDS = 40  # Obergrenze fuer Schritt 1
PRESTAGE_MIN = timedelta(hours=1)  # ab so viel Verlaengerung wird die Vorstufe geprueft
MISSING_MINUTES = 120.0  # Strafe je fehlendem Kelvin, wenn ein Raum nie warm wird
COOLDOWN_MIN = timedelta(hours=2)  # warmer Raum darf abkuehlen, wenn er so lange nicht heizen muss
DESCENT_STEPS = (6, 2)  # Schrittweiten der Feinsuche in STEP (3 h, 1 h, 20 min)
SCAN = timedelta(minutes=30)  # Suchschritt beim Einplanen


@dataclass
class RoomInput:
    """Was der Planer ueber einen Raum wissen muss."""

    name: str
    params: RoomParams
    temp: float
    comfort: float
    setback: float
    priority: int = 5  # 1 = hoechster Vorrang
    safety: float = 1.25  # Sicherheitsfaktor auf die Aufheizdauer
    # (Beginn, Ende), sortiert; auch ein gerade laufender Termin (er belegt Kessel und Puffer)
    events: list[tuple[datetime, datetime]] = field(default_factory=list)
    fixed: float | None = None  # Sollwert, der nicht geplant wird (Handverstellung, Automatik aus)
    committed: str | None = None  # laeuft schon: "pre" (Vorstufe), "main" (heizt auf Komfort), "warm" (haelt Komfort)
    valve: float | None = None  # aktuelle Ventilstellung 0..1 (Vorgeschichte fuer die Totzeit)


@dataclass
class RoomPlan:
    """Ergebnis fuer einen Raum."""

    main: datetime | None = None  # ab hier Komfort
    pre: datetime | None = None  # ab hier Vorstufe
    pre_level: float | None = None
    deadline: datetime | None = None
    event_end: datetime | None = None  # Ende des Termins, fuer den geplant wird
    reach: datetime | None = None  # vorhergesagt (vorsichtig)
    reach_nominal: datetime | None = None  # vorhergesagt ohne Sicherheitsfaktor
    late: bool = False
    idle_drop: bool = False  # wartende Raeume waehrend der Heizphase abgesenkt

    def heat_start(self) -> datetime | None:
        return self.pre or self.main


@dataclass
class Window:
    """Zeitraum, in dem wartende Raeume zusaetzlich abgesenkt werden."""

    start: datetime | None = None
    end: datetime | None = None

    def contains(self, moment: datetime) -> bool:
        return self.start is not None and self.end is not None and self.start <= moment < self.end


def setpoint_at(room: RoomInput, plan: RoomPlan, moment: datetime, idle: Window | None = None) -> float:
    """Sollwert eines Raums zu einem Zeitpunkt laut Plan."""
    if room.fixed is not None:
        return room.fixed
    for start, end in room.events:
        if start <= moment < end:
            return room.comfort
    if plan.main is not None and plan.deadline is not None and plan.event_end is not None:
        if plan.main <= moment < plan.event_end:
            return room.comfort
        # spaetere Termine: gleiche Vorlaufzeit wie beim geplanten
        target_start = plan.deadline + EARLY
        lead = target_start - plan.main
        for start, end in room.events:
            if start > target_start and start - lead <= moment < end:
                return room.comfort
    if plan.pre is not None and plan.main is not None and plan.pre <= moment < plan.main:
        return plan.pre_level if plan.pre_level is not None else room.comfort
    if idle is not None and plan.deadline is None and idle.contains(moment):
        return room.setback - IDLE_DROP
    return room.setback


@dataclass
class SimResult:
    reach: dict[str, datetime | None]
    energy: float  # gelieferte Energie in Kessel-Stunden
    peak: dict[str, float] = field(default_factory=dict)  # hoechste Temperatur je Raum


def simulate(
    rooms: list[RoomInput],
    plans: dict[str, RoomPlan],
    plant: PlantParams,
    state: PlantState,
    outside: Callable[[datetime], float],
    now: datetime,
    until: datetime,
    cautious: bool = True,
    idle: Window | None = None,
) -> SimResult:
    """Alle Raeume gemeinsam simulieren; Zeitpunkt 'warm' je Raum fuer seinen ersten Termin."""
    state = state.copy()
    temps = [r.temp for r in rooms]
    params = [
        # vorsichtig = jeder Raum reagiert um den Sicherheitsfaktor langsamer
        # (Aufheizen, Auskuehlen, Totzeit), Aufheizdauern also um diesen Faktor laenger
        replace(r.params, a=r.params.a / factor, b=r.params.b / factor, d=r.params.d * factor)
        for r, factor in ((r, r.safety if cautious else 1.0) for r in rooms)
    ]
    delays = [Delay(p.d, STEP_H, r.valve or 0.0) for r, p in zip(rooms, params)]
    reach: dict[str, datetime | None] = {r.name: None for r in rooms}
    peak = {r.name: r.temp for r in rooms}
    energy = 0.0
    moment = now
    while moment < until:
        setpoints = [setpoint_at(r, plans.get(r.name, RoomPlan()), moment, idle) for r in rooms]
        valves = [valve(s, t) for s, t in zip(setpoints, temps)]
        demand = sum(p.w * v for p, v in zip(params, valves))
        supply, share = plant_step(demand, plant, state, STEP_H)
        energy += supply * STEP_H
        out = outside(moment)
        temps = [
            room_step(t, p, delay.push(v * share), out, STEP_H)
            for t, p, v, delay in zip(temps, params, valves, delays)
        ]
        moment += STEP
        for room, temp in zip(rooms, temps):
            peak[room.name] = max(peak[room.name], temp)
            plan = plans.get(room.name)
            if reach[room.name] is not None or plan is None or plan.deadline is None:
                continue
            start = plan.heat_start()
            if start is not None and moment > start and temp >= room.comfort - REACH_TOLERANCE:
                reach[room.name] = moment
    return SimResult(reach, energy, peak)


def _weight(priority: int) -> float:
    """Gewicht einer Verspaetungsminute: jede Vorrangstufe zaehlt dreimal so viel wie die naechste."""
    return 10.0 * 3.0 ** max(0, 9 - priority)


def _cost(rooms: list[RoomInput], plans: dict[str, RoomPlan], result: SimResult, until: datetime) -> float:
    """Kosten eines Plans: Energie in Kessel-Minuten plus gewichtete Verspaetung."""
    cost = result.energy * 60.0
    for room in rooms:
        plan = plans.get(room.name)
        if plan is None or plan.deadline is None:
            continue
        reached = result.reach[room.name]
        if reached is not None:
            late = (reached - plan.deadline).total_seconds() / 60
        else:
            # nie warm: Zeit bis Simulationsende plus fehlende Kelvin
            missing = room.comfort - REACH_TOLERANCE - result.peak.get(room.name, room.temp)
            late = (until - plan.deadline).total_seconds() / 60 + MISSING_MINUTES * max(0.0, missing)
        if late > 0:
            cost += late * _weight(room.priority)
    return cost


def _floor(moment: datetime, now: datetime) -> datetime:
    """Auf das 10-Minuten-Raster ab 'now' abrunden, nicht vor 'now'."""
    steps = int((moment - now) / STEP)
    return max(now, now + STEP * steps)


@dataclass
class PlanResult:
    """Ergebnis der Planung."""

    plans: dict[str, RoomPlan]
    idle: Window
    cost: float = 0.0


def _idle_window(rooms: list[RoomInput], plans: dict[str, RoomPlan]) -> Window:
    """Zeitraum, in dem wartende Raeume abgesenkt werden: nur bei Bedarf ueber Kesselleistung."""
    heating = [r for r in rooms if plans[r.name].deadline is not None and plans[r.name].heat_start() is not None]
    if not heating:
        return Window()
    if max(r.params.w for r in heating) < 0.8 and sum(r.params.w for r in heating) <= 1.0:
        return Window()
    return Window(
        min(plans[r.name].heat_start() for r in heating),
        max(plans[r.name].deadline for r in heating),
    )


def plan_heating(
    rooms: list[RoomInput],
    plant: PlantParams,
    state: PlantState,
    outside: Callable[[datetime], float],
    now: datetime,
) -> PlanResult:
    """Heizplan fuer alle Raeume berechnen."""
    base: dict[str, RoomPlan] = {}
    for room in rooms:
        plan = RoomPlan()
        upcoming = _next_event(room, now)
        if room.fixed is None and upcoming is not None:
            plan.deadline = upcoming[0] - EARLY
            plan.event_end = upcoming[1]
        base[room.name] = plan
    planned = [r for r in rooms if base[r.name].deadline is not None]
    if not planned:
        return PlanResult(base, Window(), 0.0)
    until = max(base[r.name].deadline for r in planned) + EXTRA
    never = until + timedelta(hours=2)
    deadline = {r.name: base[r.name].deadline for r in planned}

    def run(current: dict[str, RoomPlan], cautious: bool = True) -> SimResult:
        return simulate(rooms, current, plant, state, outside, now, until, cautious, _idle_window(rooms, current))

    def reached(result: SimResult, room: RoomInput) -> datetime:
        return result.reach[room.name] or never

    def cost_of(current: dict[str, RoomPlan]) -> float:
        return _cost(rooms, current, run(current), until)

    # Spaetester Start je Raum, wenn er allein heizen wuerde
    latest: dict[str, datetime] = {}
    for room in planned:
        solo = simulate(
            [room], {room.name: replace(base[room.name], main=now)}, plant,
            PlantState(plant.e_buf), outside, now, deadline[room.name] + timedelta(hours=12),
        )
        duration = (solo.reach[room.name] - now) if solo.reach[room.name] else timedelta(hours=12)
        latest[room.name] = _floor(deadline[room.name] - duration, now)

    def make(room: RoomInput, main: datetime) -> RoomPlan:
        """Plan mit Komfort ab 'main'; laufende Vorstufe bleibt erhalten."""
        plan = replace(base[room.name], main=main, pre=None, pre_level=None)
        if room.committed == "pre" and main > now:
            plan.pre, plan.pre_level = now, room.comfort - PRESTAGE_DROP
        return plan

    def keep_warm(room: RoomInput) -> bool:
        # Ein schon warmer Raum kuehlt nur ab, wenn er dann mindestens
        # COOLDOWN_MIN lang nicht heizen muss; sonst lohnen die Befehle nicht.
        return room.committed == "warm" and latest[room.name] - now < COOLDOWN_MIN

    fixed = [r for r in planned if r.committed == "main" or keep_warm(r)]
    movable = [r for r in planned if r not in fixed]
    for room in fixed:
        base[room.name] = make(room, now)

    # A) Nacheinander einplanen: jeder Raum so spaet wie moeglich, ohne die
    #    schon eingeplanten zu verspaeten
    def sequential(order: list[RoomInput]) -> dict[str, RoomPlan]:
        current = dict(base)
        placed = list(fixed)
        for room in order:
            before = run(current)
            allowed = {o.name: max(deadline[o.name], reached(before, o)) for o in placed}

            def check(main: datetime) -> tuple[bool, bool, datetime]:
                result = run({**current, room.name: make(room, main)})
                done = reached(result, room)
                protect = all(reached(result, o) <= allowed[o.name] for o in placed)
                return done <= deadline[room.name], protect, done

            chosen = None
            fallback: tuple[datetime, datetime] | None = None  # (warm um, Start)
            main = latest[room.name]
            while chosen is None:
                on_time, protect, done = check(main)
                if on_time and protect:
                    chosen = main
                    # grob gefunden: in 10-Minuten-Schritten wieder nach hinten
                    while chosen + STEP <= latest[room.name]:
                        on_time, protect, _done = check(chosen + STEP)
                        if not (on_time and protect):
                            break
                        chosen += STEP
                    break
                if protect and (fallback is None or done < fallback[0]):
                    fallback = (done, main)
                if main <= now:
                    break
                main = max(now, main - SCAN)
            if chosen is None:
                # nicht puenktlich: auch Starts nach dem spaetesten pruefen ("nach dem Saal")
                main = latest[room.name] + SCAN
                while main < deadline[room.name]:
                    _on_time, protect, done = check(main)
                    if protect and (fallback is None or done < fallback[0]):
                        fallback = (done, main)
                    main += SCAN
                chosen = fallback[1] if fallback else latest[room.name]
            current[room.name] = make(room, chosen)
            placed.append(room)
        return current

    def hold_load(room: RoomInput) -> float:
        return room.params.w * room.params.a / max(room.params.b, 1e-6)

    by_priority = sorted(movable, key=lambda r: (r.priority, deadline[r.name]))
    small_first = sorted(movable, key=lambda r: (hold_load(r), -deadline[r.name].timestamp()))

    # B) Gleichzeitig verlaengern: alle beginnen so spaet wie allein noetig;
    #    wer zu spaet waere, beginnt um seine Verspaetung frueher
    def stretched() -> dict[str, RoomPlan]:
        current = dict(base)
        for room in movable:
            current[room.name] = make(room, latest[room.name])
        for _ in range(MAX_ROUNDS):
            result = run(current)
            changed = False
            for room in movable:
                late = reached(result, room) - deadline[room.name]
                if late > timedelta(0) and current[room.name].main > now:
                    current[room.name] = make(room, max(now, current[room.name].main - STEP * -(-late // STEP)))
                    changed = True
            if not changed:
                break
        return current

    def keeps(before: SimResult, after: SimResult) -> bool:
        """Wird kein Raum spaeter warm als seine Frist bzw. als bisher?"""
        return all(
            reached(after, r) <= max(deadline[r.name], reached(before, r)) for r in planned
        )

    def right_shift(current: dict[str, RoomPlan], rounds: int = 3) -> dict[str, RoomPlan]:
        """Jeden Raum so weit nach hinten schieben, wie es ohne Verspaetung und
        ohne hoehere Kosten geht.

        Mehrere Runden, weil das Verschieben eines Raums anderen wieder Luft gibt.
        Zuerst die Raeume mit dem groessten Warmhaltebedarf (Saal): deren
        unnoetige Warmhaltestunden kosten am meisten.
        """
        order = sorted(movable, key=hold_load, reverse=True)
        for _ in range(rounds):
            moved = False
            for room in order:
                before = run(current)
                before_cost = _cost(rooms, current, before, until)
                plan = current[room.name]
                fields = ["main"] if plan.pre is None or room.committed == "pre" else ["pre", "main"]
                for name in fields:
                    plan = current[room.name]
                    low = getattr(plan, name)
                    high = deadline[room.name] if name == "main" else plan.main - STEP
                    lo, hi = 0, max(0, int((high - low) / STEP))
                    while lo < hi:
                        mid = (lo + hi + 1) // 2
                        trial = {**current, room.name: replace(plan, **{name: low + STEP * mid})}
                        after = run(trial)
                        if keeps(before, after) and _cost(rooms, trial, after, until) <= before_cost + 1e-6:
                            lo = mid
                        else:
                            hi = mid - 1
                    if lo > 0:
                        current = {**current, room.name: replace(plan, **{name: low + STEP * lo})}
                        moved = True
            if not moved:
                break
        return current

    candidates = [sequential(by_priority), stretched()]
    if [r.name for r in small_first] != [r.name for r in by_priority]:
        candidates.append(sequential(small_first))
    costs = [cost_of(c) for c in candidates]
    plans = right_shift(candidates[costs.index(min(costs))])
    best = cost_of(plans)
    if best > min(costs):  # sollte nicht vorkommen, aber sicher ist sicher
        plans, best = candidates[costs.index(min(costs))], min(costs)

    # C) Feinsuche: Starts einzeln verschieben, solange die Kosten sinken
    for size in DESCENT_STEPS:
        for _ in range(2):
            improved = False
            for room in movable:
                for sign in (1, -1):
                    main = plans[room.name].main + sign * STEP * size
                    main = min(max(main, now), deadline[room.name])
                    if main == plans[room.name].main:
                        continue
                    trial = {**plans, room.name: make(room, main) if plans[room.name].pre is None or main <= plans[room.name].pre else replace(plans[room.name], main=main)}
                    cost = cost_of(trial)
                    if cost < best - 1e-6:
                        plans, best, improved = trial, cost, True
            if not improved:
                break

    # D) Vorstufe pruefen, wo ein Raum deutlich frueher beginnt als allein noetig
    for room in movable:
        plan = plans[room.name]
        if plan.pre is not None or latest[room.name] - plan.main < PRESTAGE_MIN:
            continue
        staged = replace(plan, pre=plan.main, pre_level=room.comfort - PRESTAGE_DROP, main=latest[room.name])
        cost = cost_of({**plans, room.name: staged})
        for size in DESCENT_STEPS:
            while staged.main - STEP * size > staged.pre:
                trial = replace(staged, main=staged.main - STEP * size)
                trial_cost = cost_of({**plans, room.name: trial})
                if trial_cost >= cost - 1e-6:
                    break
                staged, cost = trial, trial_cost
        if cost < best - 1e-6:
            plans, best = {**plans, room.name: staged}, cost

    # Ein warmer Raum kuehlt nur ab, wenn er laut fertigem Plan mindestens
    # COOLDOWN_MIN lang nicht heizt; sonst lohnen die zwei Befehle nicht.
    for room in movable:
        start = plans[room.name].heat_start()
        if room.committed == "warm" and start is not None and start - now < COOLDOWN_MIN:
            plans = {**plans, room.name: make(room, now)}

    result = run(plans)
    nominal = run(plans, cautious=False)
    for room in planned:
        plan = plans[room.name]
        plan.reach = result.reach[room.name]
        plan.reach_nominal = nominal.reach[room.name]
        plan.late = plan.reach is None or plan.reach > plan.deadline
    return PlanResult(plans, _idle_window(rooms, plans), _cost(rooms, plans, result, until))


def _next_event(room: RoomInput, now: datetime) -> tuple[datetime, datetime] | None:
    """Naechster Termin, der noch beginnt und im Horizont liegt."""
    for start, end in room.events:
        if start > now:
            return (start, end) if start - EARLY <= now + HORIZON else None
    return None


def plan_signature(rooms: list[RoomInput], now: datetime) -> tuple:
    """Alles, was bei Aenderung eine neue Planung erzwingt (Termine, Einstellungen)."""
    items = []
    for room in sorted(rooms, key=lambda r: r.name):
        events = tuple(
            (start, end) for start, end in room.events if end > now and start - EARLY <= now + HORIZON
        )
        items.append((room.name, events, room.comfort, room.setback, room.priority, room.fixed))
    return tuple(items)


def plan_still_valid(
    rooms: list[RoomInput],
    previous: PlanResult,
    plant: PlantParams,
    state: PlantState,
    outside: Callable[[datetime], float],
    now: datetime,
) -> bool:
    """Geht der bestehende Plan aus dem aktuellen Zustand noch auf?

    Ein Simulationslauf. Ungueltig, wenn ein Raum laut Plan puenktlich sein
    sollte und jetzt mehr als einen Rechenschritt zu spaet kaeme.
    """
    plans = previous.plans
    deadlines = [p.deadline for p in plans.values() if p.deadline is not None]
    if not deadlines:
        return True
    until = max(deadlines) + EXTRA
    result = simulate(rooms, plans, plant, state, outside, now, until, True, previous.idle)
    for room in rooms:
        plan = plans.get(room.name)
        if plan is None or plan.deadline is None or plan.deadline <= now:
            continue
        reached = result.reach[room.name]
        if not plan.late and (reached is None or reached > plan.deadline + STEP):
            if room.temp < room.comfort - REACH_TOLERANCE:
                return False
    return True
