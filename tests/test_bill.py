"""Tests for bill.py pure functions."""

from datetime import date, datetime
from unittest.mock import MagicMock

import pytest
from freezegun import freeze_time

from custom_components.iec.bill import (
    _build_backstream_totals,
    _calculate_estimated_bill,
    _extract_valid_future_consumption,
    _future_consumption_candidate_dates,
    _get_invoice_reading_dates,
    _is_backstream_meter_kind,
    _map_meter_kind_to_remote_reading_param,
    _needs_future_consumption_fallback,
    _parse_invoice_last_date,
    _select_meter_data,
)
from custom_components.iec.const import EMPTY_INVOICE
from iec_api.models.remote_reading import (
    FutureConsumptionInfo,
    MeterReadingData,
    ReadingResolution,
)


class TestIsBackstreamMeterKind:
    def test_int_2_returns_true(self):
        assert _is_backstream_meter_kind(2) is True

    def test_int_3_returns_false(self):
        assert _is_backstream_meter_kind(3) is False

    def test_string_backstream_returns_true(self):
        assert _is_backstream_meter_kind("BackStream") is True

    def test_string_backstream_canonical_returns_true(self):
        assert _is_backstream_meter_kind("Backstream") is True

    def test_string_consumption_returns_false(self):
        assert _is_backstream_meter_kind("Consumption") is False

    def test_string_hebrew_returns_true(self):
        assert _is_backstream_meter_kind("דו כיווני") is True

    def test_string_hebrew_hyphen_returns_true(self):
        assert _is_backstream_meter_kind("דו-כיווני") is True

    def test_none_returns_false(self):
        assert _is_backstream_meter_kind(None) is False

    def test_emptystring_returns_false(self):
        assert _is_backstream_meter_kind("") is False

    def test_string_2_returns_true(self):
        assert _is_backstream_meter_kind("2") is True

    def test_enum_like_with_value_2(self):
        obj = MagicMock()
        obj.value = 2
        assert _is_backstream_meter_kind(obj) is True

    def test_enum_like_with_value_3(self):
        obj = MagicMock()
        obj.value = 3
        assert _is_backstream_meter_kind(obj) is False


class TestMapMeterKind:
    def test_consumption_hebrew(self):
        assert _map_meter_kind_to_remote_reading_param("צריכה") == "Consumption"

    def test_backstream_hebrew(self):
        assert _map_meter_kind_to_remote_reading_param("דו כיווני") == "Backstream"

    def test_backstream_hebrew_hyphen(self):
        assert _map_meter_kind_to_remote_reading_param("דו-כיווני") == "Backstream"

    def test_none_returns_empty(self):
        assert _map_meter_kind_to_remote_reading_param(None) == ""

    def test_english_identity(self):
        assert _map_meter_kind_to_remote_reading_param("Consumption") == "Consumption"

    def test_english_backstream(self):
        assert _map_meter_kind_to_remote_reading_param("BackStream") == "Backstream"

    def test_unknown_returns_consumption(self):
        assert _map_meter_kind_to_remote_reading_param("SomeKind") == "Consumption"

    def test_int_2_returns_backstream(self):
        assert _map_meter_kind_to_remote_reading_param(2) == "Backstream"

    def test_int_1_returns_consumption(self):
        assert _map_meter_kind_to_remote_reading_param(1) == "Consumption"

    def test_int_3_returns_consumption(self):
        assert _map_meter_kind_to_remote_reading_param(3) == "Consumption"

    def test_string_2_returns_backstream(self):
        assert _map_meter_kind_to_remote_reading_param("2") == "Backstream"

    def test_string_1_returns_consumption(self):
        assert _map_meter_kind_to_remote_reading_param("1") == "Consumption"

    def test_enum_like(self):
        obj = MagicMock()
        obj.value = "צריכה"
        assert _map_meter_kind_to_remote_reading_param(obj) == "Consumption"

    def test_enum_like_with_numeric_value(self):
        obj = MagicMock()
        obj.value = 2
        assert _map_meter_kind_to_remote_reading_param(obj) == "Backstream"


class TestBuildBackstreamTotals:
    def test_none_input(self):
        result = _build_backstream_totals(None)
        assert result == {"total_back_stream_for_period": None, "total_export": None}

    def test_with_future_info(self):
        info = MagicMock(spec=FutureConsumptionInfo)
        info.future_back_stream = 100.0
        info.total_export = 200.0
        result = _build_backstream_totals(info)
        assert result == {"total_back_stream_for_period": 100.0, "total_export": 200.0}


