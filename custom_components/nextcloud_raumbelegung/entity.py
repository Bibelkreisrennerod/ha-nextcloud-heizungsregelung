"""Gemeinsame Basis: ein Geraet je Raum."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.util import slugify

from .const import DOMAIN


def signal_preheat(entry: ConfigEntry) -> str:
    """Signal: eine Vorheizzeit wurde geaendert."""
    return f"{DOMAIN}_{entry.entry_id}_preheat"


def room_unique_id(entry: ConfigEntry, room: str, key: str) -> str:
    """Eindeutige Kennung einer Raum-Entitaet."""
    return f"{entry.entry_id}_{slugify(room)}_{key}"


def room_device_info(entry: ConfigEntry, room: str) -> DeviceInfo:
    """Geraet, unter dem alle Entitaeten eines Raums haengen."""
    return DeviceInfo(
        identifiers={(DOMAIN, f"{entry.entry_id}_{slugify(room)}")},
        name=room,
        manufacturer="Nextcloud",
        model="Raum",
        entry_type=DeviceEntryType.SERVICE,
    )
