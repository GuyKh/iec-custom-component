"""Adds config flow for Blueprint."""
from __future__ import annotations

import voluptuous as vol
from homeassistant import config_entries

from collections.abc import Mapping
import logging
from typing import Any

from iec_api.iec_client import IecClient
from iec_api.models.exceptions import IECError

from homeassistant.const import CONF_TOKEN
from homeassistant.data_entry_flow import FlowResult

from .const import CONF_USER_ID, CONF_OTP, DOMAIN

_LOGGER = logging.getLogger(__name__)


class IecFlowHandler(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow for IEC."""

    VERSION = 1

    def __init__(self) -> None:
        """Device settings."""
        self._user_id: str | None = None
        self._description_placeholders = None
        self._otp: str | None = None
        self._token: str | None = None
        self._api: IecClient | None = None

    async def async_step_user(
            self,
            user_input: dict | None = None,
    ) -> config_entries.FlowResult:
        """Handle a flow initialized by the user."""
        _errors = {}

        errors: dict[str, Any] = {}

        if user_input is None:
            return self._show_setup_form(user_input, errors, "user")

        return await self._validate_user_id(user_input)

    def _show_setup_form(
            self,
            user_input: dict[str, str] | None = None,
            errors: dict[str, str] | None = None,
            step_id: str = "user",
    ) -> FlowResult:
        """Show the setup form to the user."""
        if user_input is None:
            user_input = {}

        if step_id == "user":
            schema = {
                vol.Required(
                    CONF_USER_ID, default=user_input.get(CONF_USER_ID, "")
                ): str
            }
        else:
            schema = {vol.Required(CONF_OTP, default=user_input.get(CONF_OTP, "")): str}

        return self.async_show_form(
            step_id=step_id,
            data_schema=vol.Schema(schema),
            errors=errors or {},
            description_placeholders=self._description_placeholders,
        )

    async def _validate_user_id(self, user_input: dict[str, str]) -> FlowResult:
        """Check if config is valid and create entry if so."""

        self._user_id = user_input[CONF_USER_ID]

        # Check if already configured
        if self.unique_id is None:
            await self.async_set_unique_id(self._user_id)
            self._abort_if_unique_id_configured()

        self._api = IecClient(self._user_id)

        try:
            self._api.login_with_id()
        except IECError as exp:
            _LOGGER.error("Failed to connect to API: %s", exp)
            return self._show_setup_form(user_input, {"base": "cannot_connect"}, "user")

        return await self.async_step_one_time_password()

    async def _validate_one_time_password(
            self, user_input: dict[str, str]
    ) -> FlowResult:
        self._otp = user_input[CONF_OTP]

        assert isinstance(self._api, IecClient)
        assert isinstance(self._user_id, str)
        assert isinstance(self._otp, str)

        try:
            token = self._api.verify_otp(self._otp)
        except IECError as exp:
            _LOGGER.error("Failed to connect to API: %s", exp)
            return self._show_setup_form(
                user_input, {"base": "cannot_connect"}, CONF_OTP
            )

        if token:
            self._token = token

            data = {
                CONF_TOKEN: self._token,
                CONF_USER_ID: self._user_id,
                CONF_OTP: self._otp
            }
            return self.async_create_entry(title=self._user_id, data=data)
        return self._show_setup_form(user_input, {CONF_OTP: "invalid_auth"}, CONF_OTP)

    async def async_step_one_time_password(
            self,
            user_input: dict[str, Any] | None = None,
            errors: dict[str, str] | None = None,
    ) -> FlowResult:
        """Ask the otp code to the user."""
        if errors is None:
            errors = {}

        if user_input is None:
            return await self._show_otp_form(errors)

        return await self._validate_one_time_password(user_input)

    async def _show_otp_form(
            self,
            errors: dict[str, str] | None = None,
    ) -> FlowResult:
        """Show the otp_code form to the user."""

        return self.async_show_form(
            step_id=CONF_OTP,
            data_schema=vol.Schema({vol.Required(CONF_OTP): str}),
            errors=errors or {},
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> FlowResult:
        """Handle reauthorization request from IEC."""
        self._api = IecClient(entry_data[CONF_USER_ID])
        if entry_data[CONF_TOKEN]:
            self._api.load_jwt_token(entry_data[CONF_TOKEN])
        self._user_id = entry_data[CONF_USER_ID]
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
            self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle reauthorization flow."""
        if user_input is None:
            return self.async_show_form(
                step_id="reauth_confirm",
                data_schema=vol.Schema(
                    {
                        vol.Required(
                            CONF_USER_ID, default=self._user_id
                        ): str,
                    }
                ),
            )
        return await self.async_step_user({CONF_USER_ID: self._user_id})

    async def _test_credentials(self) -> None:
        """Validate credentials."""
        client = self._api
        client.check_token()
