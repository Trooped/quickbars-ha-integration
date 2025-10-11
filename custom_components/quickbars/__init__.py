"""The QuickBars for Home Assistant Integration"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import asyncio
import logging

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.components import zeroconf as ha_zc
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from zeroconf import ServiceStateChange
from zeroconf.asyncio import AsyncServiceBrowser

import secrets

from quickbars_bridge.events import ws_ping
from quickbars_bridge.hass_helpers import build_notify_payload

from .constants import DOMAIN  # DOMAIN = "quickbars"

_LOGGER = logging.getLogger(__name__)

EVENT_NAME = "quickbars.open"
SERVICE_TYPE = "_quickbars._tcp.local."

# camera positions
POS_CHOICES = ["top_left", "top_right", "bottom_left", "bottom_right"]

# ----- Service Schemas -----
QUICKBAR_SCHEMA = vol.Schema({
    vol.Required("alias"): cv.string,
    vol.Optional("device_id"): cv.string,      
})

CAMERA_SCHEMA = vol.Schema({
    # Exactly one of these:
    vol.Exclusive("camera_alias",  "cam_id"): cv.string,
    vol.Exclusive("camera_entity", "cam_id"): cv.entity_id,

    # Optional rendering options
    vol.Optional("position"): vol.In(POS_CHOICES),

    # Either preset size OR custom size in px
    vol.Exclusive("size", "cam_size"): vol.In(["small", "medium", "large"]),
    vol.Exclusive("size_px", "cam_size"): vol.Schema({
        vol.Required("w"): vol.All(vol.Coerce(int), vol.Range(min=48, max=3840)),
        vol.Required("h"): vol.All(vol.Coerce(int), vol.Range(min=48, max=2160)),
    }),

    # Auto-hide in seconds: 0 = never, 15..300 otherwise
    vol.Optional("auto_hide", default=30): vol.All(vol.Coerce(int), vol.Range(min=0, max=300)),

    # Show title overlay?
    vol.Optional("show_title", default=True): cv.boolean,

    vol.Optional("device_id"): cv.string, 
})


# ============ Connectivity (Coordinator) ============
class QuickBarsCoordinator(DataUpdateCoordinator[bool]):
    """Polls /api/ping periodically to determine connectivity."""

    def __init__(self, hass: HomeAssistant, entry: config_entries.ConfigEntry) -> None:
        self.entry = entry
        super().__init__(
            hass,
            _LOGGER,
            name=f"quickbars_{entry.entry_id}_conn",
            update_interval=timedelta(seconds=10),
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
            raise UpdateFailed(f"WS ping error: {e}") from e


class _Presence:
    """Zeroconf: track the app instance and keep host/port fresh."""

    def __init__(self, hass: HomeAssistant, entry: config_entries.ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._browser: AsyncServiceBrowser | None = None
        self._aiozc = None

    async def start(self) -> None:
        self._aiozc = await ha_zc.async_get_async_instance(self.hass)
        self._browser = AsyncServiceBrowser(
            self._aiozc.zeroconf, SERVICE_TYPE, handlers=[self._on_change]
        )

    async def stop(self) -> None:
        if self._browser:
            await self._browser.async_cancel()
            self._browser = None

    def _on_change(self, *args, **kwargs) -> None:
        if kwargs:
            service_type = kwargs.get("service_type")
            name = kwargs.get("name")
            state_change = kwargs.get("state_change")
        else:
            _, service_type, name, state_change = args
        self.hass.async_create_task(self._handle_change(service_type, name, state_change))

    async def _handle_change(self, service_type: str, name: str, state_change: ServiceStateChange) -> None:
        if service_type != SERVICE_TYPE:
            return

        wanted_id = (self.entry.data.get("id") or "").strip().lower()

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
        if host and port and (
            host != self.entry.data.get(CONF_HOST)
            or port != self.entry.data.get(CONF_PORT)
        ):
            new_data = {**self.entry.data, CONF_HOST: host, CONF_PORT: port}
            _LOGGER.debug("Presence: updating host/port -> %s:%s", host, port)
            self.hass.config_entries.async_update_entry(self.entry, data=new_data)

            coordinator = self.hass.data[DOMAIN][self.entry.entry_id].get("coordinator")
            if coordinator:
                self.hass.async_create_task(coordinator.async_request_refresh())


async def async_setup(hass: HomeAssistant, _config: dict[str, Any]) -> bool:
    """Register global service actions so they exist even without entries."""

    def _entry_for_device(device_id: str | None):
        """Resolve our config entry from a HA device_id; fallback if only one entry exists."""
        entries = hass.config_entries.async_entries(DOMAIN)
        if device_id:
            dev = dr.async_get(hass).async_get(device_id)
            if dev:
                ident = next((v for (d, v) in dev.identifiers if d == DOMAIN), None)
                if ident:
                    for ent in entries:
                        if ent.data.get("id") == ident or ent.entry_id == ident:
                            return ent
        if len(entries) == 1:
            return entries[0]
        return None  # ambiguous or none configured

    async def handle_quickbar(call: ServiceCall) -> None:
        data: dict[str, Any] = {"alias": call.data["alias"]}
        target_device_id = call.data.get("device_id")
        if target_device_id:
            ent = _entry_for_device(target_device_id)
            if ent:
                data["id"] = ent.data.get("id") or ent.entry_id
        hass.bus.async_fire(EVENT_NAME, data)

    hass.services.async_register(DOMAIN, "quickbar_toggle", handle_quickbar, QUICKBAR_SCHEMA)

    async def handle_camera(call: ServiceCall) -> None:
        data: dict[str, Any] = {}
        # optional device targeting
        target_device_id = call.data.get("device_id")
        if target_device_id:
            ent = _entry_for_device(target_device_id)
            if ent:
                data["id"] = ent.data.get("id") or ent.entry_id

        # id/alias
        alias = call.data.get("camera_alias")
        entity = call.data.get("camera_entity")
        if alias:
            data["camera_alias"] = alias
        if entity:
            data["camera_entity"] = entity

        # options
        pos = call.data.get("position")
        if pos in POS_CHOICES:
            data["position"] = pos
        if "size" in call.data:
            data["size"] = call.data["size"]  # small|medium|large
        elif "size_px" in call.data:
            sp = call.data["size_px"] or {}
            try:
                w = int(sp.get("w")); h = int(sp.get("h"))
                if w > 0 and h > 0:
                    data["size_px"] = {"w": w, "h": h}
            except Exception:
                pass
        auto_hide = call.data.get("auto_hide")
        if isinstance(auto_hide, int):
            if auto_hide != 0 and auto_hide < 5:
                auto_hide = 5
            data["auto_hide"] = auto_hide
        show_title = call.data.get("show_title")
        if isinstance(show_title, bool):
            data["show_title"] = show_title

        hass.bus.async_fire(EVENT_NAME, data)

    hass.services.async_register(DOMAIN, "camera_toggle", handle_camera, CAMERA_SCHEMA)

    async def _svc_notify(call: ServiceCall) -> None:
        # Resolve entry for optional device scoping
        target_device_id = call.data.get("device_id")
        entry2 = _entry_for_device(target_device_id) if target_device_id else None

        # Build payload via your helper (unchanged behavior)
        payload = await build_notify_payload(hass, call.data)

        # Add integration id if we targeted a device
        if entry2:
            payload["id"] = entry2.data.get("id") or entry2.entry_id

        # Correlation id (use provided or generate)
        cid = call.data.get("cid") or secrets.token_urlsafe(8)
        payload["cid"] = cid

        # Fire events
        hass.bus.async_fire("quickbars.notify", payload)

        # Optional: emit a metadata event if we have device metadata available
        if entry2:
            dev_meta = hass.data.get(DOMAIN, {}).get(entry2.entry_id, {})
            dev_id = dev_meta.get("device_id")
            hass.bus.async_fire(
                f"{DOMAIN}.notification_sent",
                {
                    **({"device_id": dev_id} if dev_id else {}),
                    "entry_id": entry2.entry_id,
                    "cid": cid,
                    "title": payload.get("title"),
                },
            )

    # Note: no voluptuous schema for notify (by design)
    hass.services.async_register(DOMAIN, "notify", _svc_notify)

    return True


async def async_setup_entry(hass: HomeAssistant, entry: config_entries.ConfigEntry) -> bool:
    """Per-entry setup: presence tracking, coordinator, device registration, and action -> HA event bridge."""
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {"snapshot": None}

    # Create a Device for device_id targeting
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.data.get("id") or entry.entry_id)},
        manufacturer="QuickBars",
        name=entry.title or "QuickBars TV",
    )
    hass.data[DOMAIN][entry.entry_id]["device_id"] = device.id

    # Presence (Zeroconf) and connectivity
    presence = _Presence(hass, entry)
    await presence.start()

    coordinator = QuickBarsCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()

    # Bridge TV button clicks -> HA event (per-entry)
    def _on_action(evt):
        data = evt.data or {}
        exp_id = entry.data.get("id") or entry.entry_id
        if data.get("id") and data.get("id") != exp_id:
            return
        hass.bus.async_fire(
            f"{DOMAIN}.notification_action",
            {
                "device_id": hass.data[DOMAIN][entry.entry_id]["device_id"],
                "entry_id": entry.entry_id,
                "cid": data.get("cid"),
                "action_id": data.get("action_id"),
                "label": data.get("label"),
            },
        )

    unsub_action = hass.bus.async_listen("quickbars.action", _on_action)

    hass.data[DOMAIN][entry.entry_id].update(
        presence=presence,
        coordinator=coordinator,
        unsub_actions=unsub_action,
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: config_entries.ConfigEntry) -> bool:
    stored = hass.data.get(DOMAIN, {}).pop(entry.entry_id, {})
    if (u := stored.get("unsub_actions")):
        u()
    if (presence := stored.get("presence")):
        await presence.stop()
    # no platforms to unload
    return True