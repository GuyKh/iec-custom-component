"""Config flow for IEC integration."""

from __future__ import annotations

import logging
import asyncio
from collections import defaultdict
from collections.abc import Mapping
from typing import Any, TYPE_CHECKING

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_API_TOKEN
from homeassistant.core import HomeAssistant, callback

if TYPE_CHECKING:
    from typing import Any as ConfigFlowResult
else:
    try:
        from homeassistant.config_entries import ConfigFlowResult
    except ImportError:
        ConfigFlowResult = Any

from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.config_validation import multi_select
from iec_api.iec_client import IecClient
from iec_api.models.contract import Contract
from iec_api.masa_api_models.contact_account_user_profile import MainPortalContract
from iec_api.models.exceptions import IECError
from iec_api.models.jwt import JWT

from .const import (
    CONF_AVAILABLE_CONTRACTS,
    CONF_BP_NUMBER,
    CONF_BP_NUMBER_TO_CONTRACT,
    CONF_SELECTED_CONTRACTS,
    CONF_TOTP_SECRET,
    CONF_USER_ID,
    CONF_OTP_METHOD,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)
CONTRACT_OPTIONS_KEY = "available_contract_options"

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USER_ID): str,
        vol.Required(CONF_OTP_METHOD, default="sms"): vol.In({"sms": "SMS", "email": "Email"}),
    }
)


async def _validate_login(
    hass: HomeAssistant, login_data: dict[str, Any], api: IecClient
) -> dict[str, str]:
    """Validate login data and return any errors."""
    if not login_data or not api:
        return {"base": "cannot_connect"}
    if not login_data.get(CONF_USER_ID):
        return {"base": "invalid_auth"}
    if not (login_data.get(CONF_TOTP_SECRET) or login_data.get(CONF_API_TOKEN)):
        return {"base": "invalid_auth"}

    normalized_otp_secret = _normalize_otp_secret(login_data.get(CONF_TOTP_SECRET))
    if login_data.get(CONF_TOTP_SECRET):
        if not normalized_otp_secret:
            return {"base": "invalid_auth"}
        try:
            login_data[CONF_TOTP_SECRET] = normalized_otp_secret
            await api.verify_otp(normalized_otp_secret)
        except asyncio.CancelledError:
            return {"base": "cannot_connect"}
        except IECError:
            return {"base": "invalid_auth"}

    elif login_data.get(CONF_API_TOKEN):
        try:
            await api.load_jwt_token(JWT.from_dict(login_data[CONF_API_TOKEN]))
        except asyncio.CancelledError:
            return {"base": "cannot_connect"}
        except IECError:
            return {"base": "invalid_auth"}

    errors: dict[str, str] = {}
    try:
        await api.check_token()
    except asyncio.CancelledError:
        errors["base"] = "cannot_connect"
    except IECError:
        errors["base"] = "invalid_auth"

    return errors


def _build_contract_label(contract_id: int, address: str | None) -> str:
    normalized_address = address or "Unknown Address"
    return f"Contract {contract_id} - {normalized_address}"


def _normalize_bp_number(bp_number: str | None) -> str | None:
    if not bp_number:
        return None
    try:
        return str(int(bp_number))
    except ValueError:
        return bp_number


def _normalize_otp_secret(otp_secret: str | None) -> str:
    if not otp_secret:
        return ""
    return "".join(char for char in otp_secret if char.isdigit())


def _filter_bp_number_to_contract(
    bp_number_to_contract: dict[str, list[int]], selected_contracts: list[int]
) -> dict[str, list[int]]:
    selected_set = set(selected_contracts)
    filtered: dict[str, list[int]] = {}
    for bp_number, contracts in bp_number_to_contract.items():
        matched = sorted(contract for contract in contracts if contract in selected_set)
        if matched:
            filtered[bp_number] = matched
    return filtered


