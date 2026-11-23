"""Temperatur-Offset der Thermostate: Lesen aus Homematic IP Cloud, Umrechnen der Messpunkte."""
import importlib
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ha_stubs  # noqa: E402  (stellt homeassistant-Module bereit)

pk = types.ModuleType("nextcloud_raumbelegung")
pk.__path__ = [os.path.join(HERE, "..", "..", "custom_components", "nextcloud_raumbelegung")]
sys.modules["nextcloud_raumbelegung"] = pk
E = importlib.import_module("nextcloud_raumbelegung.estimation")
H = importlib.import_module("nextcloud_raumbelegung.homematic")
L = importlib.import_module("nextcloud_raumbelegung.learning")

failures = []


def check(label, got, want):
    ok = got == want
    print(("ok  " if ok else "FEHL"), label, "->", got, "" if ok else f"(erwartet {want})")
    if not ok:
        failures.append(label)


# ---- Umrechnen auf den aktuellen Offset
# Fuehler misst immer 20,0; angezeigt wird 20,0 plus Offset
temps = [20.0, 20.0, 18.5, 18.5, None, 19.0]
offsets = [None, 0.0, -1.5, -1.5, -1.5, -1.0]
check("Messpunkte auf letzten Offset", E._on_current_offset(temps, offsets), [19.0, 19.0, 19.0, 19.0, None, 19.0])
check("ohne bekannten Offset unveraendert", E._on_current_offset([1.0, 2.0], [None, None]), [1.0, 2.0])
check("alte Messpunkte ohne viertes Feld", E._offset([20.0, 0.3, 21.0]), None)
check("Offset aus Messpunkt", E._offset([20.0, 0.3, 21.0, -2.5]), -2.5)


# ---- Lesen aus der Integration Homematic IP Cloud (nachgebildete Bibliothek)
class Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def thermostat(device_id, offset, kind="HEATING_THERMOSTAT_CHANNEL"):
    return Obj(
        id=device_id,
        _rawJSONData={
            "functionalChannels": {
                "0": {"functionalChannelType": "DEVICE_OPERATIONLOCK"},
                "1": {"functionalChannelType": kind, "temperatureOffset": offset, "valvePosition": 0.1},
            }
        },
    )


trv_a, trv_b = thermostat("TRV-A", -1.5), thermostat("TRV-B", -2.5)
wall = thermostat("WTH-1", 0.5, "WALL_MOUNTED_THERMOSTAT_WITHOUT_DISPLAY_CHANNEL")
trv_saal = thermostat("TRV-S", -3.0)
# Gruppe ohne Geraeteliste: Zuordnung ueber die Rohdaten der Kanaele
ng = Obj(id="g-ng", devices=[], _rawJSONData={"channels": [{"deviceId": "TRV-A"}, {"deviceId": "TRV-B"}]})
saal = Obj(id="g-saal", devices=[wall, trv_saal])
home = Obj(groups=[ng, saal], devices=[trv_a, trv_b, wall, trv_saal])


class Entries:
    def async_entries(self, domain):
        return [Obj(runtime_data=Obj(home=home))] if domain == "homematicip_cloud" else []


hass = ha_stubs.Hass()
hass.config_entries = Entries()
hass.data = {}
ha_stubs.REG["er"] = ha_stubs.ER(
    [
        ha_stubs.Ent("climate.ng", "ng"),
        ha_stubs.Ent("climate.saal", "saal"),
        ha_stubs.Ent("climate.andere", "x", platform="other"),
    ]
)
ha_stubs.REG["er"].entities["climate.ng"].unique_id = "HomematicipHeatingGroup_g-ng"
ha_stubs.REG["er"].entities["climate.saal"].unique_id = "HomematicipHeatingGroup_g-saal"
got = H.climate_offsets(hass, ["climate.ng", "climate.saal", "climate.andere", "climate.fehlt"])
check("Mittel der Heizkoerperthermostate", got["climate.ng"], -2.0)
check("Wandthermostat misst die Raumtemperatur", got["climate.saal"], 0.5)
check("andere Integration", got["climate.andere"], None)
check("unbekannte Entitaet", got["climate.fehlt"], None)
broken = ha_stubs.Hass()  # ohne config_entries: nicht lesbar, kein Fehler
check("Integration nicht erreichbar", H.climate_offsets(broken, ["climate.ng"]), {"climate.ng": None})


