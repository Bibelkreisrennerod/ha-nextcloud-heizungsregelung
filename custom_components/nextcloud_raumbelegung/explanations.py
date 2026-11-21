"""Erklaerungen zu den Entitaeten (Attribut "erklaerung", sichtbar beim Antippen)."""

from __future__ import annotations

from typing import Any

EXPLANATIONS: dict[str, str] = {
    # Raum: Einstellungen
    "automatic": (
        "Aus: Die Steuerung lässt die Thermostate dieses Raums in Ruhe, auch beim Kalibrierlauf. "
        "Wirkt nur, wenn die globale Automatik an ist."
    ),
    "comfort": "Zieltemperatur während eines Termins. Der Raum soll sie zu Terminbeginn erreicht haben. Einzelne Termine können abweichen: ein Tag „20“ oder „20 °C“ im Termin, „Temperatur: 21“ in der Beschreibung oder eine Regel in den Optionen der Integration (z. B. „gottesdienst @ Saal = -1,5 K“). Erlaubt sind 15–25 °C; andere Angaben werden übergangen und gemeldet.",
    "preheat": (
        "Schätzung, wie lange der Raum allein von 14 auf 21 °C braucht (bei 0 °C aussen). "
        "Nur Startannahme: Sobald der Raum in einer Modellanpassung war, gelten die gelernten Werte."
    ),
    "priority": (
        "1 = am wichtigsten. Reicht die Kesselleistung nicht für alle, wird der Raum mit der kleineren "
        "Zahl zuerst warm; Räume mit größerer Zahl müssen notfalls warten."
    ),
    # Raum: Zustand und Plan
    "heating_demand": "An, solange die Steuerung den Raum aufheizt oder warm hält (Komfort-Sollwert). Attribut „zieltemperatur“: Ziel des laufenden oder nächsten Termins, „ziel_grund“ sagt, woher es kommt (Regel, Angabe im Termin oder Komforttemperatur des Raums).",
    "occupied": "An während eines Termins im Nextcloud-Kalender, der diesen Raum belegt.",
    "room_temperature": "Gemessene Raumtemperatur, gemittelt über die Heizgruppen des Raums.",
    "setpoint": "Sollwert, den die Steuerung gerade für diesen Raum vorsieht (Komfort, Vorstufe oder Absenkung).",
    "valve": (
        "Mittlere Öffnung der Heizkörperventile in Prozent, wie die Thermostate sie melden. "
        "Die Attribute zeigen die gelernte Ventilkennlinie: wie weit die Ventile beim Aufheizen öffnen, "
        "abhängig davon, wie weit der Raum unter dem Sollwert liegt."
    ),
    "lead": (
        "Wie viele Minuten vor Terminbeginn das Aufheizen laut Plan beginnt. Der Plan berücksichtigt die "
        "anderen Räume, die Kesselleistung, die Wettervorhersage und den Sicherheitszuschlag. "
        "Leer, solange nichts geplant ist (z. B. Automatik aus). Attribut allein_bestfall_min: wie lange der "
        "Raum jetzt allein bei vollem Puffer bis zur Komforttemperatur bräuchte, ohne Zuschlag."
    ),
    "planned_start": "Zeitpunkt, zu dem laut Plan das Aufheizen beginnt (Vorstufe oder Komfort). Attribut „zieltemperatur“: Ziel des Termins, für den geplant wird.",
    "warm_at": "Zeitpunkt, zu dem der Raum laut Plan die Komforttemperatur erreicht (bis 0,5 K darunter).",
    "protocol": "Die letzten Heizvorgänge: Beginn, wann warm, Vorhersage und Abweichung zur Frist.",
    # Raum: gelernte Werte
    "cooling_rate": (
        "Wie schnell der Raum ohne Heizen auskühlt. Angezeigt in K/h bei 20 K Unterschied zwischen Raum "
        "und draußen. Gerechnet wird mit der Auskühlkonstante in 1/h (= K/h je K Unterschied): bei 0,05 "
        "und 10 K Unterschied kühlt der Raum 0,5 K pro Stunde ab. Zeitkonstante = 1 / Konstante."
    ),
    "heating_rate": (
        "Wie schnell die Heizkörper den Raum erwärmen können, ohne Abzug des Wärmeverlusts und bei vollem "
        "Puffer. Das Modell rechnet je 100 % Ventilstellung: Wärme = Aufheizrate x gemeldete Ventilstellung. "
        "Da die Ventile nur bis zur Höchstöffnung öffnen, ist Aufheizrate x Höchstöffnung der realistische "
        "Wert; netto bleibt davon der Wärmeverlust abzuziehen."
    ),
    "power_demand": (
        "Anteil der Kessel-Dauerleistung, den der Raum zieht, gerechnet je 100 % Ventilstellung. Ziehen alle "
        "offenen Räume zusammen mehr als 100 %, leert sich der Puffer und alle heizen langsamer."
    ),
    "dead_time": (
        "Totzeit: wie lange es nach dem Öffnen der Ventile dauert, bis überhaupt Wärme am Thermometer "
        "ankommt. Attribut traegheit_min: danach kommt die Wärme verzögert an und wirkt nach dem Schließen "
        "noch nach (Speicherwirkung von Wasser und Heizkörper)."
    ),
    "prediction_error": (
        "Alle 30 min rechnet die Steuerung die letzten 2 Stunden mit dem Modell nach und vergleicht mit der "
        "Messung. Angezeigt ist der mittlere Fehler in K. Unter 0,5 K (nach mindestens 12 h ohne und 4 h mit "
        "Heizen) gilt das Modell des Raums als kalibriert."
    ),
    "calibrated": (
        "An, wenn der Vorhersagefehler unter 0,5 K liegt, gemessen über mindestens 12 Stunden ohne und "
        "4 Stunden mit Heizen. Solange nicht kalibriert, plant die Steuerung vorsichtiger."
    ),
    # Anlage
    "global_automatic": (
        "Hauptschalter. Aus: Die Steuerung gibt alle Heizgruppen an ihr Homematic-Profil zurück, beobachtet "
        "nur und lernt weiter. An: Die Steuerung stellt die Sollwerte nach Kalender und Plan."
    ),
    "setback": "Temperatur, auf die Räume außerhalb von Terminen abgesenkt werden.",
    "global_status": (
        "Was die Steuerung gerade tut: 'Automatik aus' (nur beobachten und lernen), 'n Räume heizen' "
        "(Automatik an) oder der Stand eines Kalibrierlaufs. Das Attribut plan zeigt den aktuellen Heizplan."
    ),
    "global_buffer_reserve": (
        "Geschätzte nutzbare Wärme im Pufferspeicher in Prozent der Pufferkapazität. Ohne Puffersensoren "
        "aus Ventilstellungen und Kesselleistung fortgeschrieben."
    ),
    "global_buffer_capacity": (
        "Wie lange der Puffer zusammen mit dem Kessel mehr Leistung liefern kann, als der Kessel allein schafft, "
        "umgerechnet in kWh. Gelernt aus Heizphasen, in denen der Puffer leer lief."
    ),
    "global_boiler_rated": "Dauerleistung des Kessels (aus StokerCloud oder 30 kW); Bezug für den Leistungsbedarf der Räume.",
    "global_boiler_startup": "Wie lange der Kessel nach dem Anfordern braucht, bis er Leistung liefert.",
    "global_calibration": (
        "Stand des Kalibrierlaufs (Dienst kalibrierlauf): 'kein Lauf', 'wartet auf Ruhe' (90 min, bis der Puffer "
        "voll ist) oder 'läuft'. Das Attribut zeigt die letzte Auswertung."
    ),
    "global_model_fit": (
        "Mittlerer Fehler (K), mit dem das Modell bei der letzten täglichen Anpassung die gemessenen "
        "Raumtemperaturen 30, 60 und 120 min voraus nachgerechnet hat. Unter 0,2 K gut, über 0,5 K stimmt "
        "das Modell noch nicht. Die Attribute zeigen je Raum die Unsicherheit der gelernten Werte."
    ),
    "global_test_run": (
        "Der Kalibrierlauf, der die Unsicherheit der gelernten Werte am stärksten verkleinern würde, "
        "berechnet für die kälteste Stunde der nächsten 24 h. Informationsgewinn 1,0 heisst: der Bereich "
        "der noch möglichen Werte schrumpft um den Faktor e (2,7). Starten über den Dienst kalibrierlauf "
        "mit den Räumen aus dem Attribut raeume."
    ),
    "global_boiler_state": "Betriebszustand des Kessels laut StokerCloud.",
    "global_boiler_flow": "Vorlauftemperatur des Kessels laut StokerCloud.",
    "global_boiler_dhw": "Warmwassertemperatur laut StokerCloud.",
    "global_boiler_power": "Aktuelle Kesselleistung in Prozent laut StokerCloud.",
    "global_boiler_updated": "Zeitpunkt der letzten Meldung des Kessels an StokerCloud.",
}


class Explained:
    """Haengt jeder Entitaet ihre Erklaerung als Attribut "erklaerung" an.

    Unterklassen liefern ihre eigenen Attribute ueber _extra(). Die Erklaerung
    landet nicht im Recorder (sie aendert sich nie).
    """

    _unrecorded_attributes: frozenset[str] = frozenset()

    def __init_subclass__(cls, **kwargs: Any) -> None:
        cls._unrecorded_attributes = frozenset(cls._unrecorded_attributes) | {"erklaerung"}
        super().__init_subclass__(**kwargs)

    def _explanation(self) -> str | None:
        key = getattr(self, "_attr_translation_key", None) or ""
        return EXPLANATIONS.get(key) or EXPLANATIONS.get(key.removeprefix("global_"))

    def _extra(self) -> dict[str, Any] | None:
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        attrs = dict(self._extra() or {})
        text = self._explanation()
        if text:
            attrs["erklaerung"] = text
        return attrs or None
