"""Konstanten der Integration Nextcloud Raumbelegung."""

from datetime import timedelta

DOMAIN = "nextcloud_raumbelegung"

CONF_CALENDARS = "calendars"

UPDATE_INTERVAL = timedelta(minutes=5)
DAYS_BACK = 1
DAYS_AHEAD = 7

DEFAULT_PREHEAT_MINUTES = 60.0
