"""Support for IEC sensors."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfEnergy, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import StateType
from iec_api.models.invoice import Invoice
from iec_api.models.remote_reading import RemoteReading

from .commons import TIMEZONE, IecEntityType, find_reading_by_date
from .const import (
    ACCESS_TOKEN_EXPIRATION_TIME,
    ACCESS_TOKEN_ISSUED_AT,
    ATTRIBUTES_DICT_NAME,
    CONTRACT_DICT_NAME,
    DAILY_READINGS_DICT_NAME,
    DOMAIN,
    EMPTY_INVOICE,
    EMPTY_REMOTE_READING,
    EST_BILL_CONSUMPTION_PRICE_ATTR_NAME,
    EST_BILL_DAYS_ATTR_NAME,
    EST_BILL_DELIVERY_PRICE_ATTR_NAME,
    EST_BILL_DISTRIBUTION_PRICE_ATTR_NAME,
    EST_BILL_KWH_CONSUMPTION_ATTR_NAME,
    EST_BILL_TOTAL_KVA_PRICE_ATTR_NAME,
    ESTIMATED_BILL_DICT_NAME,
    FUTURE_CONSUMPTIONS_DICT_NAME,
    ILS,
    ILS_PER_KWH,
    INVOICE_DICT_NAME,
    JWT_DICT_NAME,
    METER_ID_ATTR_NAME,
    STATIC_KWH_TARIFF,
    STATICS_DICT_NAME,
    TOTAL_EST_BILL_ATTR_NAME,
)
from .coordinator import IecApiCoordinator
from .iec_entity import IecEntity

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class IecEntityDescriptionMixin:
    """Mixin values for required keys."""

    value_fn: Callable[[dict | tuple], str | float | date] | None = None
    custom_attrs_fn: (
        Callable[[dict | tuple], dict[str, str | int | float | date]] | None
    ) = None


@dataclass(frozen=True, kw_only=True)
class IecEntityDescription(SensorEntityDescription, IecEntityDescriptionMixin):
    """Class describing IEC sensors entities."""


@dataclass(frozen=True, kw_only=True)
class IecMeterEntityDescription(IecEntityDescription):
    """Class describing IEC sensors entities related to specific meter."""


@dataclass(frozen=True, kw_only=True)
class IecContractEntityDescription(IecEntityDescription):
    """Class describing IEC sensors entities related to specific contract."""


def get_previous_bill_kwh_price(invoice: Invoice) -> float:
    """Calculate the previous bill's kilowatt-hour price by dividing the consumption by the original amount.

    :param invoice: An instance of the Invoice class.
    :return: The previous bill's kilowatt-hour price as a float.
    """

    if not invoice.consumption or not invoice.amount_origin:
        return 0
    return invoice.consumption / invoice.amount_origin


def _get_iec_type_by_class(description: IecEntityDescription) -> IecEntityType:
    """Get IEC type by class."""

    if isinstance(description, IecContractEntityDescription):
        return IecEntityType.CONTRACT
    if isinstance(description, IecMeterEntityDescription):
        return IecEntityType.METER
    return IecEntityType.GENERIC


def _get_reading_by_date(
    readings: list[RemoteReading] | None, desired_datetime: datetime
) -> RemoteReading:
    if not readings:
        return EMPTY_REMOTE_READING

    desired_date = desired_datetime.date()
    try:
        reading = next(
            reading
            for reading in readings
            if find_reading_by_date(reading, desired_date)
        )
        return reading

    except StopIteration:
        _LOGGER.info(
            f"Couldn't find daily reading for date: {desired_date.strftime('%Y-%m-%d')}"
        )
        return EMPTY_REMOTE_READING


DIAGNOSTICS_SENSORS: tuple[IecEntityDescription, ...] = (
    IecEntityDescription(
        key="access_token_expiry_time",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: datetime.fromtimestamp(
            data[ACCESS_TOKEN_EXPIRATION_TIME], tz=TIMEZONE
        ),
    ),
    IecEntityDescription(
        key="access_token_issued_at",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: datetime.fromtimestamp(
            data[ACCESS_TOKEN_ISSUED_AT], tz=TIMEZONE
        ),
    ),
)
SMART_ELEC_SENSORS: tuple[IecEntityDescription, ...] = (
    IecMeterEntityDescription(
        key="elec_forecasted_usage",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        # state_class=SensorStateClass.TOTAL,
        suggested_display_precision=3,
        value_fn=lambda data: (
            data[ESTIMATED_BILL_DICT_NAME][EST_BILL_KWH_CONSUMPTION_ATTR_NAME]
            if data[ESTIMATED_BILL_DICT_NAME]
            else 0
        )
        if (
            data[ESTIMATED_BILL_DICT_NAME]
            and data[ESTIMATED_BILL_DICT_NAME][EST_BILL_KWH_CONSUMPTION_ATTR_NAME]
        )
        else None,
    ),
    IecMeterEntityDescription(
        key="elec_forecasted_cost",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=ILS,
        # state_class=SensorStateClass.TOTAL,
        suggested_display_precision=2,
        # The API doesn't provide future *cost* so we can try to estimate it by the previous consumption
        value_fn=lambda data: (data[ESTIMATED_BILL_DICT_NAME][TOTAL_EST_BILL_ATTR_NAME])
        if data[ESTIMATED_BILL_DICT_NAME]
        else 0,
        custom_attrs_fn=lambda data: {
            EST_BILL_DAYS_ATTR_NAME: data[ESTIMATED_BILL_DICT_NAME][
                EST_BILL_DAYS_ATTR_NAME
            ],
            EST_BILL_CONSUMPTION_PRICE_ATTR_NAME: data[ESTIMATED_BILL_DICT_NAME][
                EST_BILL_CONSUMPTION_PRICE_ATTR_NAME
            ],
            EST_BILL_DELIVERY_PRICE_ATTR_NAME: data[ESTIMATED_BILL_DICT_NAME][
                EST_BILL_DELIVERY_PRICE_ATTR_NAME
            ],
            EST_BILL_DISTRIBUTION_PRICE_ATTR_NAME: data[ESTIMATED_BILL_DICT_NAME][
                EST_BILL_DISTRIBUTION_PRICE_ATTR_NAME
            ],
            EST_BILL_TOTAL_KVA_PRICE_ATTR_NAME: data[ESTIMATED_BILL_DICT_NAME][
                EST_BILL_TOTAL_KVA_PRICE_ATTR_NAME
            ],
        }
        if data[ESTIMATED_BILL_DICT_NAME]
        else None,
    ),
    IecMeterEntityDescription(
        key="elec_today_consumption",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        # state_class=SensorStateClass.TOTAL,
        suggested_display_precision=3,
        value_fn=lambda data: _get_reading_by_date(
            data[DAILY_READINGS_DICT_NAME][
                data[ATTRIBUTES_DICT_NAME][METER_ID_ATTR_NAME]
            ],
            TIMEZONE.localize(datetime.now()),
        ).value
        if (
            data[DAILY_READINGS_DICT_NAME]
            and [data[ATTRIBUTES_DICT_NAME][METER_ID_ATTR_NAME]]
        )
        else None,
    ),
    IecMeterEntityDescription(
        key="elec_yesterday_consumption",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        # state_class=SensorStateClass.TOTAL,
        suggested_display_precision=3,
        value_fn=lambda data: (
            _get_reading_by_date(
                data[DAILY_READINGS_DICT_NAME][
                    data[ATTRIBUTES_DICT_NAME][METER_ID_ATTR_NAME]
                ],
                TIMEZONE.localize(datetime.now()) - timedelta(days=1),
            ).value
        )
        if (data[DAILY_READINGS_DICT_NAME])
        else None,
    ),
    IecMeterEntityDescription(
        key="elec_this_month_consumption",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        # state_class=SensorStateClass.TOTAL,
        suggested_display_precision=3,
        value_fn=lambda data: (
            sum(
                [
                    reading.value
                    for reading in data[DAILY_READINGS_DICT_NAME][
                        data[ATTRIBUTES_DICT_NAME][METER_ID_ATTR_NAME]
                    ]
                    if reading.date.month == TIMEZONE.localize(datetime.now()).month
                ]
            )
        )
        if (data[DAILY_READINGS_DICT_NAME])
        else None,
    ),
    IecMeterEntityDescription(
        key="elec_latest_meter_reading",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=3,
        value_fn=lambda data: (
            data[FUTURE_CONSUMPTIONS_DICT_NAME][
                data[ATTRIBUTES_DICT_NAME][METER_ID_ATTR_NAME]
            ].total_import
            or 0
        )
        if (data[FUTURE_CONSUMPTIONS_DICT_NAME])
        else None,
    ),
)

ELEC_SENSORS: tuple[IecEntityDescription, ...] = (
    IecContractEntityDescription(
        key="iec_last_elec_usage",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=0,
        value_fn=lambda data: data[INVOICE_DICT_NAME].consumption
        if (data[INVOICE_DICT_NAME] != EMPTY_INVOICE)
        else None,
    ),
    IecContractEntityDescription(
        key="iec_last_cost",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=ILS,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=2,
        value_fn=lambda data: data[INVOICE_DICT_NAME].amount_origin
        if (data[INVOICE_DICT_NAME] != EMPTY_INVOICE)
        else None,
    ),
    IecContractEntityDescription(
        key="iec_last_bill_remain_to_pay",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=ILS,
        suggested_display_precision=2,
        value_fn=lambda data: data[INVOICE_DICT_NAME].amount_to_pay
        if (data[INVOICE_DICT_NAME] != EMPTY_INVOICE)
        else None,
    ),
    IecContractEntityDescription(
        key="iec_last_number_of_days",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.DAYS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=lambda data: data[INVOICE_DICT_NAME].days_period
        if (data[INVOICE_DICT_NAME] != EMPTY_INVOICE)
        else None,
    ),
    IecContractEntityDescription(
        key="iec_bill_date",
        device_class=SensorDeviceClass.DATE,
        value_fn=lambda data: data[INVOICE_DICT_NAME].to_date.date()
        if (data[INVOICE_DICT_NAME] != EMPTY_INVOICE)
        else None,
    ),
    IecContractEntityDescription(
        key="iec_bill_last_payment_date",
        device_class=SensorDeviceClass.DATE,
        value_fn=lambda data: data[INVOICE_DICT_NAME].last_date
        if (data[INVOICE_DICT_NAME] != EMPTY_INVOICE)
        else None,
    ),
    IecContractEntityDescription(
        key="iec_last_meter_reading",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=0,
        value_fn=lambda data: data[INVOICE_DICT_NAME].meter_readings[0].reading
        if (
            data[INVOICE_DICT_NAME] != EMPTY_INVOICE
            and data[INVOICE_DICT_NAME].meter_readings
        )
        else None,
    ),
)

STATIC_SENSORS: tuple[IecEntityDescription, ...] = (
    IecEntityDescription(
        key="iec_kwh_tariff",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=ILS_PER_KWH,
        suggested_display_precision=4,
        value_fn=lambda data: data[STATIC_KWH_TARIFF],
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the IEC sensor."""

    coordinator: IecApiCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SensorEntity] = []

    is_multi_contract = (
        len(
            list(
                filter(
                    lambda key: key != STATICS_DICT_NAME, list(coordinator.data.keys())
                )
            )
        )
        > 1
    )

    for contract_key in coordinator.data:
        if contract_key == STATICS_DICT_NAME:
            for sensor_desc in STATIC_SENSORS:
                entities.append(
                    IecSensor(
                        coordinator,
                        sensor_desc,
                        STATICS_DICT_NAME,
                        is_multi_contract=False,
                    )
                )
        elif contract_key == JWT_DICT_NAME:
            for sensor_desc in DIAGNOSTICS_SENSORS:
                entities.append(
                    IecSensor(
                        coordinator,
                        sensor_desc,
                        JWT_DICT_NAME,
                        is_multi_contract=False,
                    )
                )
        else:
            if coordinator.data[contract_key][CONTRACT_DICT_NAME].smart_meter:
                sensors_desc: tuple[IecEntityDescription, ...] = (
                    ELEC_SENSORS + SMART_ELEC_SENSORS
                )
            else:
                sensors_desc: tuple[IecEntityDescription, ...] = ELEC_SENSORS
            # sensors_desc: tuple[IecEntityDescription, ...] = ELEC_SENSORS

            contract_id = coordinator.data[contract_key][CONTRACT_DICT_NAME].contract_id
            for sensor_desc in sensors_desc:
                entities.append(
                    IecSensor(
                        coordinator,
                        sensor_desc,
                        contract_id,
                        is_multi_contract,
                        coordinator.data[contract_key][ATTRIBUTES_DICT_NAME],
                    )
                )

    async_add_entities(entities)


