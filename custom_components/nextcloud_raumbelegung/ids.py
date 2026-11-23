"""Kennungen der Entitaeten (ohne Abhaengigkeit von den Entitaetsklassen)."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.util import slugify


def room_unique_id(entry: ConfigEntry, room: str | None, key: str) -> str:
    """Eindeutige Kennung einer Entitaet (room=None: Anlage)."""
    if room is None:
        return f"{entry.entry_id}_global_{key}"
    return f"{entry.entry_id}_{slugify(room)}_{key}"
