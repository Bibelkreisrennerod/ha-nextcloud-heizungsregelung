# Simulationstests

Ohne Home Assistant lauffähig (Python 3.12+). Die „wahre“ Physik eines
Gebäudes steht in `harness.py`; die Integration kennt sie nicht und muss sie
lernen.

| Datei | Prüft |
| --- | --- |
| `test_learn.py [flow]` | Lernen aus Auskühlen und zwei Kalibrierläufen (mit `flow`: mit Vorlauftemperatur); schreibt `learned.json` |
| `test_plan.py [truth]` | Planung eines Sonntags bei 1, −7 und 7 °C; mit `truth` mit den wahren Parametern, sonst mit `learned.json` |
| `test_controller.py auto\|calib\|observe\|history` | Steuerung mit nachgebildetem Home Assistant: Sonntag mit Automatik, Kalibrierlauf, Beobachten bei Automatik aus, Lernen aus 3 Tagen Recorder-Historie |

Reihenfolge: erst `test_learn.py`, dann `test_plan.py`.
