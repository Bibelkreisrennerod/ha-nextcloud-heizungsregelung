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

# ---- Ziel je Termin
def target(room, summary, description="", categories=()):
    t = T.event_target(room, summary, description, list(categories), 21.0, rules)
    return (t.value, t.source)

check("Gottesdienst Saal", target("Saal", "Gottesdienst"), (19.5, "Regel"))
check("Abendmahlgottesdienst Saal", target("Saal", "Abendmahlgottesdienst"), (19.5, "Regel"))
check("Predigtgottesdienst klein", target("Saal", "predigtGOTTESDIENST am Sonntag"), (19.5, "Regel"))
check("Gottesdienst Nebenraum: Raumwert", target("Nebenraum (groß)", "Gottesdienst"), (21.0, "Raum"))
check("Kategorie", target("Saal", "Sonntag 10 Uhr", categories=["Gottesdienst"]), (19.5, "Regel"))
check("Jugend überall", target("Küche", "Jugendabend"), (20.0, "Regel"))
check("Beschreibung geht vor", target("Saal", "Gottesdienst", "Bitte warm!\nTemperatur: 21"), (21.0, "Termin"))
check("Beschreibung mit °C", target("Küche", "Treffen", "Temperatur = 22,5 °C"), (22.5, "Termin"))
check("Beschreibung je Raum", target("Küche", "Treffen", "Temperatur: 20\nTemperatur Küche: 18"), (18.0, "Termin"))
check("Beschreibung anderer Raum", target("Saal", "Treffen", "Temperatur Küche: 18"), (21.0, "Raum"))
check("Beschreibung relativ", target("Saal", "Treffen", "Temperatur: -1 K"), (20.0, "Termin"))
check("Datum statt Wert", target("Saal", "Treffen", "Temperatur: 21.11. besprechen"), (21.0, "Termin"))
check("unrealistisch übergangen", target("Saal", "Treffen", "Temperatur: 29"), (21.0, "Raum"))
check("Hinweis dazu", T.event_target("Saal", "Treffen", "Temperatur: 29", [], 21.0, rules).hint, "29 °C (Beschreibung) übergangen: außerhalb 15–25 °C")
check("unrealistisch, Regel greift", target("Saal", "Gottesdienst", "Temperatur: 9"), (19.5, "Regel"))
check("relativ zu groß", target("Saal", "Treffen", "Temperatur: +8 K"), (21.0, "Raum"))
check("Ergebnis begrenzt", T.event_target("Saal", "Treffen", "Temperatur: +5 K", [], 23.0, rules).value, 25.0)

# ---- Schlagworte (Nextcloud-Tags)
def tag(room, *tags, summary="Treffen", description=""):
    t = T.event_target(room, summary, description, list(tags), 21.0, rules)
    return (t.value, t.source, t.detail)

check("Tag 20", tag("Saal", "20"), (20.0, "Termin", "Tag „20“"))
check("Tag 20°C", tag("Saal", "20°C"), (20.0, "Termin", "Tag „20°C“"))
check("Tag 20 °C", tag("Saal", "20 °C"), (20.0, "Termin", "Tag „20 °C“"))
check("Tag 19,5°", tag("Saal", "19,5°"), (19.5, "Termin", "Tag „19,5°“"))
check("Tag 20.5 Grad", tag("Saal", "20.5 Grad"), (20.5, "Termin", "Tag „20.5 Grad“"))
check("Tag Temperatur 20", tag("Saal", "Temperatur 20"), (20.0, "Termin", "Tag „Temperatur 20“"))
check("Tag Temp.: 20", tag("Saal", "Temp.: 20"), (20.0, "Termin", "Tag „Temp.: 20“"))
check("Tag relativ", tag("Saal", "-1 K"), (20.0, "Termin", "Tag „-1 K“"))
check("Tag mit Raum", tag("Küche", "Küche 18"), (18.0, "Termin", "Tag „Küche 18“"))
check("Tag anderer Raum", tag("Saal", "Küche 18"), (21.0, "Raum", ""))
check("Raum vor allgemein", tag("Küche", "20", "Kueche: 18"), (18.0, "Termin", "Tag „Kueche: 18“"))
check("Raumname mit Klammern", tag("Nebenraum (groß)", "Nebenraum groß 19"), (19.0, "Termin", "Tag „Nebenraum groß 19“"))
check("Tag geht Regel vor", tag("Saal", "Gottesdienst", "21", summary="Gottesdienst"), (21.0, "Termin", "Tag „21“"))
check("Tag geht Beschreibung vor", tag("Saal", "20", description="Temperatur: 22"), (20.0, "Termin", "Tag „20“"))
check("Tag 30 übergangen", tag("Saal", "30"), (21.0, "Raum", ""))
check("Tag 12 übergangen, Regel", tag("Saal", "12", summary="Gottesdienst"), (19.5, "Regel", "gottesdienst @ Saal = -1,5 K"))
check("Tag 210 kein Wert", tag("Saal", "210"), (21.0, "Raum", ""))
check("Tag 2026 kein Wert", tag("Saal", "2026"), (21.0, "Raum", ""))
check("Tag Gruppe 1 anderer Raum", tag("Saal", "Gruppe 1"), (21.0, "Raum", ""))
check("Tag 2. Advent kein Wert", tag("Saal", "2. Advent"), (21.0, "Raum", ""))
check("Beschriftung Tag", T.event_target("Saal", "x", "", ["20 °C"], 21.0, rules).label(), "Angabe im Termin (Tag „20 °C“)")
check("Text ohne Zahl", target("Saal", "Treffen", "Temperatur: angenehm"), (21.0, "Raum"))
check("Beschriftung Regel", T.event_target("Saal", "Gottesdienst", "", [], 21.0, rules).label(), "Regel „gottesdienst @ Saal = -1,5 K“")
check("Text relativ", T.Value(-1.5, True).text(), "-1,5 K")
check("Text absolut", T.Value(20.0, False).text(), "20 °C")

# ---- Kalender: Beschreibung und Kategorien
ics = (
    "BEGIN:VCALENDAR\nBEGIN:VEVENT\nSUMMARY:Abendmahlgottesdienst\n"
    "DTSTART:20261122T090000Z\nDTEND:20261122T103000Z\n"
    "DESCRIPTION:Bitte Stühle stellen.\\nTemperatur Saal: 20\\, danke\n"
    "CATEGORIES:Gottesdienst,Gemeinde\\, intern\n"
    "ATTENDEE;CUTYPE=ROOM;CN=Saal:mailto:saal@example.org\n"
    "END:VEVENT\nEND:VCALENDAR"
)
xml = (
    '<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"><d:response><d:propstat><d:prop>'
    f"<c:calendar-data>{ics}</c:calendar-data></d:prop></d:propstat></d:response></d:multistatus>"
)
event = cal.parse_events(xml, "Gemeinde")[0]
check("Beschreibung gelesen", event.description, "Bitte Stühle stellen.\nTemperatur Saal: 20, danke")
check("Kategorien gelesen", event.categories, ["Gottesdienst", "Gemeinde, intern"])
check("Ziel aus Kalender", T.event_target("Saal", event.summary, event.description, event.categories, 21.0, rules).value, 20.0)

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
