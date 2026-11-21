"""Zieltemperatur je Termin: Regeln, Angabe in der Beschreibung, Kalender-Auswertung, Planer."""
import importlib
import os
import sys
import types
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ha_stubs  # noqa: F401,E402  (stellt homeassistant-Module bereit)

pk = types.ModuleType("nextcloud_raumbelegung")
pk.__path__ = [os.path.join(HERE, "..", "..", "custom_components", "nextcloud_raumbelegung")]
sys.modules["nextcloud_raumbelegung"] = pk
T = importlib.import_module("nextcloud_raumbelegung.targets")
cal = importlib.import_module("nextcloud_raumbelegung.caldav")
PL = importlib.import_module("nextcloud_raumbelegung.planning")
P = importlib.import_module("nextcloud_raumbelegung.physics")

failures = []


def check(label, got, want):
    ok = got == want
    print(("ok  " if ok else "FEHL"), label, "->", got, "" if ok else f"(erwartet {want})")
    if not ok:
        failures.append(label)


# ---- Regeln
rules = T.parse_rules(
    "# Kommentar\n"
    "gottesdienst @ Saal = -1,5 K\n"
    "\n"
    "Jugend = 20 °C\n"
    "chor @ Saal, Küche = +0.5\n"
)
check("drei Regeln", len(rules), 3)
check("Regel 1 relativ", (rules[0].keyword, rules[0].rooms, rules[0].value), ("gottesdienst", ("saal",), T.Value(-1.5, True)))
check("Regel 2 absolut", rules[1].value, T.Value(20.0, False))
check("Regel 3 zwei Räume", rules[2].rooms, ("saal", "kueche"))
for bad in ("gottesdienst", "gottesdienst = warm", "= 20", "x = 45", "x = -9 K", "x = 12", "x = 26 °C"):
    try:
        T.parse_rules("ok = 20\n" + bad)
        check(f"ungültig {bad!r}", "angenommen", "Fehler")
    except T.RuleError as err:
        check(f"ungültig {bad!r}", err.args[0], 2)

# ---- Temperatur-Tags
KNOWN = ["Saal", "Küche", "Küche 2", "Vorraum Küche 2", "Nebenraum (groß)", "Nebenraum (klein)"]


def tag(room, *tags, booked=("Saal", "Küche"), summary="Treffen"):
    parsed = T.parse_event_tags(list(tags), list(booked), KNOWN)
    t = T.event_target(room, summary, list(tags), 21.0, rules, parsed)
    return (t.value, t.source, t.detail)


def hints(*tags, booked=("Saal", "Küche")):
    return T.parse_event_tags(list(tags), list(booked), KNOWN).hints


