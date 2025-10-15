"""Config flow and options flow for the QuickBars integration."""

from __future__ import annotations

from contextlib import suppress
import logging
from typing import TYPE_CHECKING, Any

from aiohttp import ClientError
from quickbars_bridge import QuickBarsClient
from quickbars_bridge.events import (
    ws_entities_replace,
    ws_entities_update,
    ws_get_snapshot,
    ws_ping,
    ws_put_snapshot,
)
from quickbars_bridge.hass_flow import (
    ALLOWED_ENTITY_DOMAINS,
    decode_zeroconf,
    default_ha_url,
    map_entity_display_names,
    qb_pick_options,
    saved_pick_options,
    schema_expose,
    schema_menu,
    schema_qb_manage,
    schema_qb_pick,
    schema_token,
)
from quickbars_bridge.qb import (
    apply_edits,
    attempted_from_user,
    default_quickbar,
    defaults_from_qb,
    name_taken,
    saved_options_from_snapshot,
    unique_qb_name,
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

        self._host, self._port = host, port
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
            return self.async_show_form(
                step_id="zeroconf_confirm", data_schema=vol.Schema({})
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
        return QuickBarsOptionsFlow(config_entry)


class QuickBarsOptionsFlow(OptionsFlow):
    """Options flow for QuickBars."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        """Initialize options flow."""
        super().__init__(config_entry)
        self._snapshot: dict[str, Any] | None = None  # latest snapshot from TV
        self._qb_index: int | None = None  # which quickbar is being edited
        self._entity_id: str | None = None

    def _error_form(
        self,
        step_id: str,
        e: Exception,
        schema: vol.Schema | None = None,
        hint: str | None = None,
    ) -> ConfigFlowResult:
        """One-liner to show a standard 'tv_unreachable' style error."""
        return self.async_show_form(
            step_id=step_id,
            data_schema=schema or vol.Schema({}),
            errors={"base": "tv_unreachable"},
        )

    async def _ensure_snapshot(self, step_id_for_error: str) -> bool:
        """Ensure self._snapshot is loaded; show error form if not."""
        if self._snapshot is not None:
            return True
        try:
            self._snapshot = await ws_get_snapshot(
                self.hass, self.config_entry, timeout=15.0
            )
        except (TimeoutError, OSError, ClientError) as e:
            await self.hass.async_add_executor_job(lambda: None)  # yield
            self._snapshot = None
            _ = self._error_form(step_id_for_error, e)
            return False
        else:
            return True

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
                    step_id="init",
                    errors={"base": "tv_unreachable"},
                    data_schema=vol.Schema({}),
                )
        except Exception:
            _LOGGER.exception("Options:init ws_ping raised")
            return self.async_show_form(
                step_id="init",
                errors={"base": "tv_unreachable"},
                data_schema=vol.Schema({}),
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
                step_id="init",
                errors={"base": "tv_unreachable"},
                data_schema=vol.Schema({}),
            )

        return await self.async_step_menu()

    async def async_step_menu(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the options menu and route to the chosen action."""
        if user_input is None:
            schema = schema_menu()
            return self.async_show_form(step_id="menu", data_schema=schema)

        # Handle selected action
        action = user_input.get("action", "")
        if action == "export":
            return await self.async_step_expose()
        if action == "manage_saved":
            return await self.async_step_manage_saved_pick()
        if action == "manage_qb":
            return await self.async_step_qb_pick()

        # Fallback
        return await self.async_step_menu()

    # ---------- 1) Export/remove saved entities ----------
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

    # ---------- 2) Manage Saved Entities (placeholder for now) ----------
    async def async_step_manage_saved_pick(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick which saved entity to edit."""
        self._snapshot = None
        ok = await self._ensure_snapshot("manage_saved_pick")
        if not ok:
            return self._error_form(
                "manage_saved_pick", Exception("snapshot"), hint="Snapshot unavailable"
            )

        def _pick_schema(options):
            """Build pick schema from options list."""
            return vol.Schema(
                {
                    vol.Optional("entity"): selector(
                        {
                            "select": {
                                "options": options,  # [{"value": "...", "label": "..."}]
                                "multiple": False,
                                "mode": "dropdown",
                            }
                        }
                    )
                }
            )

        options = saved_pick_options(self._snapshot or {})
        schema = _pick_schema(options)

        if user_input is None:
            return self.async_show_form(step_id="manage_saved_pick", data_schema=schema)

        # Empty submission -> re-show pick step
        if "entity" not in user_input or not user_input["entity"]:
            return self.async_show_form(step_id="manage_saved_pick", data_schema=schema)

        self._entity_id = user_input["entity"]
        return await self.async_step_manage_saved()

    async def async_step_manage_saved(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit a saved entity's properties."""
        # Must come from pick step; ensure snapshot & valid selection
        if self._snapshot is None:
            try:
                self._snapshot = await ws_get_snapshot(
                    self.hass, self.config_entry, timeout=15.0
                )
            except (TimeoutError, OSError, ClientError):
                entity_label = getattr(self, "_entity_id", "") or "Entity"
                return self.async_show_form(
                    step_id="manage_saved",
                    errors={"base": "tv_unreachable"},
                    data_schema=vol.Schema({}),
                    description_placeholders={"entity": entity_label},
                )

        ents: list[dict[str, Any]] = [
            e
            for e in (self._snapshot.get("entities", []) or [])
            if e.get("isSaved") and e.get("id")
        ]
        by_id = {e["id"]: e for e in ents}
        if not getattr(self, "_entity_id", None) or self._entity_id not in by_id:
            # If someone lands here directly, bounce to pick
            return await self.async_step_manage_saved_pick()

        entity = by_id[self._entity_id]
        cur_name = entity.get("customName") or entity.get("friendlyName") or ""
        entity_label = (
            entity.get("customName") or entity.get("friendlyName") or entity["id"]
        )

        if user_input is None:
            schema = vol.Schema(
                {
                    vol.Required("display_name", default=cur_name): str,
                }
            )
            # Show which entity we're editing via translatable placeholder.
            return self.async_show_form(
                step_id="manage_saved",
                data_schema=schema,
                description_placeholders={"entity": entity_label},
            )

        # Save
        new_name = user_input.get("display_name", cur_name)
        try:
            await ws_entities_update(
                self.hass,
                self.config_entry,
                updates=[{"id": self._entity_id, "customName": new_name}],
                timeout=15.0,
            )
            return self.async_create_entry(
                title="", data=dict(self.config_entry.options)
            )
        except Exception:
            _LOGGER.exception("Entities_update failed")
            return self.async_show_form(
                step_id="manage_saved",
                data_schema=vol.Schema(
                    {vol.Required("display_name", default=new_name): str}
                ),
                errors={"base": "tv_unreachable"},
                description_placeholders={"entity": entity_label},
            )

    # ---------- 3) Manage QuickBars ----------
    async def async_step_qb_pick(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick which QuickBar to edit (or create a new one)."""
        self._snapshot = None
        ok = await self._ensure_snapshot("qb_pick")
        if not ok:
            return self._error_form(
                "qb_pick", Exception("snapshot"), hint="Snapshot unavailable"
            )

        snapshot_dict: dict[str, Any] = self._snapshot or {}
        qb_list_raw: list[dict[str, Any]] | None = snapshot_dict.get("quick_bars")
        if qb_list_raw is None:
            qb_list_raw = []
        qb_list: list[dict[str, Any]] = list(qb_list_raw)
        if not qb_list:
            return self.async_show_form(
                step_id="qb_pick",
                data_schema=vol.Schema({}),
                errors={"base": "no_quickbars"},
            )

        options = qb_pick_options(qb_list)

        # Default to previously selected or first
        default_idx = self._qb_index if isinstance(self._qb_index, int) else 0
        if default_idx < 0 or default_idx >= len(qb_list):
            default_idx = 0

        if user_input is None:
            schema = schema_qb_pick(options, default_idx)
            return self.async_show_form(step_id="qb_pick", data_schema=schema)

        choice = str(user_input.get("quickbar", str(default_idx)))

        if choice == "new":
            existing_names = [qb.get("name") or "" for qb in qb_list]
            name = unique_qb_name("QuickBar", existing_names)
            new_qb = default_quickbar(name)
            qb_list.append(new_qb)
            if self._snapshot is None:
                self._snapshot = {}
            self._snapshot["quick_bars"] = qb_list
            self._qb_index = len(qb_list) - 1
            return await self.async_step_qb_manage()

        # Persist choice and continue to the editor
        try:
            self._qb_index = int(choice)
        except ValueError:
            self._qb_index = default_idx
        return await self.async_step_qb_manage()

    async def async_step_qb_manage(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit QuickBar properties and persist them."""
        if self._snapshot is None:
            ok = await self._ensure_snapshot("qb_manage")
            if not ok:
                return self._error_form(
                    "qb_manage", Exception("snapshot"), hint="Snapshot unavailable"
                )

        qb_list: list[dict[str, Any]] = list(
            (self._snapshot or {}).get("quick_bars", [])
        )
        if (
            not qb_list
            or not isinstance(self._qb_index, int)
            or not (0 <= self._qb_index < len(qb_list))
        ):
            return await self.async_step_qb_pick()

        qb = qb_list[self._qb_index]
        cur = defaults_from_qb(qb)
        saved_options, saved_ids = saved_options_from_snapshot(self._snapshot or {})

        if user_input is None:
            return self.async_show_form(
                step_id="qb_manage",
                data_schema=schema_qb_manage(qb, saved_options, cur),
            )

        new_name = (user_input.get("quickbar_name", cur["name"]) or "").strip()
        if name_taken(new_name, qb_list, self._qb_index):
            attempted = attempted_from_user(cur, user_input, saved_ids)
            return self.async_show_form(
                step_id="qb_manage",
                data_schema=schema_qb_manage(qb, saved_options, attempted),
                errors={"base": "name_taken"},
            )

        # Apply edits and persist
        apply_edits(qb, cur, user_input, saved_ids)
        try:
            payload = {"quick_bars": (self._snapshot or {}).get("quick_bars", [])}
            await ws_put_snapshot(self.hass, self.config_entry, payload, timeout=20.0)
            return self.async_create_entry(
                title="", data=dict(self.config_entry.options)
            )
        except Exception:
            _LOGGER.exception("Quickbar update failed")
            attempted = defaults_from_qb(qb)  # show what we now have on the qb
            return self.async_show_form(
                step_id="qb_manage",
                data_schema=schema_qb_manage(qb, saved_options, attempted),
                errors={"base": "tv_unreachable"},
            )

    async def async_step_done(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Finalize the options flow and return the current options."""
        return self.async_create_entry(title="", data=dict(self.config_entry.options))