async def _build_bp_number_to_contract(
    client: IecClient,
) -> tuple[dict[str, list[int]], dict[str, str]]:
    bp_number_to_contract: dict[str, set[int]] = defaultdict(set)
    contract_labels: dict[str, str] = {}
    
    user_profile = None
    bp_numbers = set()

    try:
        user_profile = await client.get_masa_contact_account_user_profile()
        if user_profile and user_profile.accounts:
            bp_numbers.update(
                normalized_bp
                for account in user_profile.accounts
                if (normalized_bp := _normalize_bp_number(account.account_number)) is not None
            )
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("Failed to fetch user profile for shared accounts: %s", err)

    # Fallback: If masa API failed or returned empty accounts, try get_customer
    if not bp_numbers:
        try:
            customer = await client.get_customer()
            if customer and customer.bp_number:
                normalized_bp = _normalize_bp_number(customer.bp_number)
                if normalized_bp:
                    bp_numbers.add(normalized_bp)
        except Exception as err:
            _LOGGER.debug("Fallback to get_customer failed: %s", err)

    if not bp_numbers:
        return {}, {}

    for bp_number in bp_numbers:
        try:
            contracts = await client.get_contracts(bp_number)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Failed to fetch contracts for bp %s: %s", bp_number, err)
            continue

        for contract in contracts:
            if contract.status != 1:
                continue
            contract_id = int(contract.contract_id)
            bp_number_to_contract[bp_number].add(contract_id)
            if str(contract_id) not in contract_labels:
                contract_labels[str(contract_id)] = _build_contract_label(
                    contract_id, contract.address
                )

    if user_profile and user_profile.connection_between_contact_and_contract:
        for connection in user_profile.connection_between_contact_and_contract:
            portal_contract: MainPortalContract = connection.contract
            if not portal_contract or not portal_contract.site:
                continue

            contract_id = portal_contract.contract_acc_number_in_shoval
            if not contract_id:
                continue

            normalized_contract_id = int(contract_id)
            shared_bp_number: str | None = None
            try:
                customer_mobile = await client.get_customer_mobile(str(contract_id))
                if customer_mobile and customer_mobile.customer:
                    shared_bp_number = _normalize_bp_number(
                        customer_mobile.customer.bp_number
                    )
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug(
                    "Failed to resolve bp_number for shared contract %s: %s",
                    normalized_contract_id,
                    err,
                )

            if not shared_bp_number:
                continue

            bp_number_to_contract[shared_bp_number].add(normalized_contract_id)
            contract_labels[str(normalized_contract_id)] = _build_contract_label(
                normalized_contract_id,
                portal_contract.site.full_address,
            )

    return (
        {
            bp_number: sorted(contract_ids)
            for bp_number, contract_ids in bp_number_to_contract.items()
        },
        contract_labels,
    )


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
    ) -> ConfigFlowResult:
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
    ) -> ConfigFlowResult:
        """Handle MFA step."""
        if not self.data or not self.data.get(CONF_USER_ID):
            return self.async_show_form(
                step_id="user",
                data_schema=STEP_USER_DATA_SCHEMA,
                errors={"base": "invalid_auth"},
            )

        assert self.client is not None
        client: IecClient = self.client

        errors: dict[str, str] = {}
        if user_input is not None and user_input.get(CONF_TOTP_SECRET) is not None:
            try:
                data = {**self.data, **user_input}
                errors = await _validate_login(self.hass, data, client)
                if not errors:
                    data[CONF_API_TOKEN] = client.get_token().to_dict()

                    if data.get(CONF_TOTP_SECRET):
                        data.pop(CONF_TOTP_SECRET)

                    try:
                        (
                            bp_number_to_contract,
                            contract_labels,
                        ) = await _build_bp_number_to_contract(client)
                        contract_ids = sorted(
                            {
                                contract_id
                                for contract_ids_by_bp in bp_number_to_contract.values()
                                for contract_id in contract_ids_by_bp
                            }
                        )
                    except asyncio.CancelledError:
                        errors["base"] = "cannot_connect"
                    except IECError:
                        errors["base"] = "cannot_connect"
                    except Exception as err:  # noqa: BLE001
                        _LOGGER.exception(
                            "Unexpected error during contracts fetch: %s", err
                        )
                        errors["base"] = "cannot_connect"

                    if not errors:
                        if len(contract_ids) == 0:
                            errors["base"] = "no_active_contracts"
                        elif len(contract_ids) == 1:
                            selected_contracts = [contract_ids[0]]
                            data[CONF_SELECTED_CONTRACTS] = selected_contracts
                            data[CONF_BP_NUMBER_TO_CONTRACT] = (
                                _filter_bp_number_to_contract(
                                    bp_number_to_contract, selected_contracts
                                )
                            )
                            data.pop(CONF_BP_NUMBER, None)
                            return self._async_create_iec_entry(data)
                        else:
                            data[CONF_AVAILABLE_CONTRACTS] = contract_ids
                            data[CONTRACT_OPTIONS_KEY] = {
                                str(contract_id): contract_labels.get(
                                    str(contract_id),
                                    _build_contract_label(contract_id, None),
                                )
                                for contract_id in contract_ids
                            }
                            data[CONF_BP_NUMBER_TO_CONTRACT] = bp_number_to_contract
                            data.pop(CONF_BP_NUMBER, None)
                            self.data = data
                            return await self.async_step_select_contracts()
            except asyncio.CancelledError:
                errors["base"] = "cannot_connect"
            except IECError:
                errors["base"] = "cannot_connect"
            except Exception as err:  # noqa: BLE001
                _LOGGER.exception("Unexpected error during MFA step: %s", err)
                errors["base"] = "cannot_connect"

        if errors:
            schema = {vol.Required(CONF_USER_ID, default=self.data[CONF_USER_ID]): str}
        else:
            schema = {}

        schema[vol.Required(CONF_TOTP_SECRET)] = str
        try:
            prefer_sms = self.data.get(CONF_OTP_METHOD, "sms") == "sms"
            otp_type = await client.login_with_id(prefer_sms=prefer_sms)
        except asyncio.CancelledError:
            errors["base"] = errors.get("base") or "cannot_connect"
            otp_type = "OTP"
        except IECError:
            errors["base"] = errors.get("base") or "cannot_connect"
            otp_type = "OTP"
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("Unexpected error during login_with_id: %s", err)
            errors["base"] = errors.get("base") or "cannot_connect"
            otp_type = "OTP"

        return self.async_show_form(
            step_id="mfa",
            data_schema=vol.Schema(schema),
            description_placeholders={"otp_type": otp_type},
            errors=errors,
        )

    @callback
    def _async_create_iec_entry(self, data: dict[str, Any]) -> ConfigFlowResult:
        """Create the config entry."""
        return self.async_create_entry(
            title=f"IEC Account ({data[CONF_USER_ID]})",
            data=data,
        )

    async def async_step_select_contracts(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle Select Contract step."""
        assert self.data is not None
        assert self.data.get(CONF_USER_ID) is not None
        assert self.data.get(CONF_API_TOKEN) is not None
        assert self.data.get(CONF_BP_NUMBER_TO_CONTRACT) is not None
        assert self.data.get(CONTRACT_OPTIONS_KEY) is not None

        errors: dict[str, str] = {}
        if (
            user_input is not None
            and user_input.get(CONF_SELECTED_CONTRACTS) is not None
        ):
            selected_contracts = [
                int(contract_id)
                for contract_id in user_input.get(CONF_SELECTED_CONTRACTS, [])
            ]

            if len(selected_contracts) == 0:
                errors["base"] = "no_contracts"
            else:
                data = {**self.data}
                data[CONF_SELECTED_CONTRACTS] = selected_contracts
                data[CONF_BP_NUMBER_TO_CONTRACT] = _filter_bp_number_to_contract(
                    data[CONF_BP_NUMBER_TO_CONTRACT], selected_contracts
                )
                if data.get(CONF_AVAILABLE_CONTRACTS):
                    data.pop(CONF_AVAILABLE_CONTRACTS)
                data.pop(CONTRACT_OPTIONS_KEY, None)
                data.pop(CONF_BP_NUMBER, None)

                self.data = data
                return self._async_create_iec_entry(data)

        schema = {
            vol.Required(
                CONF_SELECTED_CONTRACTS,
                default=[
                    str(contract_id)
                    for contract_id in self.data.get(CONF_AVAILABLE_CONTRACTS, [])
                ],
            ): multi_select(self.data.get(CONTRACT_OPTIONS_KEY))  # type: ignore
        }

        return self.async_show_form(  # type: ignore
            step_id="select_contracts",
            data_schema=vol.Schema(schema),
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Handle configuration by re-auth."""
        self.reauth_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Dialog that informs the user that reauth is required."""
        assert self.reauth_entry
        errors: dict[str, str] = {}

        if user_input is not None:
            self.reauth_data = {**self.reauth_entry.data, **user_input}
            return await self.async_step_reauth_mfa()

        schema = {
            vol.Required(CONF_USER_ID, default=self.reauth_entry.data.get(CONF_USER_ID)): str,
            vol.Required(
                CONF_OTP_METHOD,
                default=self.reauth_entry.data.get(CONF_OTP_METHOD, "sms")
            ): vol.In({"sms": "SMS", "email": "Email"}),
        }

        return self.async_show_form(  # type: ignore
            step_id="reauth_confirm",
            data_schema=vol.Schema(schema),
            errors=errors,
        )

    async def async_step_reauth_mfa(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle MFA step during reauth."""
        assert self.reauth_entry
        assert hasattr(self, "reauth_data")
        errors: dict[str, str] = {}

        if not self.client:
            self.client = IecClient(
                self.reauth_data[CONF_USER_ID],
                async_create_clientsession(self.hass),
            )
        client = self.client

        if user_input is not None and user_input.get(CONF_TOTP_SECRET) is not None:
            assert client is not None
            data = {**self.reauth_data, **user_input}
            errors = await _validate_login(self.hass, data, client)
            if not errors:
                data[CONF_API_TOKEN] = client.get_token().to_dict()

                if data.get(CONF_TOTP_SECRET):
                    data.pop(CONF_TOTP_SECRET)

                self.hass.config_entries.async_update_entry(
                    self.reauth_entry, data=data
                )
                await self.hass.config_entries.async_reload(self.reauth_entry.entry_id)
                return self.async_abort(reason="reauth_successful")  # type: ignore

        try:
            prefer_sms = self.reauth_data.get(CONF_OTP_METHOD, "sms") == "sms"
            otp_type = await client.login_with_id(prefer_sms=prefer_sms)
        except asyncio.CancelledError:
            errors["base"] = errors.get("base") or "cannot_connect"
            otp_type = "OTP"
        except IECError:
            errors["base"] = errors.get("base") or "cannot_connect"
            otp_type = "OTP"
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("Unexpected error during reauth login_with_id: %s", err)
            errors["base"] = errors.get("base") or "cannot_connect"
            otp_type = "OTP"

        schema = {
            vol.Required(CONF_TOTP_SECRET): str,
        }

        return self.async_show_form(  # type: ignore
            step_id="reauth_mfa",
            description_placeholders={"otp_type": otp_type},
            data_schema=vol.Schema(schema),
            errors=errors,
        )
