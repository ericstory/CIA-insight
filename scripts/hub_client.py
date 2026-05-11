"""CIA Hub Client — 当 CIA_HUB_URL 已配置时，所有 fetch 走远程 Hub。
用法：其他模块 import 后调用 fetch(source, **params)，若 hub 不可用则抛 RuntimeError。
"""
from __future__ import annotations

import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import config

import requests

_URL = None
_TOKEN = None


def _hub_url() -> str | None:
    global _URL
    if _URL is None:
        _URL = config.get("CIA_HUB_URL", "")
    return _URL or None


def _hub_token() -> str:
    global _TOKEN
    if _TOKEN is None:
        _TOKEN = config.get("CIA_HUB_TOKEN", "")
    return _TOKEN


def enabled() -> bool:
    return bool(_hub_url())


def fetch(source: str, **params) -> list[dict]:
    """Call hub /v1/fetch. Returns raw rows."""
    url = _hub_url()
    if not url:
        raise RuntimeError("CIA_HUB_URL not set")
    resp = requests.post(
        f"{url.rstrip('/')}/v1/fetch",
        headers={"Authorization": f"Bearer {_hub_token()}"},
        json={"source": source, "params": params},
        timeout=600,
    )
    if resp.status_code == 401:
        raise RuntimeError("Hub auth failed — check CIA_HUB_TOKEN")
    resp.raise_for_status()
    return resp.json()


def check() -> dict:
    """Ping hub health endpoint."""
    url = _hub_url()
    if not url:
        return {"ok": False, "error": "CIA_HUB_URL not set"}
    try:
        r = requests.get(f"{url.rstrip('/')}/health", timeout=5)
        return r.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}
