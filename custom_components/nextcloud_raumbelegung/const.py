"""Konstanten der Integration Nextcloud Raumbelegung."""

from datetime import timedelta

DOMAIN = "nextcloud_raumbelegung"

CONF_CALENDARS = "calendars"
CONF_FLOW_SENSOR = "flow_sensor"
CONF_OUTSIDE_SENSOR = "outside_sensor"
CONF_WEATHER = "weather_entity"
CONF_DUTY_CYCLE_SENSOR = "duty_cycle_sensor"
CONF_STOKERCLOUD = "stokercloud_name"

DEFAULT_WEATHER = "weather.home"

UPDATE_INTERVAL = timedelta(minutes=5)  # Abruf Nextcloud
CONTROL_INTERVAL = timedelta(minutes=1)  # Takt der Steuerung
FORECAST_INTERVAL = timedelta(minutes=30)
BOILER_INTERVAL = timedelta(minutes=5)  # Abruf StokerCloud
BOILER_ALERT_AFTER = timedelta(hours=2)
DAYS_BACK = 1
DAYS_AHEAD = 7

# Einstellungen: Schluessel -> Standardwert. Raumwerte gelten je Raum,
# globale Werte fuer die ganze Anlage.
ROOM_DEFAULTS: dict[str, float] = {
    "preheat": 120.0,  # Startwert Vorheizzeit (min) fuer 14 -> 21 Grad bei 0 Grad aussen
    "comfort": 21.0,  # Komforttemperatur (Grad)
    "weight": 0.5,  # Lastgewicht
    "priority": 5.0,  # Vorrang, 1 = hoechster
}
GLOBAL_DEFAULTS: dict[str, float] = {
    "setback": 14.0,  # Absenktemperatur (Grad)
    "budget": 1.0,  # Lastbudget des Kessels
    "flow_threshold": 45.0,  # Vorlauf-Schwelle (Grad)
}

EARLY_MINUTES = 15  # Raum soll so viele Minuten vor Terminbeginn warm sein
REACHED_TOLERANCE = 0.5  # "warm" = Komforttemperatur minus diese Kelvin
SETPOINT_TOLERANCE = 0.25
MIN_COMMAND_GAP = timedelta(minutes=3)  # je Raum, schont das Funkkontingent
DUTY_CYCLE_LIMIT = 70.0  # ab hier nur noch Aufheiz-Befehle
DUTY_CYCLE_ALERT = 80.0
FLOW_LOW_MINUTES = 10  # so lange muss der Vorlauf zu niedrig sein
PAUSE_MINUTES = 20  # Dauer einer Lastpause
MANUAL_HOLD_OUTSIDE_EVENT = timedelta(hours=2)
SAMPLE_WINDOW = timedelta(minutes=15)  # Messfenster fuer die Aufheizrate
HISTORY_LENGTH = 50
SOURCE_ALERT_AFTER = timedelta(minutes=30)
