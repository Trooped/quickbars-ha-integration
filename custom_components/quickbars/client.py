from __future__ import annotations
from typing import Any, Optional, Mapping, Dict, List
import aiohttp
import logging
import time
import asyncio
import secrets

_LOGGER = logging.getLogger(__name__)
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry

def _ws_log(action: str, phase: str, cid: str, exp_id: str | None, extra: str = ""):
    _LOGGER.debug("WS %s %s cid=%s id=%s %s", action, phase, cid, exp_id, extra)

def _entry_id(entry) -> str:
    eid = entry.data.get("id")
    if not eid:
        raise ValueError("Config entry missing 'id' (app-generated). Re-pair to create a proper entry.")
    return eid

# ---------- Connectivity ----------

async def ping(host: str, port: int) -> bool:
    url = f"http://{host}:{port}/api/ping"
    try:
        await _request_json("GET", url, timeout=5.0)
        return True
    except Exception:
        return False

# ---- helpers to mask transient secrets in logs ----

def _mask_code(code: Optional[str]) -> str:
    if not code:
        return "<none>"
    # "D3D4" -> "D***4"
    return f"{code[:1]}***{code[-1:]}"

def _mask_sid(sid: Optional[str]) -> str:
    if not sid:
        return "<none>"
    if len(sid) <= 4:
        return "***"
    # "jGvjjfZyyH" -> "jGv***yH"
    return f"{sid[:3]}***{sid[-2:]}"

async def _request_json(
    method: str,
    url: str,
    *,
    json: Any | None = None,
    headers: Mapping[str, str] | None = None,
    timeout: float = 15.0,
) -> Any:
    t0 = time.monotonic()
    _LOGGER.debug("HTTP %s %s json=%s headers=%s", method, url, json, dict(headers or {}))
    try:
        async with aiohttp.ClientSession() as s:
            async with s.request(method, url, json=json, headers=headers, timeout=timeout) as r:
                text = await r.text()
                dt = (time.monotonic() - t0) * 1000.0
                # Try to parse JSON, but keep raw body for logging on errors
                if r.status >= 400:
                    _LOGGER.debug("HTTP %s %s -> %s in %.0f ms; body=%s", method, url, r.status, dt, text)
                    r.raise_for_status()
                try:
                    data = await r.json()
                except Exception:
                    _LOGGER.debug("HTTP %s %s -> %s in %.0f ms; non-JSON body=%s", method, url, r.status, dt, text)
                    r.raise_for_status()
                    return text
                _LOGGER.debug("HTTP %s %s -> %s in %.0f ms; json=%s", method, url, r.status, dt, data)
                return data
    except Exception as e:
        dt = (time.monotonic() - t0) * 1000.0
        _LOGGER.debug("HTTP %s %s failed in %.0f ms: %r", method, url, dt, e)
        raise

# ---------- Manual pairing ----------

async def get_pair_code(host: str, port: int) -> dict[str, Any]:
    url = f"http://{host}:{port}/api/pair/code"
    data = await _request_json("GET", url, timeout=15.0)
    # Mask in logs
    _LOGGER.debug(
        "pair_code: host=%s port=%s -> code=%s sid=%s ttl=%s",
        host, port, _mask_code(data.get("code")), _mask_sid(data.get("sid")), data.get("ttl"),
    )
    return data

async def confirm_pair(host: str, port: int, code: str, sid: str,
                       ha_instance: str | None = None,
                       ha_name: str | None = None,
                       ha_url: str | None = None) -> dict[str, Any]:
    url = f"http://{host}:{port}/api/pair/confirm"
    payload: dict[str, Any] = {"code": code, "sid": sid}
    if ha_instance:
        payload["ha_instance"] = ha_instance
    if ha_name:
        payload["ha_name"] = ha_name
    if ha_url:
        payload["ha_url"] = ha_url
    masked = dict(payload, code=_mask_code(code), sid=_mask_sid(sid))
    _LOGGER.debug("confirm_pair: host=%s port=%s payload=%s", host, port, masked)
    data = await _request_json("POST", url, json=payload, timeout=15.0)
    _LOGGER.debug("confirm_pair: response=%s", data)
    return data

