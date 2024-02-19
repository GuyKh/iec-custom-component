"""Sensor platform for iec."""
from __future__ import annotations

from typing import Any  # noqa: UP035

from homeassistant.components.sensor import SensorEntity, SensorEntityDescription, SensorDeviceClass
from homeassistant.const import UnitOfEnergy

from .const import DOMAIN, ATTR_BP_NUMBER, ATTR_METER_NUMBER, ATTR_METER_TYPE, ATTR_METER_CODE, \
    ATTR_METER_IS_ACTIVE, ATTR_METER_READINGS
from .coordinator import IecDataUpdateCoordinator
from .entity import IecEntity

SENSOR_DESCRIPTION = SensorEntityDescription(
        key="iec",
        icon="mdi:format-quote-close",
        state_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR
    )


async def async_setup_entry(hass, entry, async_add_devices):
    """Set up the sensor platform."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_devices(
        IecSensor(
            coordinator=coordinator,
            entity_description=SENSOR_DESCRIPTION,
            meter_number=coordinator.data.get(key)[ATTR_METER_NUMBER],
            meter_type=coordinator.data.get(key)[ATTR_METER_TYPE],
            meter_code=coordinator.data.get(key)[ATTR_METER_CODE],
            meter_is_active=coordinator.data.get(key)[ATTR_METER_IS_ACTIVE],
            bp_number=coordinator.data.get(key)[ATTR_BP_NUMBER]
        )
        for key in coordinator.data
    )


class IecSensor(IecEntity, SensorEntity):
    """iec Sensor class."""

    def __init__(
            self,
            coordinator: IecDataUpdateCoordinator,
            entity_description: SensorEntityDescription,
            bp_number: str,
            meter_number: str,
            meter_type: int,
            meter_code: str,
            meter_is_active: bool
    ) -> None:
        """Initialize the sensor class."""
        super().__init__(coordinator)
        self._bp_number = bp_number
        self._meter_number = meter_number
        self.coordinator = coordinator
        self.entity_description = entity_description
        self._name = "IEC Meter " + self._meter_number
        self.attrs = {
            ATTR_BP_NUMBER: self._bp_number,
            ATTR_METER_NUMBER: self._meter_number,
            ATTR_METER_TYPE: meter_type,
            ATTR_METER_CODE: meter_code,
            ATTR_METER_IS_ACTIVE: meter_is_active
        }

    @property
    def name(self) -> str:
        """Return the name of the entity."""
        return self._name

    @property
    def unique_id(self) -> str:
        """Return the unique ID of the sensor."""
        return self._meter_number

    @property
    def device_state_attributes(self) -> dict[str, Any]:
        """Return the state attributes of the sensor."""
        return self.attrs

    @property
    def native_value(self) -> str:
        """Return the native value of the sensor."""
        return self.coordinator.data.get(self._meter_number)[ATTR_METER_READINGS]
