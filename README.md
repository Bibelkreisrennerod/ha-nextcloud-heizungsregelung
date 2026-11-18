# Nextcloud Raumbelegung

Integration für Home Assistant: liest Raumbuchungen aus Nextcloud-Kalendern und
stellt je Raum einen Sensor „Heizbedarf“ bereit. Gedacht für die
kalenderbasierte Steuerung von Heizungsthermostaten.

Ein Raum gilt als gebucht, wenn er im Termin als Ressource eingetragen ist
(Nextcloud: Termin > Ressourcen). Technisch ist das ein Teilnehmer vom Typ
Raum (`CUTYPE=ROOM`). Die eingebaute CalDAV-Integration von Home Assistant
gibt diese Information nicht weiter, deshalb fragt diese Integration
Nextcloud direkt ab.

> Stand: Version 0.3.0. Modell, Lernen und Planung sind mit Simulationen
> getestet, der Betrieb mit echten Thermostaten steht noch aus.

## Was die Steuerung macht

- **Physikalisches Modell:** Je Raum drei gelernte Größen: Auskühlrate
  (Wärmeverlust, K/h je Kelvin Unterschied nach außen), maximale
  Aufheizrate (K/h bei offenen Ventilen und vollem Puffer) und
  Leistungsbedarf (Anteil der Kessel-Dauerleistung bei offenen Ventilen).
  Für die Anlage: Kessel-Dauerleistung, Anlaufzeit und nutzbare
  Pufferreserve. Zieht ein Raum mehr als der Kessel liefert (der Saal), deckt
  der Puffer die Lücke, bis er leer ist; danach teilen sich alle offenen
  Räume die Kesselleistung und heizen langsamer.
- **Planen:** Alle Räume werden zusammen simuliert, mit der Wetterprognose,
  in 10-Minuten-Schritten bis zur spätesten Frist (höchstens 24 Stunden).
  Jeder Raum beginnt so spät wie möglich und ist 15 Minuten vor
  Terminbeginn warm (Komforttemperatur minus 0,5 K). Heizen andere Räume
  gleichzeitig, verlängert sich die Vorheizzeit entsprechend. Reicht die Zeit
  nicht für alle, entscheidet der Vorrang. Wo es Energie spart, gibt es eine
  Vorstufe: früh auf einen Zwischen-Sollwert (Komfort minus 3 K), spät auf
  Komfort. Räume ohne Termin werden während knapper Phasen 2 K unter die
  Absenkung gesetzt, damit ihre Ventile den aufheizenden Räumen keinen
  Vorlauf wegnehmen.
- **Sparsam rechnen:** Ein fertiger Plan bleibt, solange er aufgeht. Alle
  10 Minuten prüft ein einziger Simulationslauf das; neu geplant wird nur bei
  geänderten Terminen oder Einstellungen, neuen gelernten Werten, spätestens
  nach 3 Stunden, oder wenn ein Raum zu spät käme.
- **Lernen:** Alle 5 Minuten ein Messpunkt (Raumtemperatur, Ventilstellung,
  Außentemperatur, Vorlauf). Auskühlphasen ergeben den Wärmeverlust,
  Heizphasen Aufheizrate, Leistungsbedarf und Pufferreserve. Der Vorlauf
  zeigt direkt, welchen Anteil ihres Bedarfs die Räume bekommen. Ein
  Sicherheitszuschlag passt sich so an, dass rund 95 % der Termine pünktlich
  warm sind (Start 25 %). Gelernt wird auch bei ausgeschalteter Automatik,
  dann aus dem Homematic-Profil.
- **Kalibrierlauf:** Der Dienst `nextcloud_raumbelegung.kalibrierlauf` heizt
  an einem freien Tag einen oder mehrere Räume unter idealen Bedingungen auf
  (Puffer voll, andere Räume tiefer abgesenkt). Zwei Läufe reichen für den
  Anfang: der Saal allein, dann alle kleinen Räume zusammen. Er wartet, bis
  90 Minuten lang kein Raum aufgeheizt hat, und gibt die Räume danach zurück.
- **Kesseldaten (optional):** Mit dem StokerCloud-Anlagennamen (z. B.
  „nawatec“ aus `stokercloud.dk/dev/nawatec.html`) liest die Integration
  Vorlauf, Warmwasser, Leistung und Status des NBE-Kessels, nur lesend und
  ohne Passwort. Werte, die älter als 30 Minuten sind, werden ignoriert;
  liefert der Kessel länger als 2 Stunden keine Daten, gibt es eine Meldung.
  Ein ausdrücklich gewählter Vorlauf-Sensor hat Vorrang.
- **Funkkontingent:** Sie sendet nur echte Änderungen, höchstens alle
  3 Minuten je Raum. Ist der Duty Cycle des Homematic Access Points über
  70 %, sendet sie nur noch Aufheiz-Befehle. Meldet der Access Point keinen
  Duty Cycle, gilt eine eigene Obergrenze von 30 Befehlen je Stunde.
  Kommt ein Befehl nicht an (z. B. kurze Cloud-Unterbrechung), wird er
  nach 3 Minuten wiederholt statt als Handverstellung gewertet.
- **Handverstellung:** Wer während eines Termins am Thermostat dreht, behält
  seinen Wert bis Terminende (außerhalb von Terminen 2 Stunden). Solche
  Vorgänge zählen nicht für den Sicherheitszuschlag.
- **Automatik aus:** Schaltet man die Automatik (global oder je Raum) aus,
  gibt die Steuerung die Thermostate sofort an ihr Homematic-Profil zurück
  (Automodus neu gesetzt). Im Heizprotokoll stehen dessen Aufheizvorgänge
  als „Homematic-Profil“.