class TestNeedsFutureConsumptionFallback:
    def _future_info(self, consumption=100.0):
        info = MagicMock(spec=FutureConsumptionInfo)
        info.future_consumption = consumption
        return info

    def test_missing_future_consumption_returns_true(self):
        assert _needs_future_consumption_fallback({}, {}, {}, "m1") is True

    def test_none_future_consumption_returns_true(self):
        assert _needs_future_consumption_fallback({"m1": None}, {}, {}, "m1") is True

    def test_consumption_meter_with_data_returns_false(self):
        future_consumption = {"m1": self._future_info()}
        assert (
            _needs_future_consumption_fallback(
                future_consumption,
                {"m1": False},
                {"m1": {"total_export": 0.0}},
                "m1",
            )
            is False
        )

    def test_backstream_meter_zero_export_returns_true(self):
        future_consumption = {"m1": self._future_info()}
        assert (
            _needs_future_consumption_fallback(
                future_consumption,
                {"m1": True},
                {"m1": {"total_export": 0.0}},
                "m1",
            )
            is True
        )

    def test_backstream_meter_missing_totals_returns_true(self):
        future_consumption = {"m1": self._future_info()}
        assert (
            _needs_future_consumption_fallback(
                future_consumption, {"m1": True}, {}, "m1"
            )
            is True
        )

    def test_backstream_meter_with_real_export_returns_false(self):
        future_consumption = {"m1": self._future_info()}
        assert (
            _needs_future_consumption_fallback(
                future_consumption,
                {"m1": True},
                {"m1": {"total_export": 39225.556}},
                "m1",
            )
            is False
        )


class TestSelectMeterData:
    def _make_meter(self, serial="S1", code="C1"):
        meter = MagicMock(spec=MeterReadingData)
        meter.meter_serial = serial
        meter.meter_code = code
        return meter

    def test_none_reading_returns_none(self):
        assert _select_meter_data(None, "d1", "c1") is None

    def test_empty_meter_list_returns_none(self):
        reading = MagicMock()
        reading.meter_list = []
        assert _select_meter_data(reading, "d1", "c1") is None

    def test_exact_match_returns_correct_meter(self):
        m1 = self._make_meter("S1", "C1")
        m2 = self._make_meter("S2", "C2")
        reading = MagicMock()
        reading.meter_list = [m1, m2]
        result = _select_meter_data(reading, "S2", "C2")
        assert result == m2

    def test_serial_fallback(self):
        m1 = self._make_meter("S1", "C1")
        reading = MagicMock()
        reading.meter_list = [m1]
        result = _select_meter_data(reading, "S1", "WRONG")
        assert result == m1

    def test_code_fallback(self):
        m1 = self._make_meter("S1", "C1")
        reading = MagicMock()
        reading.meter_list = [m1]
        result = _select_meter_data(reading, "WRONG", "C1")
        assert result == m1

    def test_fallback_to_first(self):
        m1 = self._make_meter("S1", "C1")
        reading = MagicMock()
        reading.meter_list = [m1]
        result = _select_meter_data(reading, "NO", "MATCH")
        assert result == m1


class TestParseInvoiceLastDate:
    def test_valid_string(self):
        result = _parse_invoice_last_date("01/02/2024")
        assert result == date(2024, 2, 1)

    def test_date_object(self):
        d = date(2024, 3, 15)
        result = _parse_invoice_last_date(d)
        assert result == d

    def test_invalid_string_returns_none(self):
        assert _parse_invoice_last_date("not-a-date") is None

    def test_empty_string_returns_none(self):
        assert _parse_invoice_last_date("") is None

    def test_none_returns_none(self):
        assert _parse_invoice_last_date(None) is None


