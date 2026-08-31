"""Tests for bill.select_last_electric_invoice.

Covers electric-invoice selection from a billing-invoice list, including
empty input and lists without electric invoices, which fall back to
EMPTY_INVOICE. Imports the real implementation from
custom_components.iec.bill.
"""

from dataclasses import dataclass
from datetime import datetime

from custom_components.iec.bill import select_last_electric_invoice
from custom_components.iec.const import ELECTRIC_INVOICE_DOC_ID, EMPTY_INVOICE


@dataclass
class FakeInvoice:
    """Minimal stand-in for an iec_api invoice model."""

    document_id: str
    full_date: datetime | None = None
    to_date: datetime | None = None


def test_empty_invoice_list_returns_empty_invoice():
    last_invoice, last_invoice_date, from_date = select_last_electric_invoice([])

    assert last_invoice is EMPTY_INVOICE
    assert last_invoice_date is None
    assert from_date is None


def test_no_electric_doc_id_returns_empty_invoice():
    invoices = [
        FakeInvoice(document_id="2", full_date=datetime(2026, 8, 1)),
        FakeInvoice(document_id="3", full_date=datetime(2026, 8, 2)),
    ]

    last_invoice, last_invoice_date, from_date = select_last_electric_invoice(invoices)

    assert last_invoice is EMPTY_INVOICE
    assert last_invoice_date is None
    assert from_date is None


def test_selects_most_recent_electric_invoice():
    invoices = [
        FakeInvoice(
            document_id=ELECTRIC_INVOICE_DOC_ID, full_date=datetime(2026, 7, 1)
        ),
        FakeInvoice(
            document_id=ELECTRIC_INVOICE_DOC_ID, full_date=datetime(2026, 6, 1)
        ),
        FakeInvoice(document_id="2", full_date=datetime(2026, 8, 1)),
    ]

    last_invoice, _last_invoice_date, _from_date = select_last_electric_invoice(
        invoices
    )

    assert last_invoice.full_date == datetime(2026, 7, 1)
    assert last_invoice.document_id == ELECTRIC_INVOICE_DOC_ID


def test_mixed_list_with_electric_invoice_keeps_working():
    invoices = [
        FakeInvoice(document_id="2", full_date=datetime(2026, 8, 5)),
        FakeInvoice(
            document_id=ELECTRIC_INVOICE_DOC_ID, full_date=datetime(2026, 7, 1)
        ),
        FakeInvoice(
            document_id=ELECTRIC_INVOICE_DOC_ID, full_date=datetime(2026, 6, 1)
        ),
    ]

    last_invoice, _last_invoice_date, _from_date = select_last_electric_invoice(
        invoices
    )

    assert last_invoice.full_date == datetime(2026, 7, 1)
