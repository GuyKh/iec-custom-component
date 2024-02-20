"""DataUpdateCoordinator for iec."""
from __future__ import annotations
from iec_api.iec_client import IecClient
from iec_api.models.exceptions import IECError, IECLoginError

from datetime import timedelta, datetime

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.exceptions import ConfigEntryAuthFailed
from iec_api.models.remote_reading import ReadingResolution

from .const import DOMAIN, LOGGER, ATTR_BP_NUMBER, ATTR_METER_NUMBER, ATTR_METER_TYPE, ATTR_METER_CODE, \
    ATTR_METER_IS_ACTIVE, ATTR_METER_READINGS


# https://developers.home-assistant.io/docs/integration_fetching_data#coordinated-single-api-poll-for-data-for-all-entities
class IecDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching data from the API."""

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        client: IecClient,
    ) -> None:
        """Initialize."""
        self.client = client
        super().__init__(
            hass=hass,
            logger=LOGGER,
            name=DOMAIN,
            update_interval=timedelta(minutes=15),
        )

    async def _async_update_data(self):
        """Update data via library."""
        try:
            customer = self.client.get_customer()

            data = {}
            devices = self.client.get_devices(customer.bp_number)
            for device in devices:
                readings_list = []
                for i in range(2, -1, -1):
                    date_str = (datetime.now() - timedelta(days=i)).strftime('%Y-%m-%d')
                    readings = self.client.get_remote_reading(device.device_number, int(device.device_code),
                                                              date_str, date_str, resolution=ReadingResolution.DAILY)
                    for reading in readings.data:
                        v = (reading.date.strftime('%Y-%m-%dT%H:%M:%S.%f'),  reading.value)
                        readings_list.append(v)
                data[device.device_number] = {
                                            ATTR_BP_NUMBER: customer.bp_number,
                                            ATTR_METER_NUMBER: device.device_number,
                                            ATTR_METER_TYPE: device.device_type,
                                            ATTR_METER_CODE: device.device_code,
                                            ATTR_METER_IS_ACTIVE: device.is_active,
                                            ATTR_METER_READINGS: readings_list
                                          }

            return data
        except IECLoginError as exception:
            raise ConfigEntryAuthFailed(exception) from exception
        except IECError as exception:
            raise UpdateFailed(exception) from exception