class TestGetInvoiceReadingDates:
    """Contract: the reading window derives from the most recent PAST invoice.

    Invoices whose fullDate is in the future are ignored; the from_date is the
    to_date of the next older invoice (or today when none exists). The
    last_invoice_date sent to IEC is fullDate + 1 day, which is the only value
    that returns real (non-zeroed) export data for the current period.
    """

    def test_empty_invoices(self):
        assert _get_invoice_reading_dates([]) == (None, None)

    def test_none_invoices(self):
        assert _get_invoice_reading_dates(None) == (None, None)

    @freeze_time("2024-06-15")
    def test_single_invoice_current(self):
        invoice = MagicMock()
        invoice.full_date = datetime(2024, 6, 1)
        invoice.to_date = datetime(2024, 6, 10)
        last_date, from_date = _get_invoice_reading_dates([invoice])
        assert last_date == datetime(2024, 6, 2, 0, 0)
        assert from_date == datetime(2024, 6, 15, 0, 0)

    @freeze_time("2024-03-15")
    def test_future_invoice_skipped(self):
        future = MagicMock()
        future.full_date = datetime(2024, 7, 1)
        current = MagicMock()
        current.full_date = datetime(2024, 3, 1)
        current.to_date = datetime(2024, 3, 10)
        last_date, from_date = _get_invoice_reading_dates([future, current])
        assert last_date == datetime(2024, 3, 2, 0, 0)
        assert from_date == datetime(2024, 3, 15, 0, 0)

    @freeze_time("2024-06-15")
    def test_only_future_invoices_return_none(self):
        """Contract: invoices entirely in the future yield no reading window."""
        future1 = MagicMock()
        future1.full_date = datetime(2024, 7, 1)
        future2 = MagicMock()
        future2.full_date = datetime(2024, 7, 5)
        assert _get_invoice_reading_dates([future1, future2]) == (None, None)

    @freeze_time("2024-06-15")
    def test_only_past_invoices_uses_most_recent(self):
        """Contract: the most recent past invoice sets the window start."""
        older = MagicMock()
        older.full_date = datetime(2024, 1, 1)
        older.to_date = datetime(2024, 1, 1)
        newer = MagicMock()
        newer.full_date = datetime(2024, 6, 1)
        newer.to_date = datetime(2024, 6, 1)
        last_date, from_date = _get_invoice_reading_dates([older, newer])
        assert last_date == datetime(2024, 6, 2, 0, 0)
        # from_date comes from the next older invoice's to_date.
        assert from_date == datetime(2024, 1, 1, 0, 0)

    @freeze_time("2024-06-15")
    def test_mixed_ordering_is_sorted_by_full_date(self):
        """Contract: input order must not matter; sorted by fullDate desc."""
        newest = MagicMock()
        newest.full_date = datetime(2024, 6, 1)
        oldest = MagicMock()
        oldest.full_date = datetime(2024, 1, 1)
        middle = MagicMock()
        middle.full_date = datetime(2024, 4, 1)
        middle.to_date = datetime(2024, 4, 10)
        # Unsorted input on purpose
        last_date, from_date = _get_invoice_reading_dates([oldest, newest, middle])
        assert last_date == datetime(2024, 6, 2, 0, 0)
        assert from_date == datetime(2024, 4, 10, 0, 0)

    @freeze_time("2024-06-15")
    def test_next_invoice_with_none_to_date_falls_back_to_today(self):
        """Contract: a missing to_date on the next invoice must not crash.

        The next (older) invoice's to_date can be None when the billing period
        is still open; the function must fall back to today instead of raising.
        """
        current = MagicMock()
        current.full_date = datetime(2024, 3, 1)
        next_invoice = MagicMock()
        next_invoice.full_date = datetime(2024, 2, 1)
        next_invoice.to_date = None
        last_date, from_date = _get_invoice_reading_dates([current, next_invoice])
        assert last_date == datetime(2024, 3, 2, 0, 0)
        assert from_date == datetime(2024, 6, 15, 0, 0)  # falls back to today


class TestExtractValidFutureConsumption:
    def _make_reading(self, meters=None):
        reading = MagicMock()
        reading.meter_list = meters or []
        return reading

    def _make_meter(self, future_info=None):
        meter = MagicMock(spec=MeterReadingData)
        meter.future_consumption_info = future_info
        return meter

    def _make_future(self, consumption=100.0, total_import=500.0, import_date=None):
        info = MagicMock(spec=FutureConsumptionInfo)
        info.future_consumption = consumption
        info.total_import = total_import
        info.total_import_date = import_date or date(2024, 6, 1)
        info.future_back_stream = 50.0
        info.total_export = 200.0
        return info

    def test_none_reading(self):
        assert _extract_valid_future_consumption(None) is None

    def test_empty_meter_list(self):
        reading = self._make_reading(meters=[])
        assert _extract_valid_future_consumption(reading) is None

    def test_no_future_info(self):
        meter = self._make_meter(None)
        reading = self._make_reading(meters=[meter])
        assert _extract_valid_future_consumption(reading) is None

    def test_valid_consumption(self):
        info = self._make_future(consumption=150.0, total_import=500.0)
        meter = self._make_meter(info)
        reading = self._make_reading(meters=[meter])
        result = _extract_valid_future_consumption(reading)
        assert result is info

    def test_valid_total_import(self):
        info = self._make_future(consumption=0, total_import=300.0)
        meter = self._make_meter(info)
        reading = self._make_reading(meters=[meter])
        result = _extract_valid_future_consumption(reading)
        assert result is info

    def test_both_zero_returns_none(self):
        info = self._make_future(consumption=0, total_import=0)
        meter = self._make_meter(info)
        reading = self._make_reading(meters=[meter])
        assert _extract_valid_future_consumption(reading) is None

    def test_min_date_returns_none(self):
        info = self._make_future(consumption=100.0, import_date=date.min)
        meter = self._make_meter(info)
        reading = self._make_reading(meters=[meter])
        assert _extract_valid_future_consumption(reading) is None

    def test_string_import_date(self):
        info = self._make_future(consumption=100.0, import_date="2024-06-01")
        meter = self._make_meter(info)
        reading = self._make_reading(meters=[meter])
        result = _extract_valid_future_consumption(reading)
        assert result is info

    def test_specific_meter_used(self):
        info = self._make_future(consumption=100.0)
        meter = self._make_meter(info)
        reading = self._make_reading(meters=[meter])
        result = _extract_valid_future_consumption(reading, meter=meter)
        assert result is info


