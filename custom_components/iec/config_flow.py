"""Config flow for IEC integration."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_API_TOKEN
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.config_validation import multi_select
from iec_api.iec_client import IecClient
from iec_api.models.exceptions import IECError
from iec_api.models.jwt import JWT

from .const import (
    CONF_TOTP_SECRET,
    DOMAIN,
    CONF_USER_ID,
    CONF_BP_NUMBER,
    CONF_AVAILABLE_CONTRACTS,
    CONF_SELECTED_CONTRACTS,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USER_ID): str,
    }
)


async def _validate_login(
    hass: HomeAssistant, login_data: dict[str, Any], api: IecClient
) -> dict[str, str]:
    """Validate login data and return any errors."""
    assert login_data is not None
    assert api is not None
    assert login_data.get(CONF_USER_ID) is not None
    assert (
        login_data.get(CONF_TOTP_SECRET) or login_data.get(CONF_API_TOKEN) is not None
    )

    if login_data.get(CONF_TOTP_SECRET):
        try:
            await api.verify_otp(login_data.get(CONF_TOTP_SECRET))
        except IECError:
            return {"base": "invalid_auth"}

    elif login_data.get(CONF_API_TOKEN):
        try:
            await api.load_jwt_token(JWT.from_dict(login_data.get(CONF_API_TOKEN)))
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
        self.client: IecClient | None = None

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
                self.client = IecClient(
                    self.data[CONF_USER_ID], async_create_clientsession(self.hass)
                )
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

        client: IecClient = self.client

        errors: dict[str, str] = {}
        if user_input is not None and user_input.get(CONF_TOTP_SECRET) is not None:
            data = {**self.data, **user_input}
            errors = await _validate_login(self.hass, data, client)
            if not errors:
                data[CONF_API_TOKEN] = client.get_token().to_dict()

                if data.get(CONF_TOTP_SECRET):
                    data.pop(CONF_TOTP_SECRET)

                customer = await client.get_customer()
                data[CONF_BP_NUMBER] = customer.bp_number

                contracts = await client.get_contracts(customer.bp_number)
                contract_ids = [
                    int(contract.contract_id)
                    for contract in contracts
                    if contract.status == 1
                ]
                if len(contract_ids) == 1:
                    data[CONF_SELECTED_CONTRACTS] = [contract_ids[0]]
                    return self._async_create_iec_entry(data)
                else:
                    data[CONF_AVAILABLE_CONTRACTS] = contract_ids
                    self.data = data
                    return await self.async_step_select_contracts()

        if errors:
            schema = {vol.Required(CONF_USER_ID, default=self.data[CONF_USER_ID]): str}
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
            title=f"IEC Account ({data[CONF_USER_ID]})",
            data=data,
        )

    async def async_step_select_contracts(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle Select Contract step."""
        assert self.data is not None
        assert self.data.get(CONF_USER_ID) is not None
        assert self.data.get(CONF_API_TOKEN) is not None
        assert self.data.get(CONF_BP_NUMBER) is not None

        errors: dict[str, str] = {}
        if (
            user_input is not None
            and user_input.get(CONF_SELECTED_CONTRACTS) is not None
        ):
            if len(user_input.get(CONF_SELECTED_CONTRACTS)) == 0:
                errors["base"] = "no_contracts"
            else:
                data = {**self.data, **user_input}
                if data.get(CONF_AVAILABLE_CONTRACTS):
                    data.pop(CONF_AVAILABLE_CONTRACTS)

                self.data = data
                return self._async_create_iec_entry(data)

        schema = {
            vol.Required(
                CONF_SELECTED_CONTRACTS, default=self.data.get(CONF_AVAILABLE_CONTRACTS)
            ): multi_select(self.data.get(CONF_AVAILABLE_CONTRACTS))
        }

        return self.async_show_form(
            step_id="select_contracts",
            data_schema=vol.Schema(schema),
            errors=errors,
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

        client: IecClient = self.client

        if user_input is not None and user_input[CONF_TOTP_SECRET] is not None:
            assert client
            data = {**self.reauth_entry.data, **user_input}
            errors = await _validate_login(self.hass, data, client)
            if not errors:
                data[CONF_API_TOKEN] = client.get_token().to_dict()

                if data.get(CONF_TOTP_SECRET):
                    data.pop(CONF_TOTP_SECRET)

                self.hass.config_entries.async_update_entry(
                    self.reauth_entry, data=data
                )
                await self.hass.config_entries.async_reload(self.reauth_entry.entry_id)
                return self.async_abort(reason="reauth_successful")

        if not client:
            self.client = IecClient(
                self.data[CONF_USER_ID], async_create_clientsession(self.hass)
            )
            client = self.client

        await client.login_with_id()

        schema = {
            vol.Required(CONF_USER_ID): self.reauth_entry.data[CONF_USER_ID],
            vol.Required(CONF_TOTP_SECRET): str,
        }

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(schema),
            errors=errors,
        )
