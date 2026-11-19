# Simulationstests

Ohne Home Assistant lauffähig (Python 3.12+). Die „wahre“ Physik eines
Gebäudes steht in `harness.py`; die Integration kennt sie nicht und muss sie
lernen.

| Datei | Prüft |
| --- | --- |
| `test_learn.py [flow]` | Lernen aus Auskühlen und zwei Kalibrierläufen (mit `flow`: mit Vorlauftemperatur); schreibt `learned.json` |
| `test_plan.py [truth]` | Planung eines Sonntags bei 1, −7 und 7 °C; mit `truth` mit den wahren Parametern, sonst mit `learned.json` |
| `test_controller.py auto\|calib\|observe\|history\|restart` | Steuerung mit nachgebildetem Home Assistant: Sonntag mit Automatik, Kalibrierlauf, Beobachten bei Automatik aus, Lernen aus 3 Tagen Recorder-Historie, Neustart während Kalibrierlauf und während des Aufheizens |

Reihenfolge: erst `test_learn.py`, dann `test_plan.py`.

Die wahren Thermostate verhalten sich wie am 19.11.2026 gemessen: jeder
Heizkörper hat eine eigene Höchstöffnung (Saal 48 %, Nebenraum (groß) 83 %),
die Öffnung folgt einer Kennlinie über Soll minus Ist (P-Anteil) plus einem
I-Anteil, und der Stellmotor folgt mit 10 min Verzögerung. Das Modell kennt
davon nur, was es aus den gemeldeten Ventilstellungen lernt. Bei −7 °C reicht
der Kessel in diesem Gebäude nicht für alle Räume; dort ist nur der Saal
(Vorrang 1) rechtzeitig warm, auch mit den wahren Werten.
