"""Support for IEC sensors."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import StateType
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from iec_api.models.invoice import Invoice

from .const import DOMAIN
from .coordinator import IecApiCoordinator


@dataclass(frozen=True)
class IecEntityDescriptionMixin:
    """Mixin values for required keys."""

    value_fn: Callable[[Invoice], str | float]


@dataclass(frozen=True)
class IecEntityDescription(SensorEntityDescription, IecEntityDescriptionMixin):
    """Class describing IEC sensors entities."""


# suggested_display_precision=0 for all sensors since
# IEC provides 0 decimal points for all these.
# (for the statistics in the energy dashboard IEC does provide decimal points)
SMART_ELEC_SENSORS: tuple[IecEntityDescription, ...] = (
    IecEntityDescription(
        key="elec_forecasted_usage",
        name="Current bill electric forecasted usage",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=0,
        value_fn=lambda data: data.forecasted_usage,
    ),
    IecEntityDescription(
        key="elec_forecasted_cost",
        name="Current bill electric forecasted cost",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement="ILS",
        suggested_unit_of_measurement="ILS",
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=0,
        value_fn=lambda data: data.forecasted_cost,
    ),
)

ELEC_SENSORS: tuple[IecEntityDescription, ...] = (
    IecEntityDescription(
        key="iec_last_elec_usage",
        name="Last IEC bill electric usage to date",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        # Not TOTAL_INCREASING because it can decrease for accounts with solar
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=0,
        value_fn=lambda data: data.consumption,
    ),
    IecEntityDescription(
        key="iec_last_cost",
        name="Last IEC bill electric cost",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement="USD",
        suggested_unit_of_measurement="USD",
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=0,
        value_fn=lambda data: data.amount_origin,
    ),

    IecEntityDescription(
        key="iec_last_number_of_days",
        name="Last IEC bill length in days",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.DAYS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=lambda data: data.days_period,
    ),
)


async def async_setup_entry(
        hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the IEC sensor."""

    coordinator: IecApiCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[IecSensor] = []
    contracts = coordinator.data.keys()
    for contract_id in contracts:
        sensors_desc: tuple[IecEntityDescription, ...] = ELEC_SENSORS

        for sensor_desc in sensors_desc:
            entities.append(
                IecSensor(
                    coordinator,
                    sensor_desc,
                    contract_id
                )
            )

    async_add_entities(entities)


class IecSensor(CoordinatorEntity[IecApiCoordinator], SensorEntity):
    """Representation of an IEC sensor."""

    entity_description: IecEntityDescription

    def __init__(
            self,
            coordinator: IecApiCoordinator,
            description: IecEntityDescription,
            contract_id: int,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self.contract_id = contract_id
        self._attr_unique_id = f"{str(contract_id)}_{description.key}"

    @property
    def native_value(self) -> StateType:
        """Return the state."""
        if self.coordinator.data is not None:
            return self.entity_description.value_fn(
                self.coordinator.data[self.contract_id]
            )
        return None
