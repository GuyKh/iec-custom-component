"""Tests for bill.py pure functions."""

from datetime import date, datetime
from unittest.mock import MagicMock

import pytest
from freezegun import freeze_time

from custom_components.iec.bill import (
    _build_backstream_totals,
    _calculate_estimated_bill,
    _extract_valid_future_consumption,
    _get_invoice_reading_dates,
    _is_backstream_meter_kind,
    _map_meter_kind_to_remote_reading_param,
    _parse_invoice_last_date,
    _select_meter_data,
)
from custom_components.iec.const import EMPTY_INVOICE
from iec_api.models.remote_reading import FutureConsumptionInfo, MeterReadingData


class TestIsBackstreamMeterKind:
    def test_int_2_returns_true(self):
        assert _is_backstream_meter_kind(2) is True

    def test_int_3_returns_false(self):
        assert _is_backstream_meter_kind(3) is False

    def test_string_backstream_returns_true(self):
        assert _is_backstream_meter_kind("BackStream") is True

    def test_string_hebrew_returns_true(self):
        assert _is_backstream_meter_kind("דו כיווני") is True

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
        assert _map_meter_kind_to_remote_reading_param("דו כיווני") == "BackStream"

    def test_none_returns_empty(self):
        assert _map_meter_kind_to_remote_reading_param(None) == ""

    def test_english_identity(self):
        assert _map_meter_kind_to_remote_reading_param("Consumption") == "Consumption"

    def test_unknown_identity(self):
        assert _map_meter_kind_to_remote_reading_param("SomeKind") == "SomeKind"

    def test_enum_like(self):
        obj = MagicMock()
        obj.value = "צריכה"
        assert _map_meter_kind_to_remote_reading_param(obj) == "Consumption"


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
    def test_empty_invoices(self):
        assert _get_invoice_reading_dates([]) == (None, None)

    def test_none_invoices(self):
        assert _get_invoice_reading_dates(None) == (None, None)

    @freeze_time("2024-06-15")
    def test_single_invoice_current(self):
        invoice = MagicMock()
        invoice.last_date = "10/06/2024"
        invoice.to_date = datetime(2024, 6, 10)
        last_date, from_date = _get_invoice_reading_dates([invoice])
        assert last_date == datetime(2024, 6, 10, 0, 0)
        assert from_date == datetime(2024, 6, 15, 0, 0)

    @freeze_time("2024-03-15")
    def test_future_invoice_skipped(self):
        future = MagicMock()
        future.last_date = "20/06/2024"
        current = MagicMock()
        current.last_date = "10/03/2024"
        current.to_date = datetime(2024, 3, 10)
        last_date, from_date = _get_invoice_reading_dates([future, current])
        assert last_date == datetime(2024, 3, 10, 0, 0)
        assert from_date == datetime(2024, 6, 20, 0, 0)


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
        reading = self._make_meter([])
        assert _extract_valid_future_consumption(reading) is None

    def test_no_future_info(self):
        meter = self._make_meter(None)
        reading = self._make_meter([meter])
        assert _extract_valid_future_consumption(reading) is None

    def test_valid_consumption(self):
        info = self._make_future(consumption=150.0, total_import=500.0)
        meter = self._make_meter(info)
        reading = self._make_meter([meter])
        result = _extract_valid_future_consumption(reading)
        assert result is info

    def test_valid_total_import(self):
        info = self._make_future(consumption=0, total_import=300.0)
        meter = self._make_meter(info)
        reading = self._make_meter([meter])
        result = _extract_valid_future_consumption(reading)
        assert result is info

    def test_both_zero_returns_none(self):
        info = self._make_future(consumption=0, total_import=0)
        meter = self._make_meter(info)
        reading = self._make_meter([meter])
        assert _extract_valid_future_consumption(reading) is None

    def test_min_date_returns_none(self):
        info = self._make_future(consumption=100.0, import_date=date.min)
        meter = self._make_meter(info)
        reading = self._make_meter([meter])
        assert _extract_valid_future_consumption(reading) is None

    def test_string_import_date(self):
        info = self._make_future(consumption=100.0, import_date="2024-06-01")
        meter = self._make_meter(info)
        reading = self._make_meter([meter])
        result = _extract_valid_future_consumption(reading)
        assert result is info

    def test_specific_meter_used(self):
        info = self._make_future(consumption=100.0)
        meter = self._make_meter(info)
        reading = self._make_meter([meter])
        result = _extract_valid_future_consumption(reading, meter=meter)
        assert result is info


class TestCalculateEstimatedBill:
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
        total_est, fixed, consumption_price, days, delivery, distribution, kva, fut_cons = result
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
        total_est, fixed, consumption_price, days, delivery, distribution, kva, fut_cons = result
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
