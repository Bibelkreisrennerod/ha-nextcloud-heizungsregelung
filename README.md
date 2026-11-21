# Nextcloud Raumbelegung

Integration für Home Assistant: liest Raumbuchungen aus Nextcloud-Kalendern und
stellt je Raum einen Sensor „Heizbedarf“ bereit. Gedacht für die
kalenderbasierte Steuerung von Heizungsthermostaten.

Ein Raum gilt als gebucht, wenn er im Termin als Ressource eingetragen ist
(Nextcloud: Termin > Ressourcen). Technisch ist das ein Teilnehmer vom Typ
Raum (`CUTYPE=ROOM`). Die eingebaute CalDAV-Integration von Home Assistant
gibt diese Information nicht weiter, deshalb fragt diese Integration
Nextcloud direkt ab.

> Stand: Version 0.6.0. Modell, Lernen und Planung sind mit Simulationen
> getestet, der Betrieb mit echten Thermostaten steht noch aus.

## Was die Steuerung macht

- **Physikalisches Modell:** Je Raum vier gelernte Größen: Auskühlrate
  (Wärmeverlust, K/h je Kelvin Unterschied nach außen), maximale
  Aufheizrate (K/h bei offenen Ventilen und vollem Puffer),
  Leistungsbedarf (Anteil der Kessel-Dauerleistung bei offenen Ventilen)
  und Totzeit (wie lange es nach dem Öffnen der Ventile dauert, bis die
  Wärme am Temperaturfühler ankommt) und **Trägheit der Heizkörper** (Wasser
  und Stahl speichern Wärme: sie kommt verzögert an und wirkt nach dem
  Schließen nach; beim Boost im Saal am 20.11.2026 gemessen: 10 min Totzeit,
  35 min Trägheit, die Temperatur stieg noch eine Stunde nach dem Schließen).
  Dazu je Raum die **Ventilkennlinie**: wie weit die Homematic-Thermostate
  beim Aufheizen öffnen, abhängig davon, wie weit der Raum unter dem
  Sollwert liegt. Gemessen öffnen sie nie ganz (Saal höchstens 48 %,
  Nebenraum (groß) 83 %) und drosseln schon 2–3 K vor dem Ziel. Die
  Kennlinie wird aus den gemeldeten Ventilstellungen gelernt und im
  Attribut des Sensors „Ventilstellung“ angezeigt. Aufheizrate und
  Leistungsbedarf gelten je 100 % Ventilstellung; die Schwellen für „Ventil
  offen“ beziehen sich auf die Höchstöffnung des Raums. Weil die
  Thermostate einen I-Anteil haben, hält das Modell einen Raum am Sollwert
  mit genau der Öffnung, die er dort braucht.
  Für die Anlage: Kessel-Dauerleistung, Anlaufzeit und nutzbare
  Pufferreserve. Zieht ein Raum mehr als der Kessel liefert (der Saal), deckt
  der Puffer die Lücke, bis er leer ist; danach teilen sich alle offenen
  Räume die Kesselleistung und heizen langsamer.
- **Planen:** Alle Räume werden zusammen simuliert, mit der Wetterprognose,
  in 10-Minuten-Schritten bis zur spätesten Frist (höchstens 24 Stunden).
  Jeder Raum beginnt so spät wie möglich und ist 15 Minuten vor
  Terminbeginn warm (Komforttemperatur minus 0,5 K). Heizen andere Räume
  gleichzeitig, verlängert sich die Vorheizzeit entsprechend. Reicht die Zeit
  nicht für alle, entscheidet die Priorität. Wo es Energie spart, gibt es eine
  Vorstufe: früh auf einen Zwischen-Sollwert (Komfort minus 3 K), spät auf
  Komfort. Räume ohne Termin werden während knapper Phasen 2 K unter die
  Absenkung gesetzt, damit ihre Ventile den aufheizenden Räumen keinen
  Vorlauf wegnehmen.
- **Sparsam rechnen:** Ein fertiger Plan bleibt, solange er aufgeht. Alle
  10 Minuten prüft ein einziger Simulationslauf das; neu geplant wird nur bei
  geänderten Terminen oder Einstellungen, neuen gelernten Werten, spätestens
  nach 3 Stunden, oder wenn ein Raum zu spät käme.
- **Lernen:** Alle 5 Minuten ein Messpunkt (Raumtemperatur, Ventilstellung,
  Außentemperatur, Kesselleistung, Vorlauf falls ein eigener Sensor gewählt
  ist). Auskühlphasen ergeben den Wärmeverlust, Heizphasen Aufheizrate,
  Leistungsbedarf und Pufferreserve. Ein
  Sicherheitszuschlag passt sich so an, dass rund 95 % der Termine pünktlich
  warm sind (Start 25 %). Gelernt wird auch bei ausgeschalteter Automatik,
  dann aus dem Homematic-Profil.
