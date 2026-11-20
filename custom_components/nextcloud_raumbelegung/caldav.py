"""CalDAV-Zugriff auf Nextcloud: Kalender finden, Termine und Raeume lesen.

Der Auswerteteil (parse_*) nutzt nur die Standardbibliothek und laesst sich
ohne Home Assistant testen.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import re
from urllib.parse import unquote, urljoin
import xml.etree.ElementTree as ET

DAV = "{DAV:}"
CAL = "{urn:ietf:params:xml:ns:caldav}"
CS = "{http://calendarserver.org/ns/}"

_PROPFIND_PRINCIPAL = (
    '<d:propfind xmlns:d="DAV:"><d:prop><d:current-user-principal/></d:prop></d:propfind>'
)
_PROPFIND_HOMES = (
    '<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav" '
    'xmlns:cs="http://calendarserver.org/ns/"><d:prop><c:calendar-home-set/>'
    "<cs:calendar-proxy-read-for/><cs:calendar-proxy-write-for/></d:prop></d:propfind>"
)
_PROPFIND_CALENDARS = (
    '<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"><d:prop>'
    "<d:displayname/><d:resourcetype/><c:supported-calendar-component-set/>"
    "</d:prop></d:propfind>"
)
_PROPFIND_NAMES = '<d:propfind xmlns:d="DAV:"><d:prop><d:displayname/></d:prop></d:propfind>'
_REPORT_EVENTS = (
    '<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
    '<d:prop><c:calendar-data><c:expand start="{start}" end="{end}"/></c:calendar-data></d:prop>'
    '<c:filter><c:comp-filter name="VCALENDAR"><c:comp-filter name="VEVENT">'
    '<c:time-range start="{start}" end="{end}"/>'
    "</c:comp-filter></c:comp-filter></c:filter></c:calendar-query>"
)


class NextcloudError(Exception):
    """Nextcloud nicht erreichbar oder unerwartete Antwort."""


class NextcloudAuthError(NextcloudError):
    """Benutzername oder Passwort falsch."""


@dataclass
class RoomEvent:
    """Ein einzelner Termin (Serien sind bereits aufgeloest)."""

    summary: str
    start: datetime
    end: datetime
    all_day: bool
    rooms: list[str] = field(default_factory=list)
    location: str = ""
    calendar: str = ""
    description: str = ""
    categories: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- Auswertung


def _unescape(value: str, newline: str = " ") -> str:
    return (
        value.replace("\\n", newline)
        .replace("\\N", newline)
        .replace("\\,", ",")
        .replace("\\;", ";")
        .replace("\\\\", "\\")
    )


def _split_line(line: str) -> tuple[str, dict[str, str], str]:
    """'NAME;PARAM=x:WERT' zerlegen, Anfuehrungszeichen beachten."""
    in_quotes = False
    for i, char in enumerate(line):
        if char == '"':
            in_quotes = not in_quotes
        elif char == ":" and not in_quotes:
            head, value = line[:i], line[i + 1 :]
            break
    else:
        return line.upper(), {}, ""
    parts = re.split(r';(?=(?:[^"]*"[^"]*")*[^"]*$)', head)
    params: dict[str, str] = {}
    for part in parts[1:]:
        if "=" in part:
            key, val = part.split("=", 1)
            params[key.upper()] = val.strip('"')
    return parts[0].upper(), params, value


def _parse_time(params: dict[str, str], value: str) -> tuple[datetime, bool]:
    value = value.strip()
    if params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", value):
        day = datetime.strptime(value[:8], "%Y%m%d").replace(tzinfo=timezone.utc)
        return day, True
    parsed = datetime.strptime(value.rstrip("Z")[:15], "%Y%m%dT%H%M%S")
    return parsed.replace(tzinfo=timezone.utc), False


def parse_events(xml_text: str, calendar_name: str = "") -> list[RoomEvent]:
    """Alle Termine aus einer CalDAV-Antwort lesen."""
    events: list[RoomEvent] = []
    root = ET.fromstring(xml_text)
    for node in root.iter(CAL + "calendar-data"):
        ical = (node.text or "").replace("\r\n", "\n").replace("\r", "\n")
        ical = re.sub(r"\n[ \t]", "", ical)  # umgebrochene Zeilen zusammenfuegen
        for block in ical.split("BEGIN:VEVENT")[1:]:
            block = block.split("END:VEVENT")[0]
            summary = location = description = ""
            categories: list[str] = []
            start = end = None
            all_day = cancelled = False
            rooms: list[str] = []
            depth = 0
            for line in block.split("\n"):
                if line.startswith("BEGIN:"):
                    depth += 1  # z. B. VALARM ueberspringen
                    continue
                if line.startswith("END:"):
                    depth -= 1
                    continue
                if depth or not line.strip():
                    continue
                name, params, value = _split_line(line)
                if name == "SUMMARY":
                    summary = _unescape(value).strip()
                elif name == "LOCATION":
                    location = _unescape(value).strip()
                elif name == "DESCRIPTION":
                    description = _unescape(value, "\n").strip()
                elif name == "CATEGORIES":
                    for item in re.split(r"(?<!\\),", value):
                        item = _unescape(item).strip()
                        if item and item not in categories:
                            categories.append(item)
                elif name == "DTSTART":
                    start, all_day = _parse_time(params, value)
                elif name == "DTEND":
                    end, _ = _parse_time(params, value)
                elif name == "STATUS" and value.strip().upper() == "CANCELLED":
                    cancelled = True
                elif name == "ATTENDEE" and params.get("CUTYPE", "").upper() == "ROOM":
                    if params.get("PARTSTAT", "").upper() != "DECLINED":
                        room = params.get("CN", "").strip()
                        if room and room not in rooms:
                            rooms.append(room)
            if cancelled or start is None:
                continue
            if end is None:
                end = start + (timedelta(days=1) if all_day else timedelta(hours=1))
            events.append(
                RoomEvent(
                    summary, start, end, all_day, rooms, location, calendar_name, description, categories
                )
            )
    return events


def _hrefs(element: ET.Element, tag: str) -> list[str]:
    return [
        href.text.strip()
        for found in element.iter(tag)
        for href in found.iter(DAV + "href")
        if href.text
    ]


def parse_current_principal(xml_text: str) -> str | None:
    """Pfad des angemeldeten Benutzers."""
    hrefs = _hrefs(ET.fromstring(xml_text), DAV + "current-user-principal")
    return hrefs[0] if hrefs else None


def parse_homes(xml_text: str) -> tuple[list[str], list[str]]:
    """(eigene Kalenderordner, Benutzer, die ihre Kalender uebertragen haben)."""
    root = ET.fromstring(xml_text)
    homes = _hrefs(root, CAL + "calendar-home-set")
    proxies = _hrefs(root, CS + "calendar-proxy-read-for") + _hrefs(
        root, CS + "calendar-proxy-write-for"
    )
    return homes, list(dict.fromkeys(proxies))


def parse_calendars(xml_text: str) -> list[tuple[str, str]]:
    """(Pfad, Name) aller Terminkalender eines Kalenderordners."""
    result: list[tuple[str, str]] = []
    for response in ET.fromstring(xml_text).iter(DAV + "response"):
        href = response.findtext(DAV + "href")
        if not href:
            continue
        if not any(True for rt in response.iter(DAV + "resourcetype") for _ in rt.iter(CAL + "calendar")):
            continue
        components = [comp.get("name") for comp in response.iter(CAL + "comp")]
        if components and "VEVENT" not in components:
            continue
        name = ""
        for display in response.iter(DAV + "displayname"):
            name = (display.text or "").strip()
        result.append((href.strip(), name or unquote(href.rstrip("/").rsplit("/", 1)[-1])))
    return result


def parse_room_names(xml_text: str) -> list[str]:
    """Namen aller in Nextcloud angelegten Raeume."""
    names: list[str] = []
    for response in ET.fromstring(xml_text).iter(DAV + "response"):
        for display in response.iter(DAV + "displayname"):
            name = (display.text or "").strip()
            if name and name not in names:
                names.append(name)
    return names


# -------------------------------------------------------------------- Zugriff


class NextcloudClient:
    """Liest Kalender einer Nextcloud per CalDAV (nur lesend)."""

    def __init__(self, session, base_url: str, username: str, password: str) -> None:
        import aiohttp  # in Home Assistant immer vorhanden

        self._session = session
        self._root = base_url.rstrip("/") + "/remote.php/dav/"
        self._auth = aiohttp.BasicAuth(username, password, encoding="utf-8")

    def _abs(self, href: str) -> str:
        return urljoin(self._root, href)

    async def _request(self, method: str, url: str, body: str, depth: str) -> str:
        import aiohttp

        headers = {"Content-Type": "application/xml; charset=utf-8", "Depth": depth}
        try:
            async with asyncio.timeout(30):
                async with self._session.request(
                    method, url, data=body.encode("utf-8"), headers=headers, auth=self._auth
                ) as response:
                    if response.status == 401:
                        raise NextcloudAuthError("Anmeldung abgelehnt (HTTP 401)")
                    if response.status not in (200, 207):
                        raise NextcloudError(f"HTTP {response.status} bei {method} {url}")
                    return await response.text()
        except (aiohttp.ClientError, TimeoutError) as err:
            raise NextcloudError(f"Nextcloud nicht erreichbar: {err}") from err

    async def async_discover_calendars(self) -> list[dict[str, str]]:
        """Eigene, freigegebene und uebertragene Kalender finden."""
        principal = parse_current_principal(
            await self._request("PROPFIND", self._root, _PROPFIND_PRINCIPAL, "0")
        )
        if not principal:
            raise NextcloudError("Benutzer konnte nicht ermittelt werden")
        homes, proxies = parse_homes(
            await self._request("PROPFIND", self._abs(principal), _PROPFIND_HOMES, "0")
        )
        sources: list[tuple[str, str]] = [(home, "") for home in homes]
        for proxy in proxies:
            owner = unquote(proxy.rstrip("/").rsplit("/", 1)[-1])
            proxy_homes: list[str] = []
            try:
                proxy_homes, _ = parse_homes(
                    await self._request("PROPFIND", self._abs(proxy), _PROPFIND_HOMES, "0")
                )
            except NextcloudAuthError:
                raise
            except (NextcloudError, ET.ParseError):
                pass
            if not proxy_homes and "/principals/users/" in proxy:
                proxy_homes = [proxy.replace("/principals/users/", "/calendars/")]
            sources += [(home, owner) for home in proxy_homes]

        calendars: list[dict[str, str]] = []
        seen: set[str] = set()
        for home, owner in sources:
            try:
                found = parse_calendars(
                    await self._request("PROPFIND", self._abs(home), _PROPFIND_CALENDARS, "1")
                )
            except NextcloudAuthError:
                raise
            except (NextcloudError, ET.ParseError):
                continue
            for href, name in found:
                url = self._abs(href)
                if url in seen:
                    continue
                seen.add(url)
                calendars.append({"url": url, "name": f"{name} ({owner})" if owner else name})
        return calendars

    async def async_get_room_names(self) -> list[str]:
        """In Nextcloud angelegte Raeume. Leere Liste, wenn nicht lesbar."""
        try:
            return parse_room_names(
                await self._request(
                    "PROPFIND", self._root + "principals/calendar-rooms/", _PROPFIND_NAMES, "1"
                )
            )
        except (NextcloudError, ET.ParseError):
            return []

    async def async_get_events(
        self, url: str, start: datetime, end: datetime, calendar_name: str = ""
    ) -> list[RoomEvent]:
        """Termine eines Kalenders im Zeitfenster, Serien aufgeloest."""
        body = _REPORT_EVENTS.format(
            start=start.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
            end=end.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        )
        text = await self._request("REPORT", url, body, "1")
        try:
            return parse_events(text, calendar_name)
        except (ET.ParseError, ValueError) as err:
            raise NextcloudError(f"Antwort nicht lesbar: {err}") from err
