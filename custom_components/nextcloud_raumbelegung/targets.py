"""Zieltemperatur je Termin.

Reihenfolge, die erste passende Angabe gilt:
1. Angabe in der Terminbeschreibung: "Temperatur: 21" (alle Raeume des
   Termins) oder "Temperatur Saal: 20" (nur dieser Raum).
2. Regeln aus den Optionen, eine je Zeile, die erste passende gilt:
       gottesdienst @ Saal = -1,5 K
       jugend = 20
   Das Stichwort wird ohne Gross-/Kleinschreibung im Titel und in den
   Kategorien gesucht ("gottesdienst" passt auch auf "Abendmahlgottesdienst").
   "@ Raum, Raum" beschraenkt die Regel auf diese Raeume.
3. Die Komforttemperatur des Raums.

Ein Wert mit Vorzeichen oder mit "K" ist relativ zur Komforttemperatur, sonst
absolut in Grad. Laufen mehrere Termine gleichzeitig, gilt der hoechste Wert.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable

MIN_TARGET = 16.0
MAX_TARGET = 24.0

_SIGN = r"[+\-−–]"  # Plus, Minus, typografisches Minus, Gedankenstrich
_VALUE = re.compile(rf"^({_SIGN}?)\s*(\d{{1,2}}(?:[.,]\d+)?)\s*(K|°\s*C?|Grad)?$", re.IGNORECASE)
_OVERRIDE = re.compile(
    rf"temperatur(?:\s+(?P<room>[^:=\n]+?))?\s*[:=]\s*"
    rf"(?P<value>{_SIGN}?\s*\d{{1,2}}(?:[.,]\d)?(?!\d)\s*(?:K\b|°\s*C?|Grad\b)?)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Value:
    """Absoluter Wert in Grad oder Abstand zur Komforttemperatur in Kelvin."""

    amount: float
    relative: bool

    def apply(self, comfort: float) -> float:
        value = comfort + self.amount if self.relative else self.amount
        return round(min(MAX_TARGET, max(MIN_TARGET, value)), 2)

    def text(self) -> str:
        number = f"{self.amount:+.1f}" if self.relative else f"{self.amount:.1f}"
        number = number.replace(".", ",").replace(",0", "")
        return f"{number} K" if self.relative else f"{number} °C"


@dataclass(frozen=True)
class Rule:
    keyword: str  # klein geschrieben
    rooms: tuple[str, ...]  # klein geschrieben; leer = alle Raeume
    value: Value
    line: str

    def matches(self, room: str, texts: Iterable[str]) -> bool:
        if self.rooms and room.casefold() not in self.rooms:
            return False
        return any(self.keyword in text.casefold() for text in texts)


@dataclass
class Target:
    """Ergebnis fuer einen Termin in einem Raum."""

    value: float
    source: str  # "Raum", "Termin" oder "Regel"
    detail: str = ""  # z. B. die Regelzeile

    def label(self) -> str:
        if self.source == "Regel":
            return f"Regel „{self.detail}“"
        if self.source == "Termin":
            return "Angabe im Termin"
        return "Komforttemperatur des Raums"


class RuleError(ValueError):
    """Ungueltige Regelzeile; args[0] = Zeilennummer, args[1] = Zeile."""


def parse_value(text: str) -> Value | None:
    match = _VALUE.match(text.strip())
    if not match:
        return None
    sign, number, unit = match.groups()
    amount = float(number.replace(",", "."))
    relative = bool(sign) or (unit or "").upper() == "K"
    if sign and sign != "+":
        amount = -amount
    if not relative and not 5.0 <= amount <= 30.0:
        return None
    if relative and abs(amount) > 6.0:
        return None
    return Value(amount, relative)


def parse_rules(text: str | None) -> list[Rule]:
    """Regeln aus dem Optionsfeld lesen; leere Zeilen und '#'-Kommentare zaehlen nicht."""
    rules: list[Rule] = []
    for number, raw in enumerate((text or "").splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        separator = "=" if "=" in line else ":"
        if separator not in line:
            raise RuleError(number, raw.strip())
        head, value_text = line.rsplit(separator, 1)
        value = parse_value(value_text)
        keyword, _, room_text = head.partition("@")
        keyword = keyword.strip().casefold()
        if value is None or not keyword:
            raise RuleError(number, raw.strip())
        rooms = tuple(r.strip().casefold() for r in room_text.split(",") if r.strip())
        rules.append(Rule(keyword, rooms, value, line))
    return rules


def parse_override(description: str, room: str) -> Value | None:
    """'Temperatur: 21' oder 'Temperatur Saal: 20' aus der Beschreibung; der Raum geht vor."""
    general = None
    for match in _OVERRIDE.finditer(description or ""):
        value = parse_value(match.group("value"))
        if value is None:
            continue
        named = (match.group("room") or "").strip()
        if not named:
            general = general or value
        elif named.casefold() == room.casefold():
            return value
    return general


def event_target(
    room: str,
    summary: str,
    description: str,
    categories: Iterable[str],
    comfort: float,
    rules: list[Rule],
) -> Target:
    override = parse_override(description, room)
    if override is not None:
        return Target(override.apply(comfort), "Termin", override.text())
    texts = [summary, *categories]
    for rule in rules:
        if rule.matches(room, texts):
            return Target(rule.value.apply(comfort), "Regel", rule.line)
    return Target(round(comfort, 2), "Raum")