- **Lernen aus der Historie:** Beim ersten Start liest die Integration die
  vom Recorder gespeicherten Raumtemperaturen, Sollwerte und Ventilstellungen
  (meist die letzten 10 Tage) und lernt daraus, statt bei null anzufangen.
  Der Dienst `nextcloud_raumbelegung.aus_historie_lernen` wiederholt das und
  verwirft dabei die bisher gelernten Werte.
- **Gemeinsame Anpassung (ab 0.4):** Einmal am Tag, in einer Ruhephase,
  passt die Integration alle Werte gemeinsam an die neuen Messpunkte an:
  Auskühlrate, Aufheizrate, Totzeit, Trägheit und Leistungsbedarf je Raum
  sowie die Pufferreserve. Gesucht werden die Werte, mit denen das Modell die
  gemessenen Verläufe 30, 60 und 120 Minuten voraus am besten nachrechnet
  (Systemidentifikation mit Grey-Box-Modell, Levenberg-Marquardt). Das
  Ergebnis der letzten Anpassung geht als Vorwissen ein und verliert je Tag
  an Gewicht (Halbwertszeit etwa 12 Tage); feste Werte gibt es nicht, nur
  Startwerte, die Messungen überstimmen. Aus der Krümmung der Fehlerfläche
  (Fisher-Information) folgt die Unsicherheit jedes Werts; sie steht als
  Attribut an den Sensoren und bestimmt den Sicherheitszuschlag
  (95 % der Aufheizdauern). Die Rechnung läuft im Hintergrund und dauert
  für einen Tag unter einer Sekunde.
- **Ab 0.6:**
  - **Ventilstellung → Wärme je Raum (gamma):** Ein halb offenes Ventil gibt
    mehr als die halbe Wärme ab (Boost im Saal: etwa 0,8). Wärme = Öffnung
    hoch gamma, gelernt je Raum, Startwert 0,8.
  - **Nachbarräume:** Räume, die durch Türen verbunden sind (Option
    „Durch Türen verbundene Räume“, eine Gruppe je Zeile), tauschen Wärme aus.
    Die Anpassung lernt je Raum, wie stark (k in 1/h), und schätzt für jede
    halbe Stunde, ob die Türen der Gruppe offen oder zu waren (Viterbi,
    Wechsel kosten etwas, im Zweifel offen). Die Planung rechnet mit dem
    Anteil der Zeit, in der die Türen offen waren, und nutzt k nur, soweit die
    Messungen ihn sicher belegen (untere 95-%-Grenze). Voreingestellt für die
    Gemeinde: „Saal, Küche“ und „Nebenraum (groß), Nebenraum (klein),
    Ehemaliger Sonntagsschulraum, Vorraum Küche 2, Küche 2“.
  - **Plausibilitätsgrenzen:** Werte, die physikalisch nicht passen (bei der
    Höchstöffnung schneller als 10 K/h, mehr als das 1,5-Fache der
    Kesselleistung, bei 20 K Unterschied nach außen nicht zu halten), bremst
    ein Strafterm; sie stehen im Attribut `grenzen` der „Modellanpassung“.
  - **Kesseldaten richtig gedeutet:** Die Kesseltemperatur aus StokerCloud
    ist kein Vorlauf (Puffer und Mischer liegen dazwischen; die Heizkreise
    laufen nicht über die Kesselsteuerung) und wird nicht mehr als Vorlauf
    verwendet; frühere so gespeicherte Werte werden verworfen. Die
    Kesselleistung (kW) geht als gemessene Energiezufuhr in den Puffer ein,
    in der Anpassung und in der Pufferreserve. Ein echter Vorlauf-Sensor kann
    weiterhin in den Optionen gewählt werden.
  - **„Vorheizzeit Startwert“ entfällt:** Bis zur ersten Anpassung gilt
    intern 120 Minuten (14 → 21 °C allein bei 0 °C außen).
- **Empfohlener Testlauf:** Nach jeder Anpassung rechnet die Integration
  für mögliche Kalibrierläufe (jeder Raum allein, der größte Raum mit jedem
  anderen, alle außer dem größten, alle zusammen) aus, wie stark sie die
  Unsicherheit verkleinern würden, bei der kältesten Außentemperatur der
  nächsten 24 Stunden. Der beste steht im Sensor „Empfohlener Testlauf“.
