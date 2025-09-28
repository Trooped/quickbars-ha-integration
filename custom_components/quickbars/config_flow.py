from __future__ import annotations
from typing import Any, List, Dict
from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.data_entry_flow import FlowResult
from homeassistant.config_entries import OptionsFlow, ConfigEntry
from homeassistant.core import callback
from homeassistant.helpers.selector import selector
import voluptuous as vol
from .client import get_snapshot, post_snapshot

import logging

from .client import get_pair_code, confirm_pair
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
    
_ALLOWED_ENTITY_DOMAINS = [
    "light", "switch", "button", "fan", "input_boolean", "input_button",
    "script", "scene", "climate", "cover", "lock", "media_player",
    "automation", "camera", "sensor", "binary_sensor",
]


class QuickBarsOptionsFlow(OptionsFlow):
    """Options UI for a specific QuickBars TV entry."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        self.config_entry = config_entry
        self.snapshot: Dict[str, Any] | None = None
        self._host = config_entry.data[CONF_HOST]
        self._port = config_entry.data[CONF_PORT]
        # Working state for quickbars path
        self._qb_index: int | None = None

    # ---------- helpers ----------
    def _coerce_snapshot(self) -> None:
        """Make sure snapshot has the keys in the shape we expect."""
        if self.snapshot is None:
            self.snapshot = {}
        self.snapshot.setdefault("entities", [])
        self.snapshot.setdefault("quickbars", [])
        # Normalize entities to list[{"entity_id": str}]
        norm_entities: List[Dict[str, str]] = []
        for e in list(self.snapshot.get("entities", [])):
            if isinstance(e, dict) and "entity_id" in e:
                norm_entities.append({"entity_id": str(e["entity_id"])})
            elif isinstance(e, str):
                norm_entities.append({"entity_id": e})
        self.snapshot["entities"] = norm_entities
        # Normalize quickbars structure
        norm_qb: List[Dict[str, Any]] = []
        for qb in list(self.snapshot.get("quickbars", [])):
            name = qb.get("name") if isinstance(qb, dict) else None
            ents = qb.get("entities") if isinstance(qb, dict) else None
            qe: List[Dict[str, str]] = []
            if isinstance(ents, list):
                for item in ents:
                    if isinstance(item, dict) and "entity_id" in item:
                        qe.append({"entity_id": str(item["entity_id"])})
                    elif isinstance(item, str):
                        qe.append({"entity_id": item})
            norm_qb.append({"name": name or "QuickBar", "entities": qe})
        self.snapshot["quickbars"] = norm_qb

    def _entity_ids(self) -> List[str]:
        return [e["entity_id"] for e in (self.snapshot or {}).get("entities", [])]

    def _qb_names(self) -> List[str]:
        return [qb.get("name") or f"QuickBar {i+1}" for i, qb in enumerate((self.snapshot or {}).get("quickbars", []))]

    # ---------- entry point ----------
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Pull fresh snapshot, then show menu."""
        try:
            self.snapshot = await get_snapshot(self._host, self._port)
        except Exception as e:
            _LOGGER.exception("Options: pull failed for %s:%s", self._host, self._port)
            # If there is a cached snapshot in options, use it as fallback
            self.snapshot = dict(self.config_entry.options).get("snapshot")
            if not self.snapshot:
                return self.async_show_form(
                    step_id="init",
                    errors={"base": "tv_unreachable"},
                    description_placeholders={"hint": f"{type(e).__name__}: {e}"},
                )
        self._coerce_snapshot()

        # Persist snapshot into options so the user can come back later without re-pulling
        new_opts = dict(self.config_entry.options)
        new_opts["snapshot"] = self.snapshot
        return await self.async_step_menu()

    # ---------- menu ----------
    async def async_step_menu(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is None:
            return self.async_show_form(
                step_id="menu",
                data_schema=vol.Schema({
                    vol.Required(
                        "action",
                        description={"suggested_value": "export_entities"}
                    ): selector({
                        "select": {
                            "options": [
                                {"label": "Export Entities", "value": "export_entities"},
                                {"label": "Configure QuickBars", "value": "configure_quickbars"},
                            ]
                        }
                    })
                }),
                description_placeholders={
                    "title": "QuickBars Configuration",
                    "description": "Choose what you want to configure",
                }
            )

        action = user_input["action"]
        if action == "export_entities":
            return await self.async_step_export_entities()
        if action == "configure_quickbars":
            return await self.async_step_qb_menu()
        return await self.async_step_menu()

    # ---------- Export Entities ----------
    async def async_step_export_entities(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        self._coerce_snapshot()
        preselected = self._entity_ids()

        if user_input is None:
            schema = vol.Schema({
                vol.Required(
                    "entities",
                    default=preselected
                ): selector({
                    "entity": {
                        "multiple": True,
                        "domain": _ALLOWED_ENTITY_DOMAINS
                    }
                })
            })
            return self.async_show_form(
                step_id="export_entities",
                data_schema=schema,
                description_placeholders={
                    "title": "Export Entities",
                    "description": "Pick Home Assistant entities to export to the TV app",
                }
            )

        chosen: List[str] = list(user_input.get("entities") or [])
        self.snapshot["entities"] = [{"entity_id": e} for e in chosen]

        # Save and push
        new_opts = dict(self.config_entry.options)
        new_opts["snapshot"] = self.snapshot
        try:
            await post_snapshot(self._host, self._port, self.snapshot)
        except Exception as e:
            _LOGGER.exception("Options: push entities failed for %s:%s", self._host, self._port)
            return self.async_show_form(
                step_id="export_entities",
                data_schema=vol.Schema({
                    vol.Required("entities", default=chosen): selector({
                        "entity": {"multiple": True, "domain": _ALLOWED_ENTITY_DOMAINS}
                    })
                }),
                errors={"base": "tv_unreachable"},
                description_placeholders={"hint": f"{type(e).__name__}: {e}"},
            )
        return await self.async_step_menu()

    # ---------- QuickBars: menu (pick or create) ----------
    async def async_step_qb_menu(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        self._coerce_snapshot()
        qb_names = self._qb_names()
        options = [{"label": name, "value": f"idx:{i}"} for i, name in enumerate(qb_names)]
        options.append({"label": "➕ Create new QuickBar", "value": "new"})

        if user_input is None:
            schema = vol.Schema({
                vol.Required("choice"): selector({"select": {"options": options}})
            })
            return self.async_show_form(
                step_id="qb_menu",
                data_schema=schema,
                description_placeholders={
                    "title": "Configure QuickBars",
                    "description": "Choose a QuickBar to edit, or create a new one",
                }
            )

        choice = user_input["choice"]
        if choice == "new":
            return await self.async_step_qb_create()
        if choice.startswith("idx:"):
            self._qb_index = int(choice.split(":", 1)[1])
            return await self.async_step_qb_edit()
        return await self.async_step_qb_menu()

    # ---------- QuickBars: create ----------
    async def async_step_qb_create(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        self._coerce_snapshot()
        pre = []  # default empty selection
        if user_input is None:
            schema = vol.Schema({
                vol.Required("name"): str,
                vol.Optional("entities", default=pre): selector({
                    "entity": {"multiple": True, "domain": _ALLOWED_ENTITY_DOMAINS}
                })
            })
            return self.async_show_form(
                step_id="qb_create",
                data_schema=schema,
                description_placeholders={
                    "title": "New QuickBar",
                    "description": "Give it a name and pick entities",
                }
            )

        name = (user_input.get("name") or "QuickBar").strip()
        ents: List[str] = list(user_input.get("entities") or [])
        self.snapshot["quickbars"].append({"name": name, "entities": [{"entity_id": e} for e in ents]})

        # Save & push
        try:
            await post_snapshot(self._host, self._port, self.snapshot)
        except Exception as e:
            _LOGGER.exception("Options: push new quickbar failed for %s:%s", self._host, self._port)
            # Re-show the create form with prior values
            schema = vol.Schema({
                vol.Required("name", default=name): str,
                vol.Optional("entities", default=ents): selector({
                    "entity": {"multiple": True, "domain": _ALLOWED_ENTITY_DOMAINS}
                })
            })
            return self.async_show_form(
                step_id="qb_create",
                data_schema=schema,
                errors={"base": "tv_unreachable"},
                description_placeholders={"hint": f"{type(e).__name__}: {e}"},
            )
        return await self.async_step_qb_menu()

    # ---------- QuickBars: edit existing ----------
    async def async_step_qb_edit(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        self._coerce_snapshot()
        if self._qb_index is None or self._qb_index >= len(self.snapshot["quickbars"]):
            return await self.async_step_qb_menu()

        qb = self.snapshot["quickbars"][self._qb_index]
        current_name = qb.get("name") or f"QuickBar {self._qb_index + 1}"
        current_ents = [e["entity_id"] for e in qb.get("entities", [])]

        if user_input is None:
            schema = vol.Schema({
                vol.Required("name", default=current_name): str,
                vol.Optional("entities", default=current_ents): selector({
                    "entity": {"multiple": True, "domain": _ALLOWED_ENTITY_DOMAINS}
                }),
                vol.Optional("delete_quickbar", default=False): bool,
            })
            return self.async_show_form(
                step_id="qb_edit",
                data_schema=schema,
                description_placeholders={
                    "title": "Edit QuickBar",
                    "description": "Rename, change entities, or delete this QuickBar",
                }
            )

        if user_input.get("delete_quickbar"):
            # Delete and push
            del self.snapshot["quickbars"][self._qb_index]
            try:
                await post_snapshot(self._host, self._port, self.snapshot)
            except Exception as e:
                _LOGGER.exception("Options: delete quickbar failed for %s:%s", self._host, self._port)
                return self.async_show_form(
                    step_id="qb_edit",
                    data_schema=vol.Schema({
                        vol.Required("name", default=current_name): str,
                        vol.Optional("entities", default=current_ents): selector({
                            "entity": {"multiple": True, "domain": _ALLOWED_ENTITY_DOMAINS}
                        }),
                        vol.Optional("delete_quickbar", default=True): bool,
                    }),
                    errors={"base": "tv_unreachable"},
                    description_placeholders={"hint": f"{type(e).__name__}: {e}"},
                )
            self._qb_index = None
            return await self.async_step_qb_menu()

        # Update name/entities
        new_name = (user_input.get("name") or current_name).strip()
        ents: List[str] = list(user_input.get("entities") or [])
        qb["name"] = new_name
        qb["entities"] = [{"entity_id": e} for e in ents]

        try:
            await post_snapshot(self._host, self._port, self.snapshot)
        except Exception as e:
            _LOGGER.exception("Options: push edited quickbar failed for %s:%s", self._host, self._port)
            # Re-show the edit form with user values
            schema = vol.Schema({
                vol.Required("name", default=new_name): str,
                vol.Optional("entities", default=ents): selector({
                    "entity": {"multiple": True, "domain": _ALLOWED_ENTITY_DOMAINS}
                }),
                vol.Optional("delete_quickbar", default=False): bool,
            })
            return self.async_show_form(
                step_id="qb_edit",
                data_schema=schema,
                errors={"base": "tv_unreachable"},
                description_placeholders={"hint": f"{type(e).__name__}: {e}"},
            )

        return await self.async_step_qb_menu()