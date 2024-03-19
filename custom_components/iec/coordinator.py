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
from iec_api.models.contract import Contract
from iec_api.models.device import Device
from iec_api.models.exceptions import IECError
from iec_api.models.jwt import JWT
from iec_api.models.remote_reading import ReadingResolution, RemoteReading, FutureConsumptionInfo, RemoteReadingResponse

from .commons import find_reading_by_date
from .const import DOMAIN, CONF_USER_ID, STATICS_DICT_NAME, STATIC_KWH_TARIFF, INVOICE_DICT_NAME, \
    FUTURE_CONSUMPTIONS_DICT_NAME, DAILY_READINGS_DICT_NAME, STATIC_BP_NUMBER, ILS, CONF_BP_NUMBER, \
    CONF_SELECTED_CONTRACTS, CONTRACT_DICT_NAME, EMPTY_INVOICE, ELECTRIC_INVOICE_DOC_ID

_LOGGER = logging.getLogger(__name__)
TIMEZONE = pytz.timezone("Asia/Jerusalem")


class IecApiCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
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
        self._bp_number = config_entry.data.get(CONF_BP_NUMBER)
        self._contract_ids = config_entry.data.get(CONF_SELECTED_CONTRACTS)
        self._entry_data = config_entry.data
        self._today_readings = {}
        self._devices_by_contract_id = {}
        self._kwh_tariff: float | None = None
        self._readings = {}
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

    async def _get_devices_by_contract_id(self, contract_id) -> list[Device]:
        devices = self._devices_by_contract_id.get(contract_id)
        if not devices:
            try:
                devices = await self.api.get_devices(str(contract_id))
                self._devices_by_contract_id[contract_id] = devices
            except IECError as e:
                _LOGGER.exception(f"Failed fetching devices by contract {contract_id}", e)
        return devices

    async def _get_kwh_tariff(self) -> float:
        if not self._kwh_tariff:
            try:
                self._kwh_tariff = await self.api.get_kwh_tariff() / 100
            except IECError as e:
                _LOGGER.exception(f"Failed fetching kWh Tariff", e)
        return self._kwh_tariff or 0.0

    async def _get_readings(self, contract_id: int, device_id: str | int, device_code: str | int, date: datetime,
                            resolution: ReadingResolution):
        key = (contract_id, int(device_id), int(device_code), date, resolution)
        reading = self._readings.get(key)
        if not reading:
            try:
                reading = await self.api.get_remote_reading(device_id, int(device_code),
                                                            date,
                                                            date,
                                                            resolution,
                                                            str(contract_id))
                self._readings[key] = reading
            except IECError as e:
                _LOGGER.exception(f"Failed fetching reading for Contract: {contract_id},"
                                  f"date: {date.strftime('%d-%m-%Y')}, "
                                  f"resolution: {resolution}", e)
        return reading

    async def _verify_daily_readings_exist(self, daily_readings: list[RemoteReading], desired_date: datetime,
                                           device: Device,
                                           contract_id: int,
                                           prefetched_reading: RemoteReadingResponse | None = None):
        desired_date = desired_date.replace(hour=0, minute=0, second=0, microsecond=0)
        daily_reading = next(filter(lambda x: find_reading_by_date(x, desired_date), daily_readings), None)
        if not daily_reading:
            _LOGGER.debug(
                f'Daily reading for date: {desired_date.strftime("%Y-%m-%d")} is missing, calculating manually')
            hourly_readings = prefetched_reading
            if not hourly_readings:
                hourly_readings = await self._get_readings(contract_id, device.device_number, device.device_code,
                                                           desired_date,
                                                           ReadingResolution.DAILY)

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

    async def _async_update_data(
            self,
    ) -> dict[str, dict[str, Any]]:
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

        all_contracts: list[Contract] = await self.api.get_contracts(self._bp_number)
        if not self._contract_ids:
            self._contract_ids = [int(contract.contract_id) for contract in all_contracts if contract.status == 1]

        contracts: dict[int, Contract] = {int(c.contract_id): c for c in all_contracts if c.status == 1
                                          and int(c.contract_id) in self._contract_ids}

        tariff = await self._get_kwh_tariff()

        data = {STATICS_DICT_NAME: {
            STATIC_KWH_TARIFF: tariff,
            STATIC_BP_NUMBER: self._bp_number
        }}

        _LOGGER.debug(f"All Contract Ids: {list(contracts.keys())}")

        for contract_id in self._contract_ids:
            # Because IEC API provides historical usage/cost with a delay of a couple of days
            # we need to insert data into statistics.
            _LOGGER.debug(f"Processing {contract_id}")
            await self._insert_statistics(contract_id, contracts.get(contract_id).smart_meter)

            try:
                billing_invoices = await self.api.get_billing_invoices(self._bp_number, contract_id)
            except IECError as e:
                _LOGGER.exception("Failed fetching invoices", e)
                billing_invoices = None

            if billing_invoices and billing_invoices.invoices and len(billing_invoices.invoices) > 0:
                billing_invoices.invoices = list(
                    filter(lambda inv: inv.document_id == ELECTRIC_INVOICE_DOC_ID, billing_invoices.invoices))
                billing_invoices.invoices.sort(key=lambda inv: inv.full_date, reverse=True)
                last_invoice = billing_invoices.invoices[0]
            else:
                last_invoice = EMPTY_INVOICE

            future_consumption: FutureConsumptionInfo | None = None
            daily_readings: list[RemoteReading] | None = None

            if contracts.get(contract_id).smart_meter:
                # For some reason, there are differences between sending 2024-03-01 and sending 2024-03-07 (Today)
                # So instead of sending the 1st day of the month, just sending today date

                monthly_report_req_date: datetime = TIMEZONE.localize(datetime.today().replace(hour=1, minute=0,
                                                                                               second=0, microsecond=0))

                devices = await self._get_devices_by_contract_id(contract_id)

                for device in devices:
                    remote_reading = await self._get_readings(contract_id, device.device_number, device.device_code,
                                                              monthly_report_req_date,
                                                              ReadingResolution.MONTHLY)
                    if remote_reading:
                        future_consumption = remote_reading.future_consumption_info
                        daily_readings = remote_reading.data

                    weekly_future_consumption = None
                    if datetime.today().day == 1:
                        # if today's the 1st of the month, "yesterday" is on a different month
                        yesterday: datetime = monthly_report_req_date - timedelta(days=1)
                        remote_reading = await self._get_readings(contract_id, device.device_number, device.device_code,
                                                                  yesterday,
                                                                  ReadingResolution.WEEKLY)
                        if remote_reading:
                            daily_readings += remote_reading.data
                            weekly_future_consumption = remote_reading.future_consumption_info

                            # Remove duplicates
                            daily_readings = list(dict.fromkeys(daily_readings))

                            # Sort by Date
                            daily_readings.sort(key=lambda x: x.date)

                    await self._verify_daily_readings_exist(daily_readings, datetime.today() - timedelta(days=1),
                                                            device, contract_id)

                    today_reading = self._today_readings.get(contract_id)

                    if not today_reading:
                        today_reading = await self._get_readings(contract_id, device.device_number, device.device_code,
                                                                 datetime.today(),
                                                                 ReadingResolution.DAILY)
                        self._today_readings[contract_id] = today_reading

                    await self._verify_daily_readings_exist(daily_readings, datetime.today(), device, contract_id,
                                                            today_reading)

                    # fallbacks for future consumption since IEC api is broken :/
                    if not future_consumption.future_consumption:
                        if weekly_future_consumption and weekly_future_consumption.future_consumption:
                            future_consumption = weekly_future_consumption
                        elif (self._today_readings.get(contract_id)
                              and self._today_readings.get(contract_id).future_consumption_info.future_consumption):
                            future_consumption = self._today_readings.get(contract_id).future_consumption_info
                        else:
                            req_date = datetime.today() - timedelta(days=2)
                            two_days_ago_reading = await self._get_readings(contract_id, device.device_number,
                                                                            device.device_code,
                                                                            req_date,
                                                                            ReadingResolution.DAILY)

                            if two_days_ago_reading:
                                future_consumption = two_days_ago_reading.future_consumption_info
                            else:
                                _LOGGER.debug("Failed fetching FutureConsumption, data in IEC API is corrupted")

            data[str(contract_id)] = {CONTRACT_DICT_NAME: contracts.get(contract_id),
                                      INVOICE_DICT_NAME: last_invoice,
                                      FUTURE_CONSUMPTIONS_DICT_NAME: future_consumption,
                                      DAILY_READINGS_DICT_NAME: daily_readings,
                                      STATICS_DICT_NAME: {STATIC_KWH_TARIFF: tariff}  # workaround
                                      }

        # Clean up for next cycle
        self._today_readings = {}
        self._devices_by_contract_id = {}
        self._kwh_tariff = None
        self._readings = {}

        _LOGGER.debug(f"Data Keys: {list(data.keys())}")
        return data

    async def _insert_statistics(self, contract_id: int, is_smart_meter: bool) -> None:
        if not is_smart_meter:
            _LOGGER.info(f"IEC Contract {contract_id} doesn't contain Smart Meters, not adding statistics")
            # Support only smart meters at the moment
            return

        _LOGGER.debug(f"Updating statistics for IEC Contract {contract_id}")
        devices = await self._get_devices_by_contract_id(contract_id)
        kwh_price = await self._get_kwh_tariff()

        for device in devices:
            id_prefix = f"iec_meter_{device.device_number}"
            consumption_statistic_id = f"{DOMAIN}:{id_prefix}_energy_consumption"
            cost_statistic_id = f"{DOMAIN}:{id_prefix}_energy_est_cost"

            last_stat = await get_instance(self.hass).async_add_executor_job(
                get_last_statistics, self.hass, 1, consumption_statistic_id, True, set()
            )

            if not last_stat:
                month_ago_time = (datetime.now() - timedelta(weeks=4))

                _LOGGER.debug("Updating statistic for the first time")
                _LOGGER.debug(f"Fetching consumption from {month_ago_time.strftime('%Y-%m-%d %H:%M:%S')}")
                last_stat_time = 0
                readings = await self._get_readings(contract_id, device.device_number, device.device_code,
                                                    month_ago_time,
                                                    ReadingResolution.DAILY)

            else:
                last_stat_time = last_stat[consumption_statistic_id][0]["start"]
                # API returns daily data, so need to increase the start date by 4 hrs to get the next day
                from_date = datetime.fromtimestamp(last_stat_time)
                _LOGGER.debug(f"Last statistics are from {from_date.strftime('%Y-%m-%d %H:%M:%S')}")

                if from_date.hour == 23:
                    from_date = from_date + timedelta(hours=2)

                _LOGGER.debug(f"Calculated from_date = {from_date.strftime('%Y-%m-%d %H:%M:%S')}")
                today = datetime.today()
                if today.date() == from_date.date():
                    _LOGGER.debug("The date to fetch is today or later, replacing it with Today at 01:00:00")
                    from_date = TIMEZONE.localize(today.replace(hour=1, minute=0, second=0, microsecond=0))

                _LOGGER.debug(f"Fetching consumption from {from_date.strftime('%Y-%m-%d %H:%M:%S')}")
                readings = await self._get_readings(contract_id, device.device_number, device.device_code,
                                                    from_date,
                                                    ReadingResolution.DAILY)
                if from_date.date() == today.date():
                    self._today_readings[contract_id] = readings

            if not readings or not readings.data:
                _LOGGER.debug("No recent usage data. Skipping update")
                continue

            stats = await get_instance(self.hass).async_add_executor_job(
                statistics_during_period,
                self.hass,
                readings.data[0].date - timedelta(hours=1),
                None,
                {cost_statistic_id, consumption_statistic_id},
                "hour",
                None,
                {"sum"},
            )

            if not stats.get(consumption_statistic_id):
                _LOGGER.debug("No recent usage data")
                consumption_sum = 0
            else:
                consumption_sum = cast(float, stats[consumption_statistic_id][0]["sum"])

            if not stats.get(cost_statistic_id):
                if not stats.get(consumption_statistic_id):
                    _LOGGER.debug("No recent cost data")
                    cost_sum = 0.0
                else:
                    cost_sum = cast(float, stats[consumption_statistic_id][0]["sum"]) * kwh_price
            else:
                cost_sum = cast(float, stats[cost_statistic_id][0]["sum"])

            _LOGGER.debug(f"Last Consumption Sum for {contract_id}: {consumption_sum}")
            _LOGGER.debug(f"Last Estimated Cost Sum for {contract_id}: {cost_sum}")

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

            cost_metadata = StatisticMetaData(
                has_mean=False,
                has_sum=True,
                name=f"IEC Meter {device.device_number} Estimated Cost",
                source=DOMAIN,
                statistic_id=cost_statistic_id,
                unit_of_measurement=ILS
            )

            consumption_statistics = []
            cost_statistics = []
            for key, value in sorted(readings_by_hour.items()):
                consumption_sum += value
                cost_sum += value * kwh_price

                consumption_statistics.append(
                    StatisticData(
                        start=key,
                        sum=consumption_sum,
                        state=value
                    )
                )

                cost_statistics.append(
                    StatisticData(
                        start=key,
                        sum=cost_sum,
                        state=value * kwh_price
                    )
                )

            if readings_by_hour:
                _LOGGER.debug(f"Last hour fetched for {contract_id}: {max(readings_by_hour, key=lambda k: k)}")
                _LOGGER.debug(f"New Consumption Sum for {contract_id}: {consumption_sum}")
                _LOGGER.debug(f"New Estimated Cost Sum for {contract_id}: {cost_sum}")

            async_add_external_statistics(
                self.hass, consumption_metadata, consumption_statistics
            )

            async_add_external_statistics(
                self.hass, cost_metadata, cost_statistics
            )
