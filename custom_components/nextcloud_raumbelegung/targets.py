"""Zieltemperatur je Termin.

Reihenfolge, die erste passende Angabe gilt:
1. Angabe im Termin, als Schlagwort (Nextcloud: Tag, im Kalender CATEGORIES)
   oder in der Beschreibung:
       Tag:          "20", "20 °C", "20,5°", "Temperatur 20", "Saal 20 °C"
       Beschreibung: "Temperatur: 21", "Temperatur Saal: 20"
   Ohne Raum gilt der Wert fuer alle gebuchten Raeume, mit Raum nur dort;
   die Angabe fuer den Raum geht der allgemeinen vor.
2. Regeln aus den Optionen, eine je Zeile, die erste passende gilt:
       gottesdienst @ Saal = -1,5 K
       jugend = 20
   Das Stichwort wird ohne Gross-/Kleinschreibung im Titel und in den
   Schlagworten gesucht ("gottesdienst" passt auch auf "Abendmahlgottesdienst").
   "@ Raum, Raum" beschraenkt die Regel auf diese Raeume.
3. Die Komforttemperatur des Raums.

Ein Wert mit Vorzeichen oder mit "K" ist relativ zur Komforttemperatur, sonst
absolut in Grad; "°C" ist nicht noetig. Unrealistische Werte (ausserhalb
15-25 °C, relativ mehr als 5 K) werden nicht verwendet: in Regeln lehnt der
Optionsdialog sie ab, im Termin werden sie uebergangen und als Hinweis
gemeldet. Laufen mehrere Termine gleichzeitig, gilt der hoechste Wert.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable

MIN_TARGET = 15.0
MAX_TARGET = 25.0
MAX_RELATIVE = 5.0

_SIGN = r"[+\-−–]"  # Plus, Minus, typografisches Minus, Gedankenstrich
_NUMBER = r"\d{1,2}(?:[.,]\d)?(?!\d)"
_UNIT = r"(?:K\b|°\s*C?|Grad\b)?"
_VALUE = re.compile(rf"^({_SIGN}?)\s*(\d{{1,2}}(?:[.,]\d+)?)\s*(K|°\s*C?|Grad)?$", re.IGNORECASE)
# Beschreibung: "Temperatur: 21", "Temperatur Saal = 20 °C"
_DESCRIPTION = re.compile(
    rf"temperatur(?:\s+(?P<room>[^:=\n]+?))?\s*[:=]\s*(?P<value>{_SIGN}?\s*{_NUMBER}\s*{_UNIT})",
    re.IGNORECASE,
)
# Schlagwort, das nur aus einer Temperatur besteht, wahlweise mit "Temperatur"/"Temp." und Raum davor
_TAG = re.compile(
    rf"^(?:temp(?:eratur)?\.?\s*)?(?P<room>[^\d+\-−–:=]*?)\s*[:=]?\s*(?P<value>{_SIGN}?\s*{_NUMBER}\s*{_UNIT})$",
    re.IGNORECASE,
)


def _norm(text: str) -> str:
    """Vergleichsform fuer Raumnamen: klein, Umlaute ausgeschrieben, nur Buchstaben und Ziffern."""
    text = text.casefold()
    for old, new in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        text = text.replace(old, new)
    return re.sub(r"[^a-z0-9]", "", text)


@dataclass(frozen=True)
class Value:
    """Absoluter Wert in Grad oder Abstand zur Komforttemperatur in Kelvin."""

    amount: float
    relative: bool

    def problem(self) -> str | None:
        """Warum der Wert unrealistisch ist, sonst None."""
        if self.relative and abs(self.amount) > MAX_RELATIVE:
            return f"mehr als {MAX_RELATIVE:.0f} K Abweichung"
        if not self.relative and not MIN_TARGET <= self.amount <= MAX_TARGET:
            return f"außerhalb {MIN_TARGET:.0f}–{MAX_TARGET:.0f} °C"
        return None

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
    rooms: tuple[str, ...]  # Vergleichsform; leer = alle Raeume
    value: Value
    line: str

    def matches(self, room: str, texts: Iterable[str]) -> bool:
        if self.rooms and _norm(room) not in self.rooms:
            return False
        return any(self.keyword in text.casefold() for text in texts)


@dataclass
class Target:
    """Ergebnis fuer einen Termin in einem Raum."""

    value: float
    source: str  # "Raum", "Termin" oder "Regel"
    detail: str = ""  # Regelzeile oder woher die Angabe im Termin stammt
    hint: str = ""  # uebergangene Angabe im Termin

    def label(self) -> str:
        if self.source == "Regel":
            return f"Regel „{self.detail}“"
        if self.source == "Termin":
            return f"Angabe im Termin ({self.detail})" if self.detail else "Angabe im Termin"
        return "Komforttemperatur des Raums"


class RuleError(ValueError):
    """Ungueltige Regelzeile; args[0] = Zeilennummer, args[1] = Zeile, args[2] = Grund."""


def parse_value(text: str) -> Value | None:
    """Zahl mit optionalem Vorzeichen und Einheit; None, wenn es keine ist."""
    match = _VALUE.match(text.strip())
    if not match:
        return None
    sign, number, unit = match.groups()
    amount = float(number.replace(",", "."))
    relative = bool(sign) or (unit or "").upper() == "K"
    if sign and sign != "+":
        amount = -amount
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
            raise RuleError(number, raw.strip(), "kein „=“")
        head, value_text = line.rsplit(separator, 1)
        value = parse_value(value_text)
        keyword, _, room_text = head.partition("@")
        keyword = keyword.strip().casefold()
        if not keyword:
            raise RuleError(number, raw.strip(), "kein Stichwort")
        if value is None:
            raise RuleError(number, raw.strip(), "kein Wert")
        if (problem := value.problem()) is not None:
            raise RuleError(number, raw.strip(), problem)
        rooms = tuple(_norm(r) for r in room_text.split(",") if r.strip())
        rules.append(Rule(keyword, rooms, value, line))
    return rules


def _mentions(description: str, categories: Iterable[str]) -> list[tuple[str, Value, str]]:
    """Alle Temperaturangaben im Termin: (Raum oder "", Wert, Herkunft)."""
    found: list[tuple[str, Value, str]] = []
    for category in categories:
        match = _TAG.match(category.strip())
        if match and (value := parse_value(match.group("value"))) is not None:
            found.append((match.group("room").strip(), value, f"Tag „{category.strip()}“"))
    for match in _DESCRIPTION.finditer(description or ""):
        if (value := parse_value(match.group("value"))) is not None:
            found.append(((match.group("room") or "").strip(), value, "Beschreibung"))
    return found


def find_override(description: str, categories: Iterable[str], room: str) -> tuple[Value | None, str, str]:
    """Angabe im Termin fuer diesen Raum: (Wert, Herkunft, Hinweis auf uebergangene Angaben).

    Angaben fuer den Raum gehen allgemeinen vor; Angaben fuer andere Raeume oder
    mit unbekanntem Wort davor zaehlen hier nicht.
    """
    own = _norm(room)
    chosen: dict[bool, tuple[Value, str]] = {}
    skipped: list[str] = []
    for named, value, origin in _mentions(description, categories):
        specific = bool(named)
        if specific and _norm(named) != own:
            continue
        if (problem := value.problem()) is not None:
            skipped.append(f"{value.text()} ({origin}) übergangen: {problem}")
            continue
        chosen.setdefault(specific, (value, origin))
    hint = "; ".join(skipped)
    for specific in (True, False):
        if specific in chosen:
            value, origin = chosen[specific]
            return value, origin, hint
    return None, "", hint


def parse_override(description: str, room: str) -> Value | None:
    """Nur die Beschreibung auswerten (fuer Tests und Abwaertskompatibilitaet)."""
    return find_override(description, [], room)[0]


def event_target(
    room: str,
    summary: str,
    description: str,
    categories: Iterable[str],
    comfort: float,
    rules: list[Rule],
) -> Target:
    categories = list(categories)
    override, origin, hint = find_override(description, categories, room)
    if override is not None:
        return Target(override.apply(comfort), "Termin", origin, hint)
    texts = [summary, *categories]
    for rule in rules:
        if rule.matches(room, texts):
            return Target(rule.value.apply(comfort), "Regel", rule.line, hint)
    return Target(round(comfort, 2), "Raum", "", hint)