async def set_credentials(host: str, port: int, url: str, token: str) -> dict[str, Any]:
    return await _request_json("POST", f"http://{host}:{port}/api/ha/credentials",
                               json={"url": url, "token": token}, timeout=20.0)

# ---------- Authorized (paired) endpoints ----------

async def get_snapshot(host: str, port: int) -> dict[str, Any]:
    url = f"http://{host}:{port}/api/snapshot"
    async with aiohttp.ClientSession() as s:
        async with s.get(url, timeout=15) as r:
            r.raise_for_status()
            return await r.json()

async def post_snapshot(host: str, port: int, snapshot: dict[str, Any]) -> None:
    url = f"http://{host}:{port}/api/snapshot"
    async with aiohttp.ClientSession() as s:
        async with s.post(url, json=snapshot, timeout=20) as r:
            r.raise_for_status()





EVENT_REQ = "quickbars_config_request"
EVENT_RES = "quickbars_config_response"

async def ws_get_snapshot(hass: HomeAssistant, entry: ConfigEntry, timeout: float = 15.0) -> dict[str, Any]:
    cid = secrets.token_urlsafe(8)
    exp_id = _entry_id(entry)
    fut = hass.loop.create_future()
    t0 = time.monotonic()

    def _cb(event):
        data = event.data or {}
        if data.get("cid") != cid or data.get("id") != exp_id:
            return
        _ws_log("get_snapshot", "recv", cid, exp_id, f"ok={data.get('ok')} dt_ms={(time.monotonic()-t0)*1000:.0f}")
        if not fut.done():
            fut.set_result(data)

    _ws_log("get_snapshot", "send", cid, exp_id, "")
    unsub = hass.bus.async_listen(EVENT_RES, _cb)
    try:
        hass.bus.async_fire(EVENT_REQ, {"id": exp_id, "action": "get_snapshot", "cid": cid})
        res = await asyncio.wait_for(fut, timeout)
        if not res.get("ok"):
            raise RuntimeError(f"TV error: {res}")
        return res.get("payload") or {}
    finally:
        unsub()


async def ws_entities_replace(
    hass: HomeAssistant,
    entry: ConfigEntry,
    entity_ids: list[str],
    names: Optional[Dict[str, str]] = None,
    custom_names: Optional[Dict[str, str]] = None,
    timeout: float = 25.0,
) -> dict[str, Any]:
    cid = secrets.token_urlsafe(8)
    exp_id = _entry_id(entry)  # Get verified entry ID
    fut = hass.loop.create_future()

    def _cb(event):
        data = event.data or {}
        if data.get("cid") != cid or data.get("id") != exp_id:  # Check both match
            return
        if not fut.done():
            fut.set_result(data)

    unsub = hass.bus.async_listen(EVENT_RES, _cb)
    try:
        payload: Dict[str, Any] = {"entity_ids": entity_ids}
        if names:
            payload["names"] = names
        if custom_names:
            payload["custom_names"] = custom_names

        hass.bus.async_fire(
            EVENT_REQ,
            {"id": exp_id, "action": "entities_replace", "cid": cid, "payload": payload},  # Use exp_id
        )
        res = await asyncio.wait_for(fut, timeout)
        if not res.get("ok"):
            raise RuntimeError(f"TV replied error: {res}")
        return res.get("payload") or {}
    finally:
        unsub()

async def ws_entities_update(
    hass: HomeAssistant,
    entry: ConfigEntry,
    updates: List[Dict[str, Any]],
    timeout: float = 20.0,
) -> Dict[str, Any]:
    cid = secrets.token_urlsafe(8)
    exp_id = _entry_id(entry)  # Get verified entry ID
    fut = hass.loop.create_future()

    def _cb(event):
        data = event.data or {}
        if data.get("cid") != cid or data.get("id") != exp_id:  # Check both match
            return
        if not fut.done(): 
            fut.set_result(data)

    unsub = hass.bus.async_listen(EVENT_RES, _cb)
    try:
        hass.bus.async_fire(
            EVENT_REQ,
            {
                "id": exp_id,  # Use exp_id
                "action": "entities_update",
                "cid": cid,
                "payload": {"entities": updates},
            },
        )
        res = await asyncio.wait_for(fut, timeout)
        if not res.get("ok"):
            raise RuntimeError(f"TV replied error: {res}")
        return res.get("payload") or {}
    finally:
        unsub()

