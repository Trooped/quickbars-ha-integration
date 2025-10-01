"""The QuickBars for Home Assistant Integration"""

from __future__ import annotations
from datetime import timedelta
from typing import Any

import logging
_LOGGER = logging.getLogger(__name__)

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant, ServiceCall
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.typing import ConfigType
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.helpers import device_registry as dr
from homeassistant.data_entry_flow import FlowResult
import asyncio
from homeassistant.components import zeroconf as ha_zc
from zeroconf.asyncio import AsyncServiceBrowser
from zeroconf import ServiceStateChange

from .client import get_snapshot, post_snapshot, ping, ws_ping
from .constants import DOMAIN  # DOMAIN = "quickbars"


EVENT_NAME = "quickbars.open"
SERVICE_TYPE = "_quickbars._tcp.local."

# Allowed domains (inlined to avoid bad import paths)
ALLOWED_DOMAINS = [
    "light", "switch", "button", "fan", "input_boolean", "input_button",
    "script", "scene", "climate", "cover", "sensor", "binary_sensor",
    "lock", "alarm_control_panel", "camera", "automation", "media_player",
]

# ----- Service Schemas -----
QUICKBAR_SCHEMA = vol.Schema({vol.Required("alias"): cv.string})
CAMERA_SCHEMA = vol.Schema({vol.Required("camera_alias"): cv.string})


# ============ Connectivity (Coordinator) ============
class QuickBarsCoordinator(DataUpdateCoordinator[bool]):
    """Polls /api/ping periodically to determine connectivity."""

    def __init__(self, hass: HomeAssistant, entry: config_entries.ConfigEntry) -> None:
        self.entry = entry
        super().__init__(
            hass,
            _LOGGER,
            name=f"quickbars_{entry.entry_id}_conn",
            update_interval=timedelta(seconds=10),  # adjust if you want
        )

    async def _async_update_data(self) -> bool:
        """Single-shot WS connectivity check (no HTTP)."""
        try:
            ok = await ws_ping(self.hass, self.entry, timeout=5.0)
            if not ok:
                raise UpdateFailed()
            return True
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # Make the integration show as unavailable/failed, HA will retry later
            raise UpdateFailed(f"WS ping error: {e}") from e


class _Presence:
    """Zeroconf: track the app instance and keep host/port fresh."""

    def __init__(self, hass: HomeAssistant, entry: config_entries.ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._browser: AsyncServiceBrowser | None = None
        self._aiozc = None

    async def start(self) -> None:
        self._aiozc = await ha_zc.async_get_async_instance(self.hass)  # shared AsyncZeroconf
        self._browser = AsyncServiceBrowser(
            self._aiozc.zeroconf, SERVICE_TYPE, handlers=[self._on_change]
        )

    async def stop(self) -> None:
        if self._browser:
            await self._browser.async_cancel()
            self._browser = None

    def _on_change(self, *args, **kwargs) -> None:
        """Compat handler for zeroconf callbacks (positional or keyword)."""
        if kwargs:
            service_type = kwargs.get("service_type")
            name = kwargs.get("name")
            state_change = kwargs.get("state_change")
        else:
            # Old style: (zeroconf, service_type, name, state_change)
            _, service_type, name, state_change = args

        # Defer real work to an async task
        self.hass.async_create_task(self._handle_change(service_type, name, state_change))

    async def _handle_change(self, service_type: str, name: str, state_change: ServiceStateChange) -> None:
        if service_type != SERVICE_TYPE:
            return

        wanted_id = (self.entry.data.get("id") or "").strip().lower()

        # Only resolve on add/update — we no longer synthesize availability here;
        # the coordinator ping is the source of truth for connectivity.
        if state_change is ServiceStateChange.Removed:
            return

        info = await self._aiozc.async_get_service_info(service_type, name, 3000)
        if not info:
            return

        props: dict[str, str] = {}
        for k, v in (info.properties or {}).items():
            key = k.decode() if isinstance(k, (bytes, bytearray)) else str(k)
            val = v.decode() if isinstance(v, (bytes, bytearray)) else str(v)
            props[key] = val

        found_id = (props.get("id") or "").strip().lower()
        if not found_id or found_id != wanted_id:
            return

        host = (info.parsed_addresses() or [self.entry.data.get(CONF_HOST)])[0]
        port = info.port or self.entry.data.get(CONF_PORT)
        if host and port and (host != self.entry.data.get(CONF_HOST) or port != self.entry.data.get(CONF_PORT)):
            new_data = {**self.entry.data, CONF_HOST: host, CONF_PORT: port}
            _LOGGER.debug("Presence: updating host/port -> %s:%s", host, port)
            self.hass.config_entries.async_update_entry(self.entry, data=new_data)

            coordinator = self.hass.data[DOMAIN][self.entry.entry_id].get("coordinator")
            if coordinator:
                self.hass.async_create_task(coordinator.async_request_refresh())


async def async_setup(hass: HomeAssistant, _config: dict[str, Any]) -> bool:
    """Register two tiny services that just fire quickbars.open."""
    async def handle_quickbar(call: ServiceCall) -> None:
        hass.bus.async_fire(EVENT_NAME, {"alias": call.data["alias"]})

    async def handle_camera(call: ServiceCall) -> None:
        hass.bus.async_fire(EVENT_NAME, {"camera_alias": call.data["camera_alias"]})

    hass.services.async_register(DOMAIN, "quickbar_toggle", handle_quickbar, QUICKBAR_SCHEMA)
    hass.services.async_register(DOMAIN, "camera_toggle", handle_camera, CAMERA_SCHEMA)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: config_entries.ConfigEntry) -> bool:
    """Set up QuickBars from a config entry."""
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {"snapshot": None}

    # Optional: create a Device even if we expose no entities
    dev_reg = dr.async_get(hass)
    dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.data.get("id") or entry.entry_id)},
        manufacturer="QuickBars",
        name=entry.title or "QuickBars TV",
    )

    # 1) Start presence (keeps host/port fresh)
    presence = _Presence(hass, entry)
    await presence.start()

    # 2) Start the ping coordinator (source of truth for connectivity)
    coordinator = QuickBarsCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()

    hass.data[DOMAIN][entry.entry_id].update(
        presence=presence,
        coordinator=coordinator,
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: config_entries.ConfigEntry) -> bool:
    stored = hass.data.get(DOMAIN, {}).pop(entry.entry_id, {})
    presence = stored.get("presence")
    if presence:
        await presence.stop()
    # no platforms to unload
    return True
