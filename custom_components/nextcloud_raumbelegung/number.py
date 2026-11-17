"""Einstellbare Werte je Raum und fuer die Anlage."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.number import NumberDeviceClass, NumberMode, RestoreNumber
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfTemperature, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import GLOBAL_DEFAULTS, ROOM_DEFAULTS
from .entity import add_room_entities, room_device_info, room_unique_id


@dataclass(frozen=True)
class Spec:
    minimum: float
    maximum: float
    step: float
    unit: str | None = None
    device_class: NumberDeviceClass | None = None
    config: bool = True


ROOM_SPECS = {
    "comfort": Spec(10, 26, 0.5, UnitOfTemperature.CELSIUS, NumberDeviceClass.TEMPERATURE, False),
    "preheat": Spec(0, 720, 5, UnitOfTime.MINUTES, NumberDeviceClass.DURATION),
    "weight": Spec(0, 2, 0.05),
    "priority": Spec(1, 9, 1),
}
GLOBAL_SPECS = {
    "setback": Spec(5, 20, 0.5, UnitOfTemperature.CELSIUS, NumberDeviceClass.TEMPERATURE, False),
    "budget": Spec(0.25, 5, 0.05),
    "flow_threshold": Spec(20, 80, 1, UnitOfTemperature.CELSIUS, NumberDeviceClass.TEMPERATURE),
}


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    add_room_entities(
        entry,
        async_add_entities,
        lambda room: [SettingNumber(entry, room, key, spec) for key, spec in ROOM_SPECS.items()],
        [SettingNumber(entry, None, key, spec) for key, spec in GLOBAL_SPECS.items()],
    )


class SettingNumber(RestoreNumber):
    """Wert, den die Steuerung liest. Bleibt ueber Neustarts erhalten."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_mode = NumberMode.BOX

    def __init__(self, entry: ConfigEntry, room: str | None, key: str, spec: Spec) -> None:
        self._entry = entry
        self._room = room
        self._key = key
        self._attr_translation_key = key
        self._attr_unique_id = room_unique_id(entry, room, key)
        self._attr_device_info = room_device_info(entry, room)
        self._attr_native_min_value = spec.minimum
        self._attr_native_max_value = spec.maximum
        self._attr_native_step = spec.step
        self._attr_native_unit_of_measurement = spec.unit
        self._attr_device_class = spec.device_class
        if spec.config:
            self._attr_entity_category = EntityCategory.CONFIG
        defaults = ROOM_DEFAULTS if room is not None else GLOBAL_DEFAULTS
        self._attr_native_value = defaults[key]

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_number_data()
        if last is not None and last.native_value is not None:
            self._attr_native_value = last.native_value
        self._publish()

    async def async_set_native_value(self, value: float) -> None:
        self._attr_native_value = value
        self._publish()
        self.async_write_ha_state()
        controller = self._entry.runtime_data.controller
        if controller is not None:
            controller.request_update()

    def _publish(self) -> None:
        self._entry.runtime_data.settings[(self._room, self._key)] = float(self._attr_native_value)
