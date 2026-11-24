"""Runtime configuration for ComfyUI-CivitaiHub.

Everything that makes the plugin work without a VPN lives here: the REST API
mirror pool, the public image proxies and the Backblaze B2 clusters that back
`civitai-modelfiles`. All of them are resolved at runtime and can be swapped
from the UI without touching code.
"""

from __future__ import annotations

import copy
import json
import os
import threading
from pathlib import Path
from typing import Any

PKG_DIR = Path(__file__).resolve().parent
CONFIG_PATH = PKG_DIR / "config.json"
CACHE_DIR = PKG_DIR / "cache"
THUMB_DIR = CACHE_DIR / "thumbs"
TMP_DIR = CACHE_DIR / "tmp"

DEFAULTS: dict[str, Any] = {
    # Tried in order. The official host wins whenever a proxy/TUN is available,
    # which makes the ordering byte-identical to the website; the mirror is the
    # fallback for when there is no VPN.
    # Both verified reachable without a VPN; requests race them concurrently.
    # civitai.com is intentionally absent: it is SNI-blocked here, so listing it
    # just costs a connect timeout on every single request.
    "api_mirrors": ["https://civitai.red", "https://civitai.work"],
    # Public image proxies able to fetch image.civitai.com.
    "image_proxies": ["wsrv.nl", "images.weserv.nl"],
    # Backblaze B2 clusters hosting the civitai-modelfiles bucket.
    "b2_endpoints": ["f004.backblazeb2.com"],
    # Optional HTTP proxy for reaching civitai.com directly (e.g. a local
    # Clash/v2ray port). Empty = rely on environment variables only.
    "http_proxy": "",
    # Optional Civitai API key, unlocks mature content beyond the anonymous feed.
    "civitai_api_key": "",
    # Alternative to the key: paste the browser cookie after signing in with
    # email (or Google/Discord). Either one is enough.
    "civitai_cookie": "",
    # Ask the mirror for mature models by default.
    "nsfw": True,
    # Width requested from the image proxy.
    "thumb_width": 450,
    # Parallel downloads.
    "max_concurrent_downloads": 2,
    # R2 caps per-connection throughput around 40-220 KB/s, so ranges are the
    # only way to fill a fast link. Configurable in the settings panel (1-128).
    "download_segments": 64,
    # Empty means "pick the folder that matches the model type".
    # Manually chosen folder under models/. Everything goes here regardless of
    # model type — no automatic per-type routing.
    "save_subdir": "loras",
    # Per-type destinations, shown as one row of inputs in the settings panel.
    # An empty value falls back to save_subdir.
    "save_dirs": {
        "LORA": "loras",
        "Checkpoint": "checkpoints",
        "LoCon": "loras",
        "DoRA": "loras",
        "Controlnet": "controlnet",
        "TextualInversion": "embeddings",
        "VAE": "vae",
        "Upscaler": "upscale_models",
        "Hypernetwork": "hypernetworks",
        "Poses": "poses",
        "MotionModule": "animatediff_models",
        "AestheticGradient": "loras",
        "Wildcards": "wildcards",
        "Workflows": "workflows",
    },
    # Preferred download folder key, used when the model type is missing.
    "fallback_folder": "loras",
    # Per-site credentials. Sites that require a login read from here; empty
    # values mean "not configured" and the site is hidden from the switcher.
    "accounts": {
        "liblib": {
            "access_key": "",
            "secret_key": "",
            "cookie": "",
            "api_base": "https://openapi.liblibai.cloud",
            "model_list_path": "/api/v1/model/list",
        },
        "tusi": {
            "access_key": "",
            "secret_key": "",
            "cookie": "",
            "api_base": "https://api.tusiart.cn",
            "model_list_path": "/api/v1/model/list",
        },
    },
}

_lock = threading.RLock()


def _ensure_dirs() -> None:
    for d in (CACHE_DIR, THUMB_DIR, TMP_DIR):
        d.mkdir(parents=True, exist_ok=True)


class Config:
    def __init__(self, path: Path = CONFIG_PATH) -> None:
        self.path = path
        self._data: dict[str, Any] = copy.deepcopy(DEFAULTS)
        self.load()

    def load(self) -> dict[str, Any]:
        with _lock:
            if self.path.exists():
                try:
                    raw = json.loads(self.path.read_text(encoding="utf-8"))
                    if isinstance(raw, dict):
                        for k, v in raw.items():
                            if k in DEFAULTS:
                                self._data[k] = v
                except Exception:
                    # A corrupt config must never stop ComfyUI from booting.
                    pass
            else:
                self.save()
            _ensure_dirs()
            return self._data

    def save(self) -> None:
        with _lock:
            _ensure_dirs()
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(self._data, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            os.replace(tmp, self.path)

    def get(self, key: str, default: Any = None) -> Any:
        with _lock:
            return self._data.get(key, DEFAULTS.get(key, default))

    def set(self, key: str, value: Any) -> None:
        with _lock:
            if key not in DEFAULTS:
                raise KeyError(f"unknown config key: {key}")
            self._data[key] = value
            self.save()

    def update(self, patch: dict[str, Any]) -> dict[str, Any]:
        with _lock:
            patch = dict(patch)
            if isinstance(patch.get("accounts"), dict):
                patch["accounts"] = self._merge_accounts(patch["accounts"])
            for k, v in patch.items():
                if k in DEFAULTS:
                    self._data[k] = v
            self.save()
            return dict(self._data)

    def _merge_accounts(self, incoming: dict[str, Any]) -> dict[str, Any]:
        """Merge per-site credentials, treating ``***`` as "leave unchanged"."""
        merged = copy.deepcopy(self._data.get("accounts") or {})
        for site, creds in incoming.items():
            if not isinstance(creds, dict):
                continue
            bucket = merged.setdefault(site, {})
            for key, value in creds.items():
                if value == "***":
                    continue
                bucket[str(key)] = value
        return merged

    def accounts(self) -> dict[str, dict[str, Any]]:
        with _lock:
            return copy.deepcopy(self._data.get("accounts") or {})

    def site_configured(self, site: str) -> bool:
        creds = (self._data.get("accounts") or {}).get(site) or {}
        return bool(creds.get("access_key") and creds.get("secret_key")) or bool(
            creds.get("cookie")
        )

    def as_dict(self, *, redact: bool = True) -> dict[str, Any]:
        with _lock:
            data = copy.deepcopy(self._data)
        if redact:
            # Everything sensitive is masked now, the API key included. It used
            # to be returned in clear text on the grounds that the endpoint was
            # local-only — which was never actually checked, so any host that
            # could reach ComfyUI could read the key out of this response.
            if data.get("civitai_api_key"):
                data["civitai_api_key"] = "***"
            if data.get("civitai_cookie"):
                data["civitai_cookie"] = "***"
            for creds in (data.get("accounts") or {}).values():
                if not isinstance(creds, dict):
                    continue
                for key in ("secret_key", "cookie", "api_key"):
                    if creds.get(key):
                        creds[key] = "***"
        data["_has_api_key"] = bool(self._data.get("civitai_api_key"))
        data["_configured_sites"] = [
            site for site in (self._data.get("accounts") or {})
            if self.site_configured(site)
        ]
        return data


config = Config()