# ---- Learner: Vorgeschichte folgt einer Offset-Aenderung
from datetime import datetime, timedelta, timezone  # noqa: E402

lr = L.Learner()
t0 = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)
for i in range(4):
    offset = 0.0 if i < 3 else -1.5
    temp = 20.0 + offset
    sample = L.RoomSample(t0 + timedelta(minutes=5 * i), temp, 0.0, 14.0, offset=offset)
    lr._follow_offset("NG", sample)
    lr._windows.setdefault("NG", __import__("collections").deque(maxlen=20)).append(sample)
check("Fenster ohne Sprung", [s.temp for s in lr._windows["NG"]], [18.5, 18.5, 18.5, 18.5])


# ---- Lernen aus der Historie: Offset-Verlauf wird mitgegeben
Hi = importlib.import_module("nextcloud_raumbelegung.history")
P = importlib.import_module("nextcloud_raumbelegung.physics")
start = datetime(2026, 11, 22, 10, 0, tzinfo=timezone.utc)
end = start + timedelta(hours=4)
# Fuehler zeigt 20,0; um 11:45 Offset 0 -> -1,5: Anzeige springt auf 18,5
temps = [(start, 20.0), (start + timedelta(minutes=105), 18.5)]
offsets = [(start + timedelta(minutes=5), 0.0), (start + timedelta(minutes=105), -1.5)]  # erst ab 10:05 aufgezeichnet
series = Hi.RoomSeries(temps=[temps], targets=[[(start, 14.0)]], valves=[[(start, 0.0)]], boosts=[[]], offsets=[[], offsets])
lr2 = L.Learner()
Hi.replay(lr2, {"NG": series}, [(start, 5.0)], [], start, end, {"NG": 120.0}, P.PlantParams())
rows = lr2._rows
fourth = [r["r"]["NG"][3] for r in rows]
check("Offset in Messpunkten", (fourth[0], fourth[10], fourth[-1]), (0.0, 0.0, -1.5))
fixed = E._on_current_offset([r["r"]["NG"][0] for r in rows], [E._offset(r["r"]["NG"]) for r in rows])
check("umgerechnet ohne Sprung", sorted(set(fixed)), [18.5])
check("Offset gilt bis zur Aenderung (kein Hoechstalter)", fourth[-1], -1.5)


# ---- Nachgetragener Offset-Verlauf (Dienst offset_nachtragen)
C = importlib.import_module("nextcloud_raumbelegung.controller")
fake = types.SimpleNamespace(rooms={"Nebenraum (groß)": None}, offset_log={}, _save=lambda: None)
local = timezone(timedelta(hours=1))
C.HeatingController.set_offset_log(fake, "nebenraum (gross)", [
    (datetime(2026, 11, 22, 17, 22, tzinfo=local), -3.5),
    (datetime(2026, 11, 1, 0, 0, tzinfo=local), 0.0),
    (datetime(2026, 11, 22, 11, 44, tzinfo=local), -1.5),
])
got = C.HeatingController._offset_log_series(fake, "Nebenraum (groß)")
check("Verlauf sortiert, UTC", [(t.strftime("%d.%m. %H:%M"), v) for t, v in got],
      [("31.10. 23:00", 0.0), ("22.11. 10:44", -1.5), ("22.11. 16:22", -3.5)])
C.HeatingController.set_offset_log(fake, "Nebenraum (groß)", [])
check("leere Liste entfernt", fake.offset_log, {})

print("FEHLER:", failures if failures else "keine")
sys.exit(1 if failures else 0)