check("Temperatur 20", tag("Saal", "Temperatur 20"), (20.0, "Termin", "Temperatur 20"))
check("20 Temperatur", tag("Saal", "20 Temperatur"), (20.0, "Termin", "20 Temperatur"))
check("20 °C Temperatur", tag("Saal", "20 °C Temperatur"), (20.0, "Termin", "20 °C Temperatur"))
check("temperatur: 19,5°", tag("Saal", "temperatur: 19,5°"), (19.5, "Termin", "temperatur: 19,5°"))
check("ohne Raum für alle gebuchten", tag("Küche", "Temperatur 20"), (20.0, "Termin", "Temperatur 20"))
check("Temperatur Saal 20°C", tag("Saal", "Temperatur Saal 20°C"), (20.0, "Termin", "Temperatur Saal 20°C"))
check("20 Temperatur Saal", tag("Saal", "20 Temperatur Saal"), (20.0, "Termin", "20 Temperatur Saal"))
check("Saal Temperatur: 20", tag("Saal", "Saal Temperatur: 20"), (20.0, "Termin", "Saal Temperatur: 20"))
check("Raum-Tag gilt nicht im anderen Raum", tag("Küche", "Temperatur Saal 20"), (21.0, "Raum", ""))
check("Raum vor allgemein", tag("Küche", "Temperatur 20", "Temperatur Kueche 18"), (18.0, "Termin", "Temperatur Kueche 18"))
check("allgemein im anderen Raum", tag("Saal", "Temperatur 20", "Temperatur Küche 18"), (20.0, "Termin", "Temperatur 20"))
check("relativ", tag("Saal", "Temperatur -1 K"), (20.0, "Termin", "Temperatur -1 K"))
check("Füllwort im", tag("Saal", "Temperatur im Saal 20"), (20.0, "Termin", "Temperatur im Saal 20"))
check("Raum mit Klammern", tag("Nebenraum (groß)", "Temperatur Nebenraum groß 19", booked=["Nebenraum (groß)"]), (19.0, "Termin", "Temperatur Nebenraum groß 19"))
check("Küche 2 ist nicht Küche", tag("Küche 2", "Temperatur Küche 2 19", booked=["Küche", "Küche 2"]), (19.0, "Termin", "Temperatur Küche 2 19"))
check("Küche 2 trifft Küche nicht", tag("Küche", "Temperatur Küche 2 19", booked=["Küche", "Küche 2"]), (21.0, "Raum", ""))
check("Küche 20 ist Küche", tag("Küche", "Temperatur Küche 20", booked=["Küche", "Küche 2"]), (20.0, "Termin", "Temperatur Küche 20"))
check("Vorraum Küche 2", tag("Vorraum Küche 2", "Temperatur Vorraum Küche 2 18", booked=["Vorraum Küche 2"]), (18.0, "Termin", "Temperatur Vorraum Küche 2 18"))
check("Tag geht Regel vor", tag("Saal", "Temperatur 21", summary="Gottesdienst"), (21.0, "Termin", "Temperatur 21"))
check("ohne Wort Temperatur zählt nicht", tag("Saal", "20", "20 °C", "Saal 20"), (21.0, "Raum", ""))
check("ohne Wort: kein Hinweis", hints("20", "Jugend", "2. Advent"), [])
check("Regel über Tag-Stichwort", tag("Saal", "Gottesdienst"), (19.5, "Regel", "gottesdienst @ Saal = -1,5 K"))
check("Gottesdienst im Titel", tag("Saal", summary="Abendmahlgottesdienst"), (19.5, "Regel", "gottesdienst @ Saal = -1,5 K"))
check("Gottesdienst nur Saal", tag("Küche", summary="Predigtgottesdienst"), (21.0, "Raum", ""))
check("Jugend überall", tag("Küche", summary="Jugendabend"), (20.0, "Regel", "Jugend = 20 °C"))
# uebergangen
check("nicht gebucht", tag("Saal", "Temperatur Nebenraum klein 19"), (21.0, "Raum", ""))
check("Hinweis nicht gebucht", hints("Temperatur Nebenraum klein 19"), ["Tag „Temperatur Nebenraum klein 19“ übergangen: Nebenraum (klein) ist in diesem Termin nicht als Ressource gebucht"])
check("Hinweis unbekannter Raum", hints("Temperatur Keller 19"), ["Tag „Temperatur Keller 19“ übergangen: Raum „Keller“ unbekannt"])
check("unbekannter Raum gilt nicht für alle", tag("Saal", "Temperatur Keller 19"), (21.0, "Raum", ""))
check("Hinweis zu warm", hints("Temperatur 30"), ["Tag „Temperatur 30“ übergangen: 30 °C außerhalb 15–25 °C"])
check("zu kalt, Regel greift", tag("Saal", "Temperatur 12", summary="Gottesdienst"), (19.5, "Regel", "gottesdienst @ Saal = -1,5 K"))
check("Hinweis relativ zu groß", hints("Temperatur +8 K"), ["Tag „Temperatur +8 K“ übergangen: +8 K mehr als 5 K Abweichung"])
check("Hinweis 210", hints("Temperatur 210"), ["Tag „Temperatur 210“ übergangen: 210 ist keine realistische Temperatur"])
check("Hinweis keine Zahl", hints("Temperatur angenehm"), ["Tag „Temperatur angenehm“ übergangen: keine Zahl"])
check("Hinweis zwei Zahlen", hints("Temperatur 20 21"), ["Tag „Temperatur 20 21“ übergangen: mehrere Zahlen"])
check("Hinweis Widerspruch", hints("Temperatur 20", "Temperatur 22"), ["Tag „Temperatur 22“ übergangen: es gilt schon „Temperatur 20“"])
check("gleicher Wert doppelt ok", hints("Temperatur 20", "20 Temperatur"), [])
check("Ergebnis begrenzt", T.event_target("Saal", "x", [], 23.0, rules, T.parse_event_tags(["Temperatur +5 K"], ["Saal"], KNOWN)).value, 25.0)
check("Beschriftung Tag", T.event_target("Saal", "x", [], 21.0, rules, T.parse_event_tags(["Temperatur 20 °C"], ["Saal"], KNOWN)).label(), "Tag „Temperatur 20 °C“ im Termin")
check("Beschriftung Regel", T.event_target("Saal", "Gottesdienst", [], 21.0, rules, T.EventTags()).label(), "Regel „gottesdienst @ Saal = -1,5 K“")
check("Text relativ", T.Value(-1.5, True).text(), "-1,5 K")
check("Text absolut", T.Value(20.0, False).text(), "20 °C")