class IecSensor(IecEntity, SensorEntity):
    """Representation of an IEC sensor."""

    entity_description: IecEntityDescription

    def __init__(
        self,
        coordinator: IecApiCoordinator,
        description: IecEntityDescription,
        contract_id: str,
        is_multi_contract: bool,
        attributes_to_add: dict | None = None,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(
            coordinator,
            contract_id,
            attributes_to_add.get(METER_ID_ATTR_NAME) if attributes_to_add else None,
            _get_iec_type_by_class(description),
        )
        self.entity_description = description
        self._attr_unique_id = f"{str(contract_id)}_{description.key}"
        self._attr_translation_key = f"{description.key}"
        self._attr_translation_placeholders = {"multi_contract": f"of {contract_id}"}

        attributes = {"contract_id": contract_id}

        if attributes_to_add:
            attributes.update(attributes_to_add)

        if self.entity_description.custom_attrs_fn:
            custom_attr = self.entity_description.custom_attrs_fn(
                self.coordinator.data.get(str(int(self.contract_id)))
            )
            if custom_attr:
                attributes.update(custom_attr)

        if is_multi_contract:
            attributes["is_multi_contract"] = is_multi_contract
            self._attr_translation_placeholders = {
                "multi_contract": f" of {contract_id}"
            }
        else:
            self._attr_translation_placeholders = {"multi_contract": ""}

        self._attr_extra_state_attributes = attributes

    @property
    def native_value(self) -> StateType:
        """Return the state."""
        if self.coordinator.data is not None:
            if self.contract_id in (STATICS_DICT_NAME, JWT_DICT_NAME):
                return self.entity_description.value_fn(
                    self.coordinator.data.get(self.contract_id, self.meter_id)
                )

            # Trim leading 0000 if needed and align with coordinator keys
            return self.entity_description.value_fn(
                self.coordinator.data.get(str(int(self.contract_id)))
            )
        return None
