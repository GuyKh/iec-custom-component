"""Tests for IEC coordinator token retry/backoff and error handling.

The tests exercise the real retry loop in IecApiCoordinator._run_token_operation_with_retry
and its integration in _async_update_data, so they pin down the documented contract:

- IECError codes 400/401 (expired/invalid token) are retried ONCE after a 5s delay,
  then raise ConfigEntryAuthFailed to trigger the reauth flow.
- Any other IECError (e.g. 500) fails fast: no retry.
- During the initial token load, non-auth errors propagate as IECError (no reauth).
- During the token check, any IECError is converted to ConfigEntryAuthFailed.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import CONF_API_TOKEN
from homeassistant.exceptions import ConfigEntryAuthFailed
from iec_api.models.exceptions import IECError

from custom_components.iec.coordinator import IecApiCoordinator


def _make_coordinator(**attrs) -> IecApiCoordinator:
    """Build a coordinator without running __init__ (no Home Assistant needed)."""
    coordinator = object.__new__(IecApiCoordinator)
    coordinator.api = MagicMock()
    coordinator.api.get_token = MagicMock(return_value=MagicMock())
    coordinator.api.load_jwt_token = AsyncMock()
    coordinator.api.check_token = AsyncMock()
    coordinator.hass = MagicMock()
    coordinator._config_entry = MagicMock()
    coordinator._entry_data = {}
    coordinator._first_load = False
    for key, value in attrs.items():
        setattr(coordinator, key, value)
    return coordinator


def _mock_jwt_token_dict() -> dict:
    """Return a dict acceptable to JWT.from_dict (mashumaro dataclass)."""
    return {
        "access_token": "test_access",
        "refresh_token": "test_refresh",
        "token_type": "Bearer",
        "expires_in": 3600,
        "scope": "openid",
        "id_token": "test_id",
    }


class TestRetryHelper:
    """Unit tests for the shared token retry/backoff loop."""

    @pytest.mark.asyncio
    async def test_success_runs_operation_once_without_delay(self):
        operation = AsyncMock()
        coordinator = _make_coordinator()

        with patch(
            "custom_components.iec.coordinator.asyncio.sleep", new=AsyncMock()
        ) as sleep:
            await coordinator._run_token_operation_with_retry("check", operation)

        operation.assert_awaited_once()
        sleep.assert_not_called()

    @pytest.mark.asyncio
    async def test_auth_error_retries_once_then_succeeds(self):
        operation = AsyncMock(side_effect=[IECError(400, "expired refresh token"), None])
        coordinator = _make_coordinator()

        with patch(
            "custom_components.iec.coordinator.asyncio.sleep", new=AsyncMock()
        ) as sleep:
            await coordinator._run_token_operation_with_retry("check", operation)

        assert operation.await_count == 2
        sleep.assert_awaited_once_with(5)

    @pytest.mark.asyncio
    async def test_auth_error_twice_raises_auth_failed(self):
        operation = AsyncMock(side_effect=IECError(401, "invalid token"))
        coordinator = _make_coordinator()

        with (
            patch(
                "custom_components.iec.coordinator.asyncio.sleep", new=AsyncMock()
            ) as sleep,
            pytest.raises(ConfigEntryAuthFailed),
        ):
            await coordinator._run_token_operation_with_retry("load", operation)

        assert operation.await_count == 2  # one retry, then reauth
        sleep.assert_awaited_once_with(5)

    @pytest.mark.asyncio
    async def test_non_auth_error_fails_fast_without_retry(self):
        operation = AsyncMock(side_effect=IECError(500, "server error"))
        coordinator = _make_coordinator()

        with (
            patch(
                "custom_components.iec.coordinator.asyncio.sleep", new=AsyncMock()
            ) as sleep,
            pytest.raises(IECError) as exc_info,
        ):
            await coordinator._run_token_operation_with_retry("check", operation)

        assert exc_info.value.code == 500
        operation.assert_awaited_once()
        sleep.assert_not_called()


class TestAsyncUpdateDataRetry:
    """Integration tests: retry behavior as seen from _async_update_data."""

    @pytest.mark.asyncio
    async def test_first_load_token_failure_triggers_reauth(self):
        coordinator = _make_coordinator(_first_load=True)
        coordinator._entry_data = {CONF_API_TOKEN: _mock_jwt_token_dict()}
        coordinator.api.load_jwt_token = AsyncMock(
            side_effect=IECError(400, "expired refresh token")
        )

        with (
            patch(
                "custom_components.iec.coordinator.asyncio.sleep", new=AsyncMock()
            ) as sleep,
            patch.object(coordinator, "_update_data", new=AsyncMock()) as update_data,
            pytest.raises(ConfigEntryAuthFailed),
        ):
            await coordinator._async_update_data()

        assert coordinator.api.load_jwt_token.await_count == 2
        sleep.assert_awaited_once_with(5)
        update_data.assert_not_called()

    @pytest.mark.asyncio
    async def test_first_load_non_auth_error_propagates_raw(self):
        coordinator = _make_coordinator(_first_load=True)
        coordinator._entry_data = {CONF_API_TOKEN: _mock_jwt_token_dict()}
        coordinator.api.load_jwt_token = AsyncMock(
            side_effect=IECError(500, "server error")
        )

        with (
            patch(
                "custom_components.iec.coordinator.asyncio.sleep", new=AsyncMock()
            ) as sleep,
            patch.object(coordinator, "_update_data", new=AsyncMock()) as update_data,
            pytest.raises(IECError) as exc_info,
        ):
            await coordinator._async_update_data()

        assert exc_info.value.code == 500
        coordinator.api.load_jwt_token.assert_awaited_once()
        # Load path is not wrapped by the outer try: no reauth conversion, and
        # _first_load stays True so the load is retried on the next cycle.
        assert coordinator._first_load is True
        sleep.assert_not_called()
        update_data.assert_not_called()

    @pytest.mark.asyncio
    async def test_check_token_non_auth_error_becomes_auth_failed(self):
        coordinator = _make_coordinator(_first_load=False)
        coordinator.api.check_token = AsyncMock(
            side_effect=IECError(500, "server error")
        )

        with (
            patch(
                "custom_components.iec.coordinator.asyncio.sleep", new=AsyncMock()
            ) as sleep,
            patch.object(coordinator, "_update_data", new=AsyncMock()) as update_data,
            pytest.raises(ConfigEntryAuthFailed),
        ):
            await coordinator._async_update_data()

        coordinator.api.check_token.assert_awaited_once()
        sleep.assert_not_called()
        update_data.assert_not_called()

    @pytest.mark.asyncio
    async def test_check_token_retries_then_succeeds(self):
        coordinator = _make_coordinator(_first_load=False)
        coordinator.api.check_token = AsyncMock(
            side_effect=[IECError(401, "invalid token"), None]
        )

        with (
            patch(
                "custom_components.iec.coordinator.asyncio.sleep", new=AsyncMock()
            ) as sleep,
            patch.object(
                coordinator, "_update_data", new=AsyncMock(return_value={})
            ) as update_data,
        ):
            result = await coordinator._async_update_data()

        assert result == {}
        assert coordinator.api.check_token.await_count == 2
        sleep.assert_awaited_once_with(5)
        update_data.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_token_refresh_persists_new_token(self):
        coordinator = _make_coordinator(_first_load=False)
        coordinator._entry_data = {"existing": "value"}
        old_token = MagicMock()
        new_token = MagicMock()
        coordinator.api.get_token = MagicMock(side_effect=[old_token, new_token])
        # async_update_entry is synchronous in HA despite its async_ prefix, so
        # the coordinator calls it without await (and this mock is not async).
        coordinator.hass.config_entries.async_update_entry = MagicMock()

        with (
            patch(
                "custom_components.iec.coordinator.asyncio.sleep", new=AsyncMock()
            ) as sleep,
            patch.object(
                coordinator, "_update_data", new=AsyncMock(return_value={})
            ) as update_data,
        ):
            await coordinator._async_update_data()

        coordinator.hass.config_entries.async_update_entry.assert_called_once()
        updated_data = coordinator.hass.config_entries.async_update_entry.call_args.kwargs[
            "data"
        ]
        assert updated_data == {
            "existing": "value",
            CONF_API_TOKEN: new_token.to_dict(),
        }
        sleep.assert_not_called()
        update_data.assert_awaited_once()
