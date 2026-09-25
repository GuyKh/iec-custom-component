"""Tests for IecDataFetcher._verify_daily_readings_exist.

These pin down the fix for stale "today" sensors: the function must always
recompute today's entry as the SUM of today's DAILY-resolution (15-minute)
readings, refreshed every hourly coordinator cycle, rather than only filling
in a missing entry with a single unsummed reading.
"""

import asyncio
from datetime import date, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from iec_api.models.device_in import DeviceInDevice
from iec_api.models.remote_reading import (
    MeterReadingData,
    PeriodConsumption,
    RemoteReadingResponse,
)

from custom_components.iec.commons import TIMEZONE
from custom_components.iec.data_fetcher import IecDataFetcher

DEVICE_NUMBER = "12345"
DEVICE_CODE = "1"


def _make_fetcher() -> IecDataFetcher:
    """Build a fetcher without running __init__ (no Home Assistant needed)."""
    fetcher = object.__new__(IecDataFetcher)
    fetcher.api = MagicMock()
    fetcher.api.get_remote_reading = AsyncMock()
    fetcher._readings = {}
    fetcher._api_semaphore = asyncio.Semaphore(3)
    return fetcher


def _make_device() -> DeviceInDevice:
    return DeviceInDevice(
        is_active=True,
        device_type=3,
        device_number=DEVICE_NUMBER,
        device_code=DEVICE_CODE,
        meter_kind="Consumption",
    )


def _reading(interval: datetime, consumption: float, back_stream: float = 0.0):
    return PeriodConsumption(
        interval=interval, consumption=consumption, back_stream=back_stream
    )


def _remote_response(period_consumptions: list[PeriodConsumption]):
    return RemoteReadingResponse(
        report_status=0,
        meter_list=[
            MeterReadingData(
                meter_serial=DEVICE_NUMBER,
                meter_code=DEVICE_CODE,
                period_consumptions=period_consumptions,
            )
        ],
    )


@pytest.mark.asyncio
class TestVerifyDailyReadingsExist:
    async def test_replaces_stale_entry_with_summed_total(self):
        fetcher = _make_fetcher()
        device = _make_device()
        today = date(2026, 8, 7)

        stale_entry = _reading(
            datetime(2026, 8, 7, 0, 0, tzinfo=TIMEZONE), consumption=0.05
        )
        daily_readings = {DEVICE_NUMBER: [stale_entry]}

        prefetched = _remote_response(
            [
                _reading(datetime(2026, 8, 7, 0, 0, tzinfo=TIMEZONE), 0.3),
                _reading(datetime(2026, 8, 7, 0, 15, tzinfo=TIMEZONE), 0.4),
                _reading(datetime(2026, 8, 7, 0, 30, tzinfo=TIMEZONE), 0.5),
            ]
        )

        await fetcher._verify_daily_readings_exist(
            daily_readings, today, device, contract_id=1, prefetched_reading=prefetched
        )

        entries = daily_readings[DEVICE_NUMBER]
        assert len(entries) == 1
        assert entries[0].consumption == pytest.approx(1.2)
        assert entries[0].interval.date() == today
        fetcher.api.get_remote_reading.assert_not_called()

    async def test_appends_summed_entry_when_missing(self):
        fetcher = _make_fetcher()
        device = _make_device()
        today = date(2026, 8, 7)
        daily_readings = {DEVICE_NUMBER: []}

        prefetched = _remote_response(
            [
                _reading(datetime(2026, 8, 7, 0, 0, tzinfo=TIMEZONE), 0.1),
                _reading(datetime(2026, 8, 7, 0, 15, tzinfo=TIMEZONE), 0.2),
            ]
        )

        await fetcher._verify_daily_readings_exist(
            daily_readings, today, device, contract_id=1, prefetched_reading=prefetched
        )

        entries = daily_readings[DEVICE_NUMBER]
        assert len(entries) == 1
        assert entries[0].consumption == pytest.approx(0.3)

    async def test_leaves_daily_readings_unchanged_when_no_data_yet(self):
        fetcher = _make_fetcher()
        device = _make_device()
        today = date(2026, 8, 7)
        existing = [_reading(datetime(2026, 8, 6, 0, 0, tzinfo=TIMEZONE), 5.0)]
        daily_readings = {DEVICE_NUMBER: list(existing)}

        prefetched = _remote_response([])

        await fetcher._verify_daily_readings_exist(
            daily_readings, today, device, contract_id=1, prefetched_reading=prefetched
        )

        assert daily_readings[DEVICE_NUMBER] == existing

    async def test_fetches_internally_when_not_prefetched(self):
        fetcher = _make_fetcher()
        device = _make_device()
        today = date(2026, 8, 7)
        daily_readings = {DEVICE_NUMBER: []}

        fetcher.api.get_remote_reading.return_value = _remote_response(
            [_reading(datetime(2026, 8, 7, 0, 0, tzinfo=TIMEZONE), 1.5)]
        )

        await fetcher._verify_daily_readings_exist(
            daily_readings, today, device, contract_id=1
        )

        fetcher.api.get_remote_reading.assert_awaited_once()
        assert daily_readings[DEVICE_NUMBER][0].consumption == pytest.approx(1.5)

    async def test_sums_backstream_independently_of_consumption(self):
        fetcher = _make_fetcher()
        device = _make_device()
        today = date(2026, 8, 7)
        daily_readings = {DEVICE_NUMBER: []}

        prefetched = _remote_response(
            [
                _reading(
                    datetime(2026, 8, 7, 0, 0, tzinfo=TIMEZONE), 0.2, back_stream=1.0
                ),
                _reading(
                    datetime(2026, 8, 7, 0, 15, tzinfo=TIMEZONE), 0.3, back_stream=2.0
                ),
            ]
        )

        await fetcher._verify_daily_readings_exist(
            daily_readings, today, device, contract_id=1, prefetched_reading=prefetched
        )

        entry = daily_readings[DEVICE_NUMBER][0]
        assert entry.consumption == pytest.approx(0.5)
        assert entry.back_stream == pytest.approx(3.0)
