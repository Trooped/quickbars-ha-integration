"""The QuickBars for Home Assistant Integration"""

from __future__ import annotations

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall
import homeassistant.helpers.config_validation as cv


DOMAIN = "quickbars"
EVENT_NAME = "quickbars.open"

# Event names your Android app already listens for
EVENT_QUICKBAR_OPEN = "quickbars.open"
EVENT_CAMERA_PIP = "quickbars.camera_pip"

# ----- Service Schemas -----

QUICKBAR_SCHEMA = vol.Schema({vol.Required("alias"): cv.string})
CAMERA_SCHEMA = vol.Schema({vol.Required("camera_alias"): cv.string})


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Register two tiny services that just fire quickbars.open."""

    async def handle_quickbar(call: ServiceCall) -> None:
        alias = call.data["alias"]  # Already validated
        hass.bus.async_fire(EVENT_NAME, {"alias": alias})

    async def handle_camera(call: ServiceCall) -> None:
        camera_alias = call.data["camera_alias"]  # Already validated
        hass.bus.async_fire(EVENT_NAME, {"camera_alias": camera_alias})

    hass.services.async_register(
        DOMAIN, "quickbar_toggle", handle_quickbar, QUICKBAR_SCHEMA
    )
    hass.services.async_register(DOMAIN, "camera_toggle", handle_camera, CAMERA_SCHEMA)
    return True
