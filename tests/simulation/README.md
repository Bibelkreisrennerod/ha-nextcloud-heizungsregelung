# Simulationstests

Ohne Home Assistant lauffähig (Python 3.12+). Die „wahre“ Physik eines
Gebäudes steht in `harness.py`; die Integration kennt sie nicht und muss sie
lernen.

| Datei | Prüft |
| --- | --- |
| `test_learn.py [flow\|offset\|offset_blind]` | Lernen aus Auskühlen und drei Kalibrierläufen (mit `flow`: mit Vorlauftemperatur; mit `offset`: zwei Fühler messen 1,5 bzw. 3,5 K zu warm, ihre Offsets werden in vier Schritten nachgestellt und mitgemeldet; `offset_blind`: dieselben Schritte ohne lesbaren Offset), nach Lauf 1, 2 und 3 je eine gemeinsame Anpassung mit Unsicherheiten, gamma, Wärmeaustausch k, geschätzten Türzuständen und empfohlenem nächsten Testlauf; schreibt `learned.json` |
| `test_plan.py [truth]` | Planung eines Sonntags bei 1, −7 und 7 °C; mit `truth` mit den wahren Parametern, sonst mit `learned.json` |
| `test_controller.py auto\|calib\|observe\|history\|restart\|targets` | Steuerung mit nachgebildetem Home Assistant: Sonntag mit Automatik, Kalibrierlauf, Beobachten bei Automatik aus, Lernen aus 3 Tagen Recorder-Historie, Neustart während Kalibrierlauf und während des Aufheizens, Zieltemperatur je Termin (Regel „gottesdienst @ Saal = -1,5 K“, Temperatur-Tags, Tag für nicht gebuchten Raum wird gemeldet) |
| `test_targets.py` | Regeln, Temperatur-Tags (Wort „Temperatur“ Pflicht, Raum nur wenn gebucht, unrealistische Werte), Tags aus CalDAV, Sollwert je Termin im Planer; Rückgabewert 1 bei Fehlern |
| `test_offset.py` | Offset der Thermostate: Lesen aus nachgebildeter Homematic-IP-Bibliothek (Heizkörper- und Wandthermostat, Gruppe ohne Geräteliste), Umrechnen der Messpunkte auf den aktuellen Offset, Vorgeschichte des Learners; Rückgabewert 1 bei Fehlern |
| `test_stokercloud.py` | Auswertung einer StokerCloud-Antwort wie vom Kessel der Gemeinde: Kesseltemperatur, Warmwasser-Ist (nicht die Schaltdifferenz), Leistung in kW und Prozent der Nennleistung |

Reihenfolge: erst `test_learn.py`, dann `test_plan.py`.

Seit 0.6 hat die wahre Physik je Raum gamma (Ventilstellung → Wärme, 0,75–0,9),
Wärmeaustausch mit den Nachbarn (Saal+Küche, Nebenräume) und eine Tür
Saal–Küche, die in zwei Zeitfenstern geschlossen ist; die gemessene
Kesselleistung geht wie von StokerCloud in die Messpunkte ein. Ergebnis
(21.11.2026, nach Lauf 3): gamma auf ±0,05 getroffen, k Saal 0,06/0,08,
Nebenräume 0,15–0,25 wie wahr, Küche 0,33/0,45; Türen „offen“ 81 % (wahr 75 %);
Planung bei 1 und 7 °C für alle Räume pünktlich.

Die wahren Thermostate verhalten sich wie am 19.11.2026 gemessen: jeder
Heizkörper hat eine eigene Höchstöffnung (Saal 48 %, Nebenraum (groß) 83 %),
die Öffnung folgt einer Kennlinie über Soll minus Ist (P-Anteil) plus einem
I-Anteil, und der Stellmotor folgt mit 10 min Verzögerung. Das Modell kennt
davon nur, was es aus den gemeldeten Ventilstellungen lernt. Bei −7 °C reicht
der Kessel in diesem Gebäude nicht für alle Räume; dort ist nur der Saal
(Vorrang 1) rechtzeitig warm, auch mit den wahren Werten.

Offset (22.11.2026, `test_learn.py offset` gegen `offset_blind`): mit gelesenem
Offset trifft die erste Anpassung die Nebenräume wie ohne Verstellung
(a 0,070/0,080, gamma 0,75/0,83, k 0,15/0,20 bei wahr 0,07/0,08, 0,75/0,85,
0,15/0,20); ohne lesbaren Offset werden die Sprünge als Physik gelernt
(Nebenraum klein a 0,015, gamma 1,36; Nebenraum groß k 0,45).
