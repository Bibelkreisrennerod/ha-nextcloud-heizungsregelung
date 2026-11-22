"""Temperatur-Offset der Homematic-IP-Thermostate lesen.

Der Offset (-3,5 bis +3,5 K in 0,5-K-Schritten) ist eine Geraeteeinstellung.
Die Integration Homematic IP Cloud stellt ihn nicht als Entitaet bereit, die
Bibliothek homematicip haelt ihn aber in den Daten jedes Thermostats
(Kanal mit "temperatureOffset"). Gelesen wird nur; geht etwas nicht (andere
Version der Integration, Thermostat nicht gefunden), ist der Offset None und
die Regelung arbeitet wie bisher.

Zuordnung: Die Klima-Entitaet einer Homematic-IP-Heizgruppe hat die
Gruppen-ID in ihrer unique_id. Die Gruppe kennt ihre Geraete. Misst ein
Wandthermostat die Raumtemperatur, gilt dessen Offset, sonst der Mittelwert
der Heizkoerperthermostate.
"""

from __future__ import annotations

from collections.abc import Iterable
import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

_LOGGER = logging.getLogger(__name__)

HMIP_DOMAIN = "homematicip_cloud"
WALL_CHANNELS = ("WALL_MOUNTED_THERMOSTAT", "ROOM_CONTROL")  # Kanaltypen, die mit Wandgeraet anfangen
RADIATOR_CHANNEL = "HEATING_THERMOSTAT_CHANNEL"


def _homes(hass: HomeAssistant) -> list[Any]:
    """Alle homematicip-Home-Objekte der eingerichteten Access Points."""
    homes = []
    for entry in hass.config_entries.async_entries(HMIP_DOMAIN):
        hap = getattr(entry, "runtime_data", None)
        home = getattr(hap, "home", None)
        if home is not None:
            homes.append(home)
    data = hass.data.get(HMIP_DOMAIN)
    if isinstance(data, dict):
        for hap in data.values():
            home = getattr(hap, "home", None)
            if home is not None and home not in homes:
                homes.append(home)
    return homes


def _raw(obj: Any) -> dict[str, Any]:
    raw = getattr(obj, "_rawJSONData", None)
    return raw if isinstance(raw, dict) else {}


def _find(items: Iterable[Any] | None, wanted: str) -> Any:
    for item in items or ():
        if getattr(item, "id", None) == wanted:
            return item
    return None


def _group_devices(home: Any, group: Any) -> list[Any]:
    devices = list(getattr(group, "devices", None) or [])
    if devices:
        return devices
    # Rohdaten der Gruppe: Liste der Kanaele mit Geraete-ID
    result = []
    for channel in _raw(group).get("channels") or []:
        if isinstance(channel, dict):
            device = _find(getattr(home, "devices", None), channel.get("deviceId"))
            if device is not None and device not in result:
                result.append(device)
    return result


def _channels(device: Any) -> list[tuple[str, float]]:
    """(Kanaltyp, Offset) aller Kanaele eines Geraets mit Offset."""
    result = []
    channels = _raw(device).get("functionalChannels")
    if isinstance(channels, dict):
        for channel in channels.values():
            if isinstance(channel, dict) and isinstance(channel.get("temperatureOffset"), (int, float)):
                result.append((str(channel.get("functionalChannelType", "")), float(channel["temperatureOffset"])))
    if result:
        return result
    for channel in getattr(device, "functionalChannels", None) or []:
        value = getattr(channel, "temperatureOffset", None)
        if isinstance(value, (int, float)):
            result.append((str(getattr(channel, "functionalChannelType", "")), float(value)))
    return result


def group_offset(home: Any, group: Any) -> float | None:
    wall, radiator = [], []
    for device in _group_devices(home, group):
        for kind, value in _channels(device):
            if kind.startswith(WALL_CHANNELS):
                wall.append(value)
            elif kind == RADIATOR_CHANNEL or "THERMOSTAT" in kind:
                radiator.append(value)
    values = wall or radiator
    return sum(values) / len(values) if values else None


def climate_offsets(hass: HomeAssistant, entity_ids: Iterable[str]) -> dict[str, float | None]:
    """Offset je Klima-Entitaet (None, wenn er sich nicht lesen laesst)."""
    result: dict[str, float | None] = {}
    entity_ids = list(entity_ids)
    try:
        homes = _homes(hass)
        registry = er.async_get(hass)
        for entity_id in entity_ids:
            result[entity_id] = None
            entry = registry.async_get(entity_id)
            if entry is None or entry.platform != HMIP_DOMAIN or not homes:
                continue
            unique_id = entry.unique_id or ""
            for home in homes:
                groups = getattr(home, "groups", None) or []
                group = next((g for g in groups if getattr(g, "id", None) and g.id in unique_id), None)
                if group is not None:
                    result[entity_id] = group_offset(home, group)
                    break
    except Exception:  # noqa: BLE001 - nur lesen, nie die Regelung stoeren
        _LOGGER.debug("Offset der Thermostate nicht lesbar", exc_info=True)
        return {entity_id: None for entity_id in entity_ids}
    return result
