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

from .const import DOMAIN, ILS
from .coordinator import IecApiCoordinator


@dataclass(frozen=True, kw_only=True)
class IecEntityDescriptionMixin:
    """Mixin values for required keys."""

    value_fn: Callable[[Invoice], str | float]


@dataclass(frozen=True, kw_only=True)
class IecEntityDescription(SensorEntityDescription, IecEntityDescriptionMixin):
    """Class describing IEC sensors entities."""


def get_previous_bill_kwh_price(invoice: Invoice) -> float:
    """Calculate the previous bill's kilowatt-hour price by dividing the consumption by the original amount.

    :param invoice: An instance of the Invoice class.
    :return: The previous bill's kilowatt-hour price as a float.
    """

    if not invoice.consumption or not invoice.amount_origin:
        return 0
    return invoice.consumption/invoice.amount_origin


SMART_ELEC_SENSORS: tuple[IecEntityDescription, ...] = (
    IecEntityDescription(
        key="elec_forecasted_usage",
        name="Next bill electric forecasted usage",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        # state_class=SensorStateClass.TOTAL,
        suggested_display_precision=0,
        value_fn=lambda data: data[1].future_consumption,
    ),
    IecEntityDescription(
        key="elec_forecasted_cost",
        name="Next bill electric forecasted cost",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=ILS,
        # state_class=SensorStateClass.TOTAL,
        suggested_display_precision=0,
        # The API doesn't provide future *cost* so we can try to estimate it by the previous consumption
        value_fn=lambda data: data[1].future_consumption * get_previous_bill_kwh_price(data[0])

    ),
)

ELEC_SENSORS: tuple[IecEntityDescription, ...] = (
    IecEntityDescription(
        key="iec_last_elec_usage",
        name="Last IEC bill electric usage to date",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=0,
        value_fn=lambda data: data[0].consumption,
    ),
    IecEntityDescription(
        key="iec_last_cost",
        name="Last IEC bill electric cost",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=ILS,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=2,
        value_fn=lambda data: data[0].amount_origin,
    ),
    IecEntityDescription(
        key="iec_last_number_of_days",
        name="Last IEC bill length in days",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.DAYS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=lambda data: data[0].days_period,
    ),
    IecEntityDescription(
        key="iec_bill_date",
        name="Last IEC bill date",
        device_class=SensorDeviceClass.DATE,
        value_fn=lambda data: data[0].to_date.date(),
    ),
    IecEntityDescription(
        key="iec_last_meter_reading",
        name="Last IEC bill meter reading",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=0,
        value_fn=lambda data: data[0].meter_readings[0].reading,
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
        if coordinator.is_smart_meter:
            sensors_desc: tuple[IecEntityDescription, ...] = ELEC_SENSORS + SMART_ELEC_SENSORS
        else:
            sensors_desc: tuple[IecEntityDescription, ...] = ELEC_SENSORS
        # sensors_desc: tuple[IecEntityDescription, ...] = ELEC_SENSORS

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
