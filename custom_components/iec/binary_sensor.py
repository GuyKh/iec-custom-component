"""Support for IEC Binary sensors."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorEntityDescription,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .commons import get_device_info, IecEntityType
from .const import (
    DOMAIN,
    STATICS_DICT_NAME,
    INVOICE_DICT_NAME,
    JWT_DICT_NAME,
    EMPTY_INVOICE,
    ATTRIBUTES_DICT_NAME,
    METER_ID_ATTR_NAME,
)
from .coordinator import IecApiCoordinator
from .iec_entity import IecEntity

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class IecBinaryEntityDescriptionMixin:
    """Mixin values for required keys."""

    value_fn: Callable[dict, bool | None]


@dataclass(frozen=True, kw_only=True)
class IecBinarySensorEntityDescription(
    BinarySensorEntityDescription, IecBinaryEntityDescriptionMixin
):
    """Class describing IEC sensors entities."""


BINARY_SENSORS: tuple[IecBinarySensorEntityDescription, ...] = (
    IecBinarySensorEntityDescription(
        key="last_iec_invoice_paid",
        translation_key="last_iec_invoice_paid",
        value_fn=lambda data: (data[INVOICE_DICT_NAME].amount_to_pay == 0)
        if (data[INVOICE_DICT_NAME] != EMPTY_INVOICE)
        else None,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up a IEC binary sensors based on a config entry."""
    coordinator = hass.data[DOMAIN][entry.entry_id]

    is_multi_contract = (
        len(
            list(
                filter(
                    lambda key: key not in (STATICS_DICT_NAME, JWT_DICT_NAME),
                    list(coordinator.data.keys()),
                )
            )
        )
        > 1
    )

    entities: list[BinarySensorEntity] = []
    for contract_key in coordinator.data:
        if contract_key in (STATICS_DICT_NAME, JWT_DICT_NAME):
            continue

        for description in BINARY_SENSORS:
            entities.append(
                IecBinarySensorEntity(
                    coordinator=coordinator,
                    entity_description=description,
                    contract_id=contract_key,
                    is_multi_contract=is_multi_contract,
                    attributes_to_add=coordinator.data[contract_key][
                        ATTRIBUTES_DICT_NAME
                    ],
                )
            )

    async_add_entities(entities)


class IecBinarySensorEntity(IecEntity, BinarySensorEntity):
    """Defines an IEC binary sensor."""

    entity_description: IecBinarySensorEntityDescription

    def __init__(
        self,
        coordinator: IecApiCoordinator,
        entity_description: IecBinarySensorEntityDescription,
        contract_id: str,
        is_multi_contract: bool,
        attributes_to_add: dict | None = None,
    ):
        """Initialize the sensor."""
        super().__init__(
            coordinator,
            str(int(contract_id)),
            attributes_to_add.get(METER_ID_ATTR_NAME) if attributes_to_add else None,
            IecEntityType.CONTRACT,
        )
        self.entity_description = entity_description
        self._attr_unique_id = f"{str(contract_id)}_{entity_description.key}"

        attributes = {"contract_id": contract_id}

        if attributes_to_add:
            attributes.update(attributes_to_add)

        if is_multi_contract:
            attributes["is_multi_contract"] = is_multi_contract
            self._attr_translation_placeholders = {
                "multi_contract": f" of {contract_id}"
            }
        else:
            self._attr_translation_placeholders = {"multi_contract": ""}

        self._attr_extra_state_attributes = attributes

    @property
    def is_on(self) -> bool | None:
        """Return the state of the sensor."""
        return self.entity_description.value_fn(
            self.coordinator.data.get(self.contract_id)
        )

    @property
    def device_info(self) -> DeviceInfo:
        """Return the device info."""
        return get_device_info(self.contract_id, None, IecEntityType.CONTRACT)
