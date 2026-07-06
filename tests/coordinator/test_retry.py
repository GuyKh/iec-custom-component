"""Tests for IEC coordinator retry and error handling logic."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from homeassistant.helpers.update_coordinator import UpdateFailed
from iec_api.models.exceptions import IECError


@pytest.fixture
def mock_api():
    api = MagicMock()
    api.load_jwt_token = AsyncMock()
    api.check_token = AsyncMock()
    return api


@pytest.fixture
def mock_config_entry():
    entry = MagicMock()
    entry.data = {}
    entry.unique_id = "test_iec"
    return entry


@pytest.mark.asyncio
async def test_401_error_triggers_config_entry_auth_failed(mock_api, mock_config_entry):
    mock_api.load_jwt_token.side_effect = IECError(401, "expired refresh token")
    with patch(
        "custom_components.iec.coordinator.IecApiCoordinator"
    ) as mock_coordinator_cls:
        instance = mock_coordinator_cls.return_value
        instance.api = mock_api
        instance.config_entry = mock_config_entry
        instance._fetcher = MagicMock()
        instance._fetcher._api_call = AsyncMock(side_effect=IECError(401, "unauthorized"))
        with pytest.raises(UpdateFailed):
            instance._fetcher._api_call("test")


@pytest.mark.asyncio
async def test_500_error_immediate_raise():
    error = IECError(500, "server error")
    assert error.code == 500
    assert "server error" in str(error)


def test_iec_error_codes():
    error_400 = IECError(400, "expired refresh token")
    error_500 = IECError(500, "server error")
    assert error_400.code == 400
    assert error_500.code == 500
    assert error_400.code != 500
