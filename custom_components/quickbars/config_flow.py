from __future__ import annotations
from typing import Any, List, Dict
from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.data_entry_flow import FlowResult
from homeassistant.config_entries import OptionsFlow, OptionsFlowWithConfigEntry, ConfigEntry
from homeassistant.core import callback, State
from homeassistant.helpers.selector import selector
from homeassistant.helpers.network import get_url

import json, logging, voluptuous as vol
from .client import get_snapshot, post_snapshot

import logging

from .client import get_pair_code, confirm_pair
from .client import ws_get_snapshot, ws_entities_replace, ws_put_snapshot, ws_entities_update, ws_ping
from .constants import DOMAIN

_LOGGER = logging.getLogger(__name__)

class QuickBarsConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1
    MINOR_VERSION = 1

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
            masked = (
                f"{self._pair_sid[:3]}***{self._pair_sid[-2:]}"
                if self._pair_sid and len(self._pair_sid) >= 5
                else (self._pair_sid or "<none>")
            )
            _LOGGER.debug("step_user: received sid=%s", masked)
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

        ha_name = self.hass.config.location_name or "Home Assistant"
        ha_url  = None
        try:
            ha_url = get_url(self.hass)  # best-effort, may raise if not configured
        except Exception:
            pass

        resp = await confirm_pair(self._host, self._port, code, sid,
                                   ha_instance=self._host,
                                   ha_name=ha_name,
                                   ha_url=ha_url)
        qb_id = resp.get("id")
        if not qb_id:
            return self.async_show_form(
                step_id="pair",
                data_schema=schema,
                errors={"base": "no_unique_id"},
                description_placeholders={"hint": "QuickBars did not return a stable device ID"}
            )

        qb_name = resp.get("name") or "QuickBars TV App"
        self._paired_name = qb_name
        self.context["title_placeholders"] = {"name": qb_name}
        qb_port = int(resp.get("port") or self._port)
        has_token = bool(resp.get("has_token"))

        await self.async_set_unique_id(qb_id)
        self._abort_if_unique_id_configured(updates={CONF_HOST: self._host, CONF_PORT: qb_port, "id": qb_id})

        self._port = qb_port

        if not has_token:
            return await self.async_step_token()
        return self.async_create_entry(title=self._paired_name, data={CONF_HOST: self._host, CONF_PORT: qb_port, "id": qb_id})
    
    async def async_step_token(self, user_input=None):
        # Defaults
        default_url = None
        try:
            default_url = get_url(self.hass)
        except Exception:
            default_url = ""

        schema = vol.Schema({
            vol.Required("url", default=default_url or ""): str,
            vol.Required("token"): str,
        })

        if user_input is None:
            return self.async_show_form(step_id="token", data_schema=schema)

        url = user_input["url"].strip()
        token = user_input["token"].strip()

        try:
            from .client import set_credentials
            res = await set_credentials(self._host, self._port, url, token)
            if not res.get("ok"):
                # Keep the step open; show reason returned by TV app
                reason = (res.get("reason") or "creds_invalid").replace("_", " ")
                return self.async_show_form(
                    step_id="token",
                    data_schema=vol.Schema({
                        vol.Required("url", default=url): str,
                        vol.Required("token", default=token): str,
                    }),
                    errors={"base": "creds_invalid"},
                    description_placeholders={"hint": reason},
                )
        except Exception as e:
            return self.async_show_form(
                step_id="token",
                data_schema=vol.Schema({
                    vol.Required("url", default=url): str,
                    vol.Required("token", default=token): str,
                }),
                errors={"base": "tv_unreachable"},
                description_placeholders={"hint": f"{type(e).__name__}: {e}"},
            )

        # Success -> finish
        return self.async_create_entry(
            title=getattr(self, "_paired_name", "QuickBars TV"),
            data={CONF_HOST: self._host, CONF_PORT: self._port, "id": self.unique_id},
    )

    # -------- Zeroconf path --------
    async def async_step_zeroconf(self, discovery_info) -> FlowResult:
        """Handle discovery from Zeroconf and jump into the pairing (code) step."""
        # Support both object and dict shapes
        if hasattr(discovery_info, "host"):
            host = discovery_info.host
            port = discovery_info.port
            props_raw = dict(discovery_info.properties or {})
            hostname = getattr(discovery_info, "hostname", None)
            name = getattr(discovery_info, "name", None)
        else:
            host = discovery_info.get("host")
            port = discovery_info.get("port")
            props_raw = dict(discovery_info.get("properties") or {})
            hostname = discovery_info.get("hostname")
            name = discovery_info.get("name")

        # Decode bytes -> str
        props: dict[str, str] = {}
        for k, v in (props_raw or {}).items():
            key = k.decode() if isinstance(k, (bytes, bytearray)) else str(k)
            val = v.decode() if isinstance(v, (bytes, bytearray)) else str(v)
            props[key] = val

        # Prefer TXT 'id' if present; otherwise fall back to hostname/host
        unique = (props.get("id") or "").strip()
        title = props.get("name") or "QuickBars TV App"

        # If we don’t have host/port, abort quietly
        if not host or not port:
            return self.async_abort(reason="unknown")

        # If already configured, update host/port and abort (no duplicate flows)
        if unique:
            await self.async_set_unique_id(unique)
            self._abort_if_unique_id_configured(updates={CONF_HOST: host, CONF_PORT: port, "id": unique})


        # Save endpoint for the pairing step
        self._host, self._port = host, port
        self.context["title_placeholders"] = {"name": title}

        # Add confirmation step before starting pairing
        return self.async_show_form(
            step_id="zeroconf_confirm",
            data_schema=vol.Schema({}),  # no fields; just a Continue button
            description_placeholders={"hint": f"Prepare TV for pairing with {title}."},
        )
    
    async def async_step_zeroconf_confirm(self, user_input=None) -> FlowResult:
        """Called after the user clicks the discovered tile and presses Continue."""
        if user_input is None:
            # If HA re-renders the form without submit, just show it again.
            return self.async_show_form(step_id="zeroconf_confirm", data_schema=vol.Schema({}))

        # NOW it’s user-initiated: request a code and jump to pair step
        try:
            resp = await get_pair_code(self._host, self._port)
            self._pair_sid = resp.get("sid")
            _LOGGER.debug(
                "zeroconf_confirm: got pair sid=%s (masked)",
                (self._pair_sid[:3] + "***" + self._pair_sid[-2:]) if self._pair_sid else "<none>",
            )
        except Exception as e:
            _LOGGER.exception("zeroconf_confirm: get_pair_code failed for %s:%s", self._host, self._port)
            # Fall back to manual host:port
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


