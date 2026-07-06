"""Data fetcher for IEC API calls with caching.

IecDataFetcher wraps the IecClient and provides cached access to all
IEC API endpoints. Cache policies:
- Per-update-cycle caches are cleared after each update cycle.
- TTL-based caches expire after a configurable duration (default 24h).
"""

import asyncio
import logging
import socket
import time as time_module
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from aiohttp import ClientTimeout
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import aiohttp_client
from iec_api.iec_client import IecClient
from iec_api.models.device import Device, Devices
from iec_api.models.device_in import DeviceInDevice
from iec_api.models.exceptions import IECError
from iec_api.models.meter_reading import MeterReading
from iec_api.models.remote_reading import (
    PeriodConsumption,
    ReadingResolution,
    RemoteReadingResponse,
)

from .bill import _map_meter_kind_to_remote_reading_param, _select_meter_data
from .commons import find_reading_by_date

_LOGGER = logging.getLogger(__name__)

_MISSING: Any = object()
_TTL_CACHE_DURATION = timedelta(hours=24)


@dataclass
class _TTLCacheEntry:
    """A cache entry with timestamp for TTL-based invalidation."""

    value: Any
    fetched_at: float


class IecDataFetcher:
    """Wraps IecClient API calls with per-cycle and TTL caching."""

    def __init__(
        self,
        hass: HomeAssistant,
        api: IecClient,
        config_entry: ConfigEntry,
    ) -> None:
        """Initialize the data fetcher."""
        self._hass = hass
        self.api = api
        self._config_entry = config_entry

        self._today_readings: dict[str, RemoteReadingResponse] = {}
        self._devices_by_contract_id: dict[int, list[DeviceInDevice]] = {}
        self._last_meter_reading: dict[tuple[int, int], MeterReading] = {}
        self._devices_by_meter_id: dict[str, Devices] = {}
        self._delivery_tariff_by_phase: dict[int, float] = {}
        self._distribution_tariff_by_phase: dict[int, float] = {}
        self._power_size_by_connection_size: dict[str, float] = {}
        self._kwh_tariff: float | Any = _MISSING
        self._kva_tariff: float | Any = _MISSING
        self._readings: dict[tuple[int, int, int, str, str], RemoteReadingResponse] = {}
        self._default_account_id: Any = None
        self._account_id_by_contract: dict[int, Any] = {}
        self._connection_size_by_account_id: dict[Any, str] = {}
        self._cached_calculators_result: tuple[float | None, float | None] | Any = (
            _MISSING
        )
        self._api_session = aiohttp_client.async_get_clientsession(
            hass, family=socket.AF_INET
        )
        self._api_semaphore = asyncio.Semaphore(3)

    async def _api_call(self, coro):
        """Execute an API call with concurrency limiting via semaphore."""
        async with self._api_semaphore:
            return await coro

    @staticmethod
    def _ttl_cache_get(
        cache: dict, key: Any, ttl: timedelta = _TTL_CACHE_DURATION
    ) -> Any:
        """Get a value from a TTL cache if it exists and hasn't expired."""
        entry = cache.get(key)
        if entry is not None and isinstance(entry, _TTLCacheEntry):
            if time_module.monotonic() - entry.fetched_at < ttl.total_seconds():
                return entry.value
        return _MISSING

    @staticmethod
    def _ttl_cache_set(cache: dict, key: Any, value: Any) -> None:
        """Set a value in a TTL cache with the current timestamp."""
        cache[key] = _TTLCacheEntry(value=value, fetched_at=time_module.monotonic())

    async def _get_devices_by_contract_id(
        self, contract_id: int
    ) -> list[DeviceInDevice]:
        """Fetch devices for a contract, cached per cycle."""
        devices = self._devices_by_contract_id.get(contract_id, _MISSING)
        if devices is _MISSING:
            try:
                contract_id_normalized = str(int(contract_id))
                api_devices: list[Device] | None = await self._api_call(
                    self.api.get_devices(contract_id_normalized)
                )
                devices = []
                for device in api_devices or []:
                    if not device.device_number or not device.device_code:
                        _LOGGER.warning(
                            "Skipping device for contract %s due to missing "
                            "device_number or device_code: %s",
                            contract_id,
                            device,
                        )
                        continue
                    devices.append(
                        DeviceInDevice(
                            is_active=device.is_active,
                            device_type=device.device_type or 0,
                            device_number=device.device_number,
                            device_code=device.device_code,
                            meter_kind="Consumption",
                        )
                    )
                self._devices_by_contract_id[contract_id] = devices
            except IECError:
                _LOGGER.exception(
                    "Failed fetching devices by contract %s",
                    contract_id,
                )
                devices = []
        return devices or []

    async def _get_devices_by_device_id(self, meter_id: str) -> Devices | None:
        """Fetch device details by meter ID, cached per cycle."""
        devices = self._devices_by_meter_id.get(meter_id, _MISSING)
        if devices is _MISSING:
            try:
                devices = await self._api_call(
                    self.api.get_device_by_device_id(meter_id)
                )
                if devices:
                    self._devices_by_meter_id[meter_id] = devices
            except IECError:
                _LOGGER.exception(
                    "Failed fetching device details by meter id %s", meter_id
                )
        return self._devices_by_meter_id.get(meter_id)

    async def _get_last_meter_reading(
        self, bp_number: str, contract_id: int, meter_id: str | int
    ) -> MeterReading | None:
        """Fetch last meter reading, cached per cycle."""
        key = (contract_id, int(meter_id))
        last_meter_reading = self._last_meter_reading.get(key, _MISSING)
        if last_meter_reading is _MISSING:
            try:
                meter_readings = await self._api_call(
                    self.api.get_last_meter_reading(bp_number, str(contract_id))
                )

                if meter_readings and meter_readings.last_meters:
                    for reading in meter_readings.last_meters:
                        reading_meter_id = int(reading.serial_number)
                        if len(reading.meter_readings) > 0:
                            readings_list = reading.meter_readings
                            readings_list.sort(
                                key=lambda rdng: (
                                    rdng.reading_date
                                    if rdng.reading_date
                                    else datetime.min
                                ),
                                reverse=True,
                            )
                            last_meter_reading = readings_list[0]
                            _LOGGER.debug(
                                "Last Reading for contract %s, Meter %s: %s",
                                contract_id,
                                reading_meter_id,
                                last_meter_reading,
                            )
                            reading_key = (contract_id, reading_meter_id)
                            self._last_meter_reading[reading_key] = last_meter_reading
                        else:
                            _LOGGER.debug(
                                "No Reading found for contract %s, Meter %s",
                                contract_id,
                                reading_meter_id,
                            )
            except IECError:
                _LOGGER.exception(
                    "Failed fetching device details by meter id %s", meter_id
                )
        return self._last_meter_reading.get(key)

    async def _get_kwh_tariff(self) -> float:
        """Fetch kWh tariff with TTL caching and calculators fallback."""
        if self._kwh_tariff is _MISSING:
            try:
                self._kwh_tariff = await self._api_call(self.api.get_kwh_tariff())
            except IECError:
                _LOGGER.exception("Failed fetching kWh Tariff")
            except Exception:
                _LOGGER.exception("Unexpected error fetching kWh Tariff")

            if (
                self._kwh_tariff is _MISSING
                or not self._kwh_tariff
                or self._kwh_tariff == 0.0
            ):
                kwh_fallback, _ = await self._fetch_tariffs_from_calculators()
                if kwh_fallback and kwh_fallback > 0:
                    _LOGGER.debug(
                        "Using fallback kWh tariff from calculators API: %s",
                        kwh_fallback,
                    )
                    self._kwh_tariff = kwh_fallback
                elif self._kwh_tariff is _MISSING:
                    self._kwh_tariff = 0.0
        return self._kwh_tariff or 0.0

    async def _get_kva_tariff(self) -> float:
        """Fetch kVA tariff with TTL caching and calculators fallback."""
        if self._kva_tariff is _MISSING:
            try:
                self._kva_tariff = await self._api_call(self.api.get_kva_tariff())
            except IECError:
                _LOGGER.exception("Failed fetching KVA Tariff from IEC API")
            except Exception:
                _LOGGER.exception("Unexpected error fetching KVA Tariff")

            if (
                self._kva_tariff is _MISSING
                or not self._kva_tariff
                or self._kva_tariff == 0.0
            ):
                _, kva_fallback = await self._fetch_tariffs_from_calculators()
                if kva_fallback and kva_fallback > 0:
                    _LOGGER.debug(
                        "Using fallback kVA tariff from calculators API: %s",
                        kva_fallback,
                    )
                    self._kva_tariff = kva_fallback
                elif self._kva_tariff is _MISSING:
                    self._kva_tariff = 0.0
        return self._kva_tariff or 0.0

    async def _fetch_tariffs_from_calculators(
        self,
    ) -> tuple[float | None, float | None]:
        """Fetch tariffs from IEC calculators endpoints as a fallback.

        Returns: tuple of (kwh_home_rate, kva_rate), each may be None if not found.
        Results are cached for the duration of one update cycle.
        """
        if self._cached_calculators_result is not _MISSING:
            return self._cached_calculators_result

        async with self._api_semaphore:
            session = aiohttp_client.async_get_clientsession(
                self._hass, family=socket.AF_INET
            )
            kwh_tariff: float | None = None
            kva_tariff: float | None = None

            try:
                async with session.get(
                    "https://iecapi.iec.co.il/api/content/he-IL/calculators/period",
                    timeout=ClientTimeout(total=30),
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
                            "Fetched fallback tariffs from calculators/period: "
                            "homeRate=%s, kvaRate=%s",
                            kwh_tariff,
                            kva_tariff,
                        )
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug(
                    "Failed fetching fallback tariffs from calculators/period: %s",
                    err,
                )

            if kwh_tariff is None:
                try:
                    async with session.get(
                        "https://iecapi.iec.co.il/api/content/he-IL/calculators/gadget",
                        timeout=ClientTimeout(total=30),
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json(content_type=None)
                            rates = data.get("gadget_Calculator_Rates") or {}
                            kwh_val = rates.get("homeRate")
                            if isinstance(kwh_val, (int, float)):
                                kwh_tariff = float(kwh_val)
                            _LOGGER.debug(
                                "Fetched fallback kWh tariff from calculators/gadget: "
                                "homeRate=%s",
                                kwh_tariff,
                            )
                except Exception as err:  # noqa: BLE001
                    _LOGGER.debug(
                        "Failed fetching fallback kWh tariff from calculators/gadget: %s",
                        err,
                    )

            self._cached_calculators_result = (kwh_tariff, kva_tariff)
            return kwh_tariff, kva_tariff

    async def _get_delivery_tariff(self, phase) -> float:
        """Fetch delivery tariff by phase, cached per cycle."""
        delivery_tariff = self._delivery_tariff_by_phase.get(phase, _MISSING)
        if delivery_tariff is _MISSING:
            try:
                delivery_tariff = await self._api_call(
                    self.api.get_delivery_tariff(phase)
                )
                self._delivery_tariff_by_phase[phase] = delivery_tariff
            except IECError:
                _LOGGER.exception("Failed fetching Delivery Tariff by phase %s", phase)
                delivery_tariff = 0.0
        return delivery_tariff or 0.0

    async def _get_distribution_tariff(self, phase) -> float:
        """Fetch distribution tariff by phase, cached per cycle."""
        distribution_tariff = self._distribution_tariff_by_phase.get(phase, _MISSING)
        if distribution_tariff is _MISSING:
            try:
                distribution_tariff = await self._api_call(
                    self.api.get_distribution_tariff(phase)
                )
                self._distribution_tariff_by_phase[phase] = distribution_tariff
            except IECError:
                _LOGGER.exception(
                    "Failed fetching Distribution Tariff by phase %s", phase
                )
                distribution_tariff = 0.0
        return distribution_tariff or 0.0

    async def _get_connection_size(self, account_id) -> str | None:
        """Fetch connection size by account ID, cached per cycle."""
        if not account_id:
            return None

        connection_size = self._connection_size_by_account_id.get(account_id, _MISSING)
        if connection_size is not _MISSING:
            return connection_size

        try:
            connection_size = await self._api_call(
                self.api.get_masa_connection_size_from_masa(
                    str(account_id) if account_id else None
                )
            )
            self._connection_size_by_account_id[account_id] = connection_size
        except IECError:
            _LOGGER.exception(
                "Failed fetching Masa Connection Size for account %s", account_id
            )
            return None

        return connection_size

    async def _get_power_size(self, connection_size) -> float:
        """Fetch power size by connection size, cached per cycle."""
        power_size = self._power_size_by_connection_size.get(connection_size, _MISSING)
        if power_size is _MISSING:
            try:
                power_size = await self._api_call(
                    self.api.get_power_size(connection_size)
                )
                self._power_size_by_connection_size[connection_size] = power_size
            except IECError:
                _LOGGER.exception(
                    "Failed fetching Power Size by Connection Size %s",
                    connection_size,
                )
                power_size = 0.0
        return power_size or 0.0

    async def _get_readings(
        self,
        contract_id: int,
        device_id: str | int,
        device_code: str | int,
        reading_date: datetime,
        resolution: ReadingResolution,
        meter_kind: str,
        last_invoice_date: datetime | None = None,
    ) -> RemoteReadingResponse | None:
        """Fetch remote readings, cached per cycle with a composite key."""
        mapped_meter_kind = _map_meter_kind_to_remote_reading_param(meter_kind)
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

        key = (
            contract_id,
            int(device_id),
            int(device_code),
            mapped_meter_kind,
            date_key,
        )
        reading = self._readings.get(key, _MISSING)
        if reading is _MISSING:
            try:
                reading = await self._api_call(
                    self.api.get_remote_reading(
                        mapped_meter_kind,
                        str(device_id),
                        int(device_code),
                        last_invoice_date if last_invoice_date else reading_date,
                        reading_date,
                        resolution,
                        str(contract_id),
                    )
                )
                if reading:
                    self._readings[key] = reading
            except IECError:
                _LOGGER.exception(
                    "Failed fetching reading for Contract: %s, "
                    "date: %s, resolution: %s",
                    contract_id,
                    reading_date.strftime("%d-%m-%Y"),
                    resolution,
                )
        return reading

    async def _verify_daily_readings_exist(
        self,
        daily_readings: dict[str, list],
        desired_date: date,
        device: DeviceInDevice,
        contract_id: int,
        prefetched_reading: RemoteReadingResponse | None = None,
        last_invoice_date: datetime | None = None,
    ):
        """Verify daily readings exist and fetch if missing."""
        if not device.device_number:
            return

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
                "Daily reading for date: %s is missing, calculating manually",
                desired_date.strftime("%Y-%m-%d"),
            )
            readings = prefetched_reading
            if not readings:
                readings = await self._get_readings(
                    contract_id,
                    device.device_number,
                    device.device_code,
                    datetime.fromordinal(desired_date.toordinal()),
                    ReadingResolution.DAILY,
                    device.meter_kind,
                    last_invoice_date,
                )
            else:
                _LOGGER.debug(
                    "Daily reading for date: %s - using existing prefetched readings",
                    desired_date.strftime("%Y-%m-%d"),
                )

            matched_meter = _select_meter_data(
                readings,
                device.device_number,
                device.device_code,
            )
            if matched_meter:
                daily_readings[device.device_number] += (
                    matched_meter.period_consumptions
                )

                daily_readings[device.device_number] = list(
                    dict.fromkeys(daily_readings[device.device_number])
                )

                daily_readings[device.device_number].sort(key=lambda x: x.interval)

                desired_date_reading = next(
                    filter(
                        lambda reading: reading.interval.date() == desired_date,
                        matched_meter.period_consumptions,
                    ),
                    None,
                )
                if (
                    desired_date_reading is None
                    or desired_date_reading.consumption <= 0
                ):
                    _LOGGER.debug(
                        "Couldn't find daily reading for: %s",
                        desired_date.strftime("%Y-%m-%d"),
                    )
                else:
                    daily_readings[device.device_number].append(
                        PeriodConsumption(
                            status=0,
                            interval=datetime.combine(
                                desired_date, datetime.min.time()
                            ),
                            consumption=desired_date_reading.consumption,
                            back_stream=0,
                        )
                    )
        else:
            _LOGGER.debug(
                "Daily reading for date: %s is present: %s",
                daily_reading.interval.strftime("%Y-%m-%d"),
                daily_reading.consumption,
            )

    def clear_per_cycle_caches(self) -> None:
        """Clear all per-update-cycle caches. Called after each update cycle."""
        self._today_readings = {}
        self._devices_by_contract_id = {}
        self._readings = {}
        self._last_meter_reading = {}
        self._kwh_tariff = _MISSING
        self._kva_tariff = _MISSING
        self._cached_calculators_result = _MISSING

    @staticmethod
    def _normalize_bp_number(bp_number: str | None) -> str | None:
        """Normalize BP number to integer string."""
        if not bp_number:
            return None
        try:
            return str(int(bp_number))
        except ValueError:
            return bp_number
