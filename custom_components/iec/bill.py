"""Bill calculation and meter data processing functions for IEC.

All functions in this module are pure (no I/O, no side effects on coordinator state).
They operate on data passed in as arguments and return computed results.
"""

import calendar
import logging
from collections import Counter
from datetime import date, datetime, timedelta, time
from typing import Any

from iec_api.models.remote_reading import (
    FutureConsumptionInfo,
    MeterReadingData,
    RemoteReadingResponse,
)

from .commons import TIMEZONE
from .const import EMPTY_INVOICE

_LOGGER = logging.getLogger(__name__)


def _is_backstream_meter_kind(meter_kind: Any) -> bool:
    """Return whether the IEC meter kind represents bidirectional export."""
    if meter_kind is None:
        return False

    if isinstance(meter_kind, int):
        return meter_kind == 2

    normalized = str(
        meter_kind.value if hasattr(meter_kind, "value") else meter_kind
    ).strip()
    if not normalized:
        return False

    if normalized.isdigit():
        return int(normalized) == 2

    lowered = normalized.lower()
    return lowered in {"backstream", "דו כיווני"}


def _map_meter_kind_to_remote_reading_param(meter_kind: Any) -> str:
    """Translate IEC meter kind to the expected parameter for remote reading API."""
    if meter_kind is None:
        return ""

    normalized = str(
        meter_kind.value if hasattr(meter_kind, "value") else meter_kind
    ).strip()

    if not normalized:
        return ""

    METER_KIND_MAPPING = {
        "צריכה": "Consumption",
        "דו כיווני": "BackStream",
    }

    return METER_KIND_MAPPING.get(normalized, normalized)


def _build_backstream_totals(
    future_info: FutureConsumptionInfo | None,
) -> dict[str, float | None]:
    """Build backstream totals from a single futureConsumptionInfo object."""
    if not future_info:
        return {
            "total_back_stream_for_period": None,
            "total_export": None,
        }
    return {
        "total_back_stream_for_period": future_info.future_back_stream,
        "total_export": future_info.total_export,
    }


def _select_meter_data(
    reading: RemoteReadingResponse | None,
    device_id: str | int,
    device_code: str | int,
) -> MeterReadingData | None:
    """Select the meter payload matching the requested meter identity."""
    if not reading or not reading.meter_list:
        return None

    requested_meter_id = str(device_id)
    requested_meter_code = str(device_code)

    for meter in reading.meter_list:
        if (
            meter.meter_serial == requested_meter_id
            and meter.meter_code == requested_meter_code
        ):
            return meter

    for meter in reading.meter_list:
        if meter.meter_serial == requested_meter_id:
            return meter

    for meter in reading.meter_list:
        if meter.meter_code == requested_meter_code:
            return meter

    return reading.meter_list[0]


def _parse_invoice_last_date(last_date: str | date) -> date | None:
    """Parse invoice lastDate to a date object.

    Handles both string format 'DD/MM/YYYY' and datetime.date objects.
    """
    try:
        if isinstance(last_date, date):
            return last_date
        parts = last_date.split("/")
        if len(parts) == 3:
            day, month, year = int(parts[0]), int(parts[1]), int(parts[2])
            return date(year, month, day)
    except (ValueError, IndexError, TypeError, AttributeError):
        pass
    return None


def _get_invoice_reading_dates(
    invoices: list,
) -> tuple[datetime | None, datetime | None]:
    """Get the last invoice date and from date for RemoteReadingRange API call.

    Returns: (last_invoice_date, from_date) tuple.
    - last_invoice_date: The lastDate of the most recent invoice where lastDate <= today.
    - from_date: The toDate of the next invoice after that (or today if none exists).
    """
    if not invoices:
        return None, None

    today = date.today()

    sorted_invoices = sorted(
        invoices,
        key=lambda inv: _parse_invoice_last_date(inv.last_date) or date.min,
        reverse=True,
    )

    last_invoice_date_obj = None
    from_date_obj = None

    for i, invoice in enumerate(sorted_invoices):
        parsed_last_date = _parse_invoice_last_date(invoice.last_date)
        if parsed_last_date and parsed_last_date <= today:
            last_invoice_date_obj = datetime.combine(parsed_last_date, time.min)
            if i + 1 < len(sorted_invoices):
                to_date = sorted_invoices[i + 1].to_date
                from_date_obj = (
                    to_date
                    if isinstance(to_date, datetime)
                    else datetime.combine(to_date, time.min)
                )
            else:
                from_date_obj = datetime.combine(today, time.min)
            break

    return (last_invoice_date_obj, from_date_obj)


def _extract_valid_future_consumption(
    reading: RemoteReadingResponse | None,
    meter: MeterReadingData | None = None,
) -> FutureConsumptionInfo | None:
    """Return normalized future consumption data if the IEC payload is usable."""
    if not reading or not reading.meter_list:
        return None

    meter = meter or reading.meter_list[0]
    future_info = meter.future_consumption_info
    if not future_info:
        return None

    total_import_date = future_info.total_import_date
    if isinstance(total_import_date, str):
        try:
            total_import_date = date.fromisoformat(total_import_date)
        except ValueError:
            return None
    if total_import_date is None or total_import_date == date.min:
        return None

    if (future_info.future_consumption and future_info.future_consumption > 0) or (
        future_info.total_import and future_info.total_import > 0
    ):
        return future_info

    return None


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
    """Calculate the estimated electricity bill."""
    future_consumption_info: FutureConsumptionInfo | None = future_consumptions.get(
        meter_id
    )
    future_consumption = 0.0

    if last_meter_read and future_consumption_info:
        if future_consumption_info.total_import:
            future_consumption = future_consumption_info.total_import - last_meter_read
        elif (
            future_consumption_info.future_consumption
            and future_consumption_info.future_consumption > 0
        ):
            future_consumption = future_consumption_info.future_consumption
        else:
            _LOGGER.warning(
                "Failed to calculate Future Consumption for meter %s "
                "(missing total_import), defaulting forecasted consumption to 0",
                meter_id,
            )
            future_consumption = 0.0

    kva_price = power_size * kva_tariff / 365

    total_kva_price = 0
    distribution_price = 0
    delivery_price = 0

    consumption_price = round(future_consumption * kwh_tariff, 2)
    total_days = 0

    today = datetime.now(TIMEZONE)

    if last_invoice != EMPTY_INVOICE:
        current_date = last_meter_read_date + timedelta(days=1)
        month_counter: Counter[tuple[int, int]] = Counter()

        while current_date <= today.date():
            month_year = (current_date.year, current_date.month)
            month_counter[month_year] += 1
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
        "Calculated estimated bill: No. of days: %s, total KVA price: %s, "
        "total distribution price: %s, total delivery price: %s, "
        "consumption price: %s",
        total_days,
        total_kva_price,
        distribution_price,
        delivery_price,
        consumption_price,
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
