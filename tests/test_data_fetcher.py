"""Tests for data_fetcher.py error handling and sentinel protection."""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from custom_components.iec.data_fetcher import IecDataFetcher, _MISSING
from iec_api.models.exceptions import IECError
from iec_api.models.remote_reading import ReadingResolution


def _make_fetcher(api=None) -> IecDataFetcher:
    """Build a fetcher without running __init__ (avoids touching HA aiohttp_client)."""
    fetcher = object.__new__(IecDataFetcher)
    fetcher.api = api or MagicMock()
    fetcher._readings = {}
    fetcher._today_readings = {}
    fetcher._api_call = AsyncMock()
    return fetcher


@pytest.mark.asyncio
async def test_get_readings_iec_error_returns_none():
    """Verify that an IECError during _get_readings returns None and does not leak _MISSING."""
    fetcher = _make_fetcher()
    fetcher._api_call = AsyncMock(side_effect=IECError(-1, "Simulated IEC API Error"))

    result = await fetcher._get_readings(
        contract_id=123,
        device_id="456",
        device_code="512",
        reading_date=datetime(2026, 10, 2),
        resolution=ReadingResolution.DAILY,
        meter_kind="Consumption",
    )
    assert result is None
    assert result is not _MISSING


@pytest.mark.asyncio
async def test_get_readings_empty_response_returns_none():
    """Verify that an empty response during _get_readings returns None and does not leak _MISSING."""
    fetcher = _make_fetcher()
    fetcher._api_call = AsyncMock(return_value=None)

    result = await fetcher._get_readings(
        contract_id=123,
        device_id="456",
        device_code="512",
        reading_date=datetime(2026, 10, 2),
        resolution=ReadingResolution.DAILY,
        meter_kind="Consumption",
    )
    assert result is None
    assert result is not _MISSING
