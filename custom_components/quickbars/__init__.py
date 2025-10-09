"""The QuickBars for Home Assistant Integration"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import asyncio
import logging

import base64

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.components import zeroconf as ha_zc
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.network import get_url
from homeassistant.helpers.typing import ConfigType
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from zeroconf import ServiceStateChange
from zeroconf.asyncio import AsyncServiceBrowser
from homeassistant.components import media_source
from homeassistant.components.media_player.browse_media import async_process_play_media_url
from homeassistant.components.http.auth import async_sign_path

import secrets

from .client import (
    ws_ping,
)
from .constants import DOMAIN  # DOMAIN = "quickbars"

_LOGGER = logging.getLogger(__name__)

EVENT_NAME = "quickbars.open"
SERVICE_TYPE = "_quickbars._tcp.local."

# Allowed domains (inlined to avoid bad import paths)
ALLOWED_DOMAINS = [
    "light", "switch", "button", "fan", "input_boolean", "input_button",
    "script", "scene", "climate", "cover", "sensor", "binary_sensor",
    "lock", "alarm_control_panel", "camera", "automation", "media_player",
]
# camera positions
POS_CHOICES = ["top_left", "top_right", "bottom_left", "bottom_right"]

# ----- Service Schemas -----
QUICKBAR_SCHEMA = vol.Schema({vol.Required("alias"): cv.string})

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

    # Create a Device even if we expose no entities (lets us target device_id)
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.data.get("id") or entry.entry_id)},
        manufacturer="QuickBars",
        name=entry.title or "QuickBars TV",
    )
    hass.data[DOMAIN][entry.entry_id]["device_id"] = device.id

    # 1) Presence
    presence = _Presence(hass, entry)
    await presence.start()

    # 2) Connectivity coordinator
    coordinator = QuickBarsCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()

    # ---------- helpers that close over hass/entry ----------

    def _entry_for_device(device_id: str | None):
        """Resolve our config entry from a HA device_id; fallback to the single entry."""
        if device_id:
            dev = dr.async_get(hass).async_get(device_id)
            if dev:
                ident = next((v for (d, v) in dev.identifiers if d == DOMAIN), None)
                if ident:
                    for ent in hass.config_entries.async_entries(DOMAIN):
                        if ent.data.get("id") == ident or ent.entry_id == ident:
                            return ent
        entries = hass.config_entries.async_entries(DOMAIN)
        if len(entries) == 1:
            return entries[0]
        raise ValueError("Multiple QuickBars entries present—supply device_id")

    def _abs(spec):
        if not isinstance(spec, dict):
            return None
        if spec.get("url"):
            return spec["url"]
        if spec.get("path"):
            base = get_url(hass)
            p = str(spec["path"]).lstrip("/")
            if not p.startswith("local/"):
                p = f"local/{p}"
            return f"{base}/{p}"
        return None
    
    async def handle_camera(call: ServiceCall) -> None:
        """
        Fire quickbars.open with camera info.
        The TV app should have imported the camera (has MJPEG URL) so alias/entity can be resolved client-side.
        """
        data: dict[str, Any] = {}

        alias = call.data.get("camera_alias")
        entity = call.data.get("camera_entity")

        if alias:
            data["camera_alias"] = alias
        if entity:
            data["camera_entity"] = entity  # optional: your app can match by entity_id

        # Extra options
        pos = call.data.get("position")
        if pos in POS_CHOICES:
            data["position"] = pos

        if "size" in call.data:
            data["size"] = call.data["size"]  # "small" | "medium" | "large"
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
            # 0 = never (show until dismissed). Otherwise 5..300
            if auto_hide != 0 and auto_hide < 5:
                auto_hide = 5
            data["auto_hide"] = auto_hide

        show_title = call.data.get("show_title")
        if isinstance(show_title, bool):
            data["show_title"] = show_title

        # Tell the TV app to show the camera overlay
        hass.bus.async_fire("quickbars.open", data)

    # Register or update the service
    hass.services.async_register(DOMAIN, "camera_toggle", handle_camera, CAMERA_SCHEMA)

    async def _svc_notify(call: ServiceCall) -> None:
        entry2 = _entry_for_device(call.data.get("device_id"))

        # ---- normalize overlay color to #RRGGBB (from RGB selector or string) ----

        def _clamp8(x: int) -> int:
            return 0 if x < 0 else 255 if x > 255 else x

        col_in = call.data.get("color")
        color_hex: str | None = None
        if isinstance(col_in, (list, tuple)) and len(col_in) == 3:
            r, g, b = (_clamp8(int(col_in[0])), _clamp8(int(col_in[1])), _clamp8(int(col_in[2])))
            color_hex = f"#{r:02x}{g:02x}{b:02x}"

        elif isinstance(col_in, dict) and all(k in col_in for k in ("r", "g", "b")):
            r, g, b = (_clamp8(int(col_in["r"])), _clamp8(int(col_in["g"])), _clamp8(int(col_in["b"])))
            color_hex = f"#{r:02x}{g:02x}{b:02x}"

        elif isinstance(col_in, str) and col_in.strip():
            color_hex = col_in.strip()

        # ---- resolve mdi icon to SVG data URI (no color/size knobs) ----
        mdi_icon = call.data.get("mdi_icon")

        icon_svg_data_uri = None
        icon_url = None

        if mdi_icon:
            # Fallback URL the TV can fetch itself (works even if HA can't fetch)
            icon_id = mdi_icon.strip().replace(":", "%3A")
            icon_url = f"https://api.iconify.design/{icon_id}.svg"

            # Try to inline the SVG (may fail if HA has no internet)
            icon_svg_data_uri = await _mdi_svg_data_uri(hass, mdi_icon)

        img_url = None
        img_spec = call.data.get("image")
        if isinstance(img_spec, dict) and "media_id" in img_spec:
            img_url = await _abs_media_url(hass, img_spec)
        else:
            img_url = _abs(img_spec)  

        # Sound: support url, path, or media-source via the new object; also accept legacy sound_url
        sound_url = None
        if call.data.get("sound"):
            sound_url = await _abs_media_url(hass, call.data["sound"])

        sound_pct = None
        snd = call.data.get("sound")
        if isinstance(snd, dict) and "volume_percent" in snd:
            try:
                sound_pct = int(snd.get("volume_percent"))
            except (TypeError, ValueError):
                sound_pct = None
        elif "sound_volume_percent" in call.data:
            try:
                sound_pct = int(call.data.get("sound_volume_percent"))
            except (TypeError, ValueError):
                sound_pct = None

        # custom volume - if provided
        if sound_pct is not None:
            if sound_pct < 0: sound_pct = 0
            if sound_pct > 200: sound_pct = 200

        length_val = call.data.get("length")
        try:
            chosen_duration = int(length_val) if length_val is not None and str(length_val).strip() != "" else 6
        except Exception:
            chosen_duration = 6

        if chosen_duration < 3:
            chosen_duration = 3
        elif chosen_duration > 120:
            chosen_duration = 120

        payload = {
            "title":        call.data.get("title"),
            "message":      call.data["message"],
            "actions":      call.data.get("actions") or [],
            "duration":     chosen_duration,
            "position":     call.data.get("position"),
            "color":        color_hex,                      
            "transparency": call.data.get("transparency"),
            "interrupt":    bool(call.data.get("interrupt", False)),
            "image_url":    img_url,
            "sound_url":    sound_url,                      
            "sound_volume_percent": sound_pct,
            "icon_svg_data_uri": icon_svg_data_uri,       
            "icon_url":     icon_url,                      # fallback if SVG fetch failed
        }
        payload = {k: v for k, v in payload.items() if v not in (None, "", [])}

        # Correlation id: keep provided or generate one (to match old behavior)
        cid = call.data.get("cid") or secrets.token_urlsafe(8)
        payload["cid"] = cid  # include in the event payload just like before

        # Fire as a plain HA event (like quickbars.open)
        hass.bus.async_fire("quickbars.notify", payload)

        # Let automations latch onto the correlation id if desired
        hass.bus.async_fire(
            f"{DOMAIN}.notification_sent",
            {
                "device_id": hass.data[DOMAIN][entry.entry_id]["device_id"],
                "entry_id": entry2.entry_id,
                "cid": cid,
                "title": payload.get("title"),
            },
        )

    # Register interactive prompt service
    hass.services.async_register(DOMAIN, "notify", _svc_notify)

    # Bridge TV button clicks -> HA event (dynamic notifications)
    def _on_action(evt):
        data = evt.data or {}

        # Optional scoping by integration id:
        # If your Android client includes "id" in quickbars.action, keep this filter.
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

    # Register the listener
    unsub_action = hass.bus.async_listen("quickbars.action", _on_action)

    # Store handles for unload
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


async def _mdi_svg_data_uri(hass, mdi_icon: str) -> str | None:
    """Fetch mdi:<name> as SVG and return as a data URI (base64)."""
    try:
        icon = (mdi_icon or "").strip()
        if not icon:
            return None
        icon_id = icon.replace(":", "%3A")
        url = f"https://api.iconify.design/{icon_id}.svg"
        session = async_get_clientsession(hass)
        async with session.get(url, timeout=8) as resp:
            if resp.status != 200:
                _LOGGER.warning("iconify fetch failed (%s): %s", resp.status, url)
                return None
            svg_bytes = await resp.read()
        b64 = base64.b64encode(svg_bytes).decode()
        return f"data:image/svg+xml;base64,{b64}"
    except Exception as e:
        _LOGGER.warning("iconify fetch error for %s: %s", mdi_icon, e)
        return None
    
async def _abs_media_url(hass, spec) -> str | None:
    """
    Resolve a media spec into an absolute, fetchable URL.
    Supports:
      - {"url": "https://..."}                  -> returned as-is
      - {"path": "sub/dir/file.ext"}            -> <base>/local/sub/dir/file.ext
      - {"media_id": "media-source://..."}      -> resolved & signed URL
      - "https://..." (plain str)               -> returned as-is
      - "/local/..." (plain str)                -> prefixed with base
    """
    if not spec:
        return None

    # plain string (absolute URL or /local path)
    if isinstance(spec, str):
        if spec.startswith(("http://", "https://")):
            return spec
        if spec.startswith("/local/") or spec.startswith("local/"):
            base = get_url(hass)
            p = spec.lstrip("/")
            return f"{base}/{p}"
        return spec  # unknown string; let the app try

    # object with url / path / media_id
    if isinstance(spec, dict):
        if spec.get("url"):
            return spec["url"]

        if spec.get("path"):
            base = get_url(hass)
            p = str(spec["path"]).lstrip("/")
            if not p.startswith("local/"):
                p = f"local/{p}"
            return f"{base}/{p}"

        media_id = spec.get("media_id")
        if media_id:
            # Resolve media_source to a URL local to HA
            play_item = await media_source.async_resolve_media(hass, media_id, None)
            url = async_process_play_media_url(hass, play_item.url)
            # If HA returns a relative URL (starts with '/'), sign it so no auth header is needed
            if url.startswith("/"):
                signed_path = await async_sign_path(hass, url, timedelta(seconds=60))
                return f"{get_url(hass)}{signed_path}"
            return url

    return None