- **Kalibriert:** Alle 30 Minuten rechnet die Steuerung die letzten
  2 Stunden je Raum nach und vergleicht mit der Messung. Liegt der mittlere
  Fehler unter 0,5 K (nach mindestens 12 Stunden ohne und 4 Stunden mit
  Heizen), gilt das Modell des Raums als kalibriert. Bis zur ersten
  gemeinsamen Anpassung plant sie mit mindestens 25 % Sicherheitszuschlag.
- **Kalibrierlauf:** Der Dienst `nextcloud_raumbelegung.kalibrierlauf` heizt
  an einem freien Tag einen oder mehrere Räume unter idealen Bedingungen auf
  (Puffer voll, andere Räume tiefer abgesenkt). Zwei Läufe reichen für den
  Anfang: der Saal allein, dann alle kleinen Räume zusammen. Er wartet, bis
  90 Minuten lang kein Raum aufgeheizt hat, und gibt die Räume danach zurück.
- **Kesseldaten (optional):** Mit dem StokerCloud-Anlagennamen (z. B.
  „nawatec“ aus `stokercloud.dk/dev/nawatec.html`) liest die Integration
  Kesseltemperatur, Warmwasser, Leistung und Status des NBE-Kessels, nur
  lesend und ohne Passwort, am eigenen Gerät „Kessel“. Werte, die älter als
  30 Minuten sind, werden ignoriert; liefert der Kessel länger als 2 Stunden
  keine Daten, gibt es eine Meldung. Der Status zeigt „heizt“, „in
  Bereitschaft“, „aus“ oder „Störung“; die Nummer von StokerCloud (z. B.
  `state_7`) steht im Attribut `code`.
- **Funkkontingent:** Sie sendet nur echte Änderungen, höchstens alle
  3 Minuten je Raum. Ist der Duty Cycle des Homematic Access Points über
  70 %, sendet sie nur noch Aufheiz-Befehle. Meldet der Access Point keinen
  Duty Cycle, gilt eine eigene Obergrenze von 30 Befehlen je Stunde.
  Kommt ein Befehl nicht an (z. B. kurze Cloud-Unterbrechung), wird er
  nach 3 Minuten wiederholt statt als Handverstellung gewertet.
- **Boost:** Während ein Thermostat im Boost läuft (Ventil fest offen, meist
  80 %), sendet die Steuerung nichts, und die Einzelschätzungen
  (Kennlinie, Heizfenster, Totzeit) lassen diese Zeit aus. Die gemeinsame
  Anpassung nutzt den Boost dagegen gern: Ein kurzer Wärmestoß trennt
  Totzeit und Trägheit am besten.
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
- **Neustart von Home Assistant:** Ein laufender Kalibrierlauf geht weiter.
  Steht eine Heizgruppe noch auf „Manuell“, liest die Steuerung am Sollwert
  ab, was vorher lief: Komfort = Aufheizen oder Halten, zwischen Absenkung
  und Komfort = Vorstufe. Das wird dann nicht verschoben. Melden die
  Thermostate nach dem Neustart noch „nicht verfügbar“, wartet die Steuerung
  auf den ersten gültigen Wert; der zählt nicht als Handverstellung. Der globale Schalter
  „Automatik“ ist bei einer neuen Einrichtung aus und schaltet sich auch
  nach einem holprigen Neustart nicht von selbst ein.
- **Kalender nicht erreichbar:** Fällt ein einzelner Kalender aus, gelten
  für ihn die zuletzt gelesenen Termine weiter. Nur wenn kein Kalender
  lesbar ist oder die Anmeldung abgelehnt wird, meldet die Integration einen
  Fehler.

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

Die Kalenderauswahl lässt sich später ändern, ohne Gelerntes zu verlieren:
Geräte & Dienste > Nextcloud Raumbelegung > ⋮ > Neu konfigurieren. Fehlt dort
ein Kalender, muss er in Nextcloud erst für den Benutzer der Integration
freigegeben werden. Den Eintrag zu löschen und neu anzulegen löscht dagegen
alle gelernten Werte.

## Was angelegt wird

Jede Entität erklärt sich selbst: Beim Antippen steht im Attribut
`erklaerung`, was der Wert bedeutet und wie er entsteht.

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
| Totzeit | Minuten vom Öffnen der Ventile, bis die Wärme am Fühler ankommt |
| Vorhersagefehler | Mittlere Abweichung der 2-Stunden-Vorhersage von der Messung (K) |
| Modell kalibriert | An, wenn der Vorhersagefehler unter 0,5 K liegt |
| Heizprotokoll | Letzte Heizvorgänge mit Start, erreicht um, Abweichung zur Frist |
| Komforttemperatur | Zieltemperatur während Terminen (Standard 21 °C) |
| Priorität | 1 = am wichtigsten; entscheidet, wenn die Zeit nicht für alle reicht |
| Automatik | Aus: Thermostate dieses Raums werden nicht gesteuert (auch nicht vom Kalibrierlauf) |

