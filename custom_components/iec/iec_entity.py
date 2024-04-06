"""Support for IEC base entities."""

from homeassistant.helpers.update_coordinator import CoordinatorEntity

from custom_components.iec import IecApiCoordinator
from custom_components.iec.commons import get_device_info


class IecEntity(CoordinatorEntity[IecApiCoordinator]):
    """Class describing IEC base-class entities."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: IecApiCoordinator, contract_id: str, meter_id: str | None) -> None:
        """Set up a IEC entity."""
        super().__init__(coordinator)
        self.contract_id = contract_id
        self.meter_id = meter_id
        self._attr_device_info = get_device_info(self.contract_id, self.meter_id)
