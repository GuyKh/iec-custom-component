"""Support for IEC Binary sensors."""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import BinarySensorEntityDescription, BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, STATICS_DICT_NAME, INVOICE_DICT_NAME, \
    EMPTY_INVOICE
from .coordinator import IecApiCoordinator

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class IecBinaryEntityDescriptionMixin:
    """Mixin values for required keys."""

    value_fn: Callable[dict, bool | None]


@dataclass(frozen=True, kw_only=True)
class IecBinarySensorEntityDescription(BinarySensorEntityDescription, IecBinaryEntityDescriptionMixin):
    """Class describing IEC sensors entities."""


BINARY_SENSORS: tuple[IecBinarySensorEntityDescription, ...] = (
    IecBinarySensorEntityDescription(
        key="last_invoice_paid",
        translation_key="last_invoice_paid",
        value_fn=lambda data: (data[INVOICE_DICT_NAME].amount_to_pay == 0) if (
                data[INVOICE_DICT_NAME] != EMPTY_INVOICE) else None,
    ),
)


async def async_setup_entry(
        hass: HomeAssistant,
        entry: ConfigEntry,
        async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up a IEC binary sensors based on a config entry."""
    coordinator = hass.data[DOMAIN][entry.entry_id]

    is_multi_contract = len(list(filter(lambda key: key != STATICS_DICT_NAME, list(coordinator.data.keys())))) > 1

    entities: list[BinarySensorEntity] = []
    for contract_key in coordinator.data:
        if contract_key == STATICS_DICT_NAME:
            continue

        for description in BINARY_SENSORS:
            entities.append(
                IecBinarySensorEntity(coordinator=coordinator,
                                      entity_description=description,
                                      contract_id=contract_key,
                                      is_multi_contract=is_multi_contract)
            )

    async_add_entities(entities)


class IecBinarySensorEntity(CoordinatorEntity[IecApiCoordinator], BinarySensorEntity):
    """Defines an IEC binary sensor."""
    _attr_has_entity_name = True

    coordinator: IecApiCoordinator
    entity_description: IecBinarySensorEntityDescription
    contract_id: str

    def __init__(
            self,
            coordinator: IecApiCoordinator,
            entity_description: IecBinarySensorEntityDescription,

            contract_id: str,
            is_multi_contract: bool
    ):
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.entity_description = entity_description
        self.contract_id = contract_id
        self._attr_unique_id = f"{str(contract_id)}_{entity_description.key}"


        attributes = {
            "contract_id": contract_id
        }

        if is_multi_contract:
            attributes["is_multi_contract"] = is_multi_contract
            self._attr_translation_placeholders = {"multi_contract": f" of {contract_id}"}
        else:
            self._attr_translation_placeholders = {"multi_contract": ""}

        self._attr_extra_state_attributes = attributes

    @property
    def is_on(self) -> bool | None:
        """Return the state of the sensor."""
        return self.coordinator.data.get(str(int(self.contract_id)))
