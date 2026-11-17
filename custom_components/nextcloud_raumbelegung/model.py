"""Lernendes Aufheizmodell je Raum (reine Logik, ohne Home Assistant).

Physikalisches Modell erster Ordnung, waehrend der Raum voll heizt:

    dT/dt = p - l * (T_raum - T_aussen)        [K pro Stunde]

p: Heizleistung des Raums (K/h), l: Waermeverlust (1/h).
Beide Werte werden aus gemessenen Aufheizvorgaengen geschaetzt. Solange
wenige Messungen vorliegen, zieht ein vorsichtiger Startwert (Prior) das
Ergebnis in Richtung der heutigen Vorheizzeiten.

Der Sicherheitszuschlag kommt aus der Streuung frueherer Vorhersagen:
das 95-%-Quantil von (tatsaechliche Dauer / vorhergesagte Dauer).
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Callable

# Bezugspunkte fuer den Startwert: von 14 auf 21 Grad bei 0 Grad aussen.
PRIOR_FROM = 14.0
PRIOR_TO = 21.0
PRIOR_OUTSIDE = 0.0
PRIOR_LOSS = 0.15  # 1/h, typischer Wert fuer massive Gebaeude
PRIOR_WEIGHT = 4.0  # zaehlt wie so viele echte Messpunkte
X_REF = 15.0  # typische Differenz Raum - aussen waehrend des Aufheizens (K)

MAX_SAMPLES = 400
SAMPLE_DECAY = 0.995  # aeltere Messpunkte zaehlen etwas weniger
MIN_LOSS, MAX_LOSS = 0.02, 1.5
MAX_MINUTES = 12 * 60
STEP_MINUTES = 5

MARGIN_QUANTILE = 0.95
MIN_RATIOS_FOR_QUANTILE = 5
MAX_RATIOS = 30


def _time_to_reach(p: float, l: float, t_from: float, t_to: float, outside: float) -> float:
    """Minuten bis Zieltemperatur bei konstanter Aussentemperatur (analytisch)."""
    if t_to <= t_from:
        return 0.0
    equilibrium = outside + p / l
    if equilibrium <= t_to + 0.05:
        return float(MAX_MINUTES)
    hours = -math.log((equilibrium - t_to) / (equilibrium - t_from)) / l
    return min(hours * 60.0, float(MAX_MINUTES))


def prior_power(minutes: float, loss: float = PRIOR_LOSS) -> float:
    """Heizleistung p, mit der der Startwert genau 'minutes' ergibt."""
    low, high = 0.01, 200.0
    for _ in range(80):
        mid = (low + high) / 2
        if _time_to_reach(mid, loss, PRIOR_FROM, PRIOR_TO, PRIOR_OUTSIDE) > minutes:
            low = mid
        else:
            high = mid
    return high


@dataclass
class RoomModel:
    """Gelernte Aufheizeigenschaften eines Raums."""

    prior_minutes: float = 120.0
    samples: list[tuple[float, float]] = field(default_factory=list)  # (T_raum - T_aussen, K/h)
    ratios: list[float] = field(default_factory=list)  # tatsaechlich / vorhergesagt
    cycles: int = 0  # gueltige, gemessene Aufheizvorgaenge

    # ---------------------------------------------------------------- Lernen

    def add_sample(self, delta_outside: float, rate: float) -> None:
        """Messpunkt aus einer Aufheizphase aufnehmen (Rate in K/h)."""
        if not (-5.0 < rate < 30.0) or not (-10.0 < delta_outside < 50.0):
            return  # offensichtliche Messfehler
        self.samples.append((round(delta_outside, 2), round(rate, 3)))
        del self.samples[:-MAX_SAMPLES]

    def add_ratio(self, actual_minutes: float, predicted_minutes: float) -> None:
        """Ergebnis eines abgeschlossenen Aufheizvorgangs aufnehmen."""
        if predicted_minutes < 5 or actual_minutes <= 0:
            return
        self.ratios.append(round(actual_minutes / predicted_minutes, 3))
        del self.ratios[:-MAX_RATIOS]
        self.cycles += 1

    def parameters(self) -> tuple[float, float]:
        """Geschaetzte (p, l) aus Prior und Messpunkten (gewichtete Regression)."""
        # Regularisierte Regression um den Bezugspunkt x = X_REF:
        #   rate = q - l * (x - X_REF),  mit q = p - l * X_REF
        # q ist die Aufheizrate bei typischer Temperaturdifferenz und wird gut
        # gemessen; l ist aus kurzen Aufheizphasen schlecht bestimmbar und wird
        # staerker zum Startwert gezogen. So beeinflussen sich beide kaum.
        p0 = prior_power(self.prior_minutes)
        q0 = p0 - PRIOR_LOSS * X_REF
        lam_q = PRIOR_WEIGHT
        lam_l = PRIOR_WEIGHT * 25.0
        sw = sx = sy = sxx = sxy = 0.0
        weight = 1.0
        for x, y in reversed(self.samples):
            x -= X_REF
            sw += weight
            sx += weight * x
            sy += weight * y
            sxx += weight * x * x
            sxy += weight * x * y
            weight *= SAMPLE_DECAY
        # Normalgleichungen fuer r = y - q + l*x:
        a11, a12, b1 = sw + lam_q, -sx, sy + lam_q * q0
        a21, a22, b2 = -sx, sxx + lam_l, -sxy + lam_l * PRIOR_LOSS
        det = a11 * a22 - a12 * a21
        if abs(det) < 1e-9:
            return p0, PRIOR_LOSS
        rate_ref = (b1 * a22 - a12 * b2) / det
        loss = (a11 * b2 - a21 * b1) / det
        loss = min(max(loss, MIN_LOSS), MAX_LOSS)
        power = rate_ref + loss * X_REF
        if power <= 0.1:
            return p0, PRIOR_LOSS
        return power, loss

    # ------------------------------------------------------------ Vorhersage

    def predict_minutes(
        self,
        t_from: float,
        t_to: float,
        outside: Callable[[float], float] | float,
    ) -> float:
        """Reine Aufheizdauer in Minuten (ohne Zuschlag).

        outside: konstante Aussentemperatur oder Funktion Minuten -> Temperatur
        (fuer die Wetterprognose).
        """
        if t_to <= t_from:
            return 0.0
        power, loss = self.parameters()
        if not callable(outside):
            return _time_to_reach(power, loss, t_from, t_to, outside)
        temp = t_from
        minutes = 0.0
        while temp < t_to and minutes < MAX_MINUTES:
            out = outside(minutes)
            temp += (power - loss * (temp - out)) * STEP_MINUTES / 60.0
            minutes += STEP_MINUTES
        return min(minutes, float(MAX_MINUTES))

    def margin_factor(self) -> float:
        """Sicherheitszuschlag als Faktor auf die Vorhersage."""
        if len(self.ratios) < MIN_RATIOS_FOR_QUANTILE:
            return 1.25
        ordered = sorted(self.ratios)
        index = min(len(ordered) - 1, math.ceil(MARGIN_QUANTILE * len(ordered)) - 1)
        return min(max(ordered[index], 1.0), 2.5)

    def lead_minutes(
        self,
        t_from: float,
        t_to: float,
        outside: Callable[[float], float] | float,
    ) -> float:
        """Benoetigte Vorheizzeit inklusive Zuschlag.

        Vorsichtiger Start: Die Vorheizzeit darf je gueltigem Aufheizvorgang
        nur um 10 % des Startwerts unter den Startwert fallen. Nach zehn
        Vorgaengen gilt allein das gelernte Modell.
        """
        predicted = self.predict_minutes(t_from, t_to, outside) * self.margin_factor()
        if self.cycles < 10:
            # Startwert gilt fuer 14 -> 21 Grad; auf die tatsaechliche Spanne umrechnen.
            span = max(t_to - t_from, 0.0) / (PRIOR_TO - PRIOR_FROM)
            floor = self.prior_minutes * span * (1.0 - 0.1 * self.cycles)
            predicted = max(predicted, floor)
        return min(predicted, float(MAX_MINUTES))

    # ------------------------------------------------------------- Speichern

    def to_dict(self) -> dict:
        return {
            "samples": self.samples,
            "ratios": self.ratios,
            "cycles": self.cycles,
        }

    @classmethod
    def from_dict(cls, data: dict, prior_minutes: float) -> RoomModel:
        return cls(
            prior_minutes=prior_minutes,
            samples=[tuple(s) for s in data.get("samples", [])],
            ratios=list(data.get("ratios", [])),
            cycles=int(data.get("cycles", 0)),
        )
