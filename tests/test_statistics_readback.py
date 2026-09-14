"""Tests for statistics.async_get_recorded_daily_totals.

The backfill loop asks Home Assistant what it already recorded before asking
IEC. This reads back the hourly statistics this integration writes and folds
them into day totals - but only for days the recorder covers in full, since a
day with missing hours is one IEC was still publishing and must be re-fetched.
"""

from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from custom_components.iec.commons import TIMEZONE
from custom_components.iec.statistics import async_get_recorded_daily_totals

DEVICE_NUMBER = "12345"
CONSUMPTION_STATISTIC_ID = f"iec:iec_meter_{DEVICE_NUMBER}_energy_consumption"
PRODUCTION_STATISTIC_ID = f"iec:iec_meter_{DEVICE_NUMBER}_energy_production"
DAY = date(2026, 8, 5)


def _hourly_rows(day: date, hours: int, state: float) -> list[dict]:
    """Statistics rows as the recorder returns them: `start` is a timestamp."""
    day_start = datetime.combine(day, datetime.min.time(), tzinfo=TIMEZONE)
    return [
        {"start": (day_start + timedelta(hours=hour)).timestamp(), "state": state}
        for hour in range(hours)
    ]


def _recorder(stats: dict[str, list[dict]] | Exception):
    """Patch the recorder so the helper's executor job returns `stats`."""

    async def _run_executor_job(func, *args):
        if isinstance(stats, Exception):
            raise stats
        return func(*args)

    return (
        patch(
            "custom_components.iec.statistics.get_instance",
            return_value=SimpleNamespace(async_add_executor_job=_run_executor_job),
        ),
        patch(
            "custom_components.iec.statistics.statistics_during_period",
            MagicMock(return_value={} if isinstance(stats, Exception) else stats),
        ),
    )


async def _read_back(stats: dict[str, list[dict]] | Exception, day: date = DAY):
    instance_patch, period_patch = _recorder(stats)
    with instance_patch, period_patch as statistics_during_period:
        totals = await async_get_recorded_daily_totals(
            MagicMock(),
            DEVICE_NUMBER,
            datetime.combine(day, datetime.min.time(), tzinfo=TIMEZONE),
            datetime.combine(
                day + timedelta(days=1), datetime.min.time(), tzinfo=TIMEZONE
            ),
        )
    return totals, statistics_during_period


@pytest.mark.asyncio
class TestRecordedDailyTotals:
    async def test_a_fully_recorded_day_is_returned_as_a_day_total(self):
        totals, _ = await _read_back(
            {CONSUMPTION_STATISTIC_ID: _hourly_rows(DAY, hours=24, state=0.5)}
        )

        assert set(totals) == {DAY}
        assert totals[DAY].consumption == pytest.approx(12.0)
        assert totals[DAY].interval.date() == DAY
        assert totals[DAY].interval.hour == 0

    async def test_production_is_folded_into_back_stream(self):
        totals, _ = await _read_back(
            {
                CONSUMPTION_STATISTIC_ID: _hourly_rows(DAY, hours=24, state=0.5),
                PRODUCTION_STATISTIC_ID: _hourly_rows(DAY, hours=24, state=0.25),
            }
        )

        assert totals[DAY].consumption == pytest.approx(12.0)
        assert totals[DAY].back_stream == pytest.approx(6.0)

    async def test_a_day_with_missing_hours_is_left_to_be_fetched(self):
        totals, _ = await _read_back(
            {CONSUMPTION_STATISTIC_ID: _hourly_rows(DAY, hours=23, state=0.5)}
        )

        assert totals == {}

    async def test_a_day_the_recorder_knows_nothing_about_is_absent(self):
        totals, _ = await _read_back({})

        assert totals == {}

    async def test_hourly_statistics_are_requested_for_the_meter_only(self):
        _, statistics_during_period = await _read_back(
            {CONSUMPTION_STATISTIC_ID: _hourly_rows(DAY, hours=24, state=0.5)}
        )

        _hass, start, end, statistic_ids, period, _units, types = (
            statistics_during_period.call_args.args
        )
        assert statistic_ids == {CONSUMPTION_STATISTIC_ID, PRODUCTION_STATISTIC_ID}
        assert period == "hour"
        assert types == {"state"}
        assert (start.date(), end.date()) == (DAY, DAY + timedelta(days=1))

    async def test_a_recorder_failure_falls_back_to_fetching(self):
        """Without a usable recorder the caller must still query IEC."""
        totals, _ = await _read_back(RuntimeError("no recorder"))

        assert totals == {}

    async def test_a_dst_short_day_is_complete_with_23_hours(self):
        """Israel's spring-forward day has 23 local hours, not 24."""
        dst_day = date(2026, 3, 27)

        totals, _ = await _read_back(
            {CONSUMPTION_STATISTIC_ID: _hourly_rows(dst_day, hours=23, state=1.0)},
            day=dst_day,
        )

        assert set(totals) == {dst_day}
        assert totals[dst_day].consumption == pytest.approx(23.0)
