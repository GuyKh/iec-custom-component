"""Coordinator to handle IEC connections."""

import asyncio
import calendar
import itertools
import logging
import socket
import traceback
from collections import Counter
from datetime import date, datetime, timedelta
from typing import Any, cast  # noqa: UP035
from uuid import UUID

import jwt
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_API_TOKEN, UnitOfEnergy
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import aiohttp_client
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from iec_api.iec_client import IecClient
from iec_api.models.contract import Contract
from iec_api.models.device import Device, Devices
from iec_api.models.exceptions import IECError
from iec_api.models.jwt import JWT
from iec_api.models.meter_reading import MeterReading
from iec_api.models.remote_reading import (
    FutureConsumptionInfo,
    ReadingResolution,
    PeriodConsumption,
    RemoteReadingResponse,
)

from .commons import find_reading_by_date, localize_datetime
from .const import (
    ACCESS_TOKEN_EXPIRATION_TIME,
    ACCESS_TOKEN_ISSUED_AT,
    ATTRIBUTES_DICT_NAME,
    CONF_BP_NUMBER,
    CONF_BP_NUMBER_TO_CONTRACT,
    CONF_SELECTED_CONTRACTS,
    CONF_USER_ID,
    CONTRACT_DICT_NAME,
    CONTRACT_ID_ATTR_NAME,
    DAILY_READINGS_DICT_NAME,
    DOMAIN,
    ELECTRIC_INVOICE_DOC_ID,
    EMPTY_INVOICE,
    EST_BILL_CONSUMPTION_PRICE_ATTR_NAME,
    EST_BILL_DAYS_ATTR_NAME,
    EST_BILL_DELIVERY_PRICE_ATTR_NAME,
    EST_BILL_DISTRIBUTION_PRICE_ATTR_NAME,
    EST_BILL_KWH_CONSUMPTION_ATTR_NAME,
    EST_BILL_TOTAL_KVA_PRICE_ATTR_NAME,
    ESTIMATED_BILL_DICT_NAME,
    FUTURE_CONSUMPTIONS_DICT_NAME,
    ILS,
    INVOICE_DICT_NAME,
    IS_SHARED_ATTR_NAME,
    IS_SMART_METER_ATTR_NAME,
    JWT_DICT_NAME,
    METER_ID_ATTR_NAME,
    STATIC_BP_NUMBER,
    STATIC_KVA_TARIFF,
    STATIC_KWH_TARIFF,
    STATICS_DICT_NAME,
    TOTAL_EST_BILL_ATTR_NAME,
)