- **Betriebsart der Thermostate:** Solange die Automatik an ist, stellt die
  Steuerung die gesteuerten Heizgruppen auf „Manuell“; das Homematic-Profil
  greift dann nicht ein. Fällt Home Assistant aus, bleibt der letzte
  Sollwert stehen.

## Voraussetzungen in Homematic IP

1. Je Raum eine Heizgruppe mit allen Thermostaten (und ggf. dem
   Thermometer) des Raums. Home Assistant zeigt sie als eine
   `climate`-Entität.
2. Die Gruppe in Home Assistant dem Bereich mit **genau dem Namen der
   Nextcloud-Ressource** zuordnen (z. B. „Saal“).
3. Das bisherige Heizprofil kann bleiben: Es gilt immer dann, wenn die
   Automatik aus ist.
4. Temperatur-Offsets direkt in der Homematic-App einstellen.

## Installation

Voraussetzung: Home Assistant 2026.11 oder neuer (Marketplace).

1. Einstellungen > Marketplace öffnen und, falls noch nicht geschehen, mit
   einem GitHub-Konto verbinden.
2. Oben rechts „Add from link“ wählen und
   `Bibelkreisrennerod/ha-nextcloud-raumbelegung` als Integration hinzufügen.
3. Die Integration installieren und Home Assistant neu starten.

## Einrichtung

1. In Nextcloud ein App-Passwort erzeugen (Einstellungen > Sicherheit), am
   besten für einen eigenen Benutzer, der die Kalender nur lesen darf.
2. In Home Assistant: Einstellungen > Geräte & Dienste > Integration
   hinzufügen > „Nextcloud Raumbelegung“.
3. Adresse der Nextcloud, Benutzername und App-Passwort eingeben.
4. Die Kalender auswählen, aus denen Raumbuchungen gelesen werden. Angeboten
   werden eigene, freigegebene und übertragene (delegierte) Kalender.

## Was angelegt wird

Je Raum ein Gerät:

| Entität | Bedeutung |
| --- | --- |
| Heizbedarf | An, solange die Steuerung den Raum aufheizt, warm hält oder ein Termin läuft |
| Belegt | An, solange laut Kalender ein Termin läuft |
| Sollwert Steuerung | Was die Steuerung gerade vorgibt, auch Zwischenwerte; Attribut Betriebsart |
| Geplanter Heizstart | Beginn von Vorstufe oder Komfort; Attribute mit beiden Zeiten und Frist |
| Warm um (Prognose) | Wann der Raum laut Plan warm ist (mit Sicherheitszuschlag) |
| Vorheizzeit | Frist minus geplanter Heizstart |
| Raumtemperatur | Temperatur, mit der die Steuerung rechnet |
| Ventilstellung | Mittel der Heizkörperventile im Raum |
| Auskühlrate | Temperaturverlust in K/h bei 20 K Unterschied nach außen |
| Max. Aufheizrate | K/h bei offenen Ventilen und vollem Puffer |
| Leistungsbedarf | Anteil der Kessel-Dauerleistung bei offenen Ventilen (Attribut kW) |
| Heizprotokoll | Letzte Heizvorgänge mit Start, erreicht um, Abweichung zur Frist |
| Komforttemperatur | Zieltemperatur während Terminen (Standard 21 °C) |
| Vorheizzeit Startwert | Wie lange der Raum allein von 14 auf 21 °C braucht, bei 0 °C außen; gilt, bis gemessen wurde (Standard 120 min) |
| Vorrang | 1 = höchster; entscheidet, wenn die Zeit nicht für alle reicht |
| Automatik | Aus: Thermostate dieses Raums werden nicht gesteuert |

Dazu ein Gerät „Heizungssteuerung“ mit Status (Attribut `plan`),
Pufferreserve, Pufferkapazität, Kessel-Dauerleistung, Kessel-Anlaufzeit,
Kalibrierung (letzte Auswertung als Attribut), Absenktemperatur (14 °C) und
dem globalen Schalter „Automatik“. In den Optionen der Integration lassen
sich Wetter-Entität, Vorlauf-Sensor, Außentemperatur-Sensor,
Duty-Cycle-Sensor und StokerCloud-Anlagenname wählen.

Lastgewicht, Lastbudget und Vorlauf-Schwelle aus Version 0.2 gibt es nicht
mehr; die Entitäten werden beim Update entfernt.

Räume werden aus den in Nextcloud angelegten Ressourcen und aus den Terminen
selbst übernommen. Neue Räume erscheinen automatisch. Die Attribute der
Sensoren zeigen den laufenden oder nächsten Termin (`event`, `start`, `end`,
`calendar`).

## Verhalten

- Abruf der Termine alle 5 Minuten, Zeitfenster von gestern bis 7 Tage voraus; die Steuerung rechnet jede Minute.
- Serientermine werden von Nextcloud aufgelöst, Ausnahmen einzelner Termine
  sind berücksichtigt.
- Abgesagte Termine und abgelehnte Raumbuchungen zählen nicht.
- Ganztägige Termine zählen nicht.
- Ist Nextcloud über 30 Minuten nicht erreichbar, plant die Steuerung mit dem
  letzten Stand weiter und meldet das unter „Benachrichtigungen“. Ebenso
  gemeldet werden verspätet warme Räume und ein knappes Funkkontingent.

## Entfernen

Den Eintrag unter Geräte & Dienste löschen, danach die Integration im
Marketplace entfernen. Es bleiben keine Dateien oder Einstellungen zurück.
