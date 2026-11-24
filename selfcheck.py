"""Self-check for ComfyUI-CivitaiHub.

Run this with ComfyUI's own interpreter to verify the three network hops work
from the current machine (Windows syntax; use your own paths):

    <ComfyUI>\\python_embeded\\python.exe ^
        <ComfyUI>\\ComfyUI\\custom_nodes\\ComfyUI-CivitaiHub\\selfcheck.py

It never writes into models/; the download probe only fetches the first 256 KB.
"""

import asyncio
import importlib.util
import sys
import time
import traceback
import types
from pathlib import Path

try:  # keep the console readable on Windows code pages
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

PKG_DIR = Path(__file__).resolve().parent
PKG_NAME = "cvhub_selfcheck"

# The plugin registers aiohttp routes at import time; outside ComfyUI there is
# no `server` module, so stub the minimum it needs.
if "server" not in sys.modules:
    from aiohttp import web as _web

    _fake = types.ModuleType("server")

    class _PromptServer:
        instance = types.SimpleNamespace(routes=_web.RouteTableDef())

    _fake.PromptServer = _PromptServer
    sys.modules["server"] = _fake


def load():
    spec = importlib.util.spec_from_file_location(
        PKG_NAME, PKG_DIR / "__init__.py", submodule_search_locations=[str(PKG_DIR)]
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[PKG_NAME] = mod
    spec.loader.exec_module(mod)
    return importlib.import_module(f"{PKG_NAME}.hub_client")


async def main() -> int:
    hub_client = load()
    client = hub_client.client
    failures = 0

    print("=" * 66)
    print("ComfyUI-CivitaiHub self-check")
    print("=" * 66)

    health = await client.health()
    for name, key in (("API 镜像", "mirrors"), ("图片代理", "images")):
        for host, info in health[key].items():
            flag = "OK " if info.get("ok") else "FAIL"
            detail = f"{info.get('ms', '-')}ms" if info.get("ok") else info.get("error", "error")
            print(f"[{flag}] {name} {host:34s} {detail}")
            if not info.get("ok"):
                failures += 1
    if not health["summary"]["mirror_ok"]:
        print("\n没有可用镜像，请在面板「通道设置」里换一个。")
        await client.close()
        return 1

    t0 = time.time()
    data = await client.search("style", types="LORA", limit=3)
    items = data.get("items") or []
    print(f"[{'OK ' if items else 'FAIL'}] 搜索           {len(items)} 条 {time.time()-t0:.2f}s")
    if not items:
        failures += 1
        await client.close()
        return 1

    model = items[0]
    version = (model.get("modelVersions") or [{}])[0]
    print(f"[OK ] 详情           {model.get('name', '')[:44]}  "
          f"{len(model.get('modelVersions') or [])} 个版本")

    info = await client.resolve_file(version["id"])
    ok = info["url"].startswith("https://") and info["host"] != "b2.civitai.com"
    print(f"[{'OK ' if ok else 'FAIL'}] 下载地址改写   host={info['host']}")
    if not ok:
        failures += 1

    session = await client._get_session()
    t0 = time.time()
    async with session.get(info["url"], headers={"Range": "bytes=0-262143"}) as resp:
        chunk = await resp.read()
    ok = resp.status in (200, 206) and len(chunk) > 0
    print(f"[{'OK ' if ok else 'FAIL'}] 文件分片        HTTP {resp.status} "
          f"{len(chunk)}B {time.time()-t0:.2f}s")
    if not ok:
        failures += 1

    imgs = version.get("images") or []
    if imgs:
        got = await client.fetch_thumb(imgs[0]["url"], 200)
        ok = bool(got) and len(got[0]) > 512
        print(f"[{'OK ' if ok else 'FAIL'}] 缩略图          "
              f"{len(got[0]) if got else 0}B  {got[1] if got else ''}")
        if not ok:
            failures += 1

    print("-" * 66)
    print("附加站点（失败不影响主通道）：")
    try:
        providers = importlib.import_module(f"{PKG_NAME}.hub_providers")
        result = await asyncio.wait_for(
            providers.hf_provider.search("lora", limit=3), timeout=35
        )
        count = len(result.get("items") or [])
        print(f"[{'OK ' if count else '-- '}] HuggingFace   {count} 条结果")
    except Exception as exc:  # noqa: BLE001
        print(f"[-- ] HuggingFace   暂不可用（该镜像限流）：{type(exc).__name__}")

    await client.close()
    print("-" * 66)
    if failures:
        print(f"{failures} 项失败 · 在面板「通道设置」里调整对应通道")
    else:
        print("全部通道正常，可以开始搜索下载")
    return 0 if not failures else 1


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except Exception:
        traceback.print_exc()
        sys.exit(1)
