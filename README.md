# Nextcloud Raumbelegung

Integration für Home Assistant: liest Raumbuchungen aus Nextcloud-Kalendern und
stellt je Raum einen Sensor „Heizbedarf“ bereit. Gedacht für die
kalenderbasierte Steuerung von Heizungsthermostaten.

Ein Raum gilt als gebucht, wenn er im Termin als Ressource eingetragen ist
(Nextcloud: Termin > Ressourcen). Technisch ist das ein Teilnehmer vom Typ
Raum (`CUTYPE=ROOM`). Die eingebaute CalDAV-Integration von Home Assistant
gibt diese Information nicht weiter, deshalb fragt diese Integration
Nextcloud direkt ab.

> Stand: Version 0.1.0, Prototyp. Die Kalenderauswertung ist getestet, der
> Betrieb in Home Assistant noch nicht.

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

Je Raum entsteht ein Gerät mit drei Entitäten:

| Entität | Bedeutung |
| --- | --- |
| Heizbedarf | An ab „Vorheizzeit“ vor Terminbeginn bis Terminende |
| Belegt | An, solange ein Termin im Raum läuft |
| Vorheizzeit | Minuten Vorlauf, einstellbar von 0 bis 240, Standard 60 |

Räume werden aus den in Nextcloud angelegten Ressourcen und aus den Terminen
selbst übernommen. Neue Räume erscheinen automatisch. Die Attribute der
Sensoren zeigen den laufenden oder nächsten Termin (`event`, `start`, `end`,
`calendar`).

## Verhalten

- Abruf alle 5 Minuten, Zeitfenster von gestern bis 7 Tage voraus.
- Serientermine werden von Nextcloud aufgelöst, Ausnahmen einzelner Termine
  sind berücksichtigt.
- Abgesagte Termine und abgelehnte Raumbuchungen zählen nicht.
- Ganztägige Termine zählen nicht.
- Ist Nextcloud nicht erreichbar, werden die Sensoren „nicht verfügbar“. Eine
  Automation sollte diesen Zustand wie „aus“ behandeln oder melden.

## Beispiel-Automation

```yaml
automation:
  - alias: Heizung Saal nach Kalender
    triggers:
      - trigger: state
        entity_id: binary_sensor.saal_heizbedarf
    actions:
      - action: climate.set_temperature
        target:
          entity_id: climate.saal
        data:
          temperature: "{{ 21 if trigger.to_state.state == 'on' else 16 }}"
```

## Entfernen

Den Eintrag unter Geräte & Dienste löschen, danach die Integration im
Marketplace entfernen. Es bleiben keine Dateien oder Einstellungen zurück.
