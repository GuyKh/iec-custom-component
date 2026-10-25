"""Tests for IecApiCoordinator._backfill_missing_recent_days.

IEC publishes a day's smart-meter readings with a lag of a day or two, and can
be slower still. This backfill loop re-checks the past week each cycle for any
day that's still missing data, without re-fetching days that already have it,
without reaching back before the period the primary fetch covered, and without
asking IEC for a day Home Assistant already recorded hourly statistics for.
"""

from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from iec_api.models.remote_reading import PeriodConsumption

from custom_components.iec.commons import TIMEZONE
from custom_components.iec.coordinator import (
    _DAILY_BACKFILL_LOOKBACK_DAYS,
    IecApiCoordinator,
)

DEVICE_NUMBER = "12345"
WINDOW_START = date(2026, 8, 1)
TODAY = datetime(2026, 8, 9, 12, 0, tzinfo=TIMEZONE)


def _reading(days_ago: int, today: datetime) -> PeriodConsumption:
    interval = (today - timedelta(days=days_ago)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return PeriodConsumption(interval=interval, consumption=1.0)


def _reading_on(day: date, consumption: float = 1.0) -> PeriodConsumption:
    return PeriodConsumption(
        interval=datetime.combine(day, datetime.min.time(), tzinfo=TIMEZONE),
        consumption=consumption,
    )


def _make_coordinator(iec_has_data_through: date | None = None) -> IecApiCoordinator:
    """Build a coordinator without running __init__ (no Home Assistant needed).

    ``iec_has_data_through`` simulates which days IEC has actually published:
    a fetch for a day at or before it appends a reading (like the real fetcher
    would), a fetch for a later day finds nothing. Defaults to "everything IEC
    is asked for is available" so existing multi-day tests don't need to care.
    """

    async def verify_daily_readings_exist(daily_readings, backfill_date, *_args):
        if iec_has_data_through is None or backfill_date <= iec_has_data_through:
            daily_readings[DEVICE_NUMBER].append(_reading_on(backfill_date))

    coordinator = object.__new__(IecApiCoordinator)
    coordinator._fetcher = SimpleNamespace(
        _verify_daily_readings_exist=AsyncMock(
            side_effect=verify_daily_readings_exist
        )
    )
    coordinator.hass = SimpleNamespace()
    return coordinator


def _device() -> SimpleNamespace:
    return SimpleNamespace(device_number=DEVICE_NUMBER)


def _recorded(days: dict[date, PeriodConsumption] | None = None):
    """Patch the recorder read-back with a fixed date -> day-total mapping."""
    return patch(
        "custom_components.iec.coordinator.async_get_recorded_daily_totals",
        AsyncMock(return_value=dict(days or {})),
    )


async def _backfill(
    coordinator: IecApiCoordinator,
    daily_readings: dict[str, list[PeriodConsumption]],
    localized_today: datetime = TODAY,
    window_start: date = WINDOW_START,
) -> None:
    await coordinator._backfill_missing_recent_days(
        daily_readings,
        _device(),
        contract_id=1,
        last_invoice_date=None,
        localized_today=localized_today,
        window_start=window_start,
    )


def _requested_dates(coordinator: IecApiCoordinator) -> list[date]:
    return [
        call.args[1]
        for call in coordinator._fetcher._verify_daily_readings_exist.await_args_list
    ]


def _dates_in(daily_readings: dict[str, list[PeriodConsumption]]) -> set[date]:
    return {reading.interval.date() for reading in daily_readings[DEVICE_NUMBER]}


def _whole_window() -> list[date]:
    return [
        (TODAY - timedelta(days=days_ago)).date()
        for days_ago in range(1, _DAILY_BACKFILL_LOOKBACK_DAYS + 1)
    ]


@pytest.mark.asyncio
class TestBackfillMissingRecentDays:
    async def test_skips_days_that_already_have_data(self):
        coordinator = _make_coordinator()
        # Every day in the lookback window already has an entry.
        daily_readings = {
            DEVICE_NUMBER: [
                _reading(days_ago, TODAY)
                for days_ago in range(1, _DAILY_BACKFILL_LOOKBACK_DAYS + 1)
            ]
        }

        with _recorded() as recorder_lookup:
            await _backfill(coordinator, daily_readings)

        coordinator._fetcher._verify_daily_readings_exist.assert_not_awaited()
        # Nothing was missing, so the recorder isn't queried either.
        recorder_lookup.assert_not_awaited()

    async def test_backfills_only_missing_days(self):
        coordinator = _make_coordinator()
        # Only 2 and 3 days ago already have data; the rest of the week is missing.
        daily_readings = {DEVICE_NUMBER: [_reading(2, TODAY), _reading(3, TODAY)]}

        with _recorded():
            await _backfill(coordinator, daily_readings)

        expected_missing = {
            (TODAY - timedelta(days=days_ago)).date() for days_ago in (1, 4, 5, 6, 7)
        }
        assert set(_requested_dates(coordinator)) == expected_missing

    async def test_empty_daily_readings_backfills_whole_window(self):
        coordinator = _make_coordinator()

        with _recorded():
            await _backfill(coordinator, {DEVICE_NUMBER: []})

        assert set(_requested_dates(coordinator)) == set(_whole_window())

    async def test_does_not_reach_before_the_fetched_period(self):
        """Early in the month the window predates the MONTHLY fetch's start.

        Those days aren't part of any sensor's data, so they must not trigger
        a DAILY call every cycle.
        """
        coordinator = _make_coordinator()

        with _recorded():
            await _backfill(
                coordinator,
                {DEVICE_NUMBER: []},
                localized_today=datetime(2026, 8, 2, 12, 0, tzinfo=TIMEZONE),
            )

        assert _requested_dates(coordinator) == [date(2026, 8, 1)]

    async def test_stops_at_the_oldest_day_iec_has_not_published(self):
        """IEC publishes days in order, so once the oldest missing day comes back
        empty, newer days are certain to be empty too and are left for later.
        """
        oldest_missing = (TODAY - timedelta(days=7)).date()
        # IEC has nothing at or after the oldest missing day.
        coordinator = _make_coordinator(
            iec_has_data_through=oldest_missing - timedelta(days=1)
        )

        with _recorded():
            await _backfill(coordinator, {DEVICE_NUMBER: []})

        assert _requested_dates(coordinator) == [oldest_missing]

    async def test_requests_oldest_missing_day_first(self):
        """Dates are walked oldest to newest, matching IEC's publish order."""
        coordinator = _make_coordinator()

        with _recorded():
            await _backfill(coordinator, {DEVICE_NUMBER: []})

        assert _requested_dates(coordinator) == sorted(_whole_window())


@pytest.mark.asyncio
class TestBackfillUsesRecordedStatistics:
    """Days Home Assistant already recorded are rebuilt, not re-fetched."""

    async def test_recorded_days_are_not_fetched_from_iec(self):
        coordinator = _make_coordinator()
        recorded_day = (TODAY - timedelta(days=4)).date()

        with _recorded({recorded_day: _reading_on(recorded_day, 7.5)}):
            await _backfill(coordinator, {DEVICE_NUMBER: []})

        assert recorded_day not in _requested_dates(coordinator)
        assert set(_requested_dates(coordinator)) == set(_whole_window()) - {
            recorded_day
        }

    async def test_recorded_days_reach_daily_readings_in_order(self):
        """A recorded day still has to land in the sensor-facing data."""
        coordinator = _make_coordinator()
        recorded = {day: _reading_on(day, 7.5) for day in _whole_window()}
        daily_readings = {DEVICE_NUMBER: []}

        with _recorded(recorded):
            await _backfill(coordinator, daily_readings)

        coordinator._fetcher._verify_daily_readings_exist.assert_not_awaited()
        assert _dates_in(daily_readings) == set(_whole_window())
        intervals = [reading.interval for reading in daily_readings[DEVICE_NUMBER]]
        assert intervals == sorted(intervals)
        assert all(
            reading.consumption == 7.5 for reading in daily_readings[DEVICE_NUMBER]
        )

    async def test_only_the_missing_span_is_read_back(self):
        """The recorder is asked for the missing days, not the whole window."""
        coordinator = _make_coordinator()
        # Everything but 2 and 5 days ago is already in the period response.
        daily_readings = {
            DEVICE_NUMBER: [_reading(days_ago, TODAY) for days_ago in (1, 3, 4, 6, 7)]
        }

        with _recorded() as recorder_lookup:
            await _backfill(coordinator, daily_readings)

        _hass, device_number, start, end = recorder_lookup.await_args.args
        assert device_number == DEVICE_NUMBER
        assert start.date() == (TODAY - timedelta(days=5)).date()
        assert end.date() == (TODAY - timedelta(days=1)).date()
        assert (start.hour, end.hour) == (0, 0)
