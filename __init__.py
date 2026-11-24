"""ComfyUI-CivitaiHub

Browse and download Civitai models from inside ComfyUI without a VPN.

The network path is described in ``hub_client``. Registering routes here keeps
the plugin usable even if the optional node pack fails to import.
"""

from __future__ import annotations

import atexit
import asyncio

WEB_DIRECTORY = "./web"
NODE_CLASS_MAPPINGS: dict[str, type] = {}
NODE_DISPLAY_NAME_MAPPINGS: dict[str, str] = {}

__version__ = "1.4.0"

_hub = None
try:
    from . import hub_client, hub_downloader, hub_routes  # noqa: F401

    hub_routes.register_routes()
    _hub = hub_client.client
    print("[CivitaiHub] routes registered at /civitai_hub/*")

    # No cookie picker runs here any more. It decrypted the browser's cookie
    # database in the background and wrote the civitai session token into
    # config.json, but the settings panel never exposed that field — there was
    # no way to see it, change it, or clear it. A credential with no UI and no
    # off switch is worse than no convenience at all. Downloads use the API key;
    # browsing works anonymously.
    #
    # Warming is started lazily from the first request handler (see
    # hub_routes._ensure_warm): at import time ComfyUI's loop does not exist
    # yet, so a task created here would never run.
except Exception as exc:  # noqa: BLE001 - never break ComfyUI startup
    print(f"[CivitaiHub] failed to register routes: {type(exc).__name__}: {exc}")


def _shutdown() -> None:
    if _hub is None:
        return
    try:
        loop = asyncio.new_event_loop()
        loop.run_until_complete(_hub.close())
        loop.close()
    except Exception:  # noqa: BLE001
        pass


atexit.register(_shutdown)

__all__ = [
    "NODE_CLASS_MAPPINGS",
    "NODE_DISPLAY_NAME_MAPPINGS",
    "WEB_DIRECTORY",
    "__version__",
]
