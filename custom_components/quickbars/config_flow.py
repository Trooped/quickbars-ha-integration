from __future__ import annotations
from typing import Any, List, Dict
from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.data_entry_flow import FlowResult
from homeassistant.config_entries import OptionsFlow, ConfigEntry
from homeassistant.core import callback, State
from homeassistant.helpers.selector import selector
import json, logging, voluptuous as vol
from .client import get_snapshot, post_snapshot

import logging

from .client import get_pair_code, confirm_pair
from .client import ws_get_snapshot, ws_entities_replace
from .constants import DOMAIN

_LOGGER = logging.getLogger(__name__)

class QuickBarsConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    # ---------- Manual path ----------
    async def async_step_user(self, user_input=None) -> FlowResult:
        if user_input is None:
            return self.async_show_form(
                step_id="user",
                data_schema=vol.Schema({
                    vol.Required(CONF_HOST): str,
                    vol.Required(CONF_PORT, default=9123): int,
                })
            )
        self._host = user_input[CONF_HOST]
        self._port = user_input[CONF_PORT]
        _LOGGER.debug("step_user: host=%s port=%s -> requesting /pair/code", self._host, self._port)

        try:
            resp = await get_pair_code(self._host, self._port)
            self._pair_sid = resp.get("sid")
            _LOGGER.debug("step_user: received sid=%s (masked)", self._pair_sid[:3] + "***" + self._pair_sid[-2:] if self._pair_sid else "<none>")
        except Exception as e:
            _LOGGER.exception("step_user: get_pair_code failed for %s:%s", self._host, self._port)
            return self.async_show_form(
                step_id="user",
                data_schema=vol.Schema({
                    vol.Required(CONF_HOST, default=self._host): str,
                    vol.Required(CONF_PORT, default=self._port): int,
                }),
                errors={"base": "tv_unreachable"},
                description_placeholders={"hint": f"{type(e).__name__}: {e}"},
            )

        return await self.async_step_pair()

    async def async_step_pair(self, user_input=None) -> FlowResult:
        schema = vol.Schema({vol.Required("code"): str})
        if user_input is None:
            _LOGGER.debug("step_pair: prompting for code; sid set=%s", bool(getattr(self, "_pair_sid", None)))
            return self.async_show_form(step_id="pair", data_schema=schema)

        code = user_input["code"].strip()
        sid = getattr(self, "_pair_sid", None)
        _LOGGER.debug("step_pair: confirming with code=%s sid=%s", (code[:1]+"***"+code[-1:]), (sid[:3]+"***"+sid[-2:] if sid else "<none>"))

        try:
            resp = await confirm_pair(self._host, self._port, code, sid)
            qb_id = resp.get("id") or f"{self._host}:{self._port}"
            qb_name = resp.get("name") or "QuickBars TV"
            qb_port = int(resp.get("port") or self._port)
            _LOGGER.debug("step_pair: confirm_pair OK -> id=%s name=%s port=%s", qb_id, qb_name, qb_port)
        except Exception as e:
            _LOGGER.exception("step_pair: confirm_pair failed")
            return self.async_show_form(step_id="pair", data_schema=schema, errors={"base": "bad_code"}, description_placeholders={"hint": f"{type(e).__name__}: {e}"})

        await self.async_set_unique_id(qb_id)
        self._abort_if_unique_id_configured(updates={CONF_HOST: self._host, CONF_PORT: qb_port, "id": qb_id})
        return self.async_create_entry(title=qb_name, data={CONF_HOST: self._host, CONF_PORT: qb_port, "id": qb_id})

    # -------- Zeroconf path --------
    async def async_step_zeroconf(self, discovery_info: dict[str, Any]) -> FlowResult:
        host = discovery_info["host"]
        port = discovery_info["port"]
        props = dict(discovery_info.get("properties") or {})
        unique = props.get("id") or discovery_info.get("hostname") or host
        _LOGGER.debug("step_zeroconf: host=%s port=%s props=%s unique=%s", host, port, props, unique)

        await self.async_set_unique_id(unique)
        self._abort_if_unique_id_configured(updates={CONF_HOST: host, CONF_PORT: port, "id": unique})

        self.context["title_placeholders"] = {"name": props.get("name") or "QuickBars TV"}
        return self.async_create_entry(
            title=props.get("name") or "QuickBars TV",
            data={CONF_HOST: host, CONF_PORT: port, "id": unique},
        )
    
    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> QuickBarsOptionsFlow:
        """Expose per-entry options flow so the Configure button appears."""
        return QuickBarsOptionsFlow(config_entry)
    
_ALLOWED = [
    "light", "switch", "button", "fan", "input_boolean", "input_button",
    "script", "scene", "climate", "cover", "lock", "media_player",
    "automation", "camera", "sensor", "binary_sensor",
]


class QuickBarsOptionsFlow(OptionsFlow):
    def __init__(self, config_entry: ConfigEntry) -> None:
        self.config_entry = config_entry
        self._snapshot: Dict[str, Any] | None = None

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        # Pull snapshot over WS
        try:
            self._snapshot = await ws_get_snapshot(self.hass, self.config_entry, timeout=15.0)
        except Exception as e:
            return self.async_show_form(
                step_id="init",
                errors={"base": "tv_unreachable"},
                description_placeholders={"hint": f"{type(e).__name__}: {e}"},
            )
        # Go to expose UI
        return await self.async_step_expose()

    async def async_step_expose(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        entities: List[Dict[str, Any]] = list(self._snapshot.get("entities", []))
        saved_ids = [e.get("id") for e in entities if e.get("id")]

        if user_input is None:
            schema = vol.Schema({
                vol.Required("selected", default=saved_ids): selector({
                    "entity": {"multiple": True, "domain": _ALLOWED}
                })
            })
            return self.async_show_form(
                step_id="expose",
                data_schema=schema,
                description_placeholders={
                    "title": "Saved entities",
                    "description": "Pick which entities are saved in the TV app"
                }
            )
        
        def _display_name(hass, entity_id: str) -> str:
            st: State | None = hass.states.get(entity_id)
            if st and st.name:
                return st.name  # HA's user-facing name; already prefers attributes.friendly_name
            # fallback if somehow missing
            return entity_id.split(".", 1)[-1]

        # Build replacement list
        selected: List[str] = list(user_input.get("selected") or [])

        try:
            names = {eid: _display_name(self.hass, eid) for eid in selected}

            # Call the helper; no JSON viewer on success, just close.
            await ws_entities_replace(self.hass, self.config_entry, selected, names=names, timeout=25.0)
            return self.async_create_entry(title="", data=dict(self.config_entry.options))

        except Exception as e:
            _LOGGER.exception("entities_replace failed")
            schema = vol.Schema({
                vol.Required("selected", default=selected): selector({
                    "entity": {"multiple": True, "domain": _ALLOWED}
                })
            })
            return self.async_show_form(
                step_id="expose",
                data_schema=schema,
                errors={"base": "tv_unreachable"},
                description_placeholders={"hint": f"{type(e).__name__}: {e}"},
            )

    async def async_step_done(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        return self.async_create_entry(title="", data=dict(self.config_entry.options))
