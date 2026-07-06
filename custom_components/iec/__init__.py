"""The IEC integration."""

from __future__ import annotations
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .coordinator import IecApiCoordinator

type IecConfigEntry = ConfigEntry[IecApiCoordinator]

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR]
_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: IecConfigEntry) -> bool:
    """Set up IEC from a config entry."""
    iec_coordinator = IecApiCoordinator(hass, entry)
    entry.runtime_data = iec_coordinator

    await iec_coordinator.async_config_entry_first_refresh()

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    if not hass.services.has_service(DOMAIN, "debug_get_coordinator_data"):

        async def handle_debug_get_coordinator_data(call) -> None:  # noqa: ANN001 ARG001
            for loaded_entry in hass.config_entries.async_entries(DOMAIN):
                coordinator: IecApiCoordinator | None = loaded_entry.runtime_data
                if coordinator is None:
                    continue
                _LOGGER.info(
                    "Coordinator data (entry %s): %s",
                    loaded_entry.entry_id,
                    coordinator.data,
                )
                hass.bus.async_fire(
                    "custom_component_debug_event",
                    {
                        "entry_id": loaded_entry.entry_id,
                        "data": coordinator.data,
                    },
                )

        hass.services.async_register(
            DOMAIN, "debug_get_coordinator_data", handle_debug_get_coordinator_data
        )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: IecConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        coordinator = entry.runtime_data
        if coordinator:
            await coordinator.async_unload()
        entry.runtime_data = None  # type: ignore[assignment]

    remaining = [
        e
        for e in hass.config_entries.async_entries(DOMAIN)
        if e.runtime_data is not None
    ]
    if not remaining:
        hass.services.async_remove(
            DOMAIN, "debug_get_coordinator_data", missing_ok=True
        )

    return unload_ok
