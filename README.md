# Nextcloud Raumbelegung

Integration für Home Assistant: liest Raumbuchungen aus Nextcloud-Kalendern und
stellt je Raum einen Sensor „Heizbedarf“ bereit. Gedacht für die
kalenderbasierte Steuerung von Heizungsthermostaten.

Ein Raum gilt als gebucht, wenn er im Termin als Ressource eingetragen ist
(Nextcloud: Termin > Ressourcen). Technisch ist das ein Teilnehmer vom Typ
Raum (`CUTYPE=ROOM`). Die eingebaute CalDAV-Integration von Home Assistant
gibt diese Information nicht weiter, deshalb fragt diese Integration
Nextcloud direkt ab.

> Stand: Version 0.2.6. Planer und Lernmodell sind mit Simulationen getestet,
> der Betrieb mit echten Thermostaten steht noch aus.

## Was die Steuerung macht

- **Planen:** Für jeden gebuchten Raum berechnet sie, wie lange er zum
  Aufheizen braucht, und startet rechtzeitig, damit er 15 Minuten vor
  Terminbeginn warm ist (Komforttemperatur minus 0,5 K).
- **Lernen:** Jeder Aufheizvorgang wird gemessen. Daraus lernt sie je Raum
  Aufheizrate und Wärmeverlust, abhängig von der Außentemperatur samt
  Wetterprognose. Ein Sicherheitszuschlag passt sich so an, dass rund
  95 % der Termine pünktlich warm sind. Am Anfang gilt der eingestellte
  Startwert; er wird je gemessenem Vorgang um höchstens 10 % unterschritten.
- **Kesselengpass:** Jeder Raum hat ein Lastgewicht, der Kessel ein
  Lastbudget. Mehr Räume als das Budget erlaubt heizen nie gleichzeitig auf;
  die Steuerung staffelt stattdessen früher. Fällt die Vorlauftemperatur
  (optional) zehn Minuten unter die Schwelle, pausiert sie den Raum mit dem
  größten Zeitpuffer für 20 Minuten.
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
  Vorgänge zählen nicht zum Lernen.
- **Automatik aus:** Schaltet man die Automatik (global oder je Raum) aus,
  gibt die Steuerung die Thermostate sofort an ihr Homematic-Profil zurück
  (Automodus neu gesetzt). Solange sie aus ist, beobachtet sie die
  Aufheizvorgänge des Profils und lernt daraus; im Heizprotokoll stehen sie
  als „Homematic-Profil“.
- **Ausfallsicherheit:** Die Thermostate laufen im Homematic-Automodus mit
  einem flachen Profil auf Absenktemperatur. Ein Sollwert dieser Steuerung
  gilt nur bis zum nächsten Profil-Schaltpunkt; setzt das Profil ihn
  während einer Heizphase zurück, sendet die Steuerung ihn erneut. Fällt
  Home Assistant aus, kühlen die Räume von selbst auf Absenktemperatur ab.

## Voraussetzungen in Homematic IP

1. Je Raum eine Heizgruppe mit allen Thermostaten (und ggf. dem
   Thermometer) des Raums. Home Assistant zeigt sie als eine
   `climate`-Entität.
2. Die Gruppe in Home Assistant dem Bereich mit **genau dem Namen der
   Nextcloud-Ressource** zuordnen (z. B. „Saal“).
3. In der Homematic-App ein Heizprofil mit durchgehend Absenktemperatur
   (14 °C) und Schaltpunkten etwa alle 3 Stunden; Gruppen im Automodus.
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
| Vorheizzeit | Gelernte Vorheizzeit für den nächsten Termin (mit Zuschlag) |
| Geplanter Heizstart | Wann das Aufheizen beginnt |
| Raumtemperatur | Temperatur, mit der die Steuerung rechnet |
| Heizprotokoll | Letzte Heizvorgänge mit Start, erreicht um, Abweichung zur Frist |
| Komforttemperatur | Zieltemperatur während Terminen (Standard 21 °C) |
| Vorheizzeit Startwert | Startwert für 14 → 21 °C bei 0 °C außen (Standard 120 min) |
| Lastgewicht | Anteil am Kesselbudget während des Aufheizens (Standard 0,5) |
| Vorrang | 1 = höchster; entscheidet bei Engpässen |
| Automatik | Aus: Thermostate dieses Raums werden nicht gesteuert |

Dazu ein Gerät „Heizungssteuerung“ mit Status, Absenktemperatur (14 °C),
Lastbudget (1,0), Vorlauf-Schwelle (45 °C) und dem globalen Schalter
„Automatik“. In den Optionen der Integration lassen sich Wetter-Entität,
Vorlauf-Sensor, Außentemperatur-Sensor und Duty-Cycle-Sensor wählen.

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
