"""Gemeinsame Anpassung aller Modellwerte an gemessene Verlaeufe (reine Logik).

Grundlage ist die Systemidentifikation mit einem Grey-Box-Modell: Die
Gleichungen kommen aus der Physik (physics.py), die Zahlen aus den Messungen.
Gesucht werden die Werte, mit denen das Modell die gemessenen Verlaeufe am
besten nachrechnet:

- Je Raum Auskuehlrate a, Aufheizrate b, Totzeit d, Traegheit der
  Heizkoerper tau und Leistungsbedarf w, fuer die Anlage die Pufferreserve.
- Nachgerechnet wird aus jedem Messpunkt (alle 30 min) 30, 60 und 120 min
  voraus, mit den gemessenen Ventilstellungen. Das ist genau die Vorhersage,
  die die Planung braucht.
- Verfahren: Levenberg-Marquardt (gewichtete kleinste Quadrate). Vorwissen
  (bayessch, MAP): das Ergebnis der letzten Anpassung, dessen Gewicht je Tag
  sinkt (Halbwertszeit etwa 12 Tage); beim ersten Mal die bisherigen
  Einzelschaetzungen mit breiter Unsicherheit. Feste Startwerte gibt es also
  nicht, nur Vorwissen, das Messungen ueberstimmen.
- Unsicherheit: Kehrwert der Fisher-Information (J^T J plus Vorwissen). Sie
  zeigt, welche Werte die bisherigen Verlaeufe gut festlegen und welche sich
  noch gegenseitig erklaeren koennen (z. B. Pufferreserve und Leistungsbedarf).
- Versuchsplanung: Fuer moegliche Testlaeufe wird berechnet, wie stark sie die
  Unsicherheit verkleinern wuerden (Informationsgewinn, D-optimal); der
  groesste Gewinn ist die Empfehlung.

Alles in reinem Python, ohne zusaetzliche Pakete; eine Anpassung ueber einen
Tag dauert Sekundenbruchteile, ueber zehn Tage wenige Sekunden.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import math
from typing import Any

from .physics import Delay, PlantParams, PlantState, RoomParams, hold_opening, plant_step, valve

STEP_H = 5 / 60  # Abstand der Messpunkte (h)
HORIZONS = (6, 12, 24)  # Messpunkte voraus: 30, 60, 120 min
START_EVERY = 6  # alle 30 min ein Vorhersagestart
WARMUP = 18  # Messpunkte Vorgeschichte (90 min) fuer Totzeit und Traegheit
SIGMA_ASSUMED = 0.2  # K, Massstab der Residuen waehrend der Anpassung
SIGMA_MIN = 0.15  # K, so genau kann ein Modell die Raumtemperatur hoechstens treffen
CORRELATION = 6.0  # ueberlappende Vorhersagen sind nicht unabhaengig: Information so viel kleiner
FLOW_SIGMA = 0.1  # Unsicherheit des aus dem Vorlauf gemessenen Versorgungsanteils
MAX_ITER = 15
DAY_FORGET = 0.944  # Vorwissen behaelt je Tag so viel Gewicht (wie DECAY je 5 min)

# Breite des Vorwissens bei der ersten Anpassung (Logarithmus bzw. Stunden)
PRIOR_SIGMA = {"a": 0.7, "b": 0.3, "d": 0.15, "tau": 0.7, "w": 0.7, "e_buf": 0.7}
LOG_KEYS = {"a", "b", "tau", "w", "e_buf"}
ROOM_KEYS = ("a", "b", "d", "tau", "w")
LOCAL_KEYS = ("a", "b", "d", "tau")  # wirken nur auf den eigenen Raum
BOUNDS = {
    "a": (math.log(0.005), math.log(0.5)),
    "b": (math.log(0.1), math.log(60.0)),
    "d": (0.0, 1.5),
    "tau": (math.log(0.02), math.log(2.0)),
    "w": (math.log(0.02), math.log(10.0)),
    "e_buf": (math.log(0.05), math.log(5.0)),
}
STEPS = {"a": 0.02, "b": 0.02, "d": 0.03, "tau": 0.05, "w": 0.03, "e_buf": 0.03}  # fuer die numerische Ableitung

Name = tuple[str | None, str]  # (Raum oder None fuer die Anlage, Groesse)


@dataclass
class FitInput:
    """Alles, was eine Anpassung braucht (Momentaufnahme, unabhaengig vom Learner)."""

    rows: list[dict[str, Any]]  # {"t": Sekunden, "o": aussen, "f": Vorlauf, "r": {Raum: [T, Ventil, Soll]}}
    prior_mean: dict[Name, float]  # natuerliche Einheiten
    prior_info: dict[Name, dict[Name, float]] | None  # Information im Transformationsraum, None = erste Anpassung
    t_start: float
    flow_full: float | None = None
    flow_room: float = 20.0
    flow_min_span: float = 15.0


@dataclass
class FitResult:
    names: list[Name]
    values: dict[Name, float]  # natuerliche Einheiten
    sigma: dict[Name, float]  # Logarithmus (= relativ) bzw. Stunden bei d
    info: dict[Name, dict[Name, float]]  # Information im Transformationsraum (Vorwissen fuers naechste Mal)
    covariance: list[list[float]]
    rms: float  # K, mittlerer Fehler der Vorhersagen
    rms_rooms: dict[str, float]
    residuals: int
    iterations: int
    start: float  # Zeit des ersten und letzten Messpunkts (Sekunden)
    end: float
    extra: dict[str, Any] = field(default_factory=dict)


# ------------------------------------------------------------------ Hilfen


def _to_internal(key: str, value: float) -> float:
    return math.log(max(value, 1e-6)) if key in LOG_KEYS else value


def _to_natural(key: str, value: float) -> float:
    return math.exp(value) if key in LOG_KEYS else value


def _clamp(key: str, value: float) -> float:
    low, high = BOUNDS[key]
    return min(max(value, low), high)


def _solve(matrix: list[list[float]], rhs: list[float]) -> list[float] | None:
    """Gauss-Elimination mit Spaltenpivot (kleine dichte Systeme)."""
    n = len(rhs)
    a = [row[:] + [rhs[i]] for i, row in enumerate(matrix)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(a[r][col]))
        if abs(a[pivot][col]) < 1e-12:
            return None
        a[col], a[pivot] = a[pivot], a[col]
        for r in range(col + 1, n):
            factor = a[r][col] / a[col][col]
            if factor:
                for c in range(col, n + 1):
                    a[r][c] -= factor * a[col][c]
    x = [0.0] * n
    for r in range(n - 1, -1, -1):
        x[r] = (a[r][n] - sum(a[r][c] * x[c] for c in range(r + 1, n))) / a[r][r]
    return x


def _inverse(matrix: list[list[float]]) -> list[list[float]] | None:
    n = len(matrix)
    columns = []
    for i in range(n):
        unit = [1.0 if j == i else 0.0 for j in range(n)]
        column = _solve(matrix, unit)
        if column is None:
            return None
        columns.append(column)
    return [[columns[j][i] for j in range(n)] for i in range(n)]


def _logdet(matrix: list[list[float]]) -> float | None:
    """log det einer symmetrisch positiv definiten Matrix (Cholesky)."""
    n = len(matrix)
    low = [[0.0] * n for _ in range(n)]
    total = 0.0
    for i in range(n):
        for j in range(i + 1):
            s = matrix[i][j] - sum(low[i][k] * low[j][k] for k in range(j))
            if i == j:
                if s <= 0:
                    return None
                low[i][i] = math.sqrt(s)
                total += math.log(low[i][i])
            else:
                low[i][j] = s / low[j][j]
    return 2.0 * total


# ------------------------------------------------------------- Nachrechnen


class _Model:
    """Rechnet die gespeicherten Messpunkte mit einem Parametersatz nach."""

    def __init__(self, data: FitInput, names: list[Name]) -> None:
        self.data = data
        self.names = names
        self.rows = data.rows
        self.rooms = sorted({n[0] for n in names if n[0] is not None})
        n = len(self.rows)
        # Luecken (Home Assistant aus): dort beginnt ein neuer Abschnitt
        self.breaks = {i for i in range(1, n) if self.rows[i]["t"] - self.rows[i - 1]["t"] > 600}
        self.outside = self._fill([r.get("o") for r in self.rows])
        self.series: dict[str, tuple[list, list]] = {}
        for room in self.rooms:
            temps = [(r["r"].get(room) or [None])[0] for r in self.rows]
            valves = [
                (r["r"][room][1] if room in r["r"] and len(r["r"][room]) > 1 else None) for r in self.rows
            ]
            # Fehlende Ventilwerte: letzter bekannter Wert (sonst fehlte der Raum im
            # Bedarf und ein Engpass wuerde falsch erklaert)
            self.series[room] = (temps, self._fill(valves))
        self.measured_share = None
        if data.flow_full is not None and data.flow_full - data.flow_room >= data.flow_min_span:
            values = [r.get("f") for r in self.rows]
            if sum(1 for v in values if v is not None) >= n / 2:
                span = data.flow_full - data.flow_room
                self.measured_share = [
                    None if v is None else min(max((v - data.flow_room) / span, 0.0), 1.0) for v in values
                ]

    def _fill(self, values: list) -> list:
        """Luecken mit dem letzten bekannten Wert fuellen, nicht ueber Abschnittsgrenzen."""
        result, last = [], None
        for i, value in enumerate(values):
            if i in self.breaks:
                last = None
            if value is None:
                value = last
            else:
                last = value
            result.append(value)
        return result

    def params(self, vector: list[float]) -> tuple[dict[str, RoomParams], float]:
        values = {name: _to_natural(name[1], v) for name, v in zip(self.names, vector)}
        rooms = {}
        for room in self.rooms:
            rooms[room] = RoomParams(
                a=values[(room, "a")],
                b=values[(room, "b")],
                w=values[(room, "w")],
                d=values[(room, "d")],
                tau=values[(room, "tau")],
            )
        return rooms, values[(None, "e_buf")]

    def shares(self, rooms: dict[str, RoomParams], e_buf: float) -> list[float]:
        plant = PlantParams(e_buf=e_buf, t_start=self.data.t_start)
        state = PlantState(e_buf)
        result = []
        for i, row in enumerate(self.rows):
            if i in self.breaks:
                state = PlantState(e_buf)
            demand = 0.0
            for room, params in rooms.items():
                v = self.series[room][1][i]
                demand += params.w * (v or 0.0)
            _, share = plant_step(demand, plant, state, STEP_H)
            result.append(share)
        return result

    def room_residuals(self, room: str, params: RoomParams, shares: list[float]) -> list[float]:
        temps, valves = self.series[room]
        n = len(self.rows)
        out: list[float] = []
        last = max(HORIZONS)
        for s in range(0, n - 1, START_EVERY):
            if temps[s] is None or self.outside[s] is None or valves[s] is None:
                continue
            temp = temps[s]
            # Totzeit und Traegheit mit der Vorgeschichte fuellen (bis WARMUP
            # Messpunkte zurueck), sonst saehe ein gerade geoeffnetes Ventil so
            # aus, als liefe die Waerme schon lange
            first = s
            while first > 0 and s - first < WARMUP and (first not in self.breaks) and valves[first - 1] is not None:
                first -= 1
            delay = Delay(params.d, STEP_H, valves[first] * shares[first], params.tau)
            for i in range(first, s):
                delay.push(valves[i] * shares[i])
            for k in range(1, last + 1):
                i = s + k - 1
                if i + 1 >= n or (i + 1) in self.breaks:
                    break
                u, o = valves[i], self.outside[i]
                if u is None or o is None:
                    break
                temp += (params.b * delay.push(u * shares[i]) - params.a * (temp - o)) * STEP_H
                if k in HORIZONS and temps[i + 1] is not None:
                    out.append((temp - temps[i + 1]) / SIGMA_ASSUMED)
        return out

    def flow_residuals(self, shares: list[float]) -> list[float]:
        if self.measured_share is None:
            return []
        return [(s - m) / FLOW_SIGMA for s, m in zip(shares, self.measured_share) if m is not None]

    def residuals(self, vector: list[float]) -> dict[str, list[float]]:
        rooms, e_buf = self.params(vector)
        shares = self.shares(rooms, e_buf)
        result = {room: self.room_residuals(room, rooms[room], shares) for room in self.rooms}
        result["_flow"] = self.flow_residuals(shares)
        return result

    def jacobian(self, vector: list[float], base: dict[str, list[float]]) -> list[dict[str, list[float]]]:
        """Spalten der Ableitungsmatrix, je Spalte nur die Gruppen, die sich aendern.

        a, b, d eines Raums wirken nur auf diesen Raum; w und die Pufferreserve
        ueber den Versorgungsanteil auf alle.
        """
        rooms, e_buf = self.params(vector)
        shares = self.shares(rooms, e_buf)
        columns = []
        for index, (room, key) in enumerate(self.names):
            h = STEPS[key]
            shifted = vector[:]
            shifted[index] += h
            if room is not None and key in LOCAL_KEYS:
                params = replace(rooms[room], **{key: _to_natural(key, shifted[index])})
                new = self.room_residuals(room, params, shares)
                columns.append({room: [(x - y) / h for x, y in zip(new, base[room])]})
            else:
                changed = self.residuals(shifted)
                columns.append(
                    {g: [(x - y) / h for x, y in zip(changed[g], base[g])] for g in base if base[g]}
                )
        return columns


def _normal(columns: list[dict[str, list[float]]], residual: dict[str, list[float]]) -> tuple[list[list[float]], list[float]]:
    n = len(columns)
    jtj = [[0.0] * n for _ in range(n)]
    jtr = [0.0] * n
    for i in range(n):
        ci = columns[i]
        for group, values in ci.items():
            jtr[i] += sum(x * y for x, y in zip(values, residual[group]))
        for j in range(i, n):
            cj = columns[j]
            total = 0.0
            for group, values in ci.items():
                other = cj.get(group)
                if other is not None:
                    total += sum(x * y for x, y in zip(values, other))
            jtj[i][j] = jtj[j][i] = total
    return jtj, jtr


def _sum_squares(residual: dict[str, list[float]]) -> float:
    return sum(x * x for values in residual.values() for x in values)


# --------------------------------------------------------------- Anpassen


def fit(data: FitInput) -> FitResult | None:
    """Alle Werte gemeinsam an die Messpunkte anpassen (MAP, Levenberg-Marquardt)."""
    if len(data.rows) < max(HORIZONS) + 1:
        return None
    rooms = sorted(
        {room for row in data.rows for room in row["r"] if (room, "a") in data.prior_mean}
    )
    if not rooms:
        return None
    names: list[Name] = [(room, key) for room in rooms for key in ROOM_KEYS] + [(None, "e_buf")]
    n = len(names)
    prior = [_clamp(k, _to_internal(k, data.prior_mean[(r, k)])) for r, k in names]
    info = [[0.0] * n for _ in range(n)]
    for i, name in enumerate(names):
        stored = (data.prior_info or {}).get(name)
        for j, other in enumerate(names):
            if stored is not None and other in stored:
                info[i][j] = stored[other]
        if info[i][i] <= 0:
            info[i][i] = 1.0 / PRIOR_SIGMA[name[1]] ** 2
            for j in range(n):
                if j != i:
                    info[i][j] = info[j][i] = 0.0

    model = _Model(data, names)

    def prior_cost(vector: list[float]) -> float:
        diff = [x - y for x, y in zip(vector, prior)]
        return sum(diff[i] * info[i][j] * diff[j] for i in range(n) for j in range(n))

    vector = prior[:]
    residual = model.residuals(vector)
    if sum(len(v) for v in residual.values()) < 10:
        return None
    cost = _sum_squares(residual) + prior_cost(vector)
    mu = 1e-2
    iterations = 0
    for iterations in range(1, MAX_ITER + 1):
        columns = model.jacobian(vector, residual)
        jtj, jtr = _normal(columns, residual)
        diff = [x - y for x, y in zip(vector, prior)]
        grad = [jtr[i] + sum(info[i][j] * diff[j] for j in range(n)) for i in range(n)]
        hess = [[jtj[i][j] + info[i][j] for j in range(n)] for i in range(n)]
        improved = False
        for _ in range(8):
            damped = [row[:] for row in hess]
            for i in range(n):
                damped[i][i] += mu * max(hess[i][i], 1e-6)
            step = _solve(damped, [-g for g in grad])
            if step is None:
                mu *= 10
                continue
            trial = [_clamp(name[1], v + s) for name, v, s in zip(names, vector, step)]
            trial_residual = model.residuals(trial)
            trial_cost = _sum_squares(trial_residual) + prior_cost(trial)
            if trial_cost < cost:
                change = cost - trial_cost
                vector, residual, cost = trial, trial_residual, trial_cost
                mu = max(mu / 3, 1e-6)
                improved = True
                break
            mu *= 4
        if not improved or change < 1e-4 * cost:
            break

    # Unsicherheit: Information der Messungen (mit tatsaechlichem Fehler und
    # Korrelation der ueberlappenden Vorhersagen) plus Vorwissen
    raw = [x * SIGMA_ASSUMED for g, values in residual.items() if g != "_flow" for x in values]
    rms = math.sqrt(sum(x * x for x in raw) / len(raw)) if raw else 0.0
    sigma = max(SIGMA_MIN, rms)
    columns = model.jacobian(vector, residual)
    jtj, _ = _normal(columns, residual)
    scale = (SIGMA_ASSUMED / sigma) ** 2 / CORRELATION
    posterior = [[jtj[i][j] * scale + info[i][j] for j in range(n)] for i in range(n)]
    covariance = _inverse(posterior) or [[0.0] * n for _ in range(n)]
    rms_rooms = {}
    for room in rooms:
        values = residual.get(room) or []
        if values:
            rms_rooms[room] = math.sqrt(sum(x * x for x in values) / len(values)) * SIGMA_ASSUMED
    return FitResult(
        names=names,
        values={name: _to_natural(name[1], v) for name, v in zip(names, vector)},
        sigma={name: math.sqrt(max(covariance[i][i], 0.0)) for i, name in enumerate(names)},
        info={names[i]: {names[j]: posterior[i][j] for j in range(n) if posterior[i][j]} for i in range(n)},
        covariance=covariance,
        rms=rms,
        rms_rooms=rms_rooms,
        residuals=len(raw),
        iterations=iterations,
        start=data.rows[0]["t"],
        end=data.rows[-1]["t"],
    )


def forget(info: dict[Name, dict[Name, float]], days: float) -> dict[Name, dict[Name, float]]:
    """Vorwissen altern lassen: aeltere Messungen zaehlen weniger."""
    factor = DAY_FORGET ** max(days, 0.0)
    return {a: {b: v * factor for b, v in row.items()} for a, row in info.items()}


# ---------------------------------------------------------- Versuchsplanung


@dataclass
class Candidate:
    label: str
    rooms: list[str]


def _simulate_run(
    rooms: dict[str, RoomParams],
    plant: PlantParams,
    heated: list[str],
    target: float,
    others: float,
    start: float,
    outside: float,
    hours: float,
) -> tuple[dict[str, list[float]], float | None]:
    """Testlauf im Modell: Temperaturen alle 15 min, Dauer bis alle warm (h)."""
    state = PlantState(plant.e_buf)
    temps = {r: start for r in rooms}
    delays = {r: Delay(p.d, STEP_H, 0.0, p.tau) for r, p in rooms.items()}
    out: dict[str, list[float]] = {r: [] for r in rooms}
    done = None
    steps = int(hours / STEP_H)
    for k in range(steps):
        valves = {}
        for room, params in rooms.items():
            setpoint = target if room in heated else others
            valves[room] = valve(setpoint, temps[room], params.curve, hold_opening(params, temps[room], outside))
        demand = sum(rooms[r].w * valves[r] for r in rooms)
        _, share = plant_step(demand, plant, state, STEP_H)
        for room, params in rooms.items():
            u = delays[room].push(valves[room] * share)
            temps[room] += (params.b * u - params.a * (temps[room] - outside)) * STEP_H
        if (k + 1) % 3 == 0:
            for room in rooms:
                out[room].append(temps[room])
        if done is None and all(temps[r] >= target - 0.5 for r in heated):
            done = (k + 1) * STEP_H
    return out, done


def rank_experiments(
    result: FitResult,
    curves: dict[str, tuple[float, ...]],
    t_start: float,
    candidates: list[Candidate],
    outside: float,
    target: float = 21.0,
    others: float = 10.0,
    start: float = 14.0,
    hours: float = 6.0,
) -> list[dict[str, Any]]:
    """Moegliche Testlaeufe nach Informationsgewinn ordnen (D-optimal).

    Gewinn = halbe Differenz von log det der Information mit und ohne den Lauf;
    1,0 heisst: das Volumen der Unsicherheit schrumpft um den Faktor e.
    """
    names = result.names
    n = len(names)
    vector = [_to_internal(k, result.values[(r, k)]) for r, k in names]
    posterior = [[result.info.get(a, {}).get(b, 0.0) for b in names] for a in names]
    base_logdet = _logdet(posterior)
    if base_logdet is None:
        return []
    room_names = sorted({r for r, _ in names if r is not None})

    def params(vec: list[float]) -> tuple[dict[str, RoomParams], PlantParams]:
        values = {name: _to_natural(name[1], v) for name, v in zip(names, vec)}
        rooms = {
            r: RoomParams(
                a=values[(r, "a")],
                b=values[(r, "b")],
                w=values[(r, "w")],
                d=values[(r, "d")],
                tau=values[(r, "tau")],
                curve=curves.get(r),
            )
            for r in room_names
        }
        return rooms, PlantParams(e_buf=values[(None, "e_buf")], t_start=t_start)

    scale = (SIGMA_ASSUMED / max(SIGMA_MIN, result.rms)) ** 2 / CORRELATION
    ranked = []
    for candidate in candidates:
        rooms, plant = params(vector)
        base, done = _simulate_run(rooms, plant, candidate.rooms, target, others, start, outside, hours)
        flat = [x for r in room_names for x in base[r]]
        columns = []
        for i, (_room, key) in enumerate(names):
            shifted = vector[:]
            shifted[i] += STEPS[key]
            r2, p2 = params(shifted)
            changed, _ = _simulate_run(r2, p2, candidate.rooms, target, others, start, outside, hours)
            new = [x for r in room_names for x in changed[r]]
            columns.append([(x - y) / STEPS[key] / SIGMA_ASSUMED for x, y in zip(new, flat)])
        added = [[sum(x * y for x, y in zip(columns[i], columns[j])) * scale for j in range(n)] for i in range(n)]
        total = [[posterior[i][j] + added[i][j] for j in range(n)] for i in range(n)]
        logdet = _logdet(total)
        gain = 0.5 * (logdet - base_logdet) if logdet is not None else 0.0
        after = _inverse(total)
        improved = []
        if after is not None:
            for i, name in enumerate(names):
                before = result.sigma.get(name) or 0.0
                now = math.sqrt(max(after[i][i], 0.0))
                if before > 0:
                    improved.append((1 - now / before, name))
        improved.sort(key=lambda item: item[0], reverse=True)
        ranked.append(
            {
                "label": candidate.label,
                "rooms": candidate.rooms,
                "gain": round(gain, 2),
                "hours": None if done is None else round(done, 1),
                "improves": [
                    {"raum": r or "Anlage", "groesse": k, "unsicherheit_minus_prozent": round(100 * f)}
                    for f, (r, k) in improved[:3]
                    if f > 0.05
                ],
            }
        )
    ranked.sort(key=lambda item: item["gain"], reverse=True)
    return ranked


def candidates_for(rooms: dict[str, RoomParams]) -> list[Candidate]:
    """Sinnvolle Testlaeufe: jeder Raum allein, der groesste (meiste Leistung)
    mit jedem anderen, alle ausser dem groessten, alle zusammen."""
    if not rooms:
        return []
    load = {r: p.w * max(p.curve or (1.0,)) for r, p in rooms.items()}
    order = sorted(rooms, key=lambda r: load[r], reverse=True)
    result = [Candidate(f"{r} allein", [r]) for r in order]
    result += [Candidate(f"{order[0]} und {r}", [order[0], r]) for r in order[1:]]
    if len(order) >= 3:
        result.append(Candidate(f"alle außer {order[0]}", order[1:]))
        result.append(Candidate("alle zusammen", order))
    return result


def duration_uncertainty(result: FitResult, room: str, base: RoomParams, plant: PlantParams, minutes) -> float:
    """Relative Unsicherheit der Aufheizdauer eines Raums (aus a, b, d und deren Kovarianz).

    minutes(params) -> Aufheizdauer allein in Minuten.
    """
    keys = [k for k in LOCAL_KEYS if (room, k) in result.values]
    index = {name: i for i, name in enumerate(result.names)}
    reference = minutes(base)
    if reference <= 0:
        return 0.0
    grad = []
    for key in keys:
        value = getattr(base, key)
        step = 0.05
        changed = replace(base, **{key: math.exp(math.log(value) + step) if key in LOG_KEYS else value + step})
        grad.append((minutes(changed) - reference) / reference / step)
    var = 0.0
    for i, ki in enumerate(keys):
        for j, kj in enumerate(keys):
            var += grad[i] * grad[j] * result.covariance[index[(room, ki)]][index[(room, kj)]]
    return math.sqrt(max(var, 0.0))
