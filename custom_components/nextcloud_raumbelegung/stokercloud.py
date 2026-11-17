"""Lesezugriff auf StokerCloud (NBE-Pelletkessel), ohne Passwort.

Die StokerCloud gibt die Werte einer Anlage ueber ihren Namen frei (dieselben
Daten wie die oeffentliche Ansicht stokercloud.dk/dev/<name>.html). Zum
Lesen reicht der Name, ein Passwort ist nur zum Aendern noetig und wird hier
nie verwendet.

Die Antwort ist nicht offiziell dokumentiert. Der Auswerteteil ist deshalb
tolerant: er sammelt alle Werte mit Kennung und sucht Vorlauf, Warmwasser,
Leistung und Zeitstempel ueber mehrere moegliche Namen. Alle gefundenen
Kennungen stehen als Attribut am Status-Sensor, damit sich die Zuordnung
pruefen laesst.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import re
from typing import Any
from urllib.parse import quote

BASE = "https://stokercloud.dk/v2/dataout2/"
SCREENS = ",".join(f"b{i}" for i in range(1, 31))
STALE_AFTER = timedelta(minutes=30)

# Moegliche Kennungen/Namen (klein geschrieben, Teilstring), in Reihenfolge.
FLOW_KEYS = ("flow", "vorlauf", "fremløb", "fremlob", "boilertemp", "boiler temp")
DHW_KEYS = ("dhw", "warmwasser", "varmt vand", "hot water")
POWER_KEYS = ("power_pct", "powerpct", "power %", "power")
RETURN_KEYS = ("return", "rücklauf", "ruecklauf", "retur")
EXCLUDE = ("target", "wanted", "soll", "ønsket", "liter", "l/h", "kw", "setpoint", "diff")

_DATE = re.compile(r"(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})[ T](\d{1,2}):(\d{2})(?::(\d{2}))?")


class StokerCloudError(Exception):
    """StokerCloud nicht erreichbar oder Antwort unerwartet."""


@dataclass
class StokerData:
    """Ausgewertete Kesselwerte."""

    values: dict[str, dict[str, Any]] = field(default_factory=dict)  # Kennung -> {value, unit, name}
    flow: float | None = None
    dhw: float | None = None
    power: float | None = None
    return_temp: float | None = None
    state: str | None = None
    updated: datetime | None = None  # Zeitpunkt der letzten Meldung des Kessels
    fetched: datetime | None = None
    stale: bool = True


def _number(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        match = re.search(r"-?\d+(?:[.,]\d+)?", value)
        if match:
            return float(match.group(0).replace(",", "."))
    return None


def _collect(node: Any, out: dict[str, dict[str, Any]], dates: list[datetime], path: str = "") -> None:
    """Alle Eintraege mit Kennung und Wert einsammeln, Datumsangaben merken."""
    if isinstance(node, dict):
        key = node.get("id") or node.get("name") or (path or None)
        if key is not None and "value" in node:
            ident = str(key)
            if ident in out:
                ident = f"{path}.{ident}" if path else ident
            out[ident] = {
                "value": node.get("value"),
                "unit": node.get("unit"),
                "name": node.get("name"),
            }
        for child_key, child in node.items():
            if isinstance(child, (dict, list)):
                _collect(child, out, dates, child_key)
            elif isinstance(child, str):
                _scan_date(child, dates)
            elif child_key not in ("id", "name", "unit", "value") and isinstance(child, (int, float)):
                out.setdefault(child_key if not path else f"{path}.{child_key}", {"value": child, "unit": None, "name": None})
    elif isinstance(node, list):
        for item in node:
            _collect(item, out, dates, path)
    elif isinstance(node, str):
        _scan_date(node, dates)


def _scan_date(text: str, dates: list[datetime]) -> None:
    match = _DATE.search(text)
    if not match:
        return
    day, month, year, hour, minute, second = match.groups()
    try:
        dates.append(
            datetime(int(year), int(month), int(day), int(hour), int(minute), int(second or 0))
        )
    except ValueError:
        pass


def _pick(values: dict[str, dict[str, Any]], keys: tuple[str, ...], unit_hint: str | None) -> float | None:
    for wanted in keys:
        for ident, item in values.items():
            label = f"{ident} {item.get('name') or ''}".lower()
            if wanted not in label or any(word in label for word in EXCLUDE):
                continue
            unit = str(item.get("unit") or "").lower()
            if unit_hint and unit and unit_hint not in unit:
                continue
            number = _number(item.get("value"))
            if number is not None:
                return number
    return None


def parse(payload: Any, now: datetime, tz) -> StokerData:
    """Antwort von controllerdata2 auswerten. tz: lokale Zeitzone des Kessels."""
    values: dict[str, dict[str, Any]] = {}
    dates: list[datetime] = []
    _collect(payload, values, dates)
    data = StokerData(values=values, fetched=now)
    data.flow = _pick(values, FLOW_KEYS, "c")
    data.dhw = _pick(values, DHW_KEYS, "c")
    data.return_temp = _pick(values, RETURN_KEYS, "c")
    data.power = _pick(values, POWER_KEYS, "%")
    for ident in ("state", "status", "boilerstate"):
        for key, item in values.items():
            if key.lower().endswith(ident) and isinstance(item.get("value"), str):
                data.state = item["value"]
                break
        if data.state:
            break
    if dates:
        latest = max(dates).replace(tzinfo=tz)
        if latest <= now + timedelta(hours=1):
            data.updated = latest.astimezone(timezone.utc)
    for key, item in values.items():
        if any(word in key.lower() for word in ("connected", "online")):
            if str(item.get("value")).lower() in ("0", "false", "no"):
                data.stale = True
                return data
    data.stale = data.updated is None or now - data.updated > STALE_AFTER
    return data


class StokerCloudClient:
    """Liest die Werte einer Anlage. Nur GET-Anfragen, kein Passwort."""

    def __init__(self, session, name: str) -> None:
        self._session = session
        self._name = name.strip()
        self._token: str | None = None

    async def _get_json(self, url: str) -> Any:
        import aiohttp

        try:
            async with asyncio.timeout(20):
                async with self._session.get(url) as response:
                    if response.status != 200:
                        raise StokerCloudError(f"HTTP {response.status}")
                    return await response.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise StokerCloudError(str(err)) from err

    async def _login(self) -> None:
        data = await self._get_json(f"{BASE}login.php?user={quote(self._name)}")
        token = data.get("token") if isinstance(data, dict) else None
        if not token:
            raise StokerCloudError("Anlage nicht gefunden oder keine Freigabe")
        self._token = token

    async def async_read(self, now: datetime, tz) -> StokerData:
        """Aktuelle Werte lesen (bei abgelaufenem Zugang einmal neu anmelden)."""
        for attempt in range(2):
            if self._token is None:
                await self._login()
            payload = await self._get_json(
                f"{BASE}controllerdata2.php?screen={SCREENS}&token={self._token}"
            )
            if isinstance(payload, dict) and payload.get("notloggedin"):
                self._token = None
                continue
            return parse(payload, now, tz)
        raise StokerCloudError("Anmeldung abgelehnt")
