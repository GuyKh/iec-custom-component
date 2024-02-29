"""Coordinator to handle IEC connections."""
import itertools
import logging
import socket
from datetime import datetime, timedelta, date
from types import MappingProxyType
from typing import Any, cast

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.const import UnitOfEnergy, CONF_API_TOKEN
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import aiohttp_client
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from iec_api.iec_client import IecClient
from iec_api.models.exceptions import IECError
from iec_api.models.invoice import Invoice
from iec_api.models.remote_reading import ReadingResolution, RemoteReading

from .const import DOMAIN, CONF_USER_ID

_LOGGER = logging.getLogger(__name__)


class IecApiCoordinator(DataUpdateCoordinator[dict[int, Invoice]]):
    """Handle fetching IEC data, updating sensors and inserting statistics."""

    def __init__(
            self,
            hass: HomeAssistant,
            entry_data: MappingProxyType[str, Any],
    ) -> None:
        """Initialize the data handler."""
        super().__init__(
            hass,
            _LOGGER,
            name="Iec",
            # Data is updated daily on IEC.
            # Refresh every 4h to be at most 4h behind.
            update_interval=timedelta(hours=4),
        )
        self.bp_number = None
        self.contract_id = None
        self.api = IecClient(
            entry_data[CONF_USER_ID],
            session=aiohttp_client.async_get_clientsession(hass, family=socket.AF_INET)
        )

        self.api.load_jwt_token(entry_data[CONF_API_TOKEN])
        self.entry_data = entry_data

        @callback
        def _dummy_listener() -> None:
            pass

        # Force the coordinator to periodically update by registering at least one listener.
        # Needed when the _async_update_data below returns {} for utilities that don't provide
        # forecast, which results to no sensors added, no registered listeners, and thus
        # _async_update_data not periodically getting called which is needed for _insert_statistics.
        self.async_add_listener(_dummy_listener)

    async def _async_update_data(
            self,
    ) -> dict[int, Invoice]:
        """Fetch data from API endpoint."""
        try:
            # First thing first, check the token and refresh if needed.
            await self.api.check_token()
        except IECError as err:
            raise ConfigEntryAuthFailed from err

        if not self.bp_number:
            customer = await self.api.get_customer()
            self.bp_number = customer.bp_number

        if not self.contract_id:
            contract = await self.api.get_default_contract(self.bp_number)
            self.is_smart_meter = contract.smart_meter
            self.contract_id = contract.contract_id

        # Because IEC API provides historical usage/cost with a delay of a couple of days
        # we need to insert data into statistics.
        await self._insert_statistics()
        billing_invoices = await self.api.get_billing_invoices(self.bp_number, self.contract_id)
        billing_invoices.invoices.sort(key=lambda invoice: date.fromisoformat(invoice.full_date), reverse=True)
        invoice = billing_invoices.invoices[0]
        return {invoice.contract_number: invoice}

    async def _insert_statistics(self) -> None:
        if not self.is_smart_meter:
            # Support only smart meters at the moment
            return

        devices = await self.api.get_devices(self.contract_id)
        day_ago_time_str = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
        month_ago_time_str = (datetime.now() - timedelta(weeks=4)).strftime('%Y-%m-%d')

        for device in devices:
            id_prefix = f"meter_{device.device_number}"
            consumption_statistic_id = f"{DOMAIN}:{id_prefix}_energy_consumption"

            last_stat = await get_instance(self.hass).async_add_executor_job(
                get_last_statistics, self.hass, 1, consumption_statistic_id, True, set()
            )

            consumption_sum = 0.0
            last_stats_time = None

            if not last_stat:
                _LOGGER.debug("Updating statistic for the first time")
                readings = await self.api.get_remote_reading(device.device_number, int(device.device_code),
                                                             month_ago_time_str,
                                                             month_ago_time_str, ReadingResolution.DAILY,
                                                             self.contract_id)
            else:
                last_stat_time = last_stat[consumption_statistic_id][0]["start"]
                from_date_str = datetime.fromtimestamp(last_stat_time).strftime('%Y-%m-%d')
                readings = await self.api.get_remote_reading(device.device_number, int(device.device_code),
                                                             from_date_str, from_date_str,
                                                             ReadingResolution.DAILY, self.contract_id)

            if not readings or not readings.data:
                _LOGGER.debug("No recent usage data. Skipping update")
                continue

            stats = await get_instance(self.hass).async_add_executor_job(
                statistics_during_period,
                self.hass,
                readings.data[0].date,
                None,
                {consumption_statistic_id},
                "hour",
                None,
                {"sum"},
            )
            consumption_sum = cast(float, stats[consumption_statistic_id][0]["sum"])

            new_readings: list[RemoteReading] = filter(lambda reading:
                                                       reading.date >= datetime.fromtimestamp(last_stat_time),
                                                       readings.data)

            grouped_new_readings_by_hour = itertools.groupby(new_readings,
                                                             key=lambda reading: reading.date
                                                             .replace(minute=0, second=0, microsecond=0))
            readings_by_hour: dict[datetime, float] = {k: sum(reading.value for reading in v)
                                                       for k, v in grouped_new_readings_by_hour.items()}

            consumption_metadata = StatisticMetaData(
                has_mean=False,
                has_sum=True,
                name=f"iec meter {device.device_number} consumption",
                source=DOMAIN,
                statistic_id=consumption_statistic_id,
                unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR
            )

            consumption_statistics = []
            for key, value in readings_by_hour.items():
                consumption_sum += value
                consumption_statistics.append(
                    StatisticData(
                        start=key,
                        sum=consumption_sum,
                        state=value
                    )
                )

            async_add_external_statistics(
                self.hass, consumption_metadata, consumption_statistics
            )
