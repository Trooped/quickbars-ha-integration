from __future__ import annotations
from typing import Any, Optional, Mapping
import aiohttp
import logging
import time

_LOGGER = logging.getLogger(__name__)

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

async def confirm_pair(host: str, port: int, code: str, sid: str, ha_instance: str | None = None) -> dict[str, Any]:
    url = f"http://{host}:{port}/api/pair/confirm"
    payload: dict[str, Any] = {"code": code, "sid": sid}
    if ha_instance:
        payload["ha_instance"] = ha_instance
    masked = dict(payload, code=_mask_code(code), sid=_mask_sid(sid))
    _LOGGER.debug("confirm_pair: host=%s port=%s payload=%s", host, port, masked)
    data = await _request_json("POST", url, json=payload, timeout=15.0)
    _LOGGER.debug("confirm_pair: response=%s", data)
    return data

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