_LOGGER = logging.getLogger(__name__)


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
            config_entry=config_entry,
            name="Iec",
            # Data is updated daily on IEC.
            # Refresh every 1h to be at most 5h behind.
            update_interval=timedelta(hours=1),
        )
        _LOGGER.debug("Initializing IEC Coordinator")
        self._config_entry = config_entry
        self._bp_number = config_entry.data.get(CONF_BP_NUMBER)
        self._contract_ids: list[int] = [
            int(contract_id)
            for contract_id in config_entry.data.get(CONF_SELECTED_CONTRACTS, [])
        ]
        self._bp_number_to_contract = self._normalize_bp_number_to_contract(
            config_entry.data.get(CONF_BP_NUMBER_TO_CONTRACT)
        )
        self._contract_to_bp_number: dict[int, str] = {}
        for bp_number, contract_ids in self._bp_number_to_contract.items():
            for contract_id in contract_ids:
                self._contract_to_bp_number[contract_id] = bp_number
        self._entry_data = config_entry.data
        self._today_readings = {}
        self._devices_by_contract_id = {}
        self._last_meter_reading = {}
        self._devices_by_meter_id = {}
        self._delivery_tariff_by_phase = {}
        self._distribution_tariff_by_phase = {}
        self._power_size_by_connection_size = {}
        self._kwh_tariff: float | None = None
        self._kva_tariff: float | None = None
        self._readings = {}
        self._default_account_id: UUID | None = None
        self._account_id_by_contract: dict[int, UUID] = {}
        self._shared_contract_ids: set[int] = set()
        self._contract_account_mapping_loaded = False
        self._connection_size_by_account_id: dict[UUID, str] = {}
        self._api_session = aiohttp_client.async_get_clientsession(
            hass, family=socket.AF_INET
        )
        self.api = IecClient(
            self._entry_data[CONF_USER_ID],
            session=self._api_session,
        )
        self._first_load: bool = True

        @callback
        def _dummy_listener() -> None:
            pass

        # Force the coordinator to periodically update by registering at least one listener.
        # Needed when the _async_update_data below returns {} for utilities that don't provide
        # forecast, which results to no sensors added, no registered listeners, and thus
        # _async_update_data not periodically getting called which is needed for _insert_statistics.
        self._dummy_listener_unsub = self.async_add_listener(_dummy_listener)

    async def async_unload(self):
        """Unload the coordinator, cancel any pending tasks."""
        if self._dummy_listener_unsub is not None:
            self._dummy_listener_unsub()
            self._dummy_listener_unsub = None
        await self.async_shutdown()
        _LOGGER.info("Coordinator unloaded successfully.")

    @staticmethod
    def _normalize_bp_number_to_contract(raw_map: Any) -> dict[str, list[int]]:
        if not isinstance(raw_map, dict):
            return {}

        normalized: dict[str, list[int]] = {}
        for bp_number, contract_ids in raw_map.items():
            if not bp_number or not isinstance(contract_ids, list):
                continue
            try:
                normalized_bp_number = str(int(str(bp_number)))
            except ValueError:
                normalized_bp_number = str(bp_number)
            normalized_contracts = sorted(
                {
                    int(contract_id)
                    for contract_id in contract_ids
                    if str(contract_id).strip()
                }
            )
            if normalized_contracts:
                normalized[normalized_bp_number] = normalized_contracts
        return normalized

    def _persist_bp_number_to_contract_mapping(self) -> None:
        mapping = {
            bp_number: sorted(contract_ids)
            for bp_number, contract_ids in self._bp_number_to_contract.items()
            if contract_ids
        }
        if not mapping:
            return

        new_data = {**self._entry_data, CONF_BP_NUMBER_TO_CONTRACT: mapping}
        all_selected_contracts_mapped = all(
            contract_id in self._contract_to_bp_number for contract_id in self._contract_ids
        )
        if all_selected_contracts_mapped:
            new_data.pop(CONF_BP_NUMBER, None)
            self._bp_number = None

        if new_data != self._entry_data:
            self.hass.config_entries.async_update_entry(
                entry=self._config_entry, data=new_data
            )
            self._entry_data = new_data

    @staticmethod
    def _normalize_bp_number(bp_number: str | None) -> str | None:
        if not bp_number:
            return None
        try:
            return str(int(bp_number))
        except ValueError:
            return bp_number

    def _set_contract_bp_mapping(self, contract_id: int, bp_number: str) -> None:
        normalized_bp_number = self._normalize_bp_number(bp_number)
        if not normalized_bp_number:
            return

        existing_contracts = set(
            self._bp_number_to_contract.get(normalized_bp_number, [])
        )
        if contract_id in existing_contracts:
            return

        existing_contracts.add(contract_id)
        self._bp_number_to_contract[normalized_bp_number] = sorted(existing_contracts)
        self._contract_to_bp_number[contract_id] = normalized_bp_number

    async def _resolve_bp_number_for_contract(self, contract_id: int) -> str | None:
        mapped_bp_number = self._contract_to_bp_number.get(contract_id)
        if mapped_bp_number:
            return mapped_bp_number

        try:
            customer_mobile = await self.api.get_customer_mobile(str(contract_id))
            if customer_mobile and customer_mobile.customer:
                mapped_bp_number = self._normalize_bp_number(
                    customer_mobile.customer.bp_number
                )
        except asyncio.CancelledError:
            return None
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "Failed resolving bp_number for contract %s via customer_mobile: %s",
                contract_id,
                err,
            )
            mapped_bp_number = None

        if not mapped_bp_number and self._bp_number:
            mapped_bp_number = self._normalize_bp_number(self._bp_number)

        if mapped_bp_number:
            self._set_contract_bp_mapping(contract_id, mapped_bp_number)
            self._persist_bp_number_to_contract_mapping()

        return mapped_bp_number

    async def _load_selected_contracts(self) -> dict[int, Contract]:
        if not self._bp_number and not self._bp_number_to_contract:
            try:
                customer = await self.api.get_customer()
                self._bp_number = customer.bp_number
            except asyncio.CancelledError:
                _LOGGER.warning(
                    "Fetching customer was cancelled; using empty BP number and skipping contracts"
                )
                self._bp_number = None
            except IECError as e:
                _LOGGER.exception("Failed fetching customer", e)
                self._bp_number = None

        for contract_id in self._contract_ids:
            if contract_id not in self._contract_to_bp_number:
                await self._resolve_bp_number_for_contract(contract_id)

        bp_numbers = set(self._bp_number_to_contract.keys())
        if not bp_numbers and self._bp_number:
            bp_numbers = {self._bp_number}

        all_contracts: list[Contract] = []
        for bp_number in bp_numbers:
            try:
                contracts_for_bp = await self.api.get_contracts(bp_number)
                all_contracts.extend(contracts_for_bp)
                for contract in contracts_for_bp:
                    self._set_contract_bp_mapping(int(contract.contract_id), bp_number)
            except asyncio.CancelledError:
                _LOGGER.warning(
                    "Fetching contracts was cancelled for BP %s; continuing",
                    bp_number,
                )
            except IECError:
                _LOGGER.exception("Failed fetching contracts for BP %s", bp_number)

        if not self._contract_ids:
            self._contract_ids = [
                int(contract.contract_id)
                for contract in all_contracts
                if contract.status == 1
            ]

        self._persist_bp_number_to_contract_mapping()
        return {
            int(c.contract_id): c
            for c in all_contracts
            if c.status == 1 and int(c.contract_id) in self._contract_ids
        }

    async def _get_devices_by_contract_id(self, contract_id) -> list[Device]:
        devices = self._devices_by_contract_id.get(contract_id)
        if not devices:
            try:
                devices = await self.api.get_devices(str(contract_id))
                self._devices_by_contract_id[contract_id] = devices
            except IECError as e:
                _LOGGER.exception(
                    f"Failed fetching devices by contract {contract_id}", e
                )
        return devices

    async def _get_devices_by_device_id(self, meter_id) -> Devices:
        devices = self._devices_by_meter_id.get(meter_id)
        if not devices:
            try:
                devices = await self.api.get_device_by_device_id(str(meter_id))
                self._devices_by_meter_id[meter_id] = devices
            except IECError as e:
                _LOGGER.exception(
                    f"Failed fetching device details by meter id {meter_id}", e
                )
        return devices

    async def _get_last_meter_reading(
        self, bp_number, contract_id, meter_id
    ) -> MeterReading:
        key = (contract_id, int(meter_id))
        last_meter_reading = self._last_meter_reading.get(key)
        if not last_meter_reading:
            try:
                meter_readings = await self.api.get_last_meter_reading(
                    bp_number, contract_id
                )

                for reading in meter_readings.last_meters:
                    reading_meter_id = int(reading.serial_number)
                    if len(reading.meter_readings) > 0:
                        readings = reading.meter_readings
                        readings.sort(key=lambda rdng: rdng.reading_date, reverse=True)
                        last_meter_reading = readings[0]
                        _LOGGER.debug(
                            f"Last Reading for contract {contract_id}, Meter {reading_meter_id}: "
                            f"{last_meter_reading}"
                        )
                        reading_key = (contract_id, reading_meter_id)
                        self._last_meter_reading[reading_key] = last_meter_reading
                    else:
                        _LOGGER.debug(
                            f"No Reading found for contract {contract_id}, Meter {reading_meter_id}"
                        )
            except IECError as e:
                _LOGGER.exception(
                    f"Failed fetching device details by meter id {meter_id}", e
                )
        return self._last_meter_reading.get(key)

    async def _get_kwh_tariff(self) -> float:
        if not self._kwh_tariff:
            try:
                self._kwh_tariff = await self.api.get_kwh_tariff()
            except asyncio.CancelledError:
                _LOGGER.warning(
                    "Fetching kWh tariff was cancelled; using 0.0 and continuing"
                )
                self._kwh_tariff = 0.0
            except IECError as e:
                _LOGGER.exception("Failed fetching kWh Tariff", e)
            except Exception as e:
                _LOGGER.exception("Unexpected error fetching kWh Tariff", e)

            # Fallback: try IEC calculators API when main call failed or returned 0.0
            if not self._kwh_tariff or self._kwh_tariff == 0.0:
                kwh_fallback, _ = await self._fetch_tariffs_from_calculators()
                if kwh_fallback and kwh_fallback > 0:
                    _LOGGER.debug(
                        "Using fallback kWh tariff from calculators API: %s",
                        kwh_fallback,
                    )
                    self._kwh_tariff = kwh_fallback
        return self._kwh_tariff or 0.0

    async def _get_kva_tariff(self) -> float:
        if not self._kva_tariff:
            try:
                self._kva_tariff = await self.api.get_kva_tariff()
            except asyncio.CancelledError:
                _LOGGER.warning(
                    "Fetching kVA tariff was cancelled; using 0.0 and continuing"
                )
                self._kva_tariff = 0.0
            except IECError as e:
                _LOGGER.exception("Failed fetching KVA Tariff from IEC API", e)
            except Exception as e:
                _LOGGER.exception("Unexpected error fetching KVA Tariff", e)

            # Fallback: try IEC calculators API when main call failed or returned 0.0
            if not self._kva_tariff or self._kva_tariff == 0.0:
                _, kva_fallback = await self._fetch_tariffs_from_calculators()
                if kva_fallback and kva_fallback > 0:
                    _LOGGER.debug(
                        "Using fallback kVA tariff from calculators API: %s",
                        kva_fallback,
                    )
                    self._kva_tariff = kva_fallback
        return self._kva_tariff or 0.0

    async def _fetch_tariffs_from_calculators(
        self,
    ) -> tuple[float | None, float | None]:
        """Fetch tariffs from IEC calculators endpoints as a fallback.

        Returns: tuple of (kwh_home_rate, kva_rate), each may be None if not found.
        """
        session = aiohttp_client.async_get_clientsession(
            self.hass, family=socket.AF_INET
        )
        kwh_tariff: float | None = None
        kva_tariff: float | None = None

        # Primary fallback: calculators/period (contains both homeRate and kvaRate)
        try:
            async with session.get(
                "https://iecapi.iec.co.il/api/content/he-IL/calculators/period",
                timeout=30,
            ) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    rates = data.get("period_Calculator_Rates") or {}
                    kwh_val = rates.get("homeRate")
                    kva_val = rates.get("kvaRate")
                    if isinstance(kwh_val, (int, float)):
                        kwh_tariff = float(kwh_val)
                    if isinstance(kva_val, (int, float)):
                        kva_tariff = float(kva_val)
                    _LOGGER.debug(
                        "Fetched fallback tariffs from calculators/period: homeRate=%s, kvaRate=%s",
                        kwh_tariff,
                        kva_tariff,
                    )
        except asyncio.CancelledError:
            _LOGGER.debug("Fallback calculators/period fetch was cancelled")
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "Failed fetching fallback tariffs from calculators/period: %s", err
            )

        # Secondary fallback: calculators/gadget (has homeRate only)
        if kwh_tariff is None:
            try:
                async with session.get(
                    "https://iecapi.iec.co.il/api/content/he-IL/calculators/gadget",
                    timeout=30,
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json(content_type=None)
                        rates = data.get("gadget_Calculator_Rates") or {}
                        kwh_val = rates.get("homeRate")
                        if isinstance(kwh_val, (int, float)):
                            kwh_tariff = float(kwh_val)
                        _LOGGER.debug(
                            "Fetched fallback kWh tariff from calculators/gadget: homeRate=%s",
                            kwh_tariff,
                        )
            except asyncio.CancelledError:
                _LOGGER.debug("Fallback calculators/gadget fetch was cancelled")
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug(
                    "Failed fetching fallback kWh tariff from calculators/gadget: %s",
                    err,
                )

        return kwh_tariff, kva_tariff

    async def _get_delivery_tariff(self, phase) -> float:
        delivery_tariff = self._delivery_tariff_by_phase.get(phase)
        if not delivery_tariff:
            try:
                delivery_tariff = await self.api.get_delivery_tariff(phase)
                self._delivery_tariff_by_phase[phase] = delivery_tariff
            except IECError as e:
                _LOGGER.exception(
                    f"Failed fetching Delivery Tariff by phase {phase}", e
                )
        return delivery_tariff or 0.0

    async def _get_distribution_tariff(self, phase) -> float:
        distribution_tariff = self._distribution_tariff_by_phase.get(phase)
        if not distribution_tariff:
            try:
                distribution_tariff = await self.api.get_distribution_tariff(phase)
                self._distribution_tariff_by_phase[phase] = distribution_tariff
            except IECError as e:
                _LOGGER.exception(
                    f"Failed fetching Distribution Tariff by phase {phase}", e
                )
        return distribution_tariff or 0.0

    async def _load_contract_account_mapping(self) -> None:
        if self._contract_account_mapping_loaded:
            return
        self._contract_account_mapping_loaded = True

        try:
            user_profile = await self.api.get_masa_contact_account_user_profile()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Failed fetching Masa user profile for account mapping: %s", err)
            return

        if not user_profile or not user_profile.connection_between_contact_and_contract:
            return

        for connection in user_profile.connection_between_contact_and_contract:
            if not connection.contract or not connection.account:
                continue

            contract_id = connection.contract.contract_acc_number_in_shoval
            account_id = connection.account.id
            if not contract_id or not account_id:
                continue

            normalized_contract_id = int(contract_id)
            self._account_id_by_contract[normalized_contract_id] = account_id
            if connection.part_connection_code and connection.part_connection_code != 1:
                self._shared_contract_ids.add(normalized_contract_id)

    async def _get_account_id(self, contract_id: int) -> UUID | None:
        mapped_account_id = self._account_id_by_contract.get(contract_id)
        if mapped_account_id:
            return mapped_account_id

        await self._load_contract_account_mapping()
        mapped_account_id = self._account_id_by_contract.get(contract_id)
        if mapped_account_id:
            return mapped_account_id

        if not self._default_account_id:
            try:
                account = await self.api.get_default_account()
                self._default_account_id = account.id
            except IECError as e:
                _LOGGER.exception("Failed fetching default account", e)
                return None

        return self._default_account_id

    async def _get_connection_size(self, account_id: UUID | None) -> str | None:
        if not account_id:
            return None

        connection_size = self._connection_size_by_account_id.get(account_id)
        if connection_size:
            return connection_size

        try:
            connection_size = await self.api.get_masa_connection_size_from_masa(account_id)
            if connection_size:
                self._connection_size_by_account_id[account_id] = connection_size
        except IECError as e:
            _LOGGER.exception(
                "Failed fetching Masa Connection Size for account %s", account_id, e
            )
            return None

        return connection_size

    async def _get_power_size(self, connection_size) -> float:
        power_size = self._power_size_by_connection_size.get(connection_size)
        if not power_size:
            try:
                power_size = await self.api.get_power_size(connection_size)
                self._power_size_by_connection_size[connection_size] = power_size
            except IECError as e:
                _LOGGER.exception(
                    f"Failed fetching Power Size by Connection Size {connection_size}",
                    e,
                )
        return power_size or 0.0

    async def _get_readings(
        self,
        contract_id: int,
        device_id: str | int,
        device_code: str | int,
        reading_date: datetime,
        resolution: ReadingResolution,
    ):
        date_key = reading_date.strftime("%Y")
        match resolution:
            case ReadingResolution.DAILY:
                date_key += reading_date.strftime("-%m-%d")
            case ReadingResolution.WEEKLY:
                date_key += "/" + str(reading_date.isocalendar().week)
            case ReadingResolution.MONTHLY:
                date_key += reading_date.strftime("-%m")
            case _:
                _LOGGER.warning("Unexpected resolution value")
                date_key += reading_date.strftime("-%m-%d")

        key = (contract_id, int(device_id), date_key)
        reading = self._readings.get(key)
        if not reading:
            try:
                reading = await self.api.get_remote_reading(
                    device_id,
                    int(device_code),
                    reading_date,
                    reading_date,
                    resolution,
                    str(contract_id),
                )
                self._readings[key] = reading
            except IECError as e:
                _LOGGER.exception(
                    f"Failed fetching reading for Contract: {contract_id},"
                    f"date: {reading_date.strftime('%d-%m-%Y')}, "
                    f"resolution: {resolution}",
                    e,
                )
        return reading

    @staticmethod
    def _extract_valid_future_consumption(
        reading: RemoteReadingResponse | None,
    ) -> FutureConsumptionInfo | None:
        """Return normalized future consumption data if the IEC payload is usable."""
        if not reading or not reading.meter_list:
            return None

        meter = reading.meter_list[0]
        future_info = meter.future_consumption_info
        if not future_info:
            return None

        # IEC sometimes returns zeroed futureConsumptionInfo while meter.total_import
        # still contains a valid value for the same response.
        if (
            not future_info.total_import
            and meter.total_import
            and meter.total_import > 0
        ):
            future_info.total_import = meter.total_import

        if (
            future_info.future_consumption
            and future_info.future_consumption > 0
        ) or (future_info.total_import and future_info.total_import > 0):
            return future_info

        return None

    async def _verify_daily_readings_exist(
        self,
        daily_readings: dict[str, list[PeriodConsumption]],
        desired_date: date,
        device: Device,
        contract_id: int,
        prefetched_reading: RemoteReadingResponse | None = None,
    ):
        if not daily_readings.get(device.device_number):
            daily_readings[device.device_number] = []

        daily_reading = next(
            filter(
                lambda x: find_reading_by_date(x, desired_date),
                daily_readings[device.device_number],
            ),
            None,
        )
        if not daily_reading:
            _LOGGER.debug(
                f"Daily reading for date: {desired_date.strftime('%Y-%m-%d')} is missing, calculating manually"
            )
            readings = prefetched_reading
            if not readings:
                readings = await self._get_readings(
                    contract_id,
                    device.device_number,
                    device.device_code,
                    datetime.fromordinal(desired_date.toordinal()),
                    ReadingResolution.MONTHLY,
                )
            else:
                _LOGGER.debug(
                    f"Daily reading for date: {desired_date.strftime('%Y-%m-%d')} - using existing prefetched readings"
                )

            if readings and readings.meter_list and len(readings.meter_list) > 0:
                daily_readings[device.device_number] += readings.meter_list[
                    0
                ].period_consumptions

                # Remove duplicates
                daily_readings[device.device_number] = list(
                    dict.fromkeys(daily_readings[device.device_number])
                )

                # Sort by Date
                daily_readings[device.device_number].sort(key=lambda x: x.interval)

                desired_date_reading = next(
                    filter(
                        lambda reading: reading.interval.date() == desired_date,
                        readings.meter_list[0].period_consumptions,
                    ),
                    None,
                )
                if (
                    desired_date_reading is None
                    or desired_date_reading.consumption <= 0
                ):
                    _LOGGER.debug(
                        f"Couldn't find daily reading for: {desired_date.strftime('%Y-%m-%d')}"
                    )
                else:
                    daily_readings[device.device_number].append(
                        PeriodConsumption(
                            status=0,
                            interval=desired_date,
                            consumption=desired_date_reading.consumption,
                            back_stream=0,
                        )
                    )
        else:
            _LOGGER.debug(
                f"Daily reading for date: {daily_reading.interval.strftime('%Y-%m-%d')}"
                f" is present: {daily_reading.consumption}"
            )

    async def _update_data(
        self,
    ) -> dict[str, dict[str, Any]]:
        contracts: dict[int, Contract] = await self._load_selected_contracts()
        await self._load_contract_account_mapping()
        localized_today = localize_datetime(datetime.now())
        localized_first_of_month = localized_today.replace(day=1)
        kwh_tariff = await self._get_kwh_tariff()
        kva_tariff = await self._get_kva_tariff()

        access_token = self.api.get_token().access_token
        decoded_token = jwt.decode(access_token, options={"verify_signature": False})
        access_token_issued_at = decoded_token["iat"]
        access_token_expiration_time = decoded_token["exp"]

        data = {
            JWT_DICT_NAME: {
                ACCESS_TOKEN_ISSUED_AT: access_token_issued_at,
                ACCESS_TOKEN_EXPIRATION_TIME: access_token_expiration_time,
            },
            STATICS_DICT_NAME: {
                STATIC_KWH_TARIFF: kwh_tariff,
                STATIC_KVA_TARIFF: kva_tariff,
                STATIC_BP_NUMBER: (
                    self._bp_number
                    if self._bp_number
                    else (next(iter(self._bp_number_to_contract), None))
                ),
            },
        }

        estimated_bill_dict = None

        _LOGGER.debug(f"All Contract Ids: {list(contracts.keys())}")

        for contract_id in self._contract_ids:
            contract = contracts.get(contract_id)
            if not contract:
                _LOGGER.debug(
                    "Contract %s is selected but not available in active contracts",
                    contract_id,
                )
                continue

            bp_number_for_contract = await self._resolve_bp_number_for_contract(
                contract_id
            )
            # Because IEC API provides historical usage/cost with a delay of a couple of days
            # we need to insert data into statistics.
            self.hass.async_create_task(
                self._insert_statistics(contract_id, contract.smart_meter)
            )

            if not bp_number_for_contract:
                _LOGGER.warning(
                    "Missing bp_number for contract %s; skipping invoices fetch",
                    contract_id,
                )
                billing_invoices = None
            else:
                try:
                    billing_invoices = await self.api.get_billing_invoices(
                        bp_number_for_contract, contract_id
                    )
                except asyncio.CancelledError:
                    _LOGGER.warning(
                        "Fetching invoices was cancelled; continuing without invoices"
                    )
                    billing_invoices = None
                except IECError as e:
                    _LOGGER.exception("Failed fetching invoices", e)
                    billing_invoices = None

            if (
                billing_invoices
                and billing_invoices.invoices
                and len(billing_invoices.invoices) > 0
            ):
                billing_invoices.invoices = list(
                    filter(
                        lambda inv: inv.document_id == ELECTRIC_INVOICE_DOC_ID,
                        billing_invoices.invoices,
                    )
                )
                billing_invoices.invoices.sort(
                    key=lambda inv: inv.full_date, reverse=True
                )
                last_invoice = billing_invoices.invoices[0]
            else:
                last_invoice = EMPTY_INVOICE

            future_consumption: dict[str, FutureConsumptionInfo | None] | None = {}
            daily_readings: dict[str, list[PeriodConsumption] | None] | None = {}

            is_smart_meter = contract.smart_meter
            is_private_producer = contract.from_private_producer
            attributes_to_add = {
                CONTRACT_ID_ATTR_NAME: str(contract_id),
                IS_SMART_METER_ATTR_NAME: is_smart_meter,
                IS_SHARED_ATTR_NAME: contract_id in self._shared_contract_ids,
                METER_ID_ATTR_NAME: None,
            }

            if is_smart_meter:
                # For some reason, there are differences between sending 2024-03-01 and sending 2024-03-07 (Today)
                # So instead of sending the 1st day of the month, just sending today date

                devices = await self._get_devices_by_contract_id(contract_id)
                if not devices:
                    _LOGGER.debug(
                        f"No devices for contract {contract_id}. Skipping creating devices."
                    )
                    continue

                for device in devices or []:
                    attributes_to_add[METER_ID_ATTR_NAME] = device.device_number

                    reading_type: ReadingResolution | None = None
                    reading_date: date | None = None

                    if localized_today.date() != localized_first_of_month.date():
                        reading_type: ReadingResolution | None = (
                            ReadingResolution.MONTHLY
                        )
                        reading_date: date | None = localized_first_of_month
                    elif localized_today.date().isoweekday() != 7:
                        # If today's the 1st of the month, but not sunday, get weekly from yesterday
                        yesterday = localized_today - timedelta(days=1)
                        reading_type: ReadingResolution | None = (
                            ReadingResolution.WEEKLY
                        )
                        reading_date: date | None = yesterday
                    else:
                        # Today is the 1st and is Monday (since monday.isoweekday==1)
                        last_month_first_of_the_month = (
                            localized_first_of_month - timedelta(days=1)
                        ).replace(day=1)

                        reading_type: ReadingResolution | None = (
                            ReadingResolution.MONTHLY
                        )
                        reading_date: date | None = last_month_first_of_the_month

                    _LOGGER.debug(
                        f"Fetching {reading_type.name} readings from {reading_date}"
                    )
                    remote_reading = await self._get_readings(
                        contract_id,
                        device.device_number,
                        device.device_code,
                        reading_date,
                        reading_type,
                    )
                    if (
                        remote_reading
                        and remote_reading.meter_list
                        and len(remote_reading.meter_list) > 0
                    ):
                        daily_readings[device.device_number] = (
                            remote_reading.meter_list[0].period_consumptions
                        )
                        monthly_future_consumption = (
                            self._extract_valid_future_consumption(remote_reading)
                        )
                        if monthly_future_consumption:
                            future_consumption[device.device_number] = (
                                monthly_future_consumption
                            )
                    else:
                        _LOGGER.warning(
                            "No %s readings returned for device %s in contract %s on %s",
                            reading_type.name,
                            device.device_number,
                            contract_id,
                            reading_date,
                        )
                        daily_readings[device.device_number] = []

                    # Verify today's date appears
                    await self._verify_daily_readings_exist(
                        daily_readings,
                        localized_today.date(),
                        device,
                        contract_id,
                    )

                    today_reading_key = str(contract_id) + "-" + device.device_number
                    today_reading = self._today_readings.get(today_reading_key)

                    if not today_reading:
                        today_reading = await self._get_readings(
                            contract_id,
                            device.device_number,
                            device.device_code,
                            localized_today,
                            ReadingResolution.DAILY,
                        )
                        self._today_readings[today_reading_key] = today_reading

                    # fallbacks for future consumption since IEC api is broken :/
                    if not future_consumption.get(device.device_number):
                        today_future_consumption = (
                            self._extract_valid_future_consumption(
                                self._today_readings.get(today_reading_key)
                            )
                        )

                        if today_future_consumption:
                            future_consumption[device.device_number] = (
                                today_future_consumption
                            )
                        else:
                            req_date = localized_today - timedelta(days=2)
                            two_days_ago_reading = await self._get_readings(
                                contract_id,
                                device.device_number,
                                device.device_code,
                                req_date,
                                ReadingResolution.DAILY,
                            )
                            two_days_ago_future_consumption = (
                                self._extract_valid_future_consumption(
                                    two_days_ago_reading
                                )
                            )

                            if two_days_ago_future_consumption:
                                future_consumption[device.device_number] = (
                                    two_days_ago_future_consumption
                                )
                            else:
                                _LOGGER.warning(
                                    "Failed fetching FutureConsumption, data in IEC API is corrupted"
                                )
                                future_consumption[device.device_number] = None

                    try:
                        (
                            estimated_bill,
                            fixed_price,
                            consumption_price,
                            total_days,
                            delivery_price,
                            distribution_price,
                            total_kva_price,
                            estimated_kwh_consumption,
                        ) = await self._estimate_bill(
                            contract_id,
                            bp_number_for_contract,
                            device.device_number,
                            is_private_producer,
                            future_consumption,
                            kwh_tariff,
                            kva_tariff,
                            last_invoice,
                        )
                    except Exception as e:
                        _LOGGER.warn("Failed to calculate estimated next bill", e)
                        estimated_bill = 0
                        consumption_price = 0
                        total_days = 0
                        delivery_price = 0
                        distribution_price = 0
                        total_kva_price = 0
                        estimated_kwh_consumption = 0

                    estimated_bill_dict = {
                        TOTAL_EST_BILL_ATTR_NAME: estimated_bill,
                        EST_BILL_DAYS_ATTR_NAME: total_days,
                        EST_BILL_CONSUMPTION_PRICE_ATTR_NAME: consumption_price,
                        EST_BILL_DELIVERY_PRICE_ATTR_NAME: delivery_price,
                        EST_BILL_DISTRIBUTION_PRICE_ATTR_NAME: distribution_price,
                        EST_BILL_TOTAL_KVA_PRICE_ATTR_NAME: total_kva_price,
                        EST_BILL_KWH_CONSUMPTION_ATTR_NAME: estimated_kwh_consumption,
                    }

            data[str(contract_id)] = {
                CONTRACT_DICT_NAME: contracts.get(contract_id),
                INVOICE_DICT_NAME: last_invoice,
                FUTURE_CONSUMPTIONS_DICT_NAME: future_consumption,
                DAILY_READINGS_DICT_NAME: daily_readings,
                STATICS_DICT_NAME: {STATIC_KWH_TARIFF: kwh_tariff},  # workaround,
                ATTRIBUTES_DICT_NAME: attributes_to_add,
                ESTIMATED_BILL_DICT_NAME: estimated_bill_dict,
            }

        # Clean up for next cycle
        self._today_readings = {}
        self._devices_by_contract_id = {}
        self._kwh_tariff = None
        self._readings = {}

        return data

    async def _async_update_data(
        self,
    ) -> dict[str, dict[str, Any]]:
        """Fetch data from API endpoint."""
        if self._first_load:
            _LOGGER.debug("Loading API token from config entry")
            await self.api.load_jwt_token(
                JWT.from_dict(self._entry_data[CONF_API_TOKEN])
            )

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
                self.hass.config_entries.async_update_entry(
                    entry=self._config_entry, data=new_data
                )
                self._entry_data = new_data
        except IECError as err:
            raise ConfigEntryAuthFailed from err

        try:
            return await self._update_data()
        except asyncio.CancelledError as err:
            _LOGGER.warning(
                "Data update was cancelled (network timeout/cancelled); will retry later: %s",
                err,
            )
            # Return empty data so setup doesn't fail; periodic refresh will try again
            return {}
        except Exception as err:
            _LOGGER.error("Failed updating data. Exception: %s", err)
            _LOGGER.error(traceback.format_exc())
            raise UpdateFailed("Failed Updating IEC data", retry_after=60) from err

    async def _insert_statistics(self, contract_id: int, is_smart_meter: bool) -> None:
        if not is_smart_meter:
            _LOGGER.info(
                f"[IEC Statistics] IEC Contract {contract_id} doesn't contain Smart Meters, not adding statistics"
            )
            # Support only smart meters at the moment
            return

        _LOGGER.debug(
            f"[IEC Statistics] Updating statistics for IEC Contract {contract_id}"
        )
        devices = await self._get_devices_by_contract_id(contract_id)
        kwh_price = await self._get_kwh_tariff()
        localized_today = localize_datetime(datetime.now())

        if not devices:
            _LOGGER.error(
                f"[IEC Statistics] Failed fetching devices for IEC Contract {contract_id}"
            )
            return

        for device in devices:
            id_prefix = f"iec_meter_{device.device_number}"
            consumption_statistic_id = f"{DOMAIN}:{id_prefix}_energy_consumption"
            cost_statistic_id = f"{DOMAIN}:{id_prefix}_energy_est_cost"

            last_stat = await get_instance(self.hass).async_add_executor_job(
                get_last_statistics, self.hass, 1, consumption_statistic_id, True, set()
            )

            if not last_stat:
                _LOGGER.debug(
                    "[IEC Statistics] No statistics found, fetching today's MONTHLY readings to extract field `meterStartDate`"
                )

                month_ago_time = localized_today - timedelta(weeks=4)
                readings = await self._get_readings(
                    contract_id,
                    device.device_number,
                    device.device_code,
                    localized_today,
                    ReadingResolution.MONTHLY,
                )

                if (
                    readings
                    and readings.meter_list
                    and readings.meter_list[0].meter_start_date
                ):
                    # Fetching the last reading from either the installation date or a month ago
                    month_ago_time = max(
                        month_ago_time,
                        localize_datetime(
                            datetime.combine(
                                readings.meter_list[0].meter_start_date,
                                datetime.min.time(),
                            )
                        ),
                    )
                else:
                    _LOGGER.debug(
                        "[IEC Statistics] Failed to extract field `meterStartDate`, falling back to a month ago"
                    )

                _LOGGER.debug("[IEC Statistics] Updating statistic for the first time")
                _LOGGER.debug(
                    f"[IEC Statistics] Fetching consumption from {month_ago_time.strftime('%Y-%m-%d %H:%M:%S')}"
                )
                last_stat_time = 0
                readings = await self._get_readings(
                    contract_id,
                    device.device_number,
                    device.device_code,
                    month_ago_time,
                    ReadingResolution.DAILY,
                )

            else:
                last_stat_time = last_stat[consumption_statistic_id][0]["start"]
                # API returns daily data, so need to increase the start date by 4 hrs to get the next day
                from_date = localize_datetime(datetime.fromtimestamp(last_stat_time))
                _LOGGER.debug(
                    f"[IEC Statistics] Last statistics are from {from_date.strftime('%Y-%m-%d %H:%M:%S')}"
                )

                if from_date.hour == 23:
                    from_date = from_date + timedelta(hours=2)

                if localized_today.date() == from_date.date():
                    _LOGGER.debug(
                        "[IEC Statistics] The date to fetch is today or later, replacing it with Today at 01:00:00"
                    )
                    from_date = localized_today.replace(
                        hour=1, minute=0, second=0, microsecond=0
                    )

                min_from_date = (localized_today - timedelta(days=30)).replace(
                    hour=1, minute=0, second=0, microsecond=0
                )
                if from_date < min_from_date:
                    _LOGGER.debug(
                        "[IEC Statistics] Last statistics are too old, limiting fetch window to %s",
                        min_from_date.strftime("%Y-%m-%d %H:%M:%S"),
                    )
                    from_date = min_from_date

                _LOGGER.debug(
                    f"[IEC Statistics] Fetching consumption from {from_date.strftime('%Y-%m-%d %H:%M:%S')}"
                )
                readings = await self._get_readings(
                    contract_id,
                    device.device_number,
                    device.device_code,
                    from_date,
                    ReadingResolution.DAILY,
                )
                if from_date.date() == localized_today.date():
                    self._today_readings[
                        str(contract_id) + "-" + device.device_number
                    ] = readings

            if (
                not readings
                or not readings.meter_list
                or not len(readings.meter_list) > 0
                or not readings.meter_list[0].period_consumptions
                or not len(readings.meter_list[0].period_consumptions) > 0
            ):
                _LOGGER.debug("[IEC Statistics] No recent usage data. Skipping update")
                continue

            last_stat_hour = (
                localize_datetime(datetime.fromtimestamp(last_stat_time))
                if last_stat_time
                else readings.meter_list[0].period_consumptions[0].interval
            )
            last_stat_req_hour = (
                last_stat_hour
                if last_stat_hour.hour > 0
                else (last_stat_hour - timedelta(hours=1))
            )

            _LOGGER.debug(
                f"[IEC Statistics] Fetching LongTerm Statistics since {last_stat_req_hour}"
            )
            stats = await get_instance(self.hass).async_add_executor_job(
                statistics_during_period,
                self.hass,
                last_stat_req_hour,
                None,
                {cost_statistic_id, consumption_statistic_id},
                "hour",
                None,
                {"sum"},
            )

            if not stats.get(consumption_statistic_id):
                _LOGGER.debug("[IEC Statistics] No recent usage data")
                consumption_sum = 0
            else:
                consumption_sum = cast(float, stats[consumption_statistic_id][0]["sum"])

            if not stats.get(cost_statistic_id):
                if not stats.get(consumption_statistic_id):
                    _LOGGER.debug("[IEC Statistics] No recent cost data")
                    cost_sum = 0.0
                else:
                    cost_sum = (
                        cast(float, stats[consumption_statistic_id][0]["sum"])
                        * kwh_price
                    )
            else:
                cost_sum = cast(float, stats[cost_statistic_id][0]["sum"])

            _LOGGER.debug(
                f"[IEC Statistics] Last Consumption Sum for C[{contract_id}] D[{device.device_number}]: {consumption_sum}"
            )
            _LOGGER.debug(
                f"[IEC Statistics] Last Estimated Cost Sum for C[{contract_id}] D[{device.device_number}]: {cost_sum}"
            )

            new_readings: list[PeriodConsumption] = filter(
                lambda reading: (
                    reading.interval
                    >= localize_datetime(datetime.fromtimestamp(last_stat_time))
                ),
                readings.meter_list[0].period_consumptions,
            )

            grouped_new_readings_by_hour = itertools.groupby(
                new_readings,
                key=lambda reading: reading.interval.replace(
                    minute=0, second=0, microsecond=0
                ),
            )
            readings_by_hour: dict[datetime, float] = {}
            if last_stat_req_hour and last_stat_req_hour.tzinfo is None:
                last_stat_req_hour = localize_datetime(last_stat_req_hour)

            for key, group in grouped_new_readings_by_hour:
                group_list = list(group)
                # Apply 4 listings per hour check only for days less than 1 month old
                one_month_ago = localized_today - timedelta(days=30)
                if key.date() >= one_month_ago.date() and len(group_list) < 4:
                    _LOGGER.debug(
                        f"[IEC Statistics] LongTerm Statistics - Skipping {key} since it's partial for the hour "
                        f"(data is less than 1 month old and has only {len(group_list)} readings)"
                    )
                    continue
                if key <= last_stat_req_hour:
                    _LOGGER.debug(
                        f"[IEC Statistics] LongTerm Statistics - Skipping {key} data since it's already reported"
                    )
                    continue
                readings_by_hour[key] = sum(
                    reading.consumption for reading in group_list
                )

            consumption_metadata = StatisticMetaData(
                has_mean=False,
                has_sum=True,
                name=f"IEC Meter {device.device_number} Consumption",
                source=DOMAIN,
                statistic_id=consumption_statistic_id,
                unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
                mean_type=StatisticMeanType.NONE,
            )

            cost_metadata = StatisticMetaData(
                has_mean=False,
                has_sum=True,
                name=f"IEC Meter {device.device_number} Estimated Cost",
                source=DOMAIN,
                statistic_id=cost_statistic_id,
                unit_of_measurement=ILS,
                mean_type=StatisticMeanType.NONE,
            )

            consumption_statistics = []
            cost_statistics = []
            for key, value in sorted(readings_by_hour.items()):
                consumption_sum += value
                cost_sum += value * kwh_price

                consumption_statistics.append(
                    StatisticData(start=key, sum=consumption_sum, state=value)
                )

                cost_statistics.append(
                    StatisticData(start=key, sum=cost_sum, state=value * kwh_price)
                )

            if readings_by_hour:
                _LOGGER.debug(
                    f"[IEC Statistics] Last hour fetched for C[{contract_id}] D[{device.device_number}]: "
                    f"{max(readings_by_hour, key=lambda k: k)}"
                )
                _LOGGER.debug(
                    f"[IEC Statistics] New Consumption Sum for C[{contract_id}] D[{device.device_number}]: {consumption_sum}"
                )
                _LOGGER.debug(
                    f"[IEC Statistics] New Estimated Cost Sum for C[{contract_id}] D[{device.device_number}]: {cost_sum}"
                )

            async_add_external_statistics(
                self.hass, consumption_metadata, consumption_statistics
            )

            async_add_external_statistics(self.hass, cost_metadata, cost_statistics)

    async def _estimate_bill(
        self,
        contract_id,
        bp_number,
        device_number,
        is_private_producer,
        future_consumption,
        kwh_tariff,
        kva_tariff,
        last_invoice,
    ):
        last_meter_read: int | None = None
        last_meter_read_date: date | None = None
        phase_count: int | None = None
        connection_size: str | None = None
        devices_by_id: Devices | None = None

        if not is_private_producer:
            try:
                devices_by_id: Devices = await self._get_devices_by_device_id(
                    device_number
                )

                if (
                    devices_by_id.counter_devices
                    and len(devices_by_id.counter_devices) >= 1
                ):
                    last_meter_read = int(devices_by_id.counter_devices[0].last_mr)
                    last_meter_read_date = devices_by_id.counter_devices[0].last_mr_date
                    phase_count = devices_by_id.counter_devices[0].connection_size.phase
                    connection_size = devices_by_id.counter_devices[
                        0
                    ].connection_size.representative_connection_size
                else:
                    _LOGGER.warning(
                        "Failed to get Last Device Meter Reading, trying another way..."
                    )

            except Exception as e:
                _LOGGER.warning(
                    "Failed to fetch data from devices_by_id, falling back to Masa API",
                    e,
                )
                _LOGGER.debug(f"DevicesById Response: {devices_by_id}")
                last_meter_read = None
                last_meter_read_date = None
                phase_count = None
                connection_size = None

        if is_private_producer or not last_meter_read:
            if not bp_number:
                _LOGGER.warning(
                    "Missing bp_number for contract %s; cannot fetch last meter reading",
                    contract_id,
                )
                last_meter_reading = None
            else:
                last_meter_reading = await self._get_last_meter_reading(
                    bp_number, contract_id, device_number
                )

            if not last_meter_reading:
                _LOGGER.warning(
                    "Couldn't get Last Meter Read, WILL NOT calculate the usage part in estimated bill."
                )
                last_meter_read = None
                last_meter_read_date = localize_datetime(datetime.now()).date()
                last_invoice = EMPTY_INVOICE
            else:
                last_meter_read = last_meter_reading.reading
                last_meter_read_date = last_meter_reading.reading_date.date()

            account_id = await self._get_account_id(contract_id)
            connection_size = await self._get_connection_size(account_id)
            if connection_size:
                phase_count_str = (
                    connection_size.split("X")[0]
                    if connection_size.find("X") != -1
                    else "1"
                )
                phase_count = int(phase_count_str)

        if connection_size:
            power_size = await self._get_power_size(connection_size)
        else:
            power_size = 0.0
            _LOGGER.warning("Couldn't get Connection Size")

        if phase_count:
            distribution_tariff = await self._get_distribution_tariff(phase_count)
            delivery_tariff = await self._get_delivery_tariff(phase_count)
        else:
            distribution_tariff = 0.0
            delivery_tariff = 0.0
            if connection_size:
                _LOGGER.warning("Couldn't get Phase Count")

        return self._calculate_estimated_bill(
            device_number,
            future_consumption,
            last_meter_read,
            last_meter_read_date,
            kwh_tariff,
            kva_tariff,
            distribution_tariff,
            delivery_tariff,
            power_size,
            last_invoice,
        )

    @staticmethod
    def _calculate_estimated_bill(
        meter_id,
        future_consumptions: dict[str, FutureConsumptionInfo | None],
        last_meter_read,
        last_meter_read_date,
        kwh_tariff,
        kva_tariff,
        distribution_tariff,
        delivery_tariff,
        power_size,
        last_invoice,
    ):
        future_consumption_info: FutureConsumptionInfo | None = future_consumptions.get(
            meter_id
        )
        future_consumption = 0

        if last_meter_read and future_consumption_info:
            if future_consumption_info.total_import:
                future_consumption = (
                    future_consumption_info.total_import - last_meter_read
                )
            elif (
                future_consumption_info.future_consumption
                and future_consumption_info.future_consumption > 0
            ):
                future_consumption = future_consumption_info.future_consumption
            else:
                _LOGGER.warn(
                    f"Failed to calculate Future Consumption for meter {meter_id} "
                    f"(missing total_import), defaulting forecasted consumption to 0"
                )
                future_consumption = 0

        kva_price = power_size * kva_tariff / 365

        total_kva_price = 0
        distribution_price = 0
        delivery_price = 0

        consumption_price = round(future_consumption * kwh_tariff, 2)
        total_days = 0

        today = localize_datetime(datetime.now())

        if last_invoice != EMPTY_INVOICE:
            current_date = last_meter_read_date + timedelta(days=1)
            month_counter = Counter()

            while current_date <= today.date():
                # Use (year, month) as the key for counting
                month_year = (current_date.year, current_date.month)
                month_counter[month_year] += 1

                # Move to the next day
                current_date += timedelta(days=1)

            for (year, month), days in month_counter.items():
                days_in_month = calendar.monthrange(year, month)[1]
                total_kva_price += kva_price * days
                distribution_price += (distribution_tariff / days_in_month) * days
                delivery_price += (delivery_tariff / days_in_month) * days
                total_days += days
        else:
            total_days = today.day
            days_in_current_month = calendar.monthrange(today.year, today.month)[1]

            consumption_price = round(future_consumption * kwh_tariff, 2)
            total_kva_price = round(kva_price * total_days, 2)
            distribution_price = round(
                (distribution_tariff / days_in_current_month) * total_days, 2
            )
            delivery_price = (delivery_tariff / days_in_current_month) * total_days

        _LOGGER.debug(
            f"Calculated estimated bill: No. of days: {total_days}, total KVA price: {total_kva_price}, "
            f"total distribution price: {distribution_price}, total delivery price: {delivery_price}, "
            f"consumption price: {consumption_price}"
        )

        fixed_price = round(total_kva_price + distribution_price + delivery_price, 2)
        total_estimated_bill = round(consumption_price + fixed_price, 2)
        return (
            total_estimated_bill,
            fixed_price,
            round(consumption_price, 2),
            total_days,
            round(delivery_price, 2),
            round(distribution_price, 2),
            round(total_kva_price, 2),
            future_consumption,
        )