class QuickBarsOptionsFlow(OptionsFlowWithConfigEntry):
    def __init__(self, config_entry: ConfigEntry) -> None:
        super().__init__(config_entry)
        self._snapshot: Dict[str, Any] | None = None # latest snapshot from TV
        self._qb_index: int | None = None   # which quickbar is being edited

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        eid = self.config_entry.data.get("id")
        _LOGGER.debug(
            "options:init entry_id=%s unique_id=%s data_id=%s host=%s port=%s",
            self.config_entry.entry_id,
            getattr(self.config_entry, "unique_id", None),
            eid,
            self.config_entry.data.get("host"),
            self.config_entry.data.get("port"),
        )

        # 1) Quick connectivity check
        try:
            _LOGGER.debug("options:init -> ws_ping start (expect id=%s)", eid)
            ok = await ws_ping(self.hass, self.config_entry, timeout=5.0)
            _LOGGER.debug("options:init -> ws_ping result=%s (expect id=%s)", ok, eid)
            if not ok:
                return self.async_show_form(
                    step_id="init",
                    errors={"base": "tv_unreachable"},
                    description_placeholders={"hint": "WS ping failed"},
                    data_schema=vol.Schema({})
                )
        except Exception as e:
            _LOGGER.exception("options:init ws_ping raised")
            return self.async_show_form(
                step_id="init",
                errors={"base": "tv_unreachable"},
                description_placeholders={"hint": f"{type(e).__name__}: {e}"},
                data_schema=vol.Schema({})
            )

        # 2) Only then pull the snapshot
        try:
            _LOGGER.debug("options:init -> ws_get_snapshot start (expect id=%s)", eid)
            self._snapshot = await ws_get_snapshot(self.hass, self.config_entry, timeout=15.0)
            _LOGGER.debug(
                "options:init -> ws_get_snapshot ok: entities=%s quick_bars=%s",
                len(self._snapshot.get("entities", []) or []),
                len(self._snapshot.get("quick_bars", []) or []),
            )
        except Exception as e:
            _LOGGER.exception("options:init ws_get_snapshot raised")
            return self.async_show_form(
                step_id="init",
                errors={"base": "tv_unreachable"},
                description_placeholders={"hint": f"{type(e).__name__}: {e}"},
                data_schema=vol.Schema({})
            )

        return await self.async_step_menu()
    
    async def async_step_menu(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is None:
            # Use a simpler dropdown selector instead of buttons
            schema = vol.Schema({
                vol.Required("action"): vol.In({
                    "export": "Add / Remove Saved Entities",
                    "manage_saved": "Manage Saved Entities",
                    "manage_qb": "Manage QuickBars"
                })
            })
            return self.async_show_form(
                step_id="menu", 
                data_schema=schema,
                description_placeholders={
                    "title": "QuickBars Configuration",
                    "description": "What would you like to configure?"
                }
            )

        # Handle selected action
        action = user_input.get("action", "")
        if action == "export":
            return await self.async_step_expose()
        elif action == "manage_saved":
            return await self.async_step_manage_saved_pick()
        elif action == "manage_qb":
            return await self.async_step_qb_pick()
        
        # Fallback
        return await self.async_step_menu()
    
    # ---------- 1) Export/remove saved entities ----------
    async def async_step_expose(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        entities: List[Dict[str, Any]] = list(self._snapshot.get("entities", []))
        saved_ids = [e.get("id") for e in entities if e.get("id")]

        if user_input is None:
            schema = vol.Schema({
                vol.Required("saved", default=saved_ids): selector({
                    "entity": {"multiple": True, "domain": _ALLOWED}
                })
            })
            return self.async_show_form(
                step_id="expose",
                data_schema=schema,
                description_placeholders={
                    "title": "Saved entities",
                    "description": "Select which entities are saved in the QuickBars app."
                }
            )
        
        def _display_name(hass, entity_id: str) -> str:
            st: State | None = hass.states.get(entity_id)
            if st and st.name:
                return st.name  # HA's user-facing name; already prefers attributes.friendly_name
            # fallback if somehow missing
            return entity_id.split(".", 1)[-1]

        # Build replacement list
        selected: List[str] = list(user_input.get("saved") or [])

        try:
            names = {eid: _display_name(self.hass, eid) for eid in selected}

            # Call the helper; no JSON viewer on success, just close.
            await ws_entities_replace(self.hass, self.config_entry, selected, names=names, timeout=25.0)
            return self.async_create_entry(title="", data=dict(self.config_entry.options))

        except Exception as e:
            _LOGGER.exception("entities_replace failed")
            schema = vol.Schema({
                vol.Required("saved", default=selected): selector({
                    "entity": {"multiple": True, "domain": _ALLOWED}
                })
            })
            return self.async_show_form(
                step_id="expose",
                data_schema=schema,
                errors={"base": "tv_unreachable"},
                description_placeholders={"hint": f"{type(e).__name__}: {e}"},
            )
        
    # ---------- 2) Manage Saved Entities (placeholder for now) ----------
    async def async_step_manage_saved_pick(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Pick which saved entity to edit, then jump to your existing editor."""
        # Always refresh snapshot so defaults are current
        try:
            self._snapshot = await ws_get_snapshot(self.hass, self.config_entry, timeout=15.0)
        except Exception as e:
            return self.async_show_form(
                step_id="manage_saved_pick",
                errors={"base": "tv_unreachable"},
                description_placeholders={"hint": f"{type(e).__name__}: {e}"},
                data_schema=vol.Schema({}),
            )

        ents: List[Dict[str, Any]] = [
            e for e in (self._snapshot.get("entities") or [])
            if e.get("isSaved") and e.get("id")
        ]

        if not ents:
            # Nothing to manage; send them back to the menu gracefully
            return self.async_show_form(
                step_id="manage_saved_pick",
                data_schema=vol.Schema({}),
                description_placeholders={
                    "title": "Manage Saved Entities",
                    "description": "No saved entities."
                },
            )

        def _label(e: Dict[str, Any]) -> str:
            return f"{e.get('customName') or e.get('friendlyName') or e['id']} ({e['id']})"

        options = [{"label": _label(e), "value": e["id"]} for e in ents]

        # Default to previously selected or first
        default_id = getattr(self, "_entity_id", None)
        if default_id not in {e["id"] for e in ents}:
            default_id = ents[0]["id"]


        if user_input is None:
            schema = vol.Schema({
                vol.Required("entity", default=default_id): selector({
                    "select": {"options": options, "mode": "dropdown"}
                })
            })
            return self.async_show_form(
                step_id="manage_saved_pick",
                data_schema=schema,
                description_placeholders={
                    "title": "Manage Saved Entities",
                    "description": "Pick an entity to edit.",
                },
            )

        # Persist selection and continue to the editor
        self._entity_id = user_input.get("entity")
        return await self.async_step_manage_saved()
    
    async def async_step_manage_saved(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        # Must come from pick step; ensure snapshot & valid selection
        if self._snapshot is None:
            try:
                self._snapshot = await ws_get_snapshot(self.hass, self.config_entry, timeout=15.0)
            except Exception as e:
                return self.async_show_form(
                    step_id="manage_saved",
                    errors={"base": "tv_unreachable"},
                    description_placeholders={"hint": f"{type(e).__name__}: {e}"},
                    data_schema=vol.Schema({}),
                )

        ents: List[Dict[str, Any]] = [e for e in (self._snapshot.get("entities") or []) if e.get("isSaved") and e.get("id")]
        by_id = {e["id"]: e for e in ents}
        if not getattr(self, "_entity_id", None) or self._entity_id not in by_id:
            # If someone lands here directly, bounce to pick
            return await self.async_step_manage_saved_pick()

        entity = by_id[self._entity_id]
        cur_name = entity.get("customName") or entity.get("friendlyName") or ""

        if user_input is None:
            schema = vol.Schema({
                vol.Required("display_name", default=cur_name): str,
            })
            return self.async_show_form(
                step_id="manage_saved",
                data_schema=schema,
                description_placeholders={
                    "title": f"Edit Saved Entity",
                    "description": f"Editing: {entity.get('customName') or entity.get('friendlyName') or entity['id']}"
                },
            )

        # Save
        new_name = user_input.get("display_name", cur_name)
        try:
            await ws_entities_update(
                self.hass, self.config_entry,
                updates=[{"id": self._entity_id, "customName": new_name}],
                timeout=15.0
            )
            return self.async_create_entry(title="", data=dict(self.config_entry.options))
        except Exception as e:
            _LOGGER.exception("entities_update failed")
            return self.async_show_form(
                step_id="manage_saved",
                data_schema=vol.Schema({vol.Required("display_name", default=new_name): str}),
                errors={"base": "tv_unreachable"},
                description_placeholders={"hint": f"{type(e).__name__}: {e}"},
            )


    # ---------- 3) Manage QuickBars ----------
    async def async_step_qb_pick(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Pick which QuickBar to edit, then jump to your existing editor."""
        # Always refresh snapshot so defaults are current
        try:
            self._snapshot = await ws_get_snapshot(self.hass, self.config_entry, timeout=15.0)
        except Exception as e:
            return self.async_show_form(
                step_id="qb_pick",
                errors={"base": "tv_unreachable"},
                description_placeholders={"hint": f"{type(e).__name__}: {e}"},
                data_schema=vol.Schema({}),
            )

        qb_list: List[Dict[str, Any]] = list(self._snapshot.get("quick_bars", []))
        if not qb_list:
            return self.async_show_form(
                step_id="qb_pick",
                data_schema=vol.Schema({}),
                description_placeholders={
                    "title": "Manage QuickBars",
                    "description": "No QuickBars found."
                },
            )

        options = []
        for idx, qb in enumerate(qb_list):
            name = qb.get("name") or f"QuickBar {idx+1}"
            options.append({"label": name, "value": str(idx)})

        # Default to previously selected or first
        default_idx = self._qb_index if isinstance(self._qb_index, int) else 0
        if default_idx < 0 or default_idx >= len(qb_list):
            default_idx = 0

        if user_input is None:
            schema = vol.Schema({
                vol.Required("quickbar", default=str(default_idx)): selector({
                    "select": {"options": options, "mode": "dropdown"}
                }),
            })
            return self.async_show_form(
                step_id="qb_pick",
                data_schema=schema,
                description_placeholders={
                    "title": "Manage QuickBars",
                    "description": "Select a QuickBar to edit."
                },
            )

        # Persist choice and jump into your existing editor (unchanged)
        try:
            self._qb_index = int(user_input.get("quickbar", str(default_idx)))
        except Exception:
            self._qb_index = default_idx
        return await self.async_step_qb_manage()

    async def async_step_qb_manage(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        # Must come from pick step; ensure snapshot & valid selection
        if self._snapshot is None:
            try:
                self._snapshot = await ws_get_snapshot(self.hass, self.config_entry, timeout=15.0)
            except Exception as e:
                return self.async_show_form(
                    step_id="qb_manage",
                    errors={"base": "tv_unreachable"},
                    description_placeholders={"hint": f"{type(e).__name__}: {e}"},
                    data_schema=vol.Schema({}),
                )

        qb_list: List[Dict[str, Any]] = list(self._snapshot.get("quick_bars", []))
        if not qb_list:
            return await self.async_step_qb_pick()

        if not isinstance(self._qb_index, int) or self._qb_index < 0 or self._qb_index >= len(qb_list):
            return await self.async_step_qb_pick()

        qb = qb_list[self._qb_index]

        # SAVED entities for options (friendly labels)
        all_entities: List[Dict[str, Any]] = list(self._snapshot.get("entities", []))
        saved_entities = [e for e in all_entities if e.get("isSaved") and e.get("id")]
        saved_ids: List[str] = [e["id"] for e in saved_entities]

        def _label_for(e: Dict[str, Any]) -> str:
            name = e.get("customName") or e.get("friendlyName") or e.get("id")
            return f"{name} ({e['id']})"

        saved_options = [{"label": _label_for(e), "value": e["id"]} for e in saved_entities]

        # Current values
        cur_name = qb.get("name") or ""
        cur_saved = list(qb.get("savedEntityIds") or [])
        cur_show_name = bool(qb.get("showNameInOverlay", True))
        cur_show_time = bool(qb.get("showTimeOnQuickBar", True))
        cur_alias = qb.get("haTriggerAlias") or ""
        cur_domains = list(qb.get("autoCloseQuickBarDomains") or [])
        cur_bg_mode = qb.get("backgroundColor") or "colorSurface"
        cur_on_mode = qb.get("onStateColor") or "colorPrimary"
        cur_bg_rgb = list(qb.get("customBackgroundColor") or [24, 24, 24])   # sensible dark-ish default
        cur_on_rgb = list(qb.get("customOnStateColor") or [255, 204, 0])     # visible accent default
        cur_use_bg_custom = (cur_bg_mode == "custom")
        cur_use_on_custom = (cur_on_mode == "custom")

        if user_input is None:
            schema = vol.Schema({
                vol.Required("quickbar_name", default=cur_name): str,
                vol.Optional("saved_entities", default=cur_saved): selector({
                    "select": {"options": saved_options, "multiple": True}
                }),
                vol.Required("show_name_on_overlay", default=cur_show_name): selector({"boolean": {}}),
                vol.Required("show_time_on_quickbar", default=cur_show_time): selector({"boolean": {}}),
                vol.Optional("ha_trigger_alias", default=cur_alias): str,
                vol.Optional("auto_close_domains", default=cur_domains): selector({
                    "select": {
                        "options": [
                            "light","switch","button","input_boolean","input_button",
                            "script","scene",
                            "automation","camera"
                        ],
                        "multiple": True
                    }
                }),
                vol.Required("use_custom_bg", default=cur_use_bg_custom): selector({"boolean": {}}),
                vol.Optional("bg_rgb", default=cur_bg_rgb): selector({"color_rgb": {}}),

                vol.Required("use_custom_on_state", default=cur_use_on_custom): selector({"boolean": {}}),
                vol.Optional("on_rgb", default=cur_on_rgb): selector({"color_rgb": {}}),
            })
            return self.async_show_form(
                step_id="qb_manage",
                data_schema=schema,
                description_placeholders={
                    "title": "Manage QuickBar",
                    "description": "Adjust settings and submit to save. Use Back to pick a different QuickBar."
                },
            )

        # Normalize selection order & subset
        requested = list(user_input.get("saved_entities") or cur_saved)
        seen = set()
        normalized = []
        for eid in requested:
            if eid in saved_ids and eid not in seen:
                normalized.append(eid)
                seen.add(eid)

        # Apply edits in memory
        qb["name"] = user_input.get("quickbar_name", cur_name)
        qb["savedEntityIds"] = normalized
        qb["showNameInOverlay"] = bool(user_input.get("show_name_on_overlay", cur_show_name))
        qb["showTimeOnQuickBar"] = bool(user_input.get("show_time_on_quickbar", cur_show_time))
        qb["haTriggerAlias"] = user_input.get("ha_trigger_alias", cur_alias)
        qb["autoCloseQuickBarDomains"] = list(user_input.get("auto_close_domains") or cur_domains)

        use_bg = bool(user_input.get("use_custom_bg", cur_use_bg_custom))
        use_on = bool(user_input.get("use_custom_on_state", cur_use_on_custom))

        if use_bg:
            qb["backgroundColor"] = "custom"
            qb["customBackgroundColor"] = list(user_input.get("bg_rgb") or cur_bg_rgb)
        else:
            # keep prior theme key or reset to default theme name
            qb["backgroundColor"] = cur_bg_mode if cur_bg_mode != "custom" else "colorSurface"
            qb.pop("customBackgroundColor", None)

        if use_on:
            qb["onStateColor"] = "custom"
            qb["customOnStateColor"] = list(user_input.get("on_rgb") or cur_on_rgb)
        else:
            qb["onStateColor"] = cur_on_mode if cur_on_mode != "custom" else "colorPrimary"
            qb.pop("customOnStateColor", None)

        # Push ONLY quick_bars back
        try:
            payload = {"quick_bars": self._snapshot.get("quick_bars", [])}
            await ws_put_snapshot(self.hass, self.config_entry, payload, timeout=20.0)
            return self.async_create_entry(title="", data=dict(self.config_entry.options))
        except Exception as e:
            _LOGGER.exception("quickbar update failed")
            # Re-show with current values
            schema = vol.Schema({
                vol.Required("quickbar_name", default=qb.get("name") or cur_name): str,
                vol.Optional("saved_entities", default=qb.get("savedEntityIds") or cur_saved): selector({
                    "select": {"options": saved_options, "multiple": True}
                }),
                vol.Required("show_name_on_overlay", default=qb.get("showNameInOverlay", cur_show_name)): selector({"boolean": {}}),
                vol.Required("show_time_on_quickbar", default=qb.get("showTimeOnQuickBar", cur_show_time)): selector({"boolean": {}}),
                vol.Optional("ha_trigger_alias", default=qb.get("haTriggerAlias") or cur_alias): str,
                vol.Optional("auto_close_domains", default=qb.get("autoCloseQuickBarDomains") or cur_domains): selector({
                    "select": {
                        "options": [
                            "light","switch","button","input_boolean","input_button",
                            "script","scene",
                            "automation","camera"
                        ],
                        "multiple": True
                    }
                }),
            })
            return self.async_show_form(
                step_id="qb_manage",
                data_schema=schema,
                errors={"base": "tv_unreachable"},
                description_placeholders={"hint": f"{type(e).__name__}: {e}"},
            )


    async def async_step_done(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        return self.async_create_entry(title="", data=dict(self.config_entry.options))
