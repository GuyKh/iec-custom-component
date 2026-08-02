"""Regression tests for bill._get_invoice_reading_dates.

Guards against a crash where an invoice's `to_date` is None (e.g. an open
billing period): the function must fall back to today instead of crashing.
Imports the real implementation from custom_components.iec.bill.
"""

from dataclasses import dataclass
from datetime import date, datetime, time

from custom_components.iec.bill import _get_invoice_reading_dates


@dataclass
class FakeInvoice:
    """Minimal stand-in for an iec_api invoice model."""

    last_date: str
    to_date: datetime | None


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
