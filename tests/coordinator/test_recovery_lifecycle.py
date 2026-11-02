"""Verify IEC token errors through Home Assistant's real coordinator lifecycle."""

import logging
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_API_TOKEN
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from iec_api.models.exceptions import IECError

from custom_components.iec.coordinator import IecApiCoordinator

from tests.coordinator.test_retry import _mock_jwt_token_dict


def _coordinator(hass: HomeAssistant, *, first_load: bool) -> IecApiCoordinator:
    coordinator = object.__new__(IecApiCoordinator)
    entry = MagicMock(pref_disable_polling=False)
    entry.state = (
        ConfigEntryState.SETUP_IN_PROGRESS if first_load else ConfigEntryState.LOADED
    )
    DataUpdateCoordinator.__init__(
        coordinator,
        hass,
        logging.getLogger(__name__),
        name="Iec",
        update_interval=timedelta(hours=1),
        config_entry=entry,
    )
    coordinator._config_entry = entry
    coordinator._entry_data = {CONF_API_TOKEN: _mock_jwt_token_dict()}
    coordinator._first_load = first_load
    coordinator.api = MagicMock()
    coordinator.api.load_jwt_token = AsyncMock()
    coordinator.api.check_token = AsyncMock()
    coordinator.api.get_token = MagicMock(return_value=MagicMock())
    return coordinator


@pytest.mark.asyncio
async def test_dns_failure_keeps_periodic_refresh_and_recovers(tmp_path):
    """DNS failure must not disable refresh scheduling or start a reauth flow."""
    hass = HomeAssistant(str(tmp_path))
    hass.state = CoreState.running
    coordinator = _coordinator(hass, first_load=False)
    coordinator.api.check_token.side_effect = [IECError(-1, "DNS timeout"), None]
    unsub = coordinator.async_add_listener(lambda: None)
    try:
        with patch.object(
            coordinator, "_update_data", new=AsyncMock(return_value={})
        ) as update_data:
            await coordinator.async_refresh()
            assert coordinator.last_update_success is False
            assert isinstance(coordinator.last_exception, UpdateFailed)
            assert coordinator._unsub_refresh is not None
            coordinator._config_entry.async_start_reauth.assert_not_called()
            update_data.assert_not_called()

            await coordinator.async_refresh()
            assert coordinator.last_update_success is True
            assert coordinator._unsub_refresh is not None
            update_data.assert_awaited_once()
    finally:
        unsub()
        await coordinator.async_shutdown()


@pytest.mark.asyncio
async def test_dns_failure_during_setup_is_not_ready_not_auth_failed(tmp_path):
    """HA must retry setup after a transient token-load failure."""
    hass = HomeAssistant(str(tmp_path))
    coordinator = _coordinator(hass, first_load=True)
    coordinator.api.load_jwt_token.side_effect = IECError(-1, "DNS timeout")
    try:
        with pytest.raises(ConfigEntryNotReady):
            await coordinator.async_config_entry_first_refresh()
        assert coordinator._first_load is True
        coordinator._config_entry.async_start_reauth.assert_not_called()
    finally:
        await coordinator.async_shutdown()


@pytest.mark.asyncio
async def test_rejected_credentials_still_stop_periodic_refresh(tmp_path):
    """A genuinely rejected token must still require reauthentication."""
    hass = HomeAssistant(str(tmp_path))
    hass.state = CoreState.running
    coordinator = _coordinator(hass, first_load=False)
    coordinator.api.check_token.side_effect = IECError(401, "Invalid refresh token")
    unsub = coordinator.async_add_listener(lambda: None)
    try:
        with patch("custom_components.iec.coordinator.asyncio.sleep", new=AsyncMock()):
            await coordinator.async_refresh()
        assert coordinator.last_update_success is False
        assert isinstance(coordinator.last_exception, ConfigEntryAuthFailed)
        assert coordinator._unsub_refresh is None
        # HA 2026.10 renamed this hook; both must preserve real reauth.
        reauth = coordinator._config_entry.async_start_reauth
        reauth_if_available = coordinator._config_entry.async_start_reauth_if_available
        assert reauth.call_count + reauth_if_available.call_count == 1
        (reauth if reauth.called else reauth_if_available).assert_called_once_with(hass)
    finally:
        unsub()
        await coordinator.async_shutdown()
