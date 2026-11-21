"""Zieltemperatur je Termin.

Reihenfolge, die erste passende Angabe gilt:
1. Temperatur-Tag im Termin (Nextcloud: Tag, im Kalender CATEGORIES). Der Tag
   muss das Wort "Temperatur" enthalten, die Reihenfolge ist frei:
       "Temperatur 20", "20 °C Temperatur", "Temperatur Saal 20°C",
       "20 Temperatur Saal", "Saal Temperatur: 19,5"
   Ohne Raum gilt der Wert fuer alle als Ressource gebuchten Raeume, mit Raum
   nur dort; die Angabe fuer den Raum geht der allgemeinen vor. Ein Raum, der
   im Termin nicht als Ressource gebucht ist, ist nicht erlaubt.
2. Regeln aus den Optionen, eine je Zeile, die erste passende gilt:
       gottesdienst @ Saal = -1,5 K
       jugend = 20
   Das Stichwort wird ohne Gross-/Kleinschreibung im Titel und in den Tags
   gesucht ("gottesdienst" passt auch auf "Abendmahlgottesdienst").
   "@ Raum, Raum" beschraenkt die Regel auf diese Raeume.
3. Die Komforttemperatur des Raums.

Ein Wert mit Vorzeichen oder mit "K" ist relativ zur Komforttemperatur, sonst
absolut in Grad; "°C" ist nicht noetig. Unrealistische Werte (ausserhalb
15-25 °C, relativ mehr als 5 K) zaehlen nicht: in Regeln lehnt der
Optionsdialog sie ab, Tags werden uebergangen und als Hinweis gemeldet,
ebenso Tags mit nicht gebuchtem oder unbekanntem Raum. Laufen mehrere
Termine gleichzeitig, gilt der hoechste Wert.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Iterable

MIN_TARGET = 15.0
MAX_TARGET = 25.0
MAX_RELATIVE = 5.0

_SIGN = r"[+\-−–]"  # Plus, Minus, typografisches Minus, Gedankenstrich
_VALUE = re.compile(rf"^({_SIGN}?)\s*(\d{{1,2}}(?:[.,]\d+)?)\s*(K|°\s*C?|Grad)?$", re.IGNORECASE)
_KEYWORD = re.compile(r"temperatur", re.IGNORECASE)
# Zahl im Tag, mit Vorzeichen und Einheit
_NUMBER = re.compile(
    rf"(?P<sign>{_SIGN})?\s*(?P<number>\d+(?:[.,]\d+)?)\s*(?P<unit>K\b|°\s*C?|Grad\b)?", re.IGNORECASE
)
# Fuellwoerter, die neben Raum und Zahl stehen duerfen
_FILLER = re.compile(r"\b(?:im|in|fuer|für|der|die|das|den|raum|grad|ca|soll|ziel)\b", re.IGNORECASE)


def _norm(text: str) -> str:
    """Vergleichsform fuer Raumnamen: klein, Umlaute ausgeschrieben, nur Buchstaben und Ziffern."""
    text = text.casefold()
    for old, new in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        text = text.replace(old, new)
    return re.sub(r"[^a-z0-9]", "", text)


def _find_room(text: str, room: str) -> tuple[int, int] | None:
    """Stelle des Raumnamens im Text, Wort fuer Wort verglichen (Gross/klein, Umlaute, ss/ß egal)."""
    wanted = [_norm(word) for word in re.findall(r"[^\W_]+", room)]
    wanted = [word for word in wanted if word]
    if not wanted:
        return None
    tokens = [(_norm(m.group(0)), m.start(), m.end()) for m in re.finditer(r"[^\W_]+", text)]
    for index in range(len(tokens) - len(wanted) + 1):
        if all(tokens[index + k][0] == word for k, word in enumerate(wanted)):
            return tokens[index][1], tokens[index + len(wanted) - 1][2]
    return None


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
    detail: str = ""  # Regelzeile oder Tag

    def label(self) -> str:
        if self.source == "Regel":
            return f"Regel „{self.detail}“"
        if self.source == "Termin":
            return f"Tag „{self.detail}“ im Termin"
        return "Komforttemperatur des Raums"


@dataclass
class EventTags:
    """Temperatur-Tags eines Termins, einmal je Termin ausgewertet."""

    general: tuple[Value, str] | None = None  # (Wert, Tag) fuer alle gebuchten Raeume
    rooms: dict[str, tuple[Value, str]] = field(default_factory=dict)  # Vergleichsform -> (Wert, Tag)
    hints: list[str] = field(default_factory=list)  # uebergangene Tags

    def for_room(self, room: str) -> tuple[Value, str] | None:
        return self.rooms.get(_norm(room)) or self.general


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


def _parse_tag(tag: str, known_rooms: Iterable[str]) -> tuple[str | None, Value | None, str]:
    """Einen Temperatur-Tag zerlegen: (Raum oder None, Wert, Fehler).

    Der Raum wird unter den bekannten Raeumen gesucht (der laengste Treffer
    gewinnt, damit "Vorraum Küche 2" nicht als "Küche" gilt).
    """
    rest = tag
    room = None
    best = None
    for name in known_rooms:
        span = _find_room(rest, name)
        if span and (best is None or span[1] - span[0] > best[1][1] - best[1][0]):
            best = (name, span)
    if best is not None:
        room, (begin, end) = best
        rest = rest[:begin] + " " + rest[end:]
    rest = _KEYWORD.sub(" ", rest)
    numbers = list(_NUMBER.finditer(rest))
    if not numbers:
        return room, None, "keine Zahl"
    if len(numbers) > 1:
        return room, None, "mehrere Zahlen"
    match = numbers[0]
    value = parse_value(match.group(0))
    if value is None:
        return room, None, f"{match.group(0).strip()} ist keine realistische Temperatur"
    leftover = _FILLER.sub(" ", rest[: match.start()] + " " + rest[match.end() :])
    leftover = re.sub(r"[^\w]+", " ", leftover).strip()
    if room is None and leftover:
        return None, None, f"Raum „{leftover}“ unbekannt"
    return room, value, ""


def parse_event_tags(tags: Iterable[str], booked: Iterable[str], known_rooms: Iterable[str]) -> EventTags:
    """Alle Temperatur-Tags eines Termins; Tags ohne das Wort "Temperatur" zaehlen nicht."""
    booked = list(booked)
    booked_keys = {_norm(r) for r in booked}
    candidates = list(dict.fromkeys([*booked, *known_rooms]))
    result = EventTags()
    for raw in tags:
        tag = raw.strip()
        if not _KEYWORD.search(tag):
            continue
        room, value, error = _parse_tag(tag, candidates)
        if not error and room is not None and _norm(room) not in booked_keys:
            error = f"{room} ist in diesem Termin nicht als Ressource gebucht"
        if not error and value is not None and (problem := value.problem()) is not None:
            error = f"{value.text()} {problem}"
        if error or value is None:
            result.hints.append(f"Tag „{tag}“ übergangen: {error}")
            continue
        if room is None:
            if result.general is None:
                result.general = (value, tag)
            elif result.general[0] != value:
                result.hints.append(f"Tag „{tag}“ übergangen: es gilt schon „{result.general[1]}“")
        else:
            key = _norm(room)
            if key not in result.rooms:
                result.rooms[key] = (value, tag)
            elif result.rooms[key][0] != value:
                result.hints.append(f"Tag „{tag}“ übergangen: es gilt schon „{result.rooms[key][1]}“")
    return result


def event_target(
    room: str,
    summary: str,
    tags: Iterable[str],
    comfort: float,
    rules: list[Rule],
    parsed: EventTags,
) -> Target:
    """Ziel eines Termins in einem Raum; parsed = parse_event_tags(...) desselben Termins."""
    found = parsed.for_room(room)
    if found is not None:
        value, tag = found
        return Target(value.apply(comfort), "Termin", tag)
    texts = [summary, *tags]
    for rule in rules:
        if rule.matches(room, texts):
            return Target(rule.value.apply(comfort), "Regel", rule.line)
    return Target(round(comfort, 2), "Raum")