Dazu ein Gerät „Heizungssteuerung“ mit Status (Attribut `plan`),
Pufferreserve, Pufferkapazität,
Kalibrierung (letzte Auswertung als Attribut), Modellanpassung (mittlerer
Vorhersagefehler, Unsicherheiten je Raum), Empfohlener Testlauf,
Absenktemperatur (14 °C) und
dem globalen Schalter „Automatik“ (bei neuer Einrichtung aus). In den Optionen der Integration lassen
sich Wetter-Entität, Vorlauf-Sensor (nur ein echter Vorlauf an den
Heizkörpern), Außentemperatur-Sensor,
Duty-Cycle-Sensor, StokerCloud-Anlagenname und die Regeln für die
Zieltemperatur je Termin wählen.

und ein Gerät „Kessel“ mit Temperatur, Warmwasser, Leistung, letzter
Meldung, Status, Dauerleistung und Anlaufzeit.

Lastgewicht, Lastbudget und Vorlauf-Schwelle aus Version 0.2 und
„Vorheizzeit Startwert“ aus 0.5 gibt es nicht mehr; die Entitäten werden
beim Update entfernt. Ab 0.6 werden Entitäts-IDs mit doppeltem Raumnamen
(`sensor.saal_saal_warm_um_prognose`) und die Kessel-IDs
(`sensor.heizungssteuerung_kessel_…` → `sensor.kessel_…`, Vorlauf →
`sensor.kessel_temperatur`) beim Start umbenannt; der Verlauf bleibt erhalten.

Räume werden aus den in Nextcloud angelegten Ressourcen und aus den Terminen
selbst übernommen. Neue Räume erscheinen automatisch. Die Attribute der
Sensoren zeigen den laufenden oder nächsten Termin (`event`, `start`, `end`,
`calendar`).

## Zieltemperatur je Termin

Ohne weitere Angabe gilt im Termin die Komforttemperatur des Raums. Einzelne
Termine können abweichen, die erste passende Angabe gilt:

1. **Temperatur-Tag im Termin** (in Nextcloud „Tags“). Der Tag muss das Wort
   „Temperatur“ enthalten, damit er nicht mit anderen Tags verwechselt wird;
   die Reihenfolge ist frei und „°C“ nicht nötig: `Temperatur 20`,
   `20 °C Temperatur`, `Temperatur Saal 20°C`, `20 Temperatur Saal`. Ohne
   Raum gilt der Wert für alle im Termin als Ressource gebuchten Räume, mit
   Raum nur für diesen; die Angabe für den Raum geht der allgemeinen vor. Ein
   Raum, der im Termin nicht als Ressource gebucht ist, ist nicht erlaubt. Ein
   Wert mit Vorzeichen oder `K` ist relativ zur Komforttemperatur
   (`Temperatur -1 K`). Die Terminbeschreibung wird nicht ausgewertet.
2. **Regeln in den Optionen der Integration** (Geräte & Dienste > Nextcloud
   Raumbelegung > Konfigurieren), eine je Zeile:

   ```
   gottesdienst @ Saal = -1,5 K
   jugend = 20
   ```

   Das Stichwort wird ohne Groß-/Kleinschreibung im Titel und in den Tags
   gesucht, „gottesdienst“ passt also auch auf
   „Abendmahlgottesdienst“ und „Predigtgottesdienst“. `@ Raum` (mehrere mit
   Komma) beschränkt die Regel auf diese Räume. Die erste passende Regel gilt.
3. Sonst die **Komforttemperatur** des Raums.

Laufen mehrere Termine gleichzeitig, gilt der höchste Wert. Unrealistische
Werte zählen nicht: erlaubt sind 15–25 °C, relativ höchstens 5 K. Eine Regel
außerhalb lehnt der Optionsdialog ab. Ein Temperatur-Tag mit Wert außerhalb,
mit nicht gebuchtem oder unbekanntem Raum, ohne Zahl oder mit mehreren Zahlen
wird übergangen (dann gilt Regel oder Komforttemperatur) und unter
„Benachrichtigungen“ gemeldet. Heizt der Plan während eines Termins schon
für den nächsten mit höherem Ziel, gilt der höhere Wert. Welches Ziel gerade
gilt und warum, zeigen die Attribute `zieltemperatur`, `ziel_grund` und
`ziel_hinweis` (übergangene Tags) von „Heizbedarf“ (laufender oder nächster
Termin) und „Geplanter Heizstart“ (Termin, für den geplant wird).

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
