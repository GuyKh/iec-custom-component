"""Coordinator to handle IEC connections."""

import asyncio
import logging
import socket
import traceback
from datetime import date, datetime, timedelta, time
from typing import Any, Callable  # noqa: UP035
from uuid import UUID

import jwt

try:
    from homeassistant.components.recorder.models import StatisticMeanType
except ImportError:
    # Fallback for environments with older Home Assistant versions (<2025.10)
    from enum import StrEnum

    class StatisticMeanType(StrEnum):  # type: ignore[no-redef]
        """Statistic mean type."""

        NONE = "none"
        ARITHMETIC = "arithmetic"
        CIRCULAR = "circular"


from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_API_TOKEN
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import aiohttp_client
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from iec_api.iec_client import IecClient
from iec_api.models.contract import Contract
from iec_api.models.device import Devices
from iec_api.models.exceptions import IECError
from iec_api.models.jwt import JWT
from iec_api.models.remote_reading import (
    FutureConsumptionInfo,
    PeriodConsumption,
    ReadingResolution,
)

from .bill import (
    _build_backstream_totals,
    _calculate_estimated_bill,
    _extract_valid_future_consumption,
    _get_invoice_reading_dates,
    _is_backstream_meter_kind,
    _select_meter_data,
)
from .commons import TIMEZONE
from .const import (
    ACCESS_TOKEN_EXPIRATION_TIME,
    ACCESS_TOKEN_ISSUED_AT,
    ATTRIBUTES_DICT_NAME,
    BACKSTREAM_METERS_DICT_NAME,
    BACKSTREAM_TOTALS_DICT_NAME,
    CONF_BP_NUMBER,
    CONF_BP_NUMBER_TO_CONTRACT,
    CONF_SELECTED_CONTRACTS,
    CONF_USER_ID,
    CONTRACT_DICT_NAME,
    CONTRACT_ID_ATTR_NAME,
    DAILY_READINGS_DICT_NAME,
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

from .data_fetcher import IecDataFetcher
from .statistics import insert_statistics

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
            name="Iec",
            # Data is updated daily on IEC.
            # Refresh every 1h to be at most 5h behind.
            update_interval=timedelta(hours=1),
        )
        self.config_entry = config_entry
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
        self._entry_data: dict[str, Any] = dict(config_entry.data)
        self._account_id_by_contract: dict[int, UUID] = {}
        self._shared_contract_ids: set[int] = set()
        self._contract_account_mapping_loaded = False
        self._default_account_id: UUID | None = None
        self._api_session = aiohttp_client.async_get_clientsession(
            hass, family=socket.AF_INET
        )
        self.api = IecClient(
            self._entry_data[CONF_USER_ID],
            session=self._api_session,
        )
        self._first_load: bool = True
        self._fetcher = IecDataFetcher(hass, self.api, config_entry)

        @callback
        def _dummy_listener() -> None:
            pass

        # Force the coordinator to periodically update by registering at least one listener.
        # Needed when the _async_update_data below returns {} for utilities that don't provide
        # forecast, which results to no sensors added, no registered listeners, and thus
        # _async_update_data not periodically getting called which is needed for _insert_statistics.
        self._dummy_listener_unsub: Callable[[], None] | None = self.async_add_listener(
            _dummy_listener
        )

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
            contract_id in self._contract_to_bp_number
            for contract_id in self._contract_ids
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
                if customer:
                    self._bp_number = customer.bp_number
                else:
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

    async def _load_contract_account_mapping(self) -> None:
        if self._contract_account_mapping_loaded:
            return
        self._contract_account_mapping_loaded = True

        try:
            user_profile = await self.api.get_masa_contact_account_user_profile()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "Failed fetching Masa user profile for account mapping: %s", err
            )
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

    async def _update_data(
        self,
    ) -> dict[str, dict[str, Any]]:
        contracts: dict[int, Contract] = await self._load_selected_contracts()
        await self._load_contract_account_mapping()
        localized_today = datetime.now(TIMEZONE)
        localized_first_of_month = localized_today.replace(day=1)
        kwh_tariff, kva_tariff = await asyncio.gather(
            self._fetcher._get_kwh_tariff(),
            self._fetcher._get_kva_tariff(),
        )

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

        _LOGGER.debug(f"All Contract Ids: {list(contracts.keys())}")

        stat_tasks: list[asyncio.Task] = []

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
            stat_tasks.append(
                self.config_entry.async_create_background_task(
                    self.hass,
                    insert_statistics(
                        self.hass,
                        self.config_entry,
                        self._fetcher,
                        contract_id,
                        contract.smart_meter,
                    ),
                    name=f"iec_stats_{contract_id}",
                )
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
                        bp_number_for_contract, str(contract_id)
                    )
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
                # Get the reading dates based on invoice data
                last_invoice_date, from_date = _get_invoice_reading_dates(
                    billing_invoices.invoices
                )
                # Keep the first invoice (most recent by full_date) for other uses
                billing_invoices.invoices.sort(
                    key=lambda inv: inv.full_date or datetime.min, reverse=True
                )
                last_invoice = billing_invoices.invoices[0]
            else:
                last_invoice = EMPTY_INVOICE
                last_invoice_date = None
                from_date = None

            future_consumption: dict[str, FutureConsumptionInfo | None] = {}
            daily_readings: dict[str, list[PeriodConsumption]] = {}
            backstream_meters: dict[str, bool] = {}
            backstream_totals: dict[str, dict[str, float | None]] = {}

            estimated_bill_dict = None
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

                devices = await self._fetcher._get_devices_by_contract_id(contract_id)
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
                        reading_type = ReadingResolution.MONTHLY
                        reading_date = localized_first_of_month.date()
                    elif localized_today.date().isoweekday() != 7:
                        # If today's the 1st of the month, but not sunday, get weekly from yesterday
                        yesterday = localized_today - timedelta(days=1)
                        reading_type = ReadingResolution.WEEKLY
                        reading_date = yesterday.date()
                    else:
                        # Today is the 1st and is Monday (since monday.isoweekday==1)
                        last_month_first_of_the_month = (
                            localized_first_of_month - timedelta(days=1)
                        ).replace(day=1)

                        reading_type = ReadingResolution.MONTHLY
                        reading_date = last_month_first_of_the_month.date()

                    _LOGGER.debug(
                        f"Fetching {reading_type.name} readings from {reading_date}"
                    )
                    # Use invoice-based date for MONTHLY readings, otherwise use computed date
                    # But don't override when we specifically want current month data (first of current month)
                    assert reading_date is not None
                    actual_reading_date = datetime.combine(reading_date, time.min)
                    actual_last_invoice_date = None
                    if (
                        reading_type == ReadingResolution.MONTHLY
                        and from_date
                        and last_invoice_date
                        and reading_date != localized_first_of_month.date()
                    ):
                        actual_reading_date = from_date
                        actual_last_invoice_date = last_invoice_date

                    remote_reading = await self._fetcher._get_readings(
                        contract_id,
                        device.device_number,
                        device.device_code,
                        actual_reading_date,
                        reading_type,
                        device.meter_kind,
                        actual_last_invoice_date,
                    )
                    if (
                        remote_reading
                        and remote_reading.meter_list
                        and len(remote_reading.meter_list) > 0
                    ):
                        meter = _select_meter_data(
                            remote_reading,
                            device.device_number,
                            device.device_code,
                        )
                        if not meter:
                            _LOGGER.warning(
                                "No matching meter data for device %s/%s in contract %s on %s",
                                device.device_number,
                                device.device_code,
                                contract_id,
                                reading_date,
                            )
                            daily_readings[device.device_number] = []
                            backstream_meters[device.device_number] = False
                            backstream_totals[device.device_number] = {
                                "total_back_stream_for_period": None,
                                "total_export": None,
                            }
                            continue

                        daily_readings[device.device_number] = meter.period_consumptions
                        monthly_future_consumption = _extract_valid_future_consumption(
                            remote_reading,
                            meter,
                        )
                        if monthly_future_consumption:
                            future_consumption[device.device_number] = (
                                monthly_future_consumption
                            )
                        backstream_meters[device.device_number] = (
                            _is_backstream_meter_kind(meter.meter_kind)
                        )
                        backstream_totals[device.device_number] = (
                            _build_backstream_totals(monthly_future_consumption)
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
                        backstream_meters[device.device_number] = False
                        backstream_totals[device.device_number] = {
                            "total_back_stream_for_period": None,
                            "total_export": None,
                        }

                    # Verify today's date appears
                    await self._fetcher._verify_daily_readings_exist(
                        daily_readings,
                        localized_today.date(),
                        device,
                        contract_id,
                        None,
                        last_invoice_date,
                    )

                    today_reading_key = str(contract_id) + "-" + device.device_number
                    today_reading = self._fetcher._today_readings.get(today_reading_key)

                    if not today_reading:
                        today_reading = await self._fetcher._get_readings(
                            contract_id,
                            device.device_number,
                            device.device_code,
                            localized_today,
                            ReadingResolution.DAILY,
                            device.meter_kind,
                        )
                        if today_reading:
                            self._fetcher._today_readings[today_reading_key] = (
                                today_reading
                            )

                    # fallbacks for future consumption since IEC api is broken :/
                    if not future_consumption.get(device.device_number):
                        today_future_consumption = _extract_valid_future_consumption(
                            self._fetcher._today_readings.get(today_reading_key),
                        )
                        _LOGGER.debug(
                            "Today's future consumption extraction result: %s",
                            today_future_consumption,
                        )

                        if today_future_consumption:
                            future_consumption[device.device_number] = (
                                today_future_consumption
                            )
                            backstream_totals[device.device_number] = (
                                _build_backstream_totals(today_future_consumption)
                            )
                        else:
                            req_date = localized_today - timedelta(days=2)
                            two_days_ago_reading = await self._fetcher._get_readings(
                                contract_id,
                                device.device_number,
                                device.device_code,
                                req_date,
                                ReadingResolution.MONTHLY,
                                device.meter_kind,
                            )
                            two_days_ago_future_consumption = (
                                _extract_valid_future_consumption(
                                    two_days_ago_reading,
                                )
                            )
                            two_days_ago_meter = _select_meter_data(
                                two_days_ago_reading,
                                device.device_number,
                                device.device_code,
                            )

                            if two_days_ago_meter and not daily_readings.get(
                                device.device_number
                            ):
                                daily_readings[device.device_number] = (
                                    two_days_ago_meter.period_consumptions
                                )

                            if two_days_ago_future_consumption:
                                future_consumption[device.device_number] = (
                                    two_days_ago_future_consumption
                                )
                                backstream_totals[device.device_number] = (
                                    _build_backstream_totals(
                                        two_days_ago_future_consumption
                                    )
                                )
                            else:
                                _LOGGER.warning(
                                    "Failed fetching FutureConsumption, data in IEC API is corrupted"
                                )
                                future_consumption[device.device_number] = None
                                backstream_totals[device.device_number] = (
                                    _build_backstream_totals(None)
                                )

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
                BACKSTREAM_METERS_DICT_NAME: backstream_meters,
                BACKSTREAM_TOTALS_DICT_NAME: backstream_totals,
                STATICS_DICT_NAME: {STATIC_KWH_TARIFF: kwh_tariff},  # workaround,
                ATTRIBUTES_DICT_NAME: attributes_to_add,
                ESTIMATED_BILL_DICT_NAME: estimated_bill_dict,
            }

        # Wait for all statistics insertion tasks to complete before cleaning up shared state
        if stat_tasks:
            await asyncio.gather(*stat_tasks, return_exceptions=True)

        # Clean up per-cycle caches for next cycle
        self._fetcher.clear_per_cycle_caches()

        return data

    async def _async_update_data(
        self,
    ) -> dict[str, dict[str, Any]]:
        """Fetch data from API endpoint."""
        # Add retry logic for token operations to handle transient DNS/resolution issues
        max_retries = 2
        base_delay = 5  # Start with 5 seconds delay

        if self._first_load:
            _LOGGER.debug("Loading API token from config entry")
            for attempt in range(max_retries):
                try:
                    await self.api.load_jwt_token(
                        JWT.from_dict(self._entry_data[CONF_API_TOKEN])
                    )
                    break  # Success, exit retry loop
                except IECError as load_err:
                    if load_err.code in (400, 401):
                        # 400/401 errors indicate authentication issues (expired/invalid token)
                        # Retry once before triggering reauth flow
                        if attempt == max_retries - 1:  # Last attempt
                            _LOGGER.error(
                                "Token load failed after %d attempts with code %d: %s. "
                                "Triggering reauth flow.",
                                max_retries,
                                load_err.code,
                                load_err,
                            )
                            raise ConfigEntryAuthFailed from load_err
                        else:
                            delay = base_delay  # 5s delay before retry
                            _LOGGER.warning(
                                "Token load attempt %d failed with code %d: %s. "
                                "Retrying once in %d seconds...",
                                attempt + 1,
                                load_err.code,
                                load_err,
                                delay,
                            )
                            await asyncio.sleep(delay)
                    else:
                        # Non-auth errors don't retry
                        raise

        self._first_load = False
        try:
            _LOGGER.debug("Checking if API token needs to be refreshed")
            # First thing first, check the token and refresh if needed.
            old_token = self.api.get_token()

            for attempt in range(max_retries):
                try:
                    await self.api.check_token()
                    break  # Success, exit retry loop
                except IECError as check_err:
                    if check_err.code in (400, 401):
                        # 400/401 errors indicate authentication issues (expired/invalid token)
                        # Retry once before triggering reauth flow
                        if attempt == max_retries - 1:  # Last attempt
                            _LOGGER.error(
                                "Token check failed after %d attempts with code %d: %s. "
                                "Triggering reauth flow.",
                                max_retries,
                                check_err.code,
                                check_err,
                            )
                            raise ConfigEntryAuthFailed from check_err
                        else:
                            delay = base_delay  # 5s delay before retry
                            _LOGGER.warning(
                                "Token check attempt %d failed with code %d: %s. "
                                "Retrying once in %d seconds...",
                                attempt + 1,
                                check_err.code,
                                check_err,
                                delay,
                            )
                            await asyncio.sleep(delay)
                    else:
                        # Non-auth errors don't retry (DNS issues, etc.)
                        raise

            new_token = self.api.get_token()
            if old_token != new_token:
                _LOGGER.debug("Token refreshed")
                new_data = {**self._entry_data, CONF_API_TOKEN: new_token.to_dict()}
                self.hass.config_entries.async_update_entry(
                    entry=self._config_entry, data=new_data
                )
                self._entry_data = new_data
        except IECError as err:
            if err.code in (400, 401):
                _LOGGER.error(
                    "IEC API authentication failed with code %d: %s. "
                    "Triggering reauth flow.",
                    err.code,
                    err,
                )
            raise ConfigEntryAuthFailed from err

        try:
            return await self._update_data()
        except Exception as err:
            _LOGGER.error("Failed updating data. Exception: %s", err)
            _LOGGER.error(traceback.format_exc())
            raise UpdateFailed("Failed Updating IEC data") from err

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
                devices_by_id = await self._fetcher._get_devices_by_device_id(
                    device_number
                )

                if (
                    devices_by_id
                    and devices_by_id.counter_devices
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
                last_meter_reading = await self._fetcher._get_last_meter_reading(
                    bp_number, contract_id, device_number
                )

            if not last_meter_reading:
                _LOGGER.warning(
                    "Couldn't get Last Meter Read, WILL NOT calculate the usage part in estimated bill."
                )
                last_meter_read = None
                last_meter_read_date = datetime.now(TIMEZONE).date()
                last_invoice = EMPTY_INVOICE
            else:
                last_meter_read = last_meter_reading.reading
                last_meter_read_date = (
                    last_meter_reading.reading_date.date()
                    if last_meter_reading.reading_date
                    else datetime.now(TIMEZONE).date()
                )

            account_id = await self._get_account_id(contract_id)
            connection_size = await self._fetcher._get_connection_size(account_id)
            if connection_size:
                phase_count_str = (
                    connection_size.split("X")[0]
                    if connection_size.find("X") != -1
                    else "1"
                )
                phase_count = int(phase_count_str)

        if connection_size:
            power_size = await self._fetcher._get_power_size(connection_size)
        else:
            power_size = 0.0
            _LOGGER.warning("Couldn't get Connection Size")

        if phase_count:
            distribution_tariff = await self._fetcher._get_distribution_tariff(
                phase_count
            )
            delivery_tariff = await self._fetcher._get_delivery_tariff(phase_count)
        else:
            distribution_tariff = 0.0
            delivery_tariff = 0.0
            if connection_size:
                _LOGGER.warning("Couldn't get Phase Count")

        return _calculate_estimated_bill(
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
