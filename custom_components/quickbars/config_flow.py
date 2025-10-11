from __future__ import annotations
from typing import Any, List, Dict
from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.data_entry_flow import FlowResult
from homeassistant.config_entries import OptionsFlowWithConfigEntry, ConfigEntry
from homeassistant.core import callback, State
from homeassistant.helpers.selector import selector
from homeassistant.helpers.network import get_url

import logging, voluptuous as vol
import logging

from quickbars_bridge import QuickBarsClient

from quickbars_bridge.events import (
    ws_get_snapshot, ws_entities_replace, ws_put_snapshot, ws_entities_update, ws_ping
)

from quickbars_bridge.qb import (
    default_quickbar, unique_qb_name,
    saved_options_from_snapshot, defaults_from_qb,
    normalize_saved_entities, name_taken,
    attempted_from_user, apply_edits,
)

from quickbars_bridge.hass_flow import (
    ALLOWED_ENTITY_DOMAINS,
    mask_token, default_ha_url, decode_zeroconf, map_entity_display_names,
    schema_menu, schema_pair, schema_token,
    schema_expose, saved_pick_options, schema_manage_saved_pick,
    qb_pick_options, schema_qb_pick, schema_qb_manage,
)

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
            client = QuickBarsClient(self._host, self._port)
            resp = await client.get_pair_code()
            self._pair_sid = resp.get("sid")
            masked = mask_token(self._pair_sid)
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

        client = QuickBarsClient(self._host, self._port)
        ha_name = self.hass.config.location_name or "Home Assistant"
        ha_url  = None
        try:
            ha_url = get_url(self.hass)  # best-effort, may raise if not configured
        except Exception:
            pass

        resp = await client.confirm_pair(code, sid,
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
                # Keep the step open; show reason returned by TV app
                reason = (res.get("reason") or "creds_invalid").replace("_", " ")
                return self.async_show_form(
                    step_id="token",
                    data_schema=schema_token(url, token),
                    errors={"base": "creds_invalid"},
                    description_placeholders={"hint": reason},
                )
        except Exception as e:
            return self.async_show_form(
                step_id="token",
                data_schema=schema_token(url, token),
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
        host, port, props, hostname, name = decode_zeroconf(discovery_info)
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
            client = QuickBarsClient(self._host, self._port)         
            resp = await client.get_pair_code()
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
    

class QuickBarsOptionsFlow(OptionsFlowWithConfigEntry):
    def __init__(self, config_entry: ConfigEntry) -> None:
        super().__init__(config_entry)
        self._snapshot: Dict[str, Any] | None = None # latest snapshot from TV
        self._qb_index: int | None = None   # which quickbar is being edited

    def _error_form(self, step_id: str, e: Exception, schema: vol.Schema | None = None, hint: str | None = None) -> FlowResult:
        """One-liner to show a standard 'tv_unreachable' style error."""
        return self.async_show_form(
            step_id=step_id,
            data_schema=schema or vol.Schema({}),
            errors={"base": "tv_unreachable"},
            description_placeholders={"hint": hint or f"{type(e).__name__}: {e}"},
        )

    async def _ensure_snapshot(self, step_id_for_error: str) -> bool:
        """Make sure self._snapshot is loaded; show error form if not."""
        if self._snapshot is not None:
            return True
        try:
            self._snapshot = await ws_get_snapshot(self.hass, self.config_entry, timeout=15.0)
            return True
        except Exception as e:
            await self.hass.async_add_executor_job(lambda: None)  # yield
            self._snapshot = None
            # show standard error
            _ = self._error_form(step_id_for_error, e)
            # Returning False signals caller to return that form
            return False

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
            schema = schema_menu()
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
            schema = schema_expose(saved_ids)

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
            names = map_entity_display_names(self.hass, selected)

            # Call the helper; no JSON viewer on success, just close.
            await ws_entities_replace(self.hass, self.config_entry, selected, names=names, timeout=25.0)
            return self.async_create_entry(title="", data=dict(self.config_entry.options))

        except Exception as e:
            _LOGGER.exception("entities_replace failed")
            schema = vol.Schema({
                vol.Required("saved", default=selected): selector({
                    "entity": {"multiple": True, "domain": ALLOWED_ENTITY_DOMAINS}
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
        self._snapshot = None
        ok = await self._ensure_snapshot("manage_saved_pick")
        if not ok:
            return self._error_form("manage_saved_pick", Exception("snapshot"), hint="Snapshot unavailable")

        options = saved_pick_options(self._snapshot)

        # Default to previously selected or first
        default_id = getattr(self, "_entity_id", None)
        if default_id not in {e["value"] for e in options}:
            default_id = options[0]["value"]


        if user_input is None:
            schema = schema_manage_saved_pick(options, default_id)
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
        self._snapshot = None
        ok = await self._ensure_snapshot("qb_pick")
        if not ok:
            return self._error_form("qb_pick", Exception("snapshot"), hint="Snapshot unavailable")


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

        options = qb_pick_options(qb_list)

        # Default to previously selected or first
        default_idx = self._qb_index if isinstance(self._qb_index, int) else 0
        if default_idx < 0 or default_idx >= len(qb_list):
            default_idx = 0

        if user_input is None:
            schema = schema_qb_pick(options, default_idx)
            return self.async_show_form(
                step_id="qb_pick",
                data_schema=schema,
                description_placeholders={
                    "title": "Manage QuickBars",
                    "description": "Select a QuickBar to edit, or create a new one."
                },
            )

        choice = str(user_input.get("quickbar", str(default_idx)))

        if choice == "new":
            existing_names = [qb.get("name") or "" for qb in qb_list]
            name = unique_qb_name("QuickBar", existing_names)
            new_qb = default_quickbar(name)
            qb_list.append(new_qb)
            self._snapshot["quick_bars"] = qb_list
            self._qb_index = len(qb_list) - 1
            return await self.async_step_qb_manage()

        # Persist choice and jump into your existing editor (unchanged)
        try:
            self._qb_index = int(choice)
        except Exception:
            self._qb_index = default_idx
        return await self.async_step_qb_manage()

    async def async_step_qb_manage(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if self._snapshot is None:
            ok = await self._ensure_snapshot("qb_manage")
            if not ok:
                return self._error_form("qb_manage", Exception("snapshot"), hint="Snapshot unavailable")

        qb_list: List[Dict[str, Any]] = list(self._snapshot.get("quick_bars", []))
        if not qb_list or not isinstance(self._qb_index, int) or not (0 <= self._qb_index < len(qb_list)):
            return await self.async_step_qb_pick()

        qb = qb_list[self._qb_index]
        cur = defaults_from_qb(qb)
        saved_options, saved_ids = saved_options_from_snapshot(self._snapshot)

        if user_input is None:
            return self.async_show_form(
                step_id="qb_manage",
                data_schema=schema_qb_manage(qb, saved_options, cur),
                description_placeholders={
                    "title": "Manage QuickBar",
                    "description": (
                        "Adjust settings and submit to save. "
                        "Note: Top/Bottom/Left position and Grid layout are Plus features."
                    ),
                },
            )

        new_name = (user_input.get("quickbar_name", cur["name"]) or "").strip()
        if name_taken(new_name, qb_list, self._qb_index):
            attempted = attempted_from_user(cur, user_input, saved_ids)
            return self.async_show_form(
                step_id="qb_manage",
                data_schema=schema_qb_manage(qb, saved_options, attempted),
                errors={"base": "name_taken"},
                description_placeholders={
                    "title": "Manage QuickBar",
                    "description": (
                        "Adjust settings and submit to save. "
                        "Note: Top/Bottom/Left position and Grid layout are Plus features."
                    ),
                },
            )

        # Apply edits and persist
        apply_edits(qb, cur, user_input, saved_ids)
        try:
            payload = {"quick_bars": self._snapshot.get("quick_bars", [])}
            await ws_put_snapshot(self.hass, self.config_entry, payload, timeout=20.0)
            return self.async_create_entry(title="", data=dict(self.config_entry.options))
        except Exception as e:
            _LOGGER.exception("quickbar update failed")
            attempted = defaults_from_qb(qb)  # show what we now have on the qb
            return self.async_show_form(
                step_id="qb_manage",
                data_schema=schema_qb_manage(qb, saved_options, attempted),
                errors={"base": "tv_unreachable"},
                description_placeholders={"hint": f"{type(e).__name__}: {e}"},
            )

    async def async_step_done(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        return self.async_create_entry(title="", data=dict(self.config_entry.options))