async def ws_put_snapshot(
    hass: HomeAssistant, 
    entry: ConfigEntry, 
    snapshot: dict[str, Any], 
    timeout: float = 20.0
) -> None:
    cid = secrets.token_urlsafe(8)
    exp_id = _entry_id(entry)  # Get verified entry ID
    fut: asyncio.Future = hass.loop.create_future()

    def _cb(event):
        data = event.data or {}
        if data.get("cid") != cid or data.get("id") != exp_id:  # Check both match
            return
        if not fut.done():
            fut.set_result(data)

    unsub = hass.bus.async_listen(EVENT_RES, _cb)
    try:
        hass.bus.async_fire(
            EVENT_REQ, 
            {
                "id": exp_id,  # Use exp_id
                "action": "put_snapshot", 
                "cid": cid, 
                "payload": snapshot
            }
        )
        res = await asyncio.wait_for(fut, timeout)
        if not res.get("ok"):
            raise RuntimeError(f"TV replied error: {res}")
    finally:
        unsub()


async def ws_ping(hass: HomeAssistant, entry: ConfigEntry, timeout: float = 5.0) -> bool:
    cid = secrets.token_urlsafe(8)
    exp_id = _entry_id(entry)
    fut = hass.loop.create_future()
    t0 = time.monotonic()

    def _cb(event):
        data = event.data or {}
        # Only accept exact match on both
        if data.get("cid") != cid or data.get("id") != exp_id:
            return
        _ws_log("ping", "recv", cid, exp_id, f"ok={data.get('ok')} dt_ms={(time.monotonic()-t0)*1000:.0f}")
        if not fut.done():
            fut.set_result(bool(data.get("ok", False)))

    _ws_log("ping", "send", cid, exp_id, "")
    unsub = hass.bus.async_listen(EVENT_RES, _cb)
    try:
        hass.bus.async_fire(EVENT_REQ, {"id": exp_id, "action": "ping", "cid": cid})
        return await asyncio.wait_for(fut, timeout)
    finally:
        unsub()



async def ws_notify(hass, entry, payload: dict, timeout: float = 8.0) -> bool:
    """Send a 'notify' command with style/media options to the TV."""
    cid = secrets.token_urlsafe(8)
    exp_id = _entry_id(entry)
    fut = hass.loop.create_future()
    t0 = time.monotonic()

    def _cb(event):
        data = event.data or {}
        if data.get("cid") != cid or data.get("id") != exp_id:
            return
        _ws_log("notify", "recv", cid, exp_id, f"ok={data.get('ok')} dt_ms={(time.monotonic()-t0)*1000:.0f}")
        if not fut.done():
            fut.set_result(bool(data.get("ok", False)))

    _ws_log("notify", "send", cid, exp_id, "")
    unsub = hass.bus.async_listen(EVENT_RES, _cb)
    try:
        hass.bus.async_fire(EVENT_REQ, {"id": exp_id, "action": "notify", "cid": cid, "payload": payload})
        return await asyncio.wait_for(fut, timeout)
    finally:
        unsub()

def ws_notify_fire(hass, entry, payload: dict, cid: str | None = None) -> str:
    """Send a 'notify' command (no wait); return the correlation id used."""
    exp_id = _entry_id(entry)
    cid = cid or secrets.token_urlsafe(8)
    hass.bus.async_fire(EVENT_REQ, {"id": exp_id, "action": "notify", "cid": cid, "payload": payload})
    _ws_log("notify", "fire", cid, exp_id, "")
    return cid