"""StokerCloud-Auswertung mit einer Antwort wie vom Kessel der Gemeinde (21.11.2026)."""
import importlib
import os
import sys
import types
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ha_stubs  # noqa: F401,E402

pk = types.ModuleType("nextcloud_raumbelegung")
pk.__path__ = [os.path.join(HERE, "..", "..", "custom_components", "nextcloud_raumbelegung")]
sys.modules["nextcloud_raumbelegung"] = pk
S = importlib.import_module("nextcloud_raumbelegung.stokercloud")

failures = []


def check(label, got, want):
    ok = got == want
    print(("ok  " if ok else "FEHL"), label, "->", got, "" if ok else f"(erwartet {want})")
    if not ok:
        failures.append(label)


def item(ident, value, unit=None):
    return {"id": ident, "value": value, "unit": unit}


payload = {
    "frontdata": [
        item("1", "0.2", "LNG_DEGREE"), item("2", "3.64", "LNG_METERSEC"), item("3", "WNW"),
        item("4", "13230", "LNG_KG"), item("5", "22.6", "LNG_KW"), item("6", "100"),
        item("7", "3.5", "LNG_KW"), item("8", "30", "LNG_KW"), item("9", "95", "LNG_PERCENT"),
    ],
    "hopperdata": [item("2", "2000", "LNG_GRAM"), item("3", "12.5", "LNG_KG")],
    "dhwdata": [item("3", "15", "LNG_DEGREE"), item("4", "N/A", "LNG_PERCENT")],
    "boilerdata": [
        item("boilertemp", "71.7", "LNG_DEGREE"), item("-wantedboilertemp", "None", "LNG_DEGREE"),
        item("dhw", "65", "LNG_DEGREE"), item("dhwwanted", "65", "LNG_DEGREE"),
        item("state", "state_7", "LNG_KW"),
    ],
    "miscdata": {"alarm": 0, "running": 1, "output": 22.6, "clock": "09:44"},
    "notconnected": 0,
}
now = datetime(2026, 11, 21, 8, 45, tzinfo=timezone.utc)
data = S.parse(payload, now, timezone.utc)
check("Kesseltemperatur", data.flow, 71.7)
check("Warmwasser ist dhw, nicht dhwdata.3", data.dhw, 65.0)
check("Leistung kW", data.power_kw, 22.6)
check("Nennleistung", data.nominal_kw, 30.0)
check("Leistung Prozent", data.power, 75.3)
check("Status", data.state, "state_7")

# Mit "outputpct" (Antwort vom 21.11.2026 abends) gilt der Prozentwert direkt
payload["miscdata"]["outputpct"] = "76"
withpct = S.parse(payload, now, timezone.utc)
check("outputpct", (withpct.power, withpct.power_kw), (76.0, 22.6))
del payload["miscdata"]["outputpct"]

# Kessel aus: 0 kW
payload["frontdata"][4] = item("5", "0", "LNG_KW")
payload["miscdata"]["output"] = 0
payload["miscdata"]["outputpct"] = "0"
off = S.parse(payload, now, timezone.utc)
check("aus: kW", off.power_kw, 0.0)
check("aus: Prozent", off.power, 0.0)

# Ein Kessel, der die Leistung wirklich in Prozent meldet, bleibt unveraendert
pct = S.parse({"miscdata": {"output": 40}, "frontdata": [item("5", "12", "LNG_KW")]}, now, timezone.utc)
check("Prozent bleibt Prozent", (pct.power, pct.power_kw), (40.0, None))

# Antwort vom 21.11.2026, 20:50 (Kessel aus, Aufbau wie von controllerdata2)
live = {
    "boilerdata": [item("5", "0.0", "LNG_KW"), item("6", "0", ""), item("14", "0", "LNG_PERCENT")],
    "hopperdata": [item("2", "2000", "LNG_GRAM"), item("3", "24.1", "LNG_KG"), item("4", "13242", "LNG_KG"),
                   item("7", "3.5", "LNG_KW"), item("8", "30", "LNG_KW")],
    "dhwdata": [item("3", "15", "LNG_DEGREE"), item("4", "N/A", "LNG_PERCENT")],
    "frontdata": [item("hoppercontent", "440", "LNG_KG"), item("boilertemp", "46.1", "LNG_DEGREE"),
                  item("-wantedboilertemp", None, "LNG_DEGREE"), item("dhw", "56", "LNG_DEGREE"),
                  item("dhwwanted", "65", "LNG_DEGREE"), item("refoxygen", "20.9", "LNG_PERCENT")],
    "miscdata": {"state": {"id": "state", "value": "state_9", "unit": "", "name": "", "selection": ""},
                 "clock": {"id": "clock", "value": "20:50"}, "alarm": 0, "running": 0, "output": 0,
                 "outputpct": "0", "hopperdistance": 999},
    "notconnected": 0,
}
lv = S.parse(live, now, timezone.utc)
check("live: Kesseltemperatur", lv.flow, 46.1)
check("live: Warmwasser", lv.dhw, 56.0)
check("live: Leistung", (lv.power, lv.power_kw, lv.nominal_kw), (0.0, 0.0, 30.0))
check("live: Status", lv.state, "state_9")

print("Fehler:", failures if failures else "keine")
sys.exit(1 if failures else 0)
