"""The IEC integration."""

from __future__ import annotations
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall

from .const import DOMAIN
from .coordinator import IecApiCoordinator

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR]
_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up IEC from a config entry."""
    if DOMAIN not in hass.data:
        hass.data[DOMAIN] = {}

    iec_coordinator = IecApiCoordinator(hass, entry)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = iec_coordinator
    try:
        await hass.data[DOMAIN][entry.entry_id].async_config_entry_first_refresh()
    except Exception as err:
        # Log the error but don't fail the setup
        _LOGGER.error("Failed to fetch initial data: %s", err)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Register the debug service
    async def handle_debug_get_coordinator_data(call) -> None:  # noqa: ANN001 ARG001
        # Log or return coordinator data
        data = iec_coordinator.data
        _LOGGER.info("Coordinator data: %s", data)
        hass.bus.async_fire("custom_component_debug_event", {"data": data})

    hass.services.async_register(
        DOMAIN, "debug_get_coordinator_data", handle_debug_get_coordinator_data
    )

    async def handle_update_statistics_date(call: ServiceCall) -> None:
        datetime_str = call.data.get("datetime")
        device_number = call.data.get("device_number")
        if not datetime_str or not device_number:
            _LOGGER.error("update_statistics_date: missing required fields (datetime, device_number)")
            return
        result = await iec_coordinator.set_statistics_from_date(str(datetime_str), str(device_number))
        _LOGGER.info("update_statistics_date result: %s", result)
        hass.bus.async_fire("iec_statistics_date_updated", {"result": result})

    hass.services.async_register(
        DOMAIN, "update_statistics_date", handle_update_statistics_date
    )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        coordinator = hass.data[DOMAIN].pop(entry.entry_id, None)
        if coordinator:
            await coordinator.async_unload()

    return unload_ok
