"""Tests for IecApiCoordinator._backfill_missing_recent_days.

IEC can take several days to publish a given day's smart-meter readings.
This backfill loop re-checks the past week each cycle for any day that's
still missing data, without re-fetching days that already have it.
"""

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from iec_api.models.remote_reading import PeriodConsumption

from custom_components.iec.commons import TIMEZONE
from custom_components.iec.coordinator import IecApiCoordinator

DEVICE_NUMBER = "12345"


def _make_coordinator() -> IecApiCoordinator:
    """Build a coordinator without running __init__ (no Home Assistant needed)."""
    coordinator = object.__new__(IecApiCoordinator)
    coordinator._fetcher = SimpleNamespace(
        _verify_daily_readings_exist=AsyncMock()
    )
    return coordinator


def _device() -> SimpleNamespace:
    return SimpleNamespace(device_number=DEVICE_NUMBER)


def _reading(days_ago: int, today: datetime) -> PeriodConsumption:
    interval = (today - timedelta(days=days_ago)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return PeriodConsumption(interval=interval, consumption=1.0)


@pytest.mark.asyncio
class TestBackfillMissingRecentDays:
    async def test_skips_days_that_already_have_data(self):
        coordinator = _make_coordinator()
        today = datetime(2026, 8, 9, 12, 0, tzinfo=TIMEZONE)
        # Every day in the lookback window already has an entry.
        daily_readings = {
            DEVICE_NUMBER: [_reading(days_ago, today) for days_ago in range(1, 8)]
        }

        await coordinator._backfill_missing_recent_days(
            daily_readings, _device(), contract_id=1, last_invoice_date=None,
            localized_today=today,
        )

        coordinator._fetcher._verify_daily_readings_exist.assert_not_awaited()

    async def test_backfills_only_missing_days(self):
        coordinator = _make_coordinator()
        today = datetime(2026, 8, 9, 12, 0, tzinfo=TIMEZONE)
        # Only 2 and 3 days ago already have data; the rest of the week is missing.
        daily_readings = {
            DEVICE_NUMBER: [_reading(2, today), _reading(3, today)]
        }

        await coordinator._backfill_missing_recent_days(
            daily_readings, _device(), contract_id=1, last_invoice_date=None,
            localized_today=today,
        )

        calls = coordinator._fetcher._verify_daily_readings_exist.await_args_list
        requested_dates = {call.args[1] for call in calls}
        expected_missing = {
            (today - timedelta(days=days_ago)).date()
            for days_ago in (1, 4, 5, 6, 7)
        }
        assert requested_dates == expected_missing
        assert len(calls) == 5

    async def test_empty_daily_readings_backfills_whole_week(self):
        coordinator = _make_coordinator()
        today = datetime(2026, 8, 9, 12, 0, tzinfo=TIMEZONE)
        daily_readings = {DEVICE_NUMBER: []}

        await coordinator._backfill_missing_recent_days(
            daily_readings, _device(), contract_id=1, last_invoice_date=None,
            localized_today=today,
        )

        assert coordinator._fetcher._verify_daily_readings_exist.await_count == 7
