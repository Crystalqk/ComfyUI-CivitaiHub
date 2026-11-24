"""Extra model sources beyond Civitai.

Only zero-configuration sources live here: anything that needs a signed
request or a login cookie stays out, because a plugin that silently fails is
worse than one that simply is not there.

HuggingFace is reached through ``hf-mirror.com``, which is reachable from
mainland China while ``huggingface.co`` is not. File downloads go through the
same mirror, so no signed URL needs to be rewritten.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any
from urllib.parse import quote

from .hub_client import UA, client

HF_BASE = "https://hf-mirror.com"

# Repos whose tags say "lora" are adapters; everything else is a full model or
# a packaging repo. Used only for the badge on the card.
_LORA_HINT = re.compile(r"(^|[-_/])lora($|[-_/])", re.I)
_IMG_MD = re.compile(r"!\[[^\]]*\]\(\s*([^)\s]+)")

_WEIGHT_EXT = (".safetensors", ".ckpt", ".pt", ".pth", ".bin")


def _split_repo(repo_id: str) -> tuple[str, str]:
    parts = (repo_id or "").strip("/").split("/")
    if len(parts) >= 2:
        return parts[0], "/".join(parts[1:])
    return "", repo_id


def _repo_name(repo_id: str) -> str:
    return (repo_id or "").rstrip("/").split("/")[-1] or repo_id


def _is_lora(repo: dict[str, Any]) -> bool:
    tags = [str(t).lower() for t in (repo.get("tags") or [])]
    if "lora" in tags:
        return True
    if any(_LORA_HINT.search(t) for t in tags):
        return True
    return bool(_LORA_HINT.search(_repo_name(repo.get("id", ""))))


def _base_model(repo: dict[str, Any]) -> str:
    for tag in repo.get("tags") or []:
        low = str(tag).lower()
        if low.startswith("base_model:") and not low.startswith("base_model:adapter:"):
            return str(tag).split(":", 1)[1].split("/")[-1]
    return ""


class HuggingFaceProvider:
    key = "hf"
    label = "HuggingFace"

    def __init__(self) -> None:
        self._cache: dict[str, tuple[float, Any]] = {}
        self._cache_ttl = 300.0

    def _cached(self, key: str) -> Any | None:
        hit = self._cache.get(key)
        if not hit:
            return None
        stamp, value = hit
        if asyncio.get_event_loop().time() - stamp > self._cache_ttl:
            self._cache.pop(key, None)
            return None
        return value

    def _store(self, key: str, value: Any) -> None:
        self._cache[key] = (asyncio.get_event_loop().time(), value)
        if len(self._cache) > 200:
            oldest = sorted(self._cache.items(), key=lambda kv: kv[1][0])[:50]
            for k, _ in oldest:
                self._cache.pop(k, None)

    async def _json(self, path: str, params: dict[str, Any] | None = None,
                    timeout: int = 12, retries: int = 2) -> Any:
        # hf-mirror throttles aggressively: identical requests alternate between
        # 200, 503 and timeouts. Back off and retry instead of surfacing a failure.
        key = f"{path}|{json.dumps(params or {}, sort_keys=True)}"
        cached = self._cached(key)
        if cached is not None:
            return cached

        session = await client._get_session()
        url = f"{HF_BASE}{path}"
        last = "unknown"
        for attempt in range(retries):
            try:
                async with session.get(url, params=params,
                                       headers={"User-Agent": UA},
                                       timeout=aiohttp_timeout(timeout)) as resp:
                    body = await resp.read()
                    if resp.status == 200:
                        data = json.loads(body.decode("utf-8", "replace"))
                        self._store(key, data)
                        return data
                    last = f"HTTP {resp.status}"
            except Exception as exc:  # noqa: BLE001
                last = type(exc).__name__
            if attempt < retries - 1:
                await asyncio.sleep(1.2 * (attempt + 1))
        raise RuntimeError(f"hf-mirror {last}")

    async def _text(self, path: str, timeout: int = 12) -> str:
        session = await client._get_session()
        try:
            async with session.get(f"{HF_BASE}{path}",
                                   headers={"User-Agent": UA},
                                   timeout=aiohttp_timeout(timeout)) as resp:
                if resp.status != 200:
                    return ""
                return (await resp.read()).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            return ""

    async def cover(self, repo_id: str) -> str:
        """First image referenced by the repo card, served through the mirror."""
        text = await self._text(f"/{repo_id}/raw/main/README.md", timeout=10)
        if not text:
            return ""
        match = _IMG_MD.search(text)
        if not match:
            return ""
        src = match.group(1).strip().strip('"').strip("'")
        if src.startswith(("http://", "https://")):
            if "huggingface.co" in src:
                src = src.replace("https://huggingface.co", HF_BASE)
                src = src.replace("https://cdn-lfs.huggingface.co", HF_BASE)
            return src
        src = src.lstrip("./")
        return f"{HF_BASE}/{repo_id}/resolve/main/{quote(src, safe='/')}"

    async def _covers(self, repo_ids: list[str], limit: int = 3) -> dict[str, str]:
        sem = asyncio.Semaphore(limit)

        async def one(rid: str) -> tuple[str, str]:
            async with sem:
                try:
                    return rid, await asyncio.wait_for(self.cover(rid), timeout=5)
                except Exception:  # noqa: BLE001
                    return rid, ""

        pairs = await asyncio.gather(*(one(r) for r in repo_ids))
        return {k: v for k, v in pairs if v}

    def slim(self, repo: dict[str, Any], cover: str = "") -> dict[str, Any]:
        repo_id = repo.get("id") or ""
        author, _ = _split_repo(repo_id)
        tags = [str(t) for t in (repo.get("tags") or []) if ":" not in str(t)][:12]
        images = []
        if cover:
            images.append({"url": cover, "nsfw": False, "width": None, "height": None})
        return {
            "site": "hf",
            "id": repo_id,
            "name": _repo_name(repo_id),
            "type": "LoRA" if _is_lora(repo) else "Model",
            "nsfw": False,
            "creator": {"username": author or "unknown"},
            "stats": {
                "downloadCount": repo.get("downloads") or 0,
                "thumbsUpCount": repo.get("likes") or 0,
            },
            "tags": tags,
            "summary": (repo.get("pipeline_tag") or "").replace("-", " "),
            "versions": [
                {
                    "id": "main",
                    "name": "main",
                    "baseModel": _base_model(repo),
                    "files": [],
                    "images": images,
                }
            ],
        }

    async def search(self, query: str, *, page: int = 1, limit: int = 24,
                     types: str = "All", **_ignored: Any) -> dict[str, Any]:
        # hf-mirror rejects `skip` with a 503 and ignores sort/filter, so paging
        # is done by widening `limit` and slicing locally.
        limit = max(1, min(int(limit), 100))
        page = max(1, int(page))
        params: dict[str, Any] = {"limit": min(100, limit * page)}
        if query:
            params["search"] = query
        repos = await self._json("/api/models", params)
        if not isinstance(repos, list):
            repos = []
        if types and types.upper() in ("LORA", "LOCON", "DORA"):
            repos = [r for r in repos if _is_lora(r)]
        start = (page - 1) * limit
        window = repos[start:start + limit]
        covers = await self._covers([r.get("id", "") for r in window[:16]])
        items = [self.slim(r, covers.get(r.get("id", ""), "")) for r in window]
        return {
            "items": items,
            "page": page,
            "total_pages": None,
            "total_items": None,
            "next_cursor": "",
            "has_more": len(repos) > start + limit,
        }

    async def detail(self, repo_id: str) -> dict[str, Any]:
        repo = await self._json(f"/api/models/{repo_id}")
        cover = await self.cover(repo_id)
        item = self.slim(repo, cover)

        tree = []
        try:
            tree = await self._json(f"/api/models/{repo_id}/tree/main")
        except Exception:  # noqa: BLE001
            tree = []
        files = []
        for entry in tree if isinstance(tree, list) else []:
            if entry.get("type") != "file":
                continue
            path = entry.get("path") or ""
            if not path.lower().endswith(_WEIGHT_EXT):
                continue
            files.append(
                {
                    "name": path,
                    "sizeKB": round((entry.get("size") or 0) / 1024, 1),
                    "type": "Model",
                    "primary": path.lower().endswith(".safetensors"),
                    "sha256": None,
                    "downloadUrl": f"{HF_BASE}/{repo_id}/resolve/main/{quote(path, safe='/')}",
                }
            )
        files.sort(key=lambda f: (not f["primary"], -(f.get("sizeKB") or 0)))
        item["versions"][0]["files"] = files

        readme = await self._text(f"/{repo_id}/raw/main/README.md", timeout=12)
        item["description"] = _readme_html(readme)
        item["summary"] = _strip_front_matter(readme)[:400] or item["summary"]
        item["stats"]["fileCount"] = len(files)
        return item


def aiohttp_timeout(seconds: int):
    import aiohttp

    return aiohttp.ClientTimeout(total=seconds, connect=10)


def _strip_front_matter(text: str) -> str:
    if not text:
        return ""
    if text.startswith("---"):
        parts = text.split("---", 2)
        text = parts[2] if len(parts) > 2 else text
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[#*`>_\[\]]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _readme_html(text: str) -> str:
    """Very small markdown-to-HTML pass, enough for a readable description."""
    if not text:
        return ""
    body = text
    if body.startswith("---"):
        parts = body.split("---", 2)
        body = parts[2] if len(parts) > 2 else body
    out: list[str] = []
    for raw_line in body.splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            continue
        img = _IMG_MD.match(line.strip())
        if img:
            src = img.group(1).strip()
            if src.startswith("http"):
                out.append(f'<p><img src="{_esc(src)}" alt=""></p>')
            continue
        low = line.strip()
        if low.startswith("#"):
            level = min(len(low) - len(low.lstrip("#")), 4) + 2
            out.append(f"<h{level}>{_esc(low.lstrip('#').strip())}</h{level}>")
            continue
        if low.startswith(("- ", "* ")):
            out.append(f"<p>· {_esc(low[2:])}</p>")
            continue
        out.append(f"<p>{_esc(low)}</p>")
    return "".join(out[:120])


def _esc(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


# --------------------------------------------------------------- signed sites
class SiteNotConfigured(RuntimeError):
    """Raised when a site needs credentials the user has not filled in yet."""


def _signed_headers(path: str, access_key: str, secret_key: str) -> dict[str, str]:
    """Liblib / Tusi open-platform signature.

    string_to_sign = f"{path}&{timestamp}&{nonce}"
    signature      = base64(hmac_sha256(string_to_sign, secret_key))
    """
    import base64
    import hashlib
    import hmac
    import secrets as _secrets
    import time as _time

    ts = str(int(_time.time()))
    nonce = _secrets.token_hex(8)
    payload = f"{path}&{ts}&{nonce}"
    digest = hmac.new(secret_key.encode(), payload.encode(), hashlib.sha256).digest()
    return {
        "AccessKey": access_key,
        "Signature": base64.b64encode(digest).decode(),
        "Timestamp": ts,
        "Nonce": nonce,
    }


_LIST_HINTS = ("list", "records", "rows", "items", "data", "models", "result")


def _dig_list(payload: Any) -> list[dict[str, Any]]:
    """Find the first array of objects anywhere in a JSON response."""
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if not isinstance(payload, dict):
        return []
    for hint in _LIST_HINTS:
        if hint in payload:
            found = _dig_list(payload[hint])
            if found:
                return found
    for value in payload.values():
        found = _dig_list(value)
        if found:
            return found
    return []


def _pick(mapping: dict[str, Any], *keys: str, default: Any = "") -> Any:
    for key in keys:
        if key in mapping and mapping[key] not in (None, ""):
            return mapping[key]
    return default


class SignedSiteProvider:
    """Base for Chinese sites that authenticate with AK/SK or a session cookie."""

    key = ""
    label = ""
    candidate_paths: tuple[str, ...] = ()

    def _creds(self) -> dict[str, Any]:
        from .hub_config import config

        return (config.accounts().get(self.key) or {})

    def configured(self) -> bool:
        creds = self._creds()
        return bool(creds.get("access_key") and creds.get("secret_key")) or bool(
            creds.get("cookie")
        )

    def _headers(self, path: str) -> dict[str, str]:
        creds = self._creds()
        headers = {"User-Agent": UA, "Accept": "application/json"}
        if creds.get("access_key") and creds.get("secret_key"):
            headers.update(_signed_headers(path, creds["access_key"], creds["secret_key"]))
        if creds.get("cookie"):
            headers["Cookie"] = creds["cookie"]
        return headers

    def _base(self) -> str:
        creds = self._creds()
        base = (creds.get("api_base") or "").strip().rstrip("/")
        return base or self.default_base

    default_base = ""

    async def _call(self, path: str, params: dict[str, Any] | None = None,
                    timeout: int = 20) -> Any:
        if not self.configured():
            raise SiteNotConfigured(f"{self.label} 尚未配置账号")
        session = await client._get_session()
        url = f"{self._base()}{path}"
        async with session.get(url, params=params, headers=self._headers(path),
                               timeout=aiohttp_timeout(timeout)) as resp:
            body = await resp.read()
            if resp.status == 401 or resp.status == 403:
                raise SiteNotConfigured(f"{self.label} 鉴权失败，检查 AK/SK 或 Cookie")
            if resp.status >= 400:
                raise RuntimeError(f"{self.label} HTTP {resp.status}")
            try:
                payload = json.loads(body.decode("utf-8", "replace"))
            except Exception:  # noqa: BLE001
                raise RuntimeError(f"{self.label} 返回非 JSON")
            # These gateways answer 200 with an auth code in the body.
            if isinstance(payload, dict):
                code = payload.get("code")
                msg = str(payload.get("msg") or payload.get("message") or "")
                if code in (401, 403, "401", "403") or any(
                    hint in msg for hint in ("签名", "鉴权", "登录", "未授权", "token")
                ):
                    raise SiteNotConfigured(
                        f"{self.label} 鉴权失败：{msg or 'AK/SK 或 Cookie 无效'}"
                    )
            return payload

    async def _working_path(self) -> str:
        cached = getattr(self, "_path_cache", None)
        if cached:
            return cached
        creds = self._creds()
        preferred = (creds.get("model_list_path") or "").strip()
        order = ([preferred] if preferred else []) + [
            p for p in self.candidate_paths if p != preferred
        ]
        last_error: Exception | None = None
        for path in order:
            try:
                payload = await self._call(path, {"page": 1, "pageSize": 3})
                if _dig_list(payload):
                    self._path_cache = path
                    return path
            except SiteNotConfigured:
                raise
            except Exception as exc:  # noqa: BLE001
                last_error = exc
        raise RuntimeError(f"{self.label} 接口不可用：{last_error}")

    def _slim(self, raw: dict[str, Any]) -> dict[str, Any]:
        name = str(_pick(raw, "name", "title", "modelName", default="untitled"))
        cover = str(_pick(raw, "coverUrl", "cover", "imageUrl", "image", "pic",
                          "previewUrl", "thumbnail", default=""))
        stats = raw.get("stats") if isinstance(raw.get("stats"), dict) else {}
        return {
            "site": self.key,
            "id": str(_pick(raw, "uuid", "id", "modelId", "versionId", default=name)),
            "name": name,
            "type": str(_pick(raw, "type", "modelType", "category", default="LoRA")),
            "nsfw": False,
            "creator": {
                "username": str(_pick(raw, "authorName", "username", "userName",
                                      "author", default="unknown")),
            },
            "stats": {
                "downloadCount": int(_pick(stats, "downloadCount", "downloads") or
                                     _pick(raw, "downloadCount", "downloads", default=0) or 0),
                "thumbsUpCount": int(_pick(stats, "likeCount", "likes") or
                                     _pick(raw, "likeCount", "likes", default=0) or 0),
            },
            "tags": [str(t) for t in (_pick(raw, "tags", default=[]) or [])][:10]
                    if isinstance(_pick(raw, "tags", default=[]), list) else [],
            "summary": str(_pick(raw, "description", "desc", "remark", default=""))[:300],
            "versions": [{
                "id": str(_pick(raw, "versionId", "uuid", "id", default=name)),
                "name": str(_pick(raw, "versionName", "version", default="default")),
                "baseModel": str(_pick(raw, "baseModel", "baseModelName", default="")),
                "files": [],
                "images": ([{"url": cover, "nsfw": False, "width": None, "height": None}]
                           if cover else []),
                "_raw": raw,
            }],
        }

    async def search(self, query: str, *, page: int = 1, limit: int = 24,
                     types: str = "All", **_ignored: Any) -> dict[str, Any]:
        path = await self._working_path()
        params = {"page": max(1, int(page)), "pageSize": max(1, min(int(limit), 100))}
        if query:
            params["keyword"] = query
            params["search"] = query
        payload = await self._call(path, params)
        rows = _dig_list(payload)
        items = [self._slim(r) for r in rows[: int(limit)]]
        return {
            "items": items,
            "page": page,
            "total_pages": None,
            "total_items": None,
            "next_cursor": "",
            "has_more": len(rows) >= int(limit),
        }

    async def detail(self, item_id: str) -> dict[str, Any]:
        path = await self._working_path()
        payload = await self._call(path, {"uuid": item_id, "id": item_id})
        rows = _dig_list(payload)
        for row in rows:
            if str(_pick(row, "uuid", "id", "modelId", default="")) == str(item_id):
                return self._slim(row)
        if rows:
            return self._slim(rows[0])
        raise RuntimeError(f"{self.label} 未找到该条目")


class LiblibProvider:
    """Liblib talks over api2.liblib.art with a session cookie.

    No request signature is involved: the web client sends only a `timestamp`
    query param plus the login cookie (`usertoken`), and the feed endpoint
    doubles as the search endpoint once `keyword` is set.
    """

    key = "liblib"
    label = "Liblib"
    BASE = "https://api2.liblib.art"
    FEED = "/api/www/model/feed/stream"

    def _creds(self) -> dict[str, Any]:
        from .hub_config import config

        return (config.accounts().get("liblib") or {})

    def configured(self) -> bool:
        return bool((self._creds().get("cookie") or "").strip())

    def _headers(self) -> dict[str, str]:
        return {
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Cookie": (self._creds().get("cookie") or "").strip(),
            "Origin": "https://www.liblib.art",
            "Referer": "https://www.liblib.art/",
        }

    @staticmethod
    def _body(query: str, page: int, limit: int) -> dict[str, Any]:
        import uuid as _uuid

        return {
            "isHome": False,
            "cid": f"{int(time.time() * 1000)}cvhub",
            "page": max(1, int(page)),
            "pageSize": max(1, min(int(limit), 50)),
            "requestId": str(_uuid.uuid4()),
            "sort": 0, "followed": 0, "liked": 0,
            "periodTime": ["all"], "tagIds": [], "models": [], "types": [],
            "vipType": [], "modelUsage": [], "modelLicense": [],
            "templateType": None,
            "keyword": query or "",
        }

    async def _post(self, path: str, body: dict[str, Any], timeout: int = 25) -> Any:
        if not self.configured():
            raise SiteNotConfigured("Liblib 尚未配置 Cookie")
        session = await client._get_session()
        url = f"{self.BASE}{path}?timestamp={int(time.time() * 1000)}"
        async with session.post(url, json=body, headers=self._headers(),
                                timeout=aiohttp_timeout(timeout)) as resp:
            raw = await resp.read()
        if resp.status in (401, 403):
            raise SiteNotConfigured("Liblib Cookie 已失效，请重新复制")
        if resp.status >= 400:
            raise RuntimeError(f"Liblib HTTP {resp.status}")
        try:
            data = json.loads(raw.decode("utf-8", "replace"))
        except Exception:  # noqa: BLE001
            raise RuntimeError("Liblib 返回非 JSON")
        if isinstance(data, dict) and data.get("code") not in (0, None):
            msg = str(data.get("msg") or "")
            if any(k in msg for k in ("登录", "token", "鉴权", "未授权")):
                raise SiteNotConfigured(f"Liblib 登录态无效：{msg}")
            raise RuntimeError(f"Liblib 接口错误：{msg or data.get('code')}")
        return data

    def _slim(self, row: dict[str, Any]) -> dict[str, Any]:
        img = row.get("imageUrl") or row.get("avatar") or ""
        words = []
        if row.get("triggerWord"):
            words = [str(row["triggerWord"])]
        return {
            "site": "liblib",
            "id": str(row.get("uuid") or row.get("id") or ""),
            "name": row.get("name") or "untitled",
            "type": row.get("modelTypeName") or "LoRA",
            "nsfw": False,
            "creator": {"username": row.get("nickname") or ""},
            "stats": {
                "downloadCount": row.get("downloadCount") or 0,
                "thumbsUpCount": row.get("likeCount") or 0,
                "commentCount": row.get("commentCount") or 0,
            },
            "tags": [t for t in (row.get("tagsLabel") or "").split(",") if t][:12],
            "summary": (row.get("description") or "")[:300],
            "versions": [{
                "id": row.get("versionId"),
                "name": row.get("versionName") or "",
                "baseModel": "",
                "files": [],
                "images": ([{"url": img, "nsfw": False,
                             "width": row.get("width"), "height": row.get("height")}]
                           if img else []),
                "trainedWords": words,
            }],
        }

    async def search(self, query: str, *, page: int = 1, limit: int = 24,
                     types: str = "All", **_ignored: Any) -> dict[str, Any]:
        data = await self._post(self.FEED, self._body(query, page, limit))
        payload = data.get("data") or {}
        rows = payload.get("data") or []
        items = [self._slim(r) for r in rows]
        if types and types != "All":
            want = types.lower().replace("lora", "lora")
            filtered = [i for i in items if want in str(i["type"]).lower()]
            if filtered:
                items = filtered
        return {
            "items": items,
            "page": page,
            "total_pages": None,
            "total_items": payload.get("total"),
            "next_cursor": "",
            "has_more": bool(payload.get("hasMore")),
        }

    async def detail(self, item_id: str) -> dict[str, Any]:
        data = await self._post(self.FEED, {
            **self._body("", 1, 20), "isHome": False,
        })
        rows = ((data.get("data") or {}).get("data")) or []
        for row in rows:
            if str(row.get("uuid")) == str(item_id) or str(row.get("id")) == str(item_id):
                return self._slim(row)
        raise RuntimeError("Liblib 未找到该条目")


class TusiProvider(SignedSiteProvider):
    key = "tusi"
    label = "吐司"
    default_base = "https://api.tusiart.cn"
    candidate_paths = (
        "/api/v1/model/list",
        "/api/v1/models",
        "/api/model/list",
    )


hf_provider = HuggingFaceProvider()
liblib_provider = LiblibProvider()
tusi_provider = TusiProvider()

PROVIDERS: dict[str, Any] = {
    "hf": hf_provider,
    "liblib": liblib_provider,
    "tusi": tusi_provider,
}

SITE_LABELS = {
    "civitai": "Civitai",
    "hf": "HuggingFace",
    "liblib": "Liblib",
    "tusi": "吐司",
}
