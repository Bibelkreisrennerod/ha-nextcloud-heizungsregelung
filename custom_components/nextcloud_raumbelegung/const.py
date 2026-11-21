"""Konstanten der Integration Nextcloud Raumbelegung."""

from datetime import timedelta

DOMAIN = "nextcloud_raumbelegung"

CONF_CALENDARS = "calendars"
CONF_FLOW_SENSOR = "flow_sensor"
CONF_OUTSIDE_SENSOR = "outside_sensor"
CONF_WEATHER = "weather_entity"
CONF_DUTY_CYCLE_SENSOR = "duty_cycle_sensor"
CONF_STOKERCLOUD = "stokercloud_name"
CONF_TARGET_RULES = "target_rules"  # Zieltemperatur je Termin, eine Regel je Zeile
CONF_ROOM_GROUPS = "room_groups"  # Raeume, die durch Tueren verbunden sind, eine Gruppe je Zeile
# Gemeinde Rennerod (Angabe vom 21.11.2026); Namen, die es nicht gibt, werden uebergangen
DEFAULT_ROOM_GROUPS = (
    "Saal, Küche\n"
    "Nebenraum (groß), Nebenraum (klein), Ehemaliger Sonntagsschulraum, Vorraum Küche 2, Küche 2"
)

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
    "comfort": 21.0,  # Komforttemperatur (Grad)
    "priority": 5.0,  # Vorrang, 1 = hoechster
}
GLOBAL_DEFAULTS: dict[str, float] = {
    "setback": 14.0,  # Absenktemperatur (Grad)
}
# bis 0.2.x bzw. 0.5.x (Vorheizzeit Startwert), Entitaeten werden entfernt
REMOVED_SETTINGS = ("weight", "budget", "flow_threshold", "preheat")
# Vorwissen fuer die Aufheizrate eines Raums ohne Messungen: allein von 14 auf
# 21 Grad bei 0 Grad aussen in so vielen Minuten (gilt nur bis zur ersten Anpassung)
PRIOR_PREHEAT_MIN = 120.0

EARLY_MINUTES = 15  # Raum soll so viele Minuten vor Terminbeginn warm sein
REACHED_TOLERANCE = 0.5  # "warm" = Komforttemperatur minus diese Kelvin
SETPOINT_TOLERANCE = 0.25
MIN_COMMAND_GAP = timedelta(minutes=3)  # je Raum, schont das Funkkontingent
COMMAND_RETRY_WINDOW = timedelta(minutes=15)  # so lange gilt ein nicht angekommener Befehl als unserer
MAX_COMMANDS_PER_HOUR = 30  # Obergrenze, wenn der Access Point keinen Duty Cycle meldet
DUTY_CYCLE_LIMIT = 70.0  # ab hier nur noch Aufheiz-Befehle
DUTY_CYCLE_ALERT = 80.0
MANUAL_HOLD_OUTSIDE_EVENT = timedelta(hours=2)
HISTORY_LENGTH = 50
SAMPLE_INTERVAL = timedelta(minutes=5)  # Messpunkt fuers Lernen
PLAN_CHECK_INTERVAL = timedelta(minutes=10)  # Plan pruefen (ein Simulationslauf)
PLAN_MAX_AGE = timedelta(hours=3)  # spaetestens dann neu planen (neue Prognose, neue Parameter)
BOILER_NOMINAL_KW = 30.0  # Kessel-Nennleistung, falls StokerCloud keine meldet

# Kalibrierlauf
CALIBRATION_REST = timedelta(minutes=90)  # vorher so lange kein Raum geheizt (Puffer voll)
CALIBRATION_WAIT = timedelta(hours=4)  # so lange auf Ruhe warten, dann abbrechen
CALIBRATION_HOURS = 6.0  # hoechstens so lange heizen
CALIBRATION_OTHERS_DROP = 4.0  # nicht beteiligte Raeume: Absenkung minus so viel
SOURCE_ALERT_AFTER = timedelta(minutes=30)