# ---- Kalender: Tags (CATEGORIES)
ics = (
    "BEGIN:VCALENDAR\nBEGIN:VEVENT\nSUMMARY:Abendmahlgottesdienst\n"
    "DTSTART:20261122T090000Z\nDTEND:20261122T103000Z\n"
    "DESCRIPTION:Temperatur: 24\n"
    "CATEGORIES:Gottesdienst,Temperatur Saal 20\\, bitte\n"
    "CATEGORIES:Gemeinde\n"
    "ATTENDEE;CUTYPE=ROOM;CN=Saal:mailto:saal@example.org\n"
    "END:VEVENT\nEND:VCALENDAR"
)
xml = (
    '<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"><d:response><d:propstat><d:prop>'
    f"<c:calendar-data>{ics}</c:calendar-data></d:prop></d:propstat></d:response></d:multistatus>"
)
event = cal.parse_events(xml, "Gemeinde")[0]
check("Tags gelesen", event.categories, ["Gottesdienst", "Temperatur Saal 20, bitte", "Gemeinde"])
check("Beschreibung zählt nicht", hasattr(event, "description"), False)
parsed = T.parse_event_tags(event.categories, event.rooms, KNOWN)
check("Ziel aus Kalender", T.event_target("Saal", event.summary, event.categories, 21.0, rules, parsed).value, 20.0)

# ---- Planer: Sollwert je Termin, Aufheizen fuer den naechsten waehrend des laufenden
day = datetime(2026, 11, 22, tzinfo=timezone.utc)
params = P.RoomParams(a=0.05, b=4.0, w=0.5, d=0.2)
room = PL.RoomInput(
    name="Saal", params=params, temp=19.0, comfort=22.0, setback=14.0,
    events=[(day + timedelta(hours=9), day + timedelta(hours=10, minutes=30)),
            (day + timedelta(hours=11), day + timedelta(hours=12)),
            (day + timedelta(hours=18), day + timedelta(hours=19))],
    targets=[19.5, 22.0, 20.0],
)
plan = PL.RoomPlan(main=day + timedelta(hours=10), deadline=day + timedelta(hours=10, minutes=45),
                   event_end=day + timedelta(hours=12))
at = lambda h, m=0: PL.setpoint_at(room, plan, day + timedelta(hours=h, minutes=m))  # noqa: E731
check("09:30 Gottesdienst", at(9, 30), 19.5)
check("10:15 Gottesdienst, Aufheizen für 11 Uhr läuft", at(10, 15), 22.0)
check("11:30 zweiter Termin", at(11, 30), 22.0)
check("14:00 dazwischen", at(14), 14.0)
check("18:30 dritter Termin", at(18, 30), 20.0)
check("ohne targets gilt comfort", PL.RoomInput(name="x", params=params, temp=19, comfort=21, setback=14).target(3), 21)

print("Fehler:", failures if failures else "keine")
sys.exit(1 if failures else 0)
