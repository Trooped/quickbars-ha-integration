"""Config flow and options flow for the QuickBars integration."""

from __future__ import annotations

from contextlib import suppress
import logging
from typing import TYPE_CHECKING, Any

from aiohttp import ClientError
from quickbars_bridge import QuickBarsClient
from quickbars_bridge.events import ws_entities_replace, ws_get_snapshot, ws_ping
from quickbars_bridge.hass_flow import (
    ALLOWED_ENTITY_DOMAINS,
    decode_zeroconf,
    default_ha_url,
    map_entity_display_names,
    schema_expose,
    schema_token,
)
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_HOST, CONF_ID, CONF_PORT
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.network import get_url
from homeassistant.helpers.selector import selector

from .constants import DOMAIN

if TYPE_CHECKING:
    from homeassistant.components.zeroconf import ZeroconfServiceInfo

_LOGGER = logging.getLogger(__name__)


def schema_host_port(default_host: str | None, default_port: int | None) -> vol.Schema:
    """Return schema for host/port input form."""
    return vol.Schema(
        {
            vol.Required(CONF_HOST, default=default_host): str,
            vol.Required(
                CONF_PORT, default=default_port if default_port is not None else 9123
            ): int,
        }
    )


class QuickBarsConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the QuickBars config flow."""

    def __init__(self) -> None:
        """Initialize flow state."""
        self._host: str | None = None
        self._port: int | None = None
        self._pair_sid: str | None = None
        self._paired_name: str | None = None
        # Options flow will set these, but keeping for type safety:
        self._snapshot: dict[str, Any] | None = None
        self._entity_id: str | None = None
        self._qb_index: int | None = None

    # ---------- Manual path ----------
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Start the flow: collect host/port and request a pairing code."""
        if user_input is None:
            return self.async_show_form(
                step_id="user", data_schema=schema_host_port(None, 9123)
            )

        self._host = user_input[CONF_HOST]
        self._port = user_input[CONF_PORT]

        try:
            client = QuickBarsClient(self._host, self._port)
            resp = await client.get_pair_code()
        except (TimeoutError, OSError, ClientError):
            _LOGGER.exception(
                "Step_user: get_pair_code failed for %s:%s", self._host, self._port
            )
            return self.async_show_form(
                step_id="user",
                data_schema=schema_host_port(self._host, self._port),
                errors={"base": "tv_unreachable"},
            )
        else:
            self._pair_sid = resp.get("sid")

        return await self.async_step_pair()

    async def async_step_pair(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Submit the code shown on the TV and create the entry (or continue to token)."""
        schema = vol.Schema({vol.Required("code"): str})
        if user_input is None:
            return self.async_show_form(step_id="pair", data_schema=schema)

        code = user_input["code"].strip()
        sid = self._pair_sid

        client = QuickBarsClient(self._host, self._port)
        ha_name = self.hass.config.location_name or "Home Assistant"

        ha_url = None
        with suppress(HomeAssistantError):
            # best effort; raises HomeAssistantError if not configured
            ha_url = get_url(self.hass)

        resp = await client.confirm_pair(
            code, sid, ha_instance=self._host, ha_name=ha_name, ha_url=ha_url
        )
        qb_id = resp.get("id")
        if not qb_id:
            return self.async_show_form(
                step_id="pair",
                data_schema=schema,
                errors={"base": "no_unique_id"},
            )

        qb_name = resp.get("name") or "QuickBars TV App"
        self._paired_name = qb_name
        self.context["title_placeholders"] = {"name": qb_name}
        port_val = resp.get("port")
        if port_val is None:
            qb_port = int(self._port) if self._port is not None else 9123
        else:
            qb_port = int(port_val)
        has_token = bool(resp.get("has_token"))

        await self.async_set_unique_id(qb_id)
        self._abort_if_unique_id_configured(
            updates={CONF_HOST: self._host, CONF_PORT: qb_port, CONF_ID: qb_id}
        )
        self._port = qb_port

        if not has_token:
            return await self.async_step_token()

        return self.async_create_entry(
            title=self._paired_name,
            data={CONF_HOST: self._host, CONF_PORT: qb_port, CONF_ID: qb_id},
        )

    async def async_step_token(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect HA URL + long-lived token and send them to the TV app."""
        default_url = default_ha_url(self.hass)
        schema = schema_token(default_url, None)

        if user_input is None:
            return self.async_show_form(step_id="token", data_schema=schema)

        url = user_input["url"].strip()
        token = user_input["token"].strip()

        try:
            client = QuickBarsClient(self._host, self._port)
            res = await client.set_credentials(url, token)
            if not res.get("ok"):
                return self.async_show_form(
                    step_id="token",
                    data_schema=schema_token(url, token),
                    errors={"base": "creds_invalid"},
                )
        except (TimeoutError, OSError, ClientError):
            return self.async_show_form(
                step_id="token",
                data_schema=schema_token(url, token),
                errors={"base": "tv_unreachable"},
            )

        return self.async_create_entry(
            title=self._paired_name or "QuickBars TV",
            data={
                CONF_HOST: self._host,
                CONF_PORT: self._port,
                CONF_ID: self.unique_id,
            },
        )

    # -------- Zeroconf path --------
    async def async_step_zeroconf(
        self, discovery_info: ZeroconfServiceInfo
    ) -> ConfigFlowResult:
        """Handle zeroconf discovery and show a confirmation step."""
        host, port, props, _hostname, _name = decode_zeroconf(discovery_info)
        unique = (props.get("id") or "").strip()
        title = props.get("name") or "QuickBars TV App"

        if not host or not port:
            return self.async_abort(reason="unknown")

        if unique:
            await self.async_set_unique_id(unique)
            self._abort_if_unique_id_configured(
                updates={CONF_HOST: host, CONF_PORT: port, CONF_ID: unique}
            )

        self._host, self._port, self.props = host, port, props
        self.context["title_placeholders"] = {"name": title}

        return self.async_show_form(
            step_id="zeroconf_confirm",
            data_schema=vol.Schema({}),
            description_placeholders={
                "id": (props.get("id") or ""),
                "host": host,
                "port": str(port),
                "api": (props.get("api") or ""),
                "app_version": (props.get("app_version") or ""),
                "name": title,
            },
        )

    async def async_step_zeroconf_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """After the user confirms the discovered device, request a code and continue."""
        if user_input is None:
            props = getattr(self, "_props", {}) or {}
            qb_name = props.get("name") or "QuickBars TV App"
            host = self._host or ""
            port = self._port
            self.context["title_placeholders"] = {"name": qb_name}

            return self.async_show_form(
                step_id="zeroconf_confirm",
                data_schema=vol.Schema({}),
                description_placeholders={
                    "id": (props.get("id") or ""),
                    "host": host,
                    "port": str(port) if port is not None else "9123",
                    "api": (props.get("api") or ""),
                    "app_version": (props.get("app_version") or ""),
                    "name": qb_name,
                },
            )

        try:
            client = QuickBarsClient(self._host, self._port)
            resp = await client.get_pair_code()
        except (TimeoutError, OSError, ClientError):
            _LOGGER.exception(
                "Step_zeroconf_confirm: get_pair_code failed for %s:%s",
                self._host,
                self._port,
            )
            return self.async_show_form(
                step_id="user",
                data_schema=schema_host_port(self._host, self._port),
                errors={"base": "tv_unreachable"},
            )
        else:
            self._pair_sid = resp.get("sid")

        return await self.async_step_pair()

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Expose per-entry options flow so the Configure button appears."""
        return QuickBarsOptionsFlow()


class QuickBarsOptionsFlow(OptionsFlow):
    """Options flow for QuickBars."""

    def __init__(self) -> None:
        """Initialize options flow."""
        self._snapshot: dict[str, Any] | None = None  # latest snapshot from TV

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Initialize the options flow: check connectivity and pull a snapshot."""
        eid = self.config_entry.data.get("id")

        # 1) Quick connectivity check
        try:
            ok = await ws_ping(self.hass, self.config_entry, timeout=5.0)
            if not ok:
                return self.async_show_form(
                    step_id="expose",
                    data_schema=schema_expose([]),
                    errors={"base": "tv_unreachable"},
                )
        except Exception:
            _LOGGER.exception("Options:init ws_ping raised")
            return self.async_show_form(
                step_id="expose",
                data_schema=schema_expose([]),
                errors={"base": "tv_unreachable"},
            )

        # 2) Only then pull the snapshot
        try:
            _LOGGER.debug("options:init -> ws_get_snapshot start (expect id=%s)", eid)
            self._snapshot = await ws_get_snapshot(
                self.hass, self.config_entry, timeout=15.0
            )
        except Exception:
            _LOGGER.exception("Options:init ws_get_snapshot raised")
            return self.async_show_form(
                step_id="expose",
                data_schema=schema_expose([]),
                errors={"base": "tv_unreachable"},
            )

        # Go directly to expose step - we're simplifying the flow by removing the menu
        return await self.async_step_expose()

    # ---------- Export/remove saved entities ----------
    async def async_step_expose(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select and save the set of 'saved' entities in the TV app."""
        entities: list[dict[str, Any]] = list(
            (self._snapshot or {}).get("entities", [])
        )
        saved_ids = [e.get("id") for e in entities if e.get("id")]

        if user_input is None:
            schema = schema_expose(saved_ids)
            return self.async_show_form(step_id="expose", data_schema=schema)

        # Build replacement list
        selected: list[str] = list(user_input.get("saved") or [])

        try:
            names = map_entity_display_names(self.hass, selected)

            # Call the helper; no JSON viewer on success, just close.
            await ws_entities_replace(
                self.hass, self.config_entry, selected, names=names, timeout=25.0
            )
            return self.async_create_entry(
                title="", data=dict(self.config_entry.options)
            )

        except Exception:
            _LOGGER.exception("Entities_replace failed")
            schema = vol.Schema(
                {
                    vol.Required("saved", default=selected): selector(
                        {"entity": {"multiple": True, "domain": ALLOWED_ENTITY_DOMAINS}}
                    )
                }
            )
            return self.async_show_form(
                step_id="expose",
                data_schema=schema,
                errors={"base": "tv_unreachable"},
            )
