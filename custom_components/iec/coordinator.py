"""Coordinator to handle IEC connections."""
import itertools
import logging
import socket
from datetime import datetime, timedelta
from typing import cast, Any  # noqa: UP035

import pytz
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy, CONF_API_TOKEN
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import aiohttp_client
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from iec_api.iec_client import IecClient
from iec_api.models.device import Device
from iec_api.models.exceptions import IECError
from iec_api.models.jwt import JWT
from iec_api.models.remote_reading import ReadingResolution, RemoteReading, FutureConsumptionInfo, RemoteReadingResponse

from .commons import find_reading_by_date
from .const import DOMAIN, CONF_USER_ID, STATICS_DICT_NAME, STATIC_KWH_TARIFF, INVOICE_DICT_NAME, \
    FUTURE_CONSUMPTIONS_DICT_NAME, DAILY_READINGS_DICT_NAME, STATIC_CONTRACT, STATIC_BP_NUMBER

_LOGGER = logging.getLogger(__name__)
TIMEZONE = pytz.timezone("Asia/Jerusalem")


async def _verify_daily_readings_exist(daily_readings: list[RemoteReading], desired_date: datetime, device: Device,
                                       contract_id: str, api: IecClient,
                                       prefetched_reading: RemoteReadingResponse | None = None):
    desired_date = desired_date.replace(hour=0, minute=0, second=0, microsecond=0)
    daily_reading = next(filter(lambda x: find_reading_by_date(x, desired_date), daily_readings), None)
    if not daily_reading:
        _LOGGER.debug(f'Daily reading for date: {desired_date.strftime("%Y-%m-%d")} is missing, calculating manually')
        hourly_readings = prefetched_reading
        if not hourly_readings:
            hourly_readings = await api.get_remote_reading(device.device_number, int(device.device_code),
                                                           desired_date, desired_date,
                                                           ReadingResolution.DAILY, contract_id)
        daily_sum = 0
        if hourly_readings is None or hourly_readings.data is None:
            _LOGGER.info(f'No readings found for date: {desired_date.strftime("%Y-%m-%d")}')
            return

        for reading in hourly_readings.data:
            daily_sum += reading.value

        daily_readings.append(RemoteReading(0, desired_date, daily_sum))
    else:
        _LOGGER.debug(f'Daily reading for date: {daily_reading.date.strftime("%Y-%m-%d")}'
                      f' is present: {daily_reading.value}')


class IecApiCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Handle fetching IEC data, updating sensors and inserting statistics."""

    def __init__(
            self,
            hass: HomeAssistant,
            config_entry: ConfigEntry,
    ) -> None:
        """Initialize the data handler."""
        super().__init__(
            hass,
            _LOGGER,
            name="Iec",
            # Data is updated daily on IEC.
            # Refresh every 1h to be at most 5h behind.
            update_interval=timedelta(hours=1),
        )
        self._config_entry = config_entry
        self._bp_number = None
        self._contract_id = None
        self._entry_data = config_entry.data
        self._today_reading = None
        self.api = IecClient(
            self._entry_data[CONF_USER_ID],
            session=aiohttp_client.async_get_clientsession(hass, family=socket.AF_INET)
        )
        self._first_load: bool = True

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
    ) -> dict[str, Any]:
        """Fetch data from API endpoint."""
        if self._first_load:
            _LOGGER.debug("Loading API token from config entry")
            await self.api.load_jwt_token(JWT.from_dict(self._entry_data[CONF_API_TOKEN]))

        self._first_load = False
        try:
            _LOGGER.debug("Checking if API token needs to be refreshed")
            # First thing first, check the token and refresh if needed.
            old_token = self.api.get_token()
            await self.api.check_token()
            new_token = self.api.get_token()
            if old_token != new_token:
                _LOGGER.debug("Token refreshed")
                new_data = {**self._entry_data, CONF_API_TOKEN: new_token.to_dict()}
                self.hass.config_entries.async_update_entry(entry=self._config_entry,
                                                            data=new_data)
        except IECError as err:
            raise ConfigEntryAuthFailed from err

        if not self._bp_number:
            customer = await self.api.get_customer()
            self._bp_number = customer.bp_number

        if not self._contract_id:
            contract = await self.api.get_default_contract(self._bp_number)
            self.is_smart_meter = contract.smart_meter
            self._contract_id = contract.contract_id

        # Because IEC API provides historical usage/cost with a delay of a couple of days
        # we need to insert data into statistics.
        await self._insert_statistics()
        billing_invoices = await self.api.get_billing_invoices(self._bp_number, self._contract_id)
        billing_invoices.invoices.sort(key=lambda inv: inv.full_date, reverse=True)
        last_invoice = billing_invoices.invoices[0]

        future_consumption: FutureConsumptionInfo | None = None
        daily_readings: list[RemoteReading] | None = None
        today_reading: RemoteReadingResponse| None = None
        if self.is_smart_meter:
            # For some reason, there are differences between sending 2024-03-01 and sending 2024-03-07 (Today)
            # So instead of sending the 1st day of the month, just sending today date
            # monthly_report_req_date: datetime = TIMEZONE.localize(datetime.today().replace(day=1, hour=0, minute=0,
            #                                                                                second=0, microsecond=0))
            monthly_report_req_date: datetime = TIMEZONE.localize(datetime.today().replace(hour=1, minute=0,
                                                                                           second=0, microsecond=0))
            devices = await self.api.get_devices(self._contract_id)
            for device in devices:
                remote_reading = await self.api.get_remote_reading(device.device_number, int(device.device_code),
                                                                   monthly_report_req_date,
                                                                   monthly_report_req_date, ReadingResolution.MONTHLY,
                                                                   self._contract_id)
                if remote_reading:
                    future_consumption = remote_reading.future_consumption_info
                    daily_readings = remote_reading.data

                weekly_future_consumption = None
                if datetime.today().day == 1:
                    # if today's the 1st of the month, "yesterday" is on a different month
                    yesterday: datetime = monthly_report_req_date - timedelta(days=1)
                    remote_reading = await self.api.get_remote_reading(device.device_number, int(device.device_code),
                                                                       yesterday, yesterday,
                                                                       ReadingResolution.WEEKLY, self._contract_id)
                    if remote_reading:
                        daily_readings += remote_reading.data
                        weekly_future_consumption = remote_reading.future_consumption_info

                        # Remove duplicates
                        daily_readings = list(dict.fromkeys(daily_readings))

                        # Sort by Date
                        daily_readings.sort(key=lambda x: x.date)

                await _verify_daily_readings_exist(daily_readings, datetime.today() - timedelta(days=1),
                                                   device, self._contract_id, self.api)

                today_reading = self._today_reading

                if not self._today_reading:
                    today_reading = await self.api.get_remote_reading(device.device_number, int(device.device_code),
                                                                      datetime.today(), datetime.today(),
                                                                      ReadingResolution.DAILY, self._contract_id)
                    self._today_reading = today_reading

                await _verify_daily_readings_exist(daily_readings, datetime.today(), device, self._contract_id, self.api,
                                                   today_reading)

                # fallbacks for future consumption since IEC api is broken :/
                if not future_consumption.future_consumption:
                    if weekly_future_consumption and weekly_future_consumption.future_consumption:
                        future_consumption = weekly_future_consumption
                    elif self._today_reading and self._today_reading.future_consumption_info.future_consumption:
                        future_consumption = self._today_reading.future_consumption_info
                    else:
                        req_date = datetime.today() - timedelta(days=2)
                        two_days_ago_reading = await self.api.get_remote_reading(device.device_number,
                                                                                 int(device.device_code),
                                                                                 req_date, req_date,
                                                                                 ReadingResolution.DAILY,
                                                                                 self._contract_id)

                        if two_days_ago_reading:
                            future_consumption = two_days_ago_reading.future_consumption_info
                        else:
                            _LOGGER.debug("Failed fetching FutureConsumption, data in IEC API is corrupted")

        static_data = {
            STATIC_KWH_TARIFF: (await self.api.get_kwh_tariff()) / 100,
            STATIC_CONTRACT: self._contract_id,
            STATIC_BP_NUMBER: self._bp_number
        }

        data = {STATICS_DICT_NAME: static_data, INVOICE_DICT_NAME: last_invoice,
                FUTURE_CONSUMPTIONS_DICT_NAME: future_consumption,
                DAILY_READINGS_DICT_NAME: daily_readings}

        # Clean today reading for next reading cycle
        self._today_reading = None
        return data

    async def _insert_statistics(self) -> None:
        if not self.is_smart_meter:
            _LOGGER.info("IEC Contract doesn't contain Smart Meters, not adding statistics")
            # Support only smart meters at the moment
            return

        _LOGGER.debug(f"Updating statistics for IEC Contract {self._contract_id}")
        devices = await self.api.get_devices(self._contract_id)
        month_ago_time = (datetime.now() - timedelta(weeks=4))

        for device in devices:
            id_prefix = f"iec_meter_{device.device_number}"
            consumption_statistic_id = f"{DOMAIN}:{id_prefix}_energy_consumption"

            last_stat = await get_instance(self.hass).async_add_executor_job(
                get_last_statistics, self.hass, 1, consumption_statistic_id, True, set()
            )

            if not last_stat:
                _LOGGER.debug("Updating statistic for the first time")
                _LOGGER.debug(f"Fetching consumption from {month_ago_time.strftime('%Y-%m-%d %H:%M:%S')}")
                last_stat_time = 0
                readings = await self.api.get_remote_reading(device.device_number, int(device.device_code),
                                                             month_ago_time,
                                                             month_ago_time, ReadingResolution.DAILY,
                                                             self._contract_id)
            else:
                last_stat_time = last_stat[consumption_statistic_id][0]["start"]
                # API returns daily data, so need to increase the start date by 4 hrs to get the next day
                from_date = datetime.fromtimestamp(last_stat_time)
                _LOGGER.debug(f"Last statistics are from {from_date.strftime('%Y-%m-%d %H:%M:%S')}")

                if from_date.hour == 23:
                    from_date = from_date + timedelta(hours=2)

                _LOGGER.debug(f"Calculated from_date = {from_date.strftime('%Y-%m-%d %H:%M:%S')}")
                if (datetime.today() - from_date).days <= 0:
                    _LOGGER.debug("The date to fetch is today or later, replacing it with Today at 01:00:00")
                    from_date = TIMEZONE.localize(datetime.today().replace(hour=1, minute=0, second=0, microsecond=0))

                _LOGGER.debug(f"Fetching consumption from {from_date.strftime('%Y-%m-%d %H:%M:%S')}")
                readings = await self.api.get_remote_reading(device.device_number, int(device.device_code),
                                                             from_date, from_date,
                                                             ReadingResolution.DAILY, self._contract_id)
                if from_date.date() == datetime.today().date():
                    self._today_reading = readings

            if not readings or not readings.data:
                _LOGGER.debug("No recent usage data. Skipping update")
                continue

            stats = await get_instance(self.hass).async_add_executor_job(
                statistics_during_period,
                self.hass,
                readings.data[0].date - timedelta(hours=1),
                None,
                {consumption_statistic_id},
                "hour",
                None,
                {"sum"},
            )

            if not stats.get(consumption_statistic_id):
                _LOGGER.debug("No recent usage data")
                consumption_sum = 0
            else:
                consumption_sum = cast(float, stats[consumption_statistic_id][0]["sum"])

            new_readings: list[RemoteReading] = filter(lambda reading:
                                                       reading.date >= TIMEZONE.localize(
                                                           datetime.fromtimestamp(last_stat_time)),
                                                       readings.data)

            grouped_new_readings_by_hour = itertools.groupby(new_readings,
                                                             key=lambda reading: reading.date
                                                             .replace(minute=0, second=0, microsecond=0))
            readings_by_hour: dict[datetime, float] = {key: sum(reading.value for reading in list(group))
                                                       for key, group in grouped_new_readings_by_hour}

            consumption_metadata = StatisticMetaData(
                has_mean=False,
                has_sum=True,
                name=f"IEC Meter {device.device_number} Consumption",
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
