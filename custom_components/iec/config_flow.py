""""Config flow for IEC integration."""
from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_USERNAME, CONF_API_TOKEN
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from iec_api.iec_client import IecClient
from iec_api.models.exceptions import IECError
from iec_api.models.jwt import JWT

from .const import CONF_TOTP_SECRET, DOMAIN, CONF_USER_ID, CONF_API_CLIENT

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USER_ID): str,
    }
)


async def _validate_login(
        hass: HomeAssistant, login_data: dict[str, Any]
) -> dict[str, str]:
    """Validate login data and return any errors."""
    assert login_data is not None
    assert login_data.get(CONF_API_CLIENT) is not None
    assert login_data.get(CONF_USER_ID) is not None
    assert login_data.get(CONF_TOTP_SECRET) or login_data.get(CONF_API_TOKEN) is not None

    api: IecClient = login_data.get(CONF_API_CLIENT)

    if login_data.get(CONF_TOTP_SECRET):
        try:
            await api.verify_otp(login_data.get(CONF_TOTP_SECRET))
        except IECError:
            return {"base": "invalid_auth"}

    elif login_data.get(CONF_API_TOKEN):
        try:
            await api.load_jwt_token(JWT.from_dict(json.loads(login_data.get(CONF_API_TOKEN))))
        except IECError:
            return {"base": "invalid_auth"}

    errors: dict[str, str] = {}
    try:
        await api.check_token()
    except IECError:
        errors["base"] = "invalid_auth"

    return errors


class IecConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for IEC."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize a new IECConfigFlow."""
        self.reauth_entry: config_entries.ConfigEntry | None = None
        self.data: dict[str, Any] | None = None

    async def async_step_user(
            self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}
        if user_input is not None:
            # self._async_abort_entries_match(
            #     {
            #         CONF_USER_ID: user_input[CONF_USER_ID],
            #         CONF_API_TOKEN: user_input[CONF_API_TOKEN]
            #     }
            # )

            _LOGGER.debug(f"User input in step_user: {user_input}")
            self.data = user_input
            try:
                self.data[CONF_API_CLIENT] = IecClient(self.data[CONF_USER_ID], async_create_clientsession(self.hass))
            except ValueError as err:
                errors["base"] = "invalid_id"
                _LOGGER.error(f"Error while creating IEC client: {err}")

            if not errors:
                return await self.async_step_mfa()

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors
        )

    async def async_step_mfa(
            self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle MFA step."""
        assert self.data is not None
        assert self.data.get(CONF_USER_ID) is not None

        client: IecClient = self.data[CONF_API_CLIENT]

        errors: dict[str, str] = {}
        _LOGGER.debug(f"User input in mfa: {user_input}")
        if user_input is not None and user_input.get(CONF_TOTP_SECRET) is not None:
            data = {**self.data, **user_input}
            errors = await _validate_login(self.hass, data)
            if not errors:
                self.data[CONF_API_TOKEN] = json.dumps(client.get_token().to_dict())
                return self._async_create_iec_entry(data)

        if errors:
            schema = {
                vol.Required(
                    CONF_USER_ID, default=self.data[CONF_USER_ID]
                ): str
            }
        else:
            schema = {}

        schema[vol.Required(CONF_TOTP_SECRET)] = str
        await client.login_with_id()

        return self.async_show_form(
            step_id="mfa",
            data_schema=vol.Schema(schema),
            errors=errors,
        )

    @callback
    def _async_create_iec_entry(self, data: dict[str, Any]) -> FlowResult:
        """Create the config entry."""
        return self.async_create_entry(
            title=f"IEC ({data[CONF_USERNAME]})",
            data=data,
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> FlowResult:
        """Handle configuration by re-auth."""
        self.reauth_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
            self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Dialog that informs the user that reauth is required."""
        assert self.reauth_entry
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {**self.reauth_entry.data, **user_input}
            errors = await _validate_login(self.hass, data)
            if not errors:
                self.hass.config_entries.async_update_entry(
                    self.reauth_entry, data=data
                )
                await self.hass.config_entries.async_reload(self.reauth_entry.entry_id)
                return self.async_abort(reason="reauth_successful")
        schema = {vol.Required(CONF_USER_ID): self.reauth_entry.data[CONF_USER_ID],
                  vol.Required(CONF_API_TOKEN): str,
                  vol.Optional(CONF_TOTP_SECRET): str}

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(schema),
            errors=errors,
        )
