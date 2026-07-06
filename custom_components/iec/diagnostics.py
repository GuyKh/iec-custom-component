"""Diagnostics support for the IEC integration."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_API_TOKEN
from homeassistant.core import HomeAssistant
from homeassistant.components.diagnostics import async_redact_data

from .const import (
    CONF_BP_NUMBER,
    CONF_BP_NUMBER_TO_CONTRACT,
    CONF_USER_ID,
    JWT_DICT_NAME,
)

TO_REDACT = {
    CONF_API_TOKEN,
    CONF_USER_ID,
    CONF_BP_NUMBER,
    CONF_BP_NUMBER_TO_CONTRACT,
    JWT_DICT_NAME,
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data

    data: dict[str, Any] = {
        "entry": async_redact_data(entry.data, TO_REDACT),
        "coordinator_data": async_redact_data(coordinator.data, TO_REDACT)
        if coordinator.data
        else None,
    }

    return data