class TestCalculateEstimatedBill:
    """Contract for future-consumption fallbacks and fixed-price scaling.

    Future consumption is total_import - last_meter_read, falling back to the
    forecasted value, then to 0; tariffs scale the fixed parts by elapsed days.
    """

    @freeze_time("2024-06-15")
    def test_with_last_invoice(self):
        result = _calculate_estimated_bill(
            meter_id="m1",
            future_consumptions={"m1": None},
            last_meter_read=100.0,
            last_meter_read_date=date(2024, 6, 1),
            kwh_tariff=0.5,
            kva_tariff=10.0,
            distribution_tariff=30.0,
            delivery_tariff=20.0,
            power_size=25.0,
            last_invoice=MagicMock(),
        )
        assert len(result) == 8
        (
            total_est,
            fixed,
            consumption_price,
            days,
            delivery,
            distribution,
            kva,
            fut_cons,
        ) = result
        assert days >= 1
        assert isinstance(total_est, float)
        assert isinstance(consumption_price, float)

    @freeze_time("2024-06-15")
    def test_without_last_invoice(self):
        result = _calculate_estimated_bill(
            meter_id="m1",
            future_consumptions={"m1": None},
            last_meter_read=100.0,
            last_meter_read_date=date(2024, 6, 1),
            kwh_tariff=0.5,
            kva_tariff=10.0,
            distribution_tariff=30.0,
            delivery_tariff=20.0,
            power_size=25.0,
            last_invoice=EMPTY_INVOICE,
        )
        (
            total_est,
            fixed,
            consumption_price,
            days,
            delivery,
            distribution,
            kva,
            fut_cons,
        ) = result
        assert isinstance(total_est, float)

    @freeze_time("2024-06-15")
    def test_with_future_consumption_info(self):
        info = MagicMock(spec=FutureConsumptionInfo)
        info.future_consumption = 200.0
        info.total_import = 500.0
        info.total_import_date = date(2024, 6, 10)
        info.future_back_stream = 0
        info.total_export = 0
        result = _calculate_estimated_bill(
            meter_id="m1",
            future_consumptions={"m1": info},
            last_meter_read=100.0,
            last_meter_read_date=date(2024, 6, 1),
            kwh_tariff=0.5,
            kva_tariff=10.0,
            distribution_tariff=30.0,
            delivery_tariff=20.0,
            power_size=25.0,
            last_invoice=MagicMock(),
        )
        _, _, consumption_price, _, _, _, _, fut_cons = result
        assert fut_cons == 400.0
        assert consumption_price == pytest.approx(400.0 * 0.5)

    @freeze_time("2024-06-15")
    def test_zero_tariffs(self):
        result = _calculate_estimated_bill(
            meter_id="m1",
            future_consumptions={"m1": None},
            last_meter_read=100.0,
            last_meter_read_date=date(2024, 6, 1),
            kwh_tariff=0.0,
            kva_tariff=0.0,
            distribution_tariff=0.0,
            delivery_tariff=0.0,
            power_size=25.0,
            last_invoice=MagicMock(),
        )
        total_est, fixed, consumption_price, days, _, _, _, _ = result
        assert total_est == 0.0
        assert consumption_price == 0.0

    @freeze_time("2024-06-15")
    def test_future_consumption_used_when_total_import_missing(self):
        """Contract: without total_import, the forecasted value is used directly."""
        info = MagicMock(spec=FutureConsumptionInfo)
        info.future_consumption = 200.0
        info.total_import = 0  # missing/unreliable total import
        info.total_import_date = date(2024, 6, 10)
        info.future_back_stream = 0
        info.total_export = 0
        result = _calculate_estimated_bill(
            meter_id="m1",
            future_consumptions={"m1": info},
            last_meter_read=100.0,
            last_meter_read_date=date(2024, 6, 1),
            kwh_tariff=0.5,
            kva_tariff=10.0,
            distribution_tariff=30.0,
            delivery_tariff=20.0,
            power_size=25.0,
            last_invoice=MagicMock(),
        )
        _, _, consumption_price, _, _, _, _, fut_cons = result
        assert fut_cons == 200.0
        assert consumption_price == pytest.approx(100.0)

    @freeze_time("2024-06-15")
    def test_both_missing_defaults_to_zero_consumption(self):
        """Contract: missing total_import and forecast falls back to 0 (with warning)."""
        info = MagicMock(spec=FutureConsumptionInfo)
        info.future_consumption = 0
        info.total_import = 0
        info.total_import_date = date(2024, 6, 10)
        result = _calculate_estimated_bill(
            meter_id="m1",
            future_consumptions={"m1": info},
            last_meter_read=100.0,
            last_meter_read_date=date(2024, 6, 1),
            kwh_tariff=0.5,
            kva_tariff=10.0,
            distribution_tariff=30.0,
            delivery_tariff=20.0,
            power_size=25.0,
            last_invoice=MagicMock(),
        )
        _, _, consumption_price, _, _, _, _, fut_cons = result
        assert fut_cons == 0.0
        assert consumption_price == 0.0

    @freeze_time("2024-06-15")
    def test_nonzero_tariffs_without_forecast_yield_fixed_price_only(self):
        """Contract: no forecast data yields a fixed-price-only estimate."""
        result = _calculate_estimated_bill(
            meter_id="m1",
            future_consumptions={"m1": None},
            last_meter_read=None,
            last_meter_read_date=None,
            kwh_tariff=0.5,
            kva_tariff=10.0,
            distribution_tariff=30.0,
            delivery_tariff=20.0,
            power_size=25.0,
            last_invoice=EMPTY_INVOICE,
        )
        (
            total_est,
            fixed,
            consumption_price,
            days,
            delivery,
            distribution,
            kva,
            fut_cons,
        ) = result
        assert fut_cons == 0.0
        assert consumption_price == 0.0
        assert days == 15  # frozen at 2024-06-15
        assert kva == pytest.approx(10.27)  # round(25*10/365*15, 2)
        assert distribution == pytest.approx(15.0)
        assert delivery == pytest.approx(10.0)
        assert fixed == pytest.approx(35.27)
        assert total_est == pytest.approx(35.27)


