"""Support for IEC sensors."""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

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
from iec_api.models.remote_reading import RemoteReading

from .commons import find_reading_by_date
from .const import DOMAIN, ILS, STATICS_DICT_NAME, STATIC_KWH_TARIFF, FUTURE_CONSUMPTIONS_DICT_NAME, INVOICE_DICT_NAME, \
    ILS_PER_KWH, DAILY_READINGS_DICT_NAME, STATIC_CONTRACT, EMPTY_REMOTE_READING
from .coordinator import IecApiCoordinator

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class IecEntityDescriptionMixin:
    """Mixin values for required keys."""

    value_fn: Callable[[dict | tuple], str | float] | None = None


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
    return invoice.consumption / invoice.amount_origin


def _get_reading_by_date(readings: list[RemoteReading] | None, desired_date: datetime) -> RemoteReading:
    if not readings:
        return EMPTY_REMOTE_READING
    try:
        reading = next(reading for reading in readings if find_reading_by_date(reading, desired_date))
        return reading

    except StopIteration:
        _LOGGER.info(f"Couldn't find daily reading for date: {desired_date.strftime('%Y-%m-%d')}")
        return EMPTY_REMOTE_READING


SMART_ELEC_SENSORS: tuple[IecEntityDescription, ...] = (
    IecEntityDescription(
        key="elec_forecasted_usage",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        # state_class=SensorStateClass.TOTAL,
        suggested_display_precision=3,
        value_fn=lambda data: data[FUTURE_CONSUMPTIONS_DICT_NAME].future_consumption,
    ),
    IecEntityDescription(
        key="elec_forecasted_cost",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=ILS,
        # state_class=SensorStateClass.TOTAL,
        suggested_display_precision=2,
        # The API doesn't provide future *cost* so we can try to estimate it by the previous consumption
        value_fn=lambda data: data[FUTURE_CONSUMPTIONS_DICT_NAME].future_consumption * data[STATICS_DICT_NAME][
            STATIC_KWH_TARIFF]
    ),
    IecEntityDescription(
        key="elec_today_consumption",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        # state_class=SensorStateClass.TOTAL,
        suggested_display_precision=3,
        value_fn=lambda data: _get_reading_by_date(data[DAILY_READINGS_DICT_NAME], datetime.now()).value
    ),
    IecEntityDescription(
        key="elec_yesterday_consumption",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        # state_class=SensorStateClass.TOTAL,
        suggested_display_precision=3,
        value_fn=lambda data: _get_reading_by_date(data[DAILY_READINGS_DICT_NAME],
                                                   datetime.now() - timedelta(days=1)).value,
    ),
    IecEntityDescription(
        key="elec_this_month_consumption",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        # state_class=SensorStateClass.TOTAL,
        suggested_display_precision=3,
        value_fn=lambda data: sum([reading.value for reading in data[DAILY_READINGS_DICT_NAME]
                                   if reading.date.month == datetime.now().month]),
    ),
    IecEntityDescription(
        key="elec_latest_meter_reading",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=3,
        value_fn=lambda data: data[FUTURE_CONSUMPTIONS_DICT_NAME].total_import
    ),
)

ELEC_SENSORS: tuple[IecEntityDescription, ...] = (
    IecEntityDescription(
        key="iec_last_elec_usage",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=0,
        value_fn=lambda data: data[INVOICE_DICT_NAME].consumption,
    ),
    IecEntityDescription(
        key="iec_last_cost",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=ILS,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=2,
        value_fn=lambda data: data[INVOICE_DICT_NAME].amount_origin,
    ),
    IecEntityDescription(
        key="iec_last_number_of_days",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.DAYS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=lambda data: data[INVOICE_DICT_NAME].days_period,
    ),
    IecEntityDescription(
        key="iec_bill_date",
        device_class=SensorDeviceClass.DATE,
        value_fn=lambda data: data[INVOICE_DICT_NAME].to_date.date(),
    ),
    IecEntityDescription(
        key="iec_last_meter_reading",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=0,
        value_fn=lambda data: data[INVOICE_DICT_NAME].meter_readings[0].reading,
    )
)

STATIC_SENSORS: tuple[IecEntityDescription, ...] = (
    IecEntityDescription(
        key="iec_kwh_tariff",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=ILS_PER_KWH,
        suggested_display_precision=4,
        value_fn=lambda data: data[STATICS_DICT_NAME][STATIC_KWH_TARIFF]
    ),
)


async def async_setup_entry(
        hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the IEC sensor."""

    coordinator: IecApiCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SensorEntity] = []

    if coordinator.is_smart_meter:
        sensors_desc: tuple[IecEntityDescription, ...] = ELEC_SENSORS + SMART_ELEC_SENSORS
    else:
        sensors_desc: tuple[IecEntityDescription, ...] = ELEC_SENSORS
    # sensors_desc: tuple[IecEntityDescription, ...] = ELEC_SENSORS

    contract_id = coordinator.data[STATICS_DICT_NAME][STATIC_CONTRACT]
    for sensor_desc in sensors_desc:
        entities.append(
            IecSensor(
                coordinator,
                sensor_desc,
                contract_id
            )
        )

    for sensor_desc in STATIC_SENSORS:
        entities.append(
            IecSensor(
                coordinator,
                sensor_desc,
                STATICS_DICT_NAME
            )
        )

    async_add_entities(entities)


class IecSensor(CoordinatorEntity[IecApiCoordinator], SensorEntity):
    """Representation of an IEC sensor."""

    _attr_has_entity_name = True
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
        self._attr_translation_key = f"{description.key}"

    @property
    def native_value(self) -> StateType:
        """Return the state."""
        if self.coordinator.data is not None:
            return self.entity_description.value_fn(
                self.coordinator.data
            )
        return None
