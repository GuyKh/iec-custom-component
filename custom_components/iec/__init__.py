"""The IEC integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .coordinator import IecApiCoordinator

type IecConfigEntry = ConfigEntry[IecApiCoordinator]

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: IecConfigEntry) -> bool:
    """Set up IEC from a config entry."""
    iec_coordinator = IecApiCoordinator(hass, entry)
    entry.runtime_data = iec_coordinator

    await iec_coordinator.async_config_entry_first_refresh()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: IecConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        coordinator = entry.runtime_data
        if coordinator:
            await coordinator.async_unload()
        entry.runtime_data = None  # type: ignore[assignment]

    return unload_ok