class TestFutureConsumptionCandidateDates:
    def test_returns_five_candidates_newest_first(self):
        candidates = _future_consumption_candidate_dates(datetime(2026, 8, 10))
        assert [resolution for _, resolution in candidates] == [
            ReadingResolution.DAILY,
            ReadingResolution.DAILY,
            ReadingResolution.DAILY,
            ReadingResolution.WEEKLY,
            ReadingResolution.MONTHLY,
        ]

    def test_daily_candidates_are_last_three_days(self):
        candidates = _future_consumption_candidate_dates(datetime(2026, 8, 10))
        assert [req_date for req_date, _ in candidates[:3]] == [
            datetime(2026, 8, 10),
            datetime(2026, 8, 9),
            datetime(2026, 8, 8),
        ]

    def test_monday_weekly_candidate_is_previous_sunday(self):
        candidates = _future_consumption_candidate_dates(datetime(2026, 8, 10))
        assert candidates[3] == (datetime(2026, 8, 9), ReadingResolution.WEEKLY)

    def test_sunday_weekly_candidate_is_two_sundays_ago(self):
        candidates = _future_consumption_candidate_dates(datetime(2026, 8, 9))
        assert candidates[3] == (datetime(2026, 8, 2), ReadingResolution.WEEKLY)

    def test_non_first_of_month_monthly_candidate_is_current_month_first(self):
        candidates = _future_consumption_candidate_dates(datetime(2026, 8, 9))
        assert candidates[4] == (datetime(2026, 8, 1), ReadingResolution.MONTHLY)

    def test_first_of_month_monthly_candidate_is_previous_month_first(self):
        candidates = _future_consumption_candidate_dates(datetime(2026, 8, 1))
        assert candidates[4] == (datetime(2026, 7, 1), ReadingResolution.MONTHLY)
