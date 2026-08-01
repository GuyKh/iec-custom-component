"""Regression tests for IecApiCoordinator._get_invoice_reading_dates.

Mirrors the date-combination logic in coordinator.py (kept dependency-free from
homeassistant/iec_api, consistent with test_retry.py) to guard against a crash
where an invoice's `to_date` is None.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time


@dataclass
class FakeInvoice:
    """Minimal stand-in for an iec_api invoice model."""

    last_date: str
    to_date: datetime | None


def _parse_invoice_last_date(last_date):
    if isinstance(last_date, date):
        return last_date
    try:
        parts = last_date.split("/")
        if len(parts) == 3:
            day, month, year = int(parts[0]), int(parts[1]), int(parts[2])
            return date(year, month, day)
    except (ValueError, IndexError, TypeError):
        pass
    return None


def _get_invoice_reading_dates(invoices):
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
                if isinstance(to_date, datetime):
                    from_date_obj = to_date
                elif to_date is not None:
                    from_date_obj = datetime.combine(to_date, time.min)
                else:
                    from_date_obj = datetime.combine(today, time.min)
            else:
                from_date_obj = datetime.combine(today, time.min)
            break

    return (last_invoice_date_obj, from_date_obj)


def test_next_invoice_with_none_to_date_does_not_crash():
    """An invoice with to_date=None must not crash _get_invoice_reading_dates.

    Regression test for: TypeError: combine() argument 1 must be
    datetime.date, not None. Occurred whenever the invoice following the
    most-recent-past invoice (by lastDate) had a to_date of None, e.g. the
    current, not-yet-closed billing period.
    """
    invoices = [
        FakeInvoice(last_date="15/07/2026", to_date=None),
        FakeInvoice(last_date="15/06/2026", to_date=None),
    ]

    last_invoice_date, from_date = _get_invoice_reading_dates(invoices)

    assert last_invoice_date == datetime(2026, 7, 15)
    assert from_date == datetime.combine(date.today(), time.min)


def test_next_invoice_with_date_to_date_is_combined():
    """A plain date to_date is combined with midnight, as before."""
    invoices = [
        FakeInvoice(last_date="15/07/2026", to_date=None),
        FakeInvoice(last_date="15/06/2026", to_date=date(2026, 6, 15)),
    ]

    last_invoice_date, from_date = _get_invoice_reading_dates(invoices)

    assert last_invoice_date == datetime(2026, 7, 15)
    assert from_date == datetime(2026, 6, 15)


def test_next_invoice_with_datetime_to_date_is_used_directly():
    """A datetime to_date is passed through unchanged, as before."""
    invoices = [
        FakeInvoice(last_date="15/07/2026", to_date=None),
        FakeInvoice(last_date="15/06/2026", to_date=datetime(2026, 6, 15, 12, 30)),
    ]

    last_invoice_date, from_date = _get_invoice_reading_dates(invoices)

    assert last_invoice_date == datetime(2026, 7, 15)
    assert from_date == datetime(2026, 6, 15, 12, 30)
