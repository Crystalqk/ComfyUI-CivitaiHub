"""Civitai access layer that works from mainland China without a VPN.

Three independent hops are stitched together:

1. Metadata  -> a REST mirror (``civitai.red``) that answers /api/v1/* for us.
2. Thumbnails -> a public image proxy (``wsrv.nl``) that can still reach
   ``image.civitai.com``.
3. Model files -> the signed URL returned by /api/download/models/{id} points
   at ``b2.civitai.com``, which is SNI-blocked. The same object is served by
   the Backblaze friendly endpoint ``fNNN.backblazeb2.com``, which is reachable,
   so we rewrite the host and keep the signature untouched.
"""

from __future__ import annotations

import asyncio
import hashlib
import random
import re
import socket
import time
from typing import Any, Iterable
from urllib.parse import quote, urlparse

import aiohttp

from .hub_config import THUMB_DIR, config

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Civitai model type -> ComfyUI folder_paths key.
TYPE_TO_FOLDER = {
    "LORA": "loras",
    "LoCon": "loras",
    "DoRA": "loras",
    "LoRA": "loras",
    "Checkpoint": "checkpoints",
    "TextualInversion": "embeddings",
    "VAE": "vae",
    "Controlnet": "controlnet",
    "ControlNet": "controlnet",
    "Upscaler": "upscale_models",
    "MotionModule": "animatediff_models",
    "Poses": "poses",
    "Wildcards": "wildcards",
    "Workflows": "workflows",
    # HuggingFace repos that are not tagged as adapters still land in loras.
    "Model": "loras",
}


class ChannelError(RuntimeError):    """Raised when every mirror in the pool failed."""


# A small, long-lived cover used to probe each image proxy. This constant was
# referenced in health() but never defined anywhere, so every probe died with
# NameError and the panel reported the image channel as down even when it worked.
_HEALTH_PROBE = ("https://image.civitai.com/xG1nkqKTMzGDvpLrqFT7WA/"
                 "e120c80a-be7b-4346-8e44-1e9c011670e7/original=true/148453948.jpeg")


class CivitaiClient:
    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None
        self._session_lock = asyncio.Lock()
        self._b2_cache: dict[str, str] = {}
        self._health: dict[str, Any] = {}
        self._bm_cache: list[dict[str, Any]] = []
        self._bm_ts: float = 0.0
        self._active_mirror: str = ""
        # Per-host health. A failed host is parked until its cooldown expires,
        # but the background probe keeps trying it: as soon as it answers again
        # it rejoins the pool as a normal candidate.
        self._mirror_bad_until: dict[str, float] = {}
        self._mirror_ok_at: dict[str, float] = {}
        self._mirror_cooldown: float = 45.0
        self._probe_task = None
        self._thumb_sem = None

    # ------------------------------------------------------------------ http
    async def _get_session(self) -> aiohttp.ClientSession:
        async with self._session_lock:
            if self._session is None or self._session.closed:
                # Family 0 = let the OS pick. Pinning AF_INET was wrong twice
                # over: on campus networks the IPv4 path to the image proxy is
                # blocked while IPv6 works fine, and on hosts whose AAAA records
                # are blackholed the reverse is true. Happy-eyeballs handles both.
                connector = aiohttp.TCPConnector(
                    family=socket.AF_UNSPEC,
                    ttl_dns_cache=300,
                    limit=64,
                    enable_cleanup_closed=True,
                )
                self._session = aiohttp.ClientSession(
                    connector=connector,
                    timeout=aiohttp.ClientTimeout(total=45, connect=15),
                    headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"},
                    trust_env=True,
                )
            return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    def _api_headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        # A Cookie reproduces the site's own view of a filter. The Bearer token
        # does not: measured against the same index and filter, it admitted extra
        # multi-base models (Mistoon_Anime under Anima) that the logged-in website
        # never lists. So the token is used for downloads only, never for search.
        cookie = (config.get("civitai_cookie") or "").strip()
        if cookie:
            headers["Cookie"] = cookie
        return headers

    def _download_headers(self) -> dict[str, str]:
        headers = {"Accept": "*/*"}
        key = (config.get("civitai_api_key") or "").strip()
        if key:
            headers["Authorization"] = f"Bearer {key}"
        cookie = (config.get("civitai_cookie") or "").strip()
        if cookie:
            headers["Cookie"] = cookie
        return headers

    @property
    def mirrors(self) -> list[str]:
        vals = config.get("api_mirrors") or []
        return [m.strip().rstrip("/") for m in vals if str(m).strip()]

    def _mirror_base(self, host: str) -> str:
        return host if host.startswith("http") else f"https://{host}"

    def _proxy(self) -> str | None:
        value = (config.get("http_proxy") or "").strip()
        return value or None

    async def probe_loop(self, interval: float = 60.0) -> None:
        """Keep every mirror's status current, and let recovered ones back in.

        Runs for the life of the process: hosts that failed are retried here even
        while parked, so a mirror that comes back is a candidate again instead of
        being written off for the session.
        """
        while True:
            try:
                await asyncio.sleep(interval)
                session = await self._get_session()
                for host in self.mirrors:
                    url = f"{self._mirror_base(host)}/api/v1/models?limit=1"
                    try:
                        async with session.get(
                            url, headers=self._api_headers(), proxy=self._proxy(),
                            timeout=aiohttp.ClientTimeout(total=8, connect=4),
                        ) as resp:
                            if resp.status == 200:
                                self._mirror_ok_at[host] = time.time()
                                self._mirror_bad_until.pop(host, None)
                            else:
                                self._mirror_bad_until[host] = time.time() + self._mirror_cooldown
                    except Exception:  # noqa: BLE001
                        self._mirror_bad_until[host] = time.time() + self._mirror_cooldown
            except asyncio.CancelledError:
                return
            except Exception:  # noqa: BLE001 - probing must never crash
                continue

    def start_probe(self, interval: float = 60.0) -> None:
        if self._probe_task is not None:
            return
        try:
            self._probe_task = asyncio.create_task(self.probe_loop(interval))
        except RuntimeError:
            self._probe_task = None

    async def api(self, path: str, params: dict[str, Any] | None = None,
                  timeout: int = 10) -> Any:
        """GET a Civitai REST path, racing the mirror pool.

        Every candidate is tried concurrently and the first 200 wins, so a dead
        or slow mirror costs nothing — it never delays the working one.
        """
        if not path.startswith("/"):
            path = "/" + path
        session = await self._get_session()
        hosts = list(self.mirrors)
        if self._active_mirror in hosts:
            hosts.remove(self._active_mirror)
            hosts.insert(0, self._active_mirror)
        now = time.time()
        hosts.sort(key=lambda h: (1 if self._mirror_bad_until.get(h, 0.0) > now else 0,
                                  -self._mirror_ok_at.get(h, 0.0)))
        if not hosts:
            raise ChannelError("no api mirror configured")

        errors: list[str] = []

        async def attempt(host: str) -> Any:
            url = f"{self._mirror_base(host)}{path}"
            async with session.get(
                url, params=params, headers=self._api_headers(),
                proxy=self._proxy(),
                timeout=aiohttp.ClientTimeout(total=timeout, connect=4),
            ) as resp:
                body = await resp.read()
                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status}")
                return host, _loads(body)

        tasks = {asyncio.create_task(attempt(h)): h for h in hosts}
        try:
            pending = set(tasks)
            while pending:
                done, pending = await asyncio.wait(
                    pending, return_when=asyncio.FIRST_COMPLETED,
                )
                for task in done:
                    host = tasks[task]
                    try:
                        ok_host, data = task.result()
                    except Exception as exc:  # noqa: BLE001
                        self._mirror_bad_until[host] = time.time() + self._mirror_cooldown
                        errors.append(f"{host} -> {type(exc).__name__}: {exc}")
                        continue
                    self._active_mirror = ok_host
                    self._mirror_ok_at[ok_host] = time.time()
                    self._mirror_bad_until.pop(ok_host, None)
                    return data
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
        raise ChannelError("; ".join(errors) or "no api mirror answered")

    # ------------------------------------------------------------- endpoints
    async def search(
        self,
        query: str = "",
        *,
        types: str = "LORA",
        sort: str = "Most Downloaded",
        period: str = "AllTime",
        page: int = 1,
        limit: int = 24,
        nsfw: bool | None = None,
        base_models: Iterable[str] | None = None,
        username: str = "",
        tag: str = "",
        cursor: str = "",
        sort_by: str = "",
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "limit": max(1, min(int(limit), 100)),
        }
        # Civitai rejects `page` together with a text query: full-text search is
        # cursor based only. Everything else still pages numerically.
        if query:
            params["query"] = query
            if cursor:
                params["cursor"] = cursor
        else:
            params["page"] = max(1, int(page))
            if cursor:
                params["cursor"] = cursor
        # An empty sort means "relevancy", which is what the website uses by
        # default. Sending our own guess is what made the first screen differ.
        if sort:
            params["sort"] = sort
            if period:
                params["period"] = period
        elif sort_by:
            # `sortBy` is the token the website's own URL carries (models_v9).
            params["sortBy"] = sort_by
        if types and types != "All":
            params["types"] = types
        if username:
            params["username"] = username
        if tag:
            params["tag"] = tag
        if base_models:
            params["baseModels"] = ",".join(base_models)
        want_nsfw = config.get("nsfw") if nsfw is None else nsfw
        params["nsfw"] = "true" if want_nsfw else "false"
        return await self.api("/api/v1/models", params)

    async def model(self, model_id: int) -> dict[str, Any]:
        return await self.api(f"/api/v1/models/{int(model_id)}")

    # ------------------------------------------------------- self-made ranking
    # The site's ordering is not exposed through any v1 parameter, so it is
    # reproduced here: results are pulled from the v1 API (which does include
    # adult models) and then re-sorted with the three rules observed on the
    # live site — official accounts first, then name-match quality, then likes.
    OFFICIAL_USERS = {"civitai", "civitaiofficial"}

    async def search_ranked(
        self,
        query: str = "",
        *,
        page: int = 1,
        limit: int = 24,
        nsfw: bool = True,
        types: str = "All",
        base_models: Iterable[str] | None = None,
        pool_pages: int = 3,
    ) -> dict[str, Any]:
        pool: list[dict[str, Any]] = []
        seen: set[Any] = set()
        for p in range(1, max(1, pool_pages) + 1):
            try:
                raw = await self.search(
                    query, types=types, sort="", page=p, limit=100,
                    nsfw=nsfw, base_models=base_models,
                )
            except Exception:  # noqa: BLE001
                break
            chunk = raw.get("items") or []
            for item in chunk:
                if item.get("id") in seen:
                    continue
                seen.add(item.get("id"))
                pool.append(item)
            if len(chunk) < 100:
                break

        needle = (query or "").strip().lower()

        def rank(item: dict[str, Any]) -> tuple:
            name = (item.get("name") or "").lower()
            creator = ((item.get("creator") or {}).get("username") or "").lower()
            stats = item.get("stats") or {}
            official = 1 if creator in self.OFFICIAL_USERS else 0
            likes = stats.get("thumbsUpCount") or 0
            if not needle:
                # Empty query: the 88 official models are pinned on top, then
                # everything by likes (verified against 400 live samples).
                return (-official, -likes)
            # With a search term the site ranks by match quality first and the
            # official pin no longer applies (searching "portrait" surfaces
            # low-like community models above official ones).
            if name == needle:
                match = 4
            elif name.startswith(needle):
                match = 3
            elif needle in name:
                match = 2
            elif any(needle in str(t).lower() for t in (item.get("tags") or [])):
                match = 1
            else:
                match = 0
            return (-match, -official, -likes)

        pool.sort(key=rank)
        start = max(0, (max(1, page) - 1) * limit)
        window = pool[start:start + limit]
        return {
            "items": window,
            "total": len(pool),
            "has_more": start + limit < len(pool),
        }

    # ---------------------------------------------------------- site ordering
    # The website does not use /api/v1/models for browsing. It POSTs to a
    # Meilisearch proxy at /api/search/multi-search with indexUid=models_v9 and
    # an explicit nsfwLevel filter. Reproducing that request is the only way to
    # get the same first page the site shows.
    MEILI_PATH = "/api/search/multi-search"
    IMG_BASE = "https://image.civitai.com/xG1nkqKTMzGDvpLrqFT7WA"
    MEILI_FACETS = [
        "category.name", "checkpointType", "fileFormats", "lastVersionAtUnix",
        "tags.name", "type", "user.username", "versions.baseModel",
    ]

    async def search_meili(
        self,
        query: str = "",
        *,
        index: str = "models_v9",
        limit: int = 24,
        offset: int = 0,
        nsfw: bool | None = None,
        types: str = "All",
        base_models: Iterable[str] | None = None,
        sort_field: str = "",
    ) -> dict[str, Any]:
        want_nsfw = config.get("nsfw") if nsfw is None else nsfw
        clauses = ["(poi != true)", "(availability != Private)"]        # Deliberately no nsfwLevel clause: filtering here changes the result
        # set and therefore the ordering, which must stay identical to what the
        # site shows. Whether adult models appear is decided by the server
        # according to the caller's credentials, not by a client-side filter.
        if types and types != "All":
            # Same escaping as base_models below. This was interpolated raw, so a
            # crafted types value could rewrite the filter expression.
            safe_type = str(types).replace("'", "")
            clauses.append(f"(type = '{safe_type}')")
        if base_models:
            names = ",".join("'%s'" % str(b).replace("'", "") for b in base_models)
            clauses.append(f"(versions.baseModel IN [{names}])")
        page_size = max(1, min(int(limit), 1000))
        offset = max(0, int(offset))
        payload = {
            "queries": [{
                "q": query or "",
                "indexUid": index,
                "attributesToHighlight": [],
                "highlightPreTag": "__ais-highlight__",
                "highlightPostTag": "__/ais-highlight__",
                # hitsPerPage/page rather than limit/offset: measured against the
                # live site, this form returns the same result set the website
                # shows (Vixon first, no Mistoon_Anime), while limit/offset leaks
                # extra rows the site never lists for the same filter.
                "hitsPerPage": page_size,
                "page": offset // page_size + 1,
                "filter": [" AND ".join(clauses)],
            }]
        }
        if sort_field:
            payload["queries"][0]["sort"] = [sort_field]

        session = await self._get_session()
        errors: list[str] = []
        hosts = list(self.mirrors)
        if self._active_mirror in hosts:
            hosts.remove(self._active_mirror)
            hosts.insert(0, self._active_mirror)
        for host in hosts:
            url = f"{self._mirror_base(host)}{self.MEILI_PATH}"
            try:
                async with session.post(
                    url, json=payload, headers=self._api_headers(),
                    proxy=self._proxy(),
                    timeout=aiohttp.ClientTimeout(total=20, connect=6),
                ) as resp:
                    body = await resp.read()
                    if resp.status != 200:
                        errors.append(f"{host} -> HTTP {resp.status}")
                        continue
                    self._active_mirror = host
                    data = _loads(body)
                    result = (data.get("results") or [{}])[0]
                    hits = result.get("hits") or []
                    return {
                        "items": [self._meili_item(h) for h in hits],
                        "total": result.get("estimatedTotalHits") or 0,
                        "offset": offset,
                        "limit": limit,
                        "facets": result.get("facetDistribution") or {},
                    }
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{host} -> {type(exc).__name__}")
        raise ChannelError("; ".join(errors) or "multi-search failed")

    def _meili_item(self, hit: dict[str, Any]) -> dict[str, Any]:
        version = hit.get("version") or {}
        metrics = hit.get("metrics") or {}
        user = hit.get("user") or {}
        images = []
        for img in (hit.get("images") or [])[:12]:
            uuid = img.get("url")
            name = img.get("name")
            if not uuid or not name:
                continue
            images.append({
                "url": f"{self.IMG_BASE}/{uuid}/width=450/{name}",
                "nsfw": int(img.get("nsfwLevel") or 1) > 1,
                "width": img.get("width"),
                "height": img.get("height"),
            })
        tags = hit.get("tags") or []
        if tags and isinstance(tags[0], dict):
            tags = [str(t.get("name", "")) for t in tags if t.get("name")]
        return {
            "id": hit.get("id"),
            "name": hit.get("name") or "",
            "type": hit.get("type") or "LORA",
            "nsfw": bool(hit.get("nsfw")),
            "creator": {"username": user.get("username") or "unknown"},
            "stats": {
                "downloadCount": metrics.get("downloadCount") or 0,
                "thumbsUpCount": metrics.get("thumbsUpCount") or 0,
                "commentCount": metrics.get("commentCount") or 0,
                "collectedCount": metrics.get("collectedCount") or 0,
            },
            "tags": [str(t) for t in tags][:12],
            "summary": "",
            "base_model": version.get("baseModel") or "",
            "versions": [{
                "id": version.get("id"),
                "name": version.get("name") or "",
                "baseModel": version.get("baseModel") or "",
                "files": [],
                "images": images,
            }],
        }

    async def version_by_hash(self, sha256: str) -> dict[str, Any]:
        return await self.api(f"/api/v1/model-versions/by-hash/{sha256}")

    # -------------------------------------------------------------- download
    async def resolve_file(self, version_id: int) -> dict[str, Any]:
        """Resolve a model version id into a directly fetchable URL.

        The mirror answers 307 with a signed ``b2.civitai.com`` location. That
        host is blocked, so the same path+signature is replayed against the
        Backblaze friendly endpoint instead.
        """
        session = await self._get_session()
        errors: list[str] = []
        hosts = list(self.mirrors)
        if self._active_mirror in hosts:
            hosts.remove(self._active_mirror)
            hosts.insert(0, self._active_mirror)
        for host in hosts:
            url = f"{self._mirror_base(host)}/api/download/models/{int(version_id)}"
            try:
                async with session.get(
                    url,
                    headers=self._download_headers(),
                    allow_redirects=False,
                    proxy=self._proxy(),
                    timeout=aiohttp.ClientTimeout(total=20, connect=5),
                ) as resp:
                    location = resp.headers.get("Location")
                    if resp.status not in (301, 302, 303, 307, 308) or not location:
                        if resp.status == 200:
                            self._active_mirror = host
                            # Mirror streams the bytes itself; use it verbatim.
                            return {"url": url, "rewritten": False,
                                    "host": urlparse(url).hostname}
                        errors.append(f"{host} -> HTTP {resp.status}")
                        continue
                    self._active_mirror = host
                    return await self._to_direct(location)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{host} -> {type(exc).__name__}")
        raise ChannelError("; ".join(errors) or "download resolve failed")

    async def _to_direct(self, location: str) -> dict[str, Any]:
        parsed = urlparse(location)
        host = (parsed.hostname or "").lower()
        path_q = parsed.path + (f"?{parsed.query}" if parsed.query else "")

        # b2.civitai.com is SNI-blocked; the same object is served by the
        # Backblaze friendly endpoint. This rewrite is unconditional because the
        # signed query must be replayed verbatim, and a path check used to let
        # some URLs through untouched — which then timed out mid-download.
        if host.endswith("backblazeb2.com"):
            return {"url": location, "rewritten": False, "host": host}

        if host.endswith("civitai.com") and "/file/" in parsed.path:
            endpoint = await self._pick_b2(parsed.path, parsed.query, host)
            direct = f"https://{endpoint}{path_q}"
            return {"url": direct, "rewritten": True, "host": endpoint,
                    "original_host": host}

        if host.startswith("b2.") or host == "b2.civitai.com":
            # Anything on the blocked b2 host: move it to the friendly endpoint.
            endpoint = await self._pick_b2(parsed.path, parsed.query, host)
            direct = f"https://{endpoint}{path_q}"
            return {"url": direct, "rewritten": True, "host": endpoint,
                    "original_host": host}

        # Unknown storage backend, hand it back unchanged.
        return {"url": location, "rewritten": False, "host": host}

    async def _pick_b2(self, path: str, query: str, blocked_host: str) -> str:
        cached = self._b2_cache.get(blocked_host)
        if cached:
            return cached
        candidates = [e.strip() for e in (config.get("b2_endpoints") or []) if e.strip()]
        if not candidates:
            candidates = ["f004.backblazeb2.com"]
        session = await self._get_session()
        for endpoint in candidates:
            url = f"https://{endpoint}{path}?{query}" if query else f"https://{endpoint}{path}"
            try:
                async with session.get(
                    url, headers={"Range": "bytes=0-0", "Accept": "*/*"}
                ) as resp:
                    # 401 bad_auth_token still proves the bucket lives here.
                    if resp.status in (200, 206, 401, 403):
                        self._b2_cache[blocked_host] = endpoint
                        return endpoint
            except Exception:  # noqa: BLE001
                continue
        self._b2_cache[blocked_host] = candidates[0]
        return candidates[0]

    # ----------------------------------------------------------------- image
    def thumb_url(self, origin: str, width: int | None = None) -> str:
        if not origin:
            return ""
        w = int(width or config.get("thumb_width") or 450)
        proxies = [p.strip() for p in (config.get("image_proxies") or []) if p.strip()]
        if not proxies:
            return origin
        host = proxies[_stable_index(origin, len(proxies))]
        return self.thumb_url_with(host, upgrade_origin(origin, w * 2), w)

    async def fetch_thumb(self, origin: str, width: int | None = None,
                          kind: str = "temp") -> tuple[bytes, str] | None:
        """Download a thumbnail through the proxy pool, with a disk cache."""
        if not origin:
            return None
        w = int(width or config.get("thumb_width") or 450)
        source = upgrade_origin(origin, w * 2)
        # Key on the ORIGINAL url, not the possibly-rewritten source: the rewrite
        # changed once and invalidated every cached file, making the grid look
        # like it never cached anything.
        key = hashlib.sha1(f"{origin}|{w}".encode()).hexdigest()
        cached = THUMB_DIR / f"{key}.bin"
        # Tag each file so the cache can be pruned by kind: "core" entries are
        # the always-visible hot covers and survive filter changes, "temp" ones
        # belong to whatever filter is on screen right now.
        kind_file = THUMB_DIR / f"{key}.kind"
        if cached.exists() and cached.stat().st_size > 512:
            meta = THUMB_DIR / f"{key}.ct"
            ctype = meta.read_text(encoding="utf-8") if meta.exists() else "image/jpeg"
            return cached.read_bytes(), ctype

        session = await self._get_session()
        proxies = [p.strip() for p in (config.get("image_proxies") or []) if p.strip()]
        # Cap concurrent proxy fetches. The earlier "6 concurrent stalls the
        # proxy" reading was taken while a 16-request prefetch side channel was
        # also running, so it measured 22 connections, not 6. Six is the browser's
        # own per-host limit and the measured rate at six is sub-second.
        if self._thumb_sem is None:
            self._thumb_sem = asyncio.Semaphore(6)
        async with self._thumb_sem:
            for attempt in range(2):
                for host in proxies:
                    url = self.thumb_url_with(host, source, w)
                    try:
                        async with session.get(
                            url, timeout=aiohttp.ClientTimeout(total=15, connect=5),
                        ) as resp:
                            if resp.status != 200:
                                continue
                            data = await resp.read()
                            if len(data) < 512:
                                continue
                            ctype = resp.headers.get("Content-Type", "image/jpeg")
                            try:
                                cached.write_bytes(data)
                                (THUMB_DIR / f"{key}.ct").write_text(ctype, encoding="utf-8")
                                if not kind_file.exists():
                                    kind_file.write_text(kind, encoding="utf-8")
                            except Exception:  # noqa: BLE001 - cache is best effort
                                pass
                            return data, ctype
                    except Exception:  # noqa: BLE001
                        continue
                if attempt == 0:
                    # Second pass with no downscale in case the resize is what failed.
                    w = min(w, 320)
                    source = upgrade_origin(origin, w)
        return None

    def thumb_url_with(self, proxy_host: str, origin: str, width: int | None = None) -> str:
        w = int(width or config.get("thumb_width") or 450)
        # q=92 made wsrv transcode for 3s+ per image; cards render around 300px
        # wide, so 78 is visually identical and much faster to produce.
        return f"https://{proxy_host}/?url={quote(origin, safe='')}&w={w}&output=jpg&q=78&we"

    async def fetch_thumb_range(self, origin: str, width: int,
                                segments: int = 8) -> tuple[bytes, str] | None:
        """Fetch one image as concurrent byte ranges.

        Large screenshots (detail-page hero, full-size previews) are the same
        bottleneck as model files: wsrv serialises a single request, so splitting
        it into ranges is the only way to go faster. Small images stay on the
        plain path because the handshake costs more than the transfer.
        """
        session = await self._get_session()
        proxies = [p.strip() for p in (config.get("image_proxies") or []) if p.strip()]
        if not proxies:
            return await self.fetch_thumb(origin, width, "temp")
        if self._thumb_sem is None:
            self._thumb_sem = asyncio.Semaphore(6)

        # Serve from disk first. The detail page keeps its previews for 15s after
        # being closed, so reopening inside that window must not re-download.
        cache_key = hashlib.sha1(f"{origin}|{int(width)}".encode()).hexdigest()
        cached_file = THUMB_DIR / f"{cache_key}.bin"
        if cached_file.exists() and cached_file.stat().st_size > 512:
            try:
                meta = THUMB_DIR / f"{cache_key}.ct"
                ctype = meta.read_text(encoding="utf-8").strip() if meta.exists() else "image/jpeg"
                return cached_file.read_bytes(), ctype
            except Exception:  # noqa: BLE001
                pass

        async with self._thumb_sem:
            for host in proxies:
                url = self.thumb_url_with(host, origin, width)
                try:
                    async with session.get(
                        url, headers={"Range": "bytes=0-0"},
                        timeout=aiohttp.ClientTimeout(total=25, connect=6),
                    ) as probe:
                        if probe.status not in (200, 206):
                            continue
                        rng = probe.headers.get("Content-Range") or ""
                        total = 0
                        if "/" in rng:
                            try:
                                total = int(rng.rsplit("/", 1)[1])
                            except ValueError:
                                total = 0
                        if total < 240 * 1024:
                            # Small enough that one request is faster.
                            async with session.get(
                                url, timeout=aiohttp.ClientTimeout(total=25, connect=6),
                            ) as r:
                                if r.status != 200:
                                    continue
                                data = await r.read()
                                if len(data) > 512:
                                    return data, r.headers.get("Content-Type", "image/jpeg")
                            continue
                    n_seg = min(int(segments), max(2, total // (96 * 1024)))
                    chunk = total // n_seg
                    bounds = [(i * chunk, total - 1 if i == n_seg - 1 else (i + 1) * chunk - 1)
                              for i in range(n_seg)]
                    buf = bytearray(total)

                    async def grab(s: int, e: int) -> None:
                        async with session.get(
                            url, headers={"Range": f"bytes={s}-{e}"},
                            timeout=aiohttp.ClientTimeout(total=30, connect=6),
                        ) as r:
                            if r.status not in (200, 206):
                                return
                            pos = s
                            async for piece in r.content.iter_chunked(65536):
                                buf[pos:pos + len(piece)] = piece
                                pos += len(piece)

                    await asyncio.gather(*(grab(s, e) for s, e in bounds))
                    data = bytes(buf)
                    if len(data) > 512:
                        try:
                            cached_file.write_bytes(data)
                            (THUMB_DIR / f"{cache_key}.ct").write_text("image/jpeg", encoding="utf-8")
                            kind_file = THUMB_DIR / f"{cache_key}.kind"
                            if not kind_file.exists():
                                kind_file.write_text("temp", encoding="utf-8")
                        except Exception:  # noqa: BLE001
                            pass
                        return data, "image/jpeg"
                except Exception:  # noqa: BLE001
                    continue
        return await self.fetch_thumb(origin, width, "temp")

    async def base_models(self, refresh: bool = False) -> list[dict[str, Any]]:
        """Poll the live feed and count every baseModel actually in use.

        The website's filter list is baked into its bundle and goes stale fast
        (anima, moss and friends keep appearing). Counting what the API returns
        keeps the dropdown current without shipping a hardcoded list.
        """
        now = time.time()
        if not refresh and self._bm_cache and now - self._bm_ts < 3600:
            return self._bm_cache
        probes = [
            {"types": "LORA", "sort": "Most Downloaded", "period": "Month", "limit": 100},
            {"types": "LORA", "sort": "Newest", "period": "Month", "limit": 100},
            {"types": "Checkpoint", "sort": "Most Downloaded", "period": "AllTime", "limit": 100},
            {"types": "LORA", "sort": "Highest Rated", "period": "Year", "limit": 100},
            # A brand new base model ships as Checkpoints first, and those have no
            # download history yet, so the AllTime probe above cannot see them.
            # Newest is the only window that catches a base model on day one.
            {"types": "Checkpoint", "sort": "Newest", "period": "Month", "limit": 100},
            {"types": "Checkpoint", "sort": "Highest Rated", "period": "Month", "limit": 100},
        ]
        counts: dict[str, int] = {}
        for probe in probes:
            try:
                data = await self.api("/api/v1/models", {**probe, "nsfw": "true"})
            except Exception:  # noqa: BLE001 - one bad probe must not kill the list
                continue
            for model in data.get("items") or []:
                seen = set()
                for ver in (model.get("modelVersions") or [])[:3]:
                    base = (ver.get("baseModel") or "").strip()
                    if base and base not in seen:
                        seen.add(base)
                        counts[base] = counts.get(base, 0) + 1
        result = [
            {"name": name, "count": count}
            for name, count in sorted(counts.items(), key=lambda kv: -kv[1])
        ]
        if result:
            self._bm_cache = result
            self._bm_ts = now
        return result

    def base_models_cached(self) -> list[dict[str, Any]]:
        """The last probe result, without paying for a new one.

        base_models() costs six API round trips, measured 4.4s cold. The panel
        asks for this list while opening, so waiting on the probes delayed the
        dropdown for seconds. Callers read the cache and warm it in the
        background instead.
        """
        return list(self._bm_cache or [])

    # ---------------------------------------------------------------- health
    async def health(self) -> dict[str, Any]:
        """Probe every channel once, concurrently.

        Sequentially this took ~32s (four probes × up to 15s); running them at
        the same time with a shorter cap keeps the button responsive.
        """
        results: dict[str, Any] = {"mirrors": {}, "images": {}, "b2": {}}
        session = await self._get_session()

        async def probe_mirror(host: str) -> None:
            t0 = time.time()
            try:
                async with session.get(
                    f"{self._mirror_base(host)}/api/v1/models?limit=1",
                    headers=self._api_headers(),
                    timeout=aiohttp.ClientTimeout(total=6),
                ) as resp:
                    results["mirrors"][host] = {
                        "ok": resp.status == 200,
                        "status": resp.status,
                        "ms": int((time.time() - t0) * 1000),
                    }
            except Exception as exc:  # noqa: BLE001
                results["mirrors"][host] = {"ok": False, "error": type(exc).__name__}

        async def probe_image(host: str) -> None:
            t0 = time.time()
            try:
                async with session.get(
                    self.thumb_url_with(host, _HEALTH_PROBE, 64),
                    timeout=aiohttp.ClientTimeout(total=6),
                ) as resp:
                    results["images"][host] = {
                        "ok": resp.status == 200,
                        "status": resp.status,
                        "ms": int((time.time() - t0) * 1000),
                    }
            except Exception as exc:  # noqa: BLE001
                results["images"][host] = {"ok": False, "error": type(exc).__name__}

        await asyncio.gather(
            *(probe_mirror(h) for h in self.mirrors),
            *(probe_image(h) for h in (config.get("image_proxies") or [])),
            return_exceptions=True,
        )

        results["summary"] = {
            "mirror_ok": any(v.get("ok") for v in results["mirrors"].values()),
            "image_ok": any(v.get("ok") for v in results["images"].values()),
        }
        self._health = results
        return results

    @property
    def last_health(self) -> dict[str, Any]:
        return self._health


_WIDTH_TOKEN = re.compile(r"/(width|original)=([^/]+)/")


def upgrade_origin(url: str, target: int) -> str:
    """Return the URL unchanged.

    This used to rewrite ``width=450`` to ``width=800`` to get a sharper source.
    But the CDN does not materialise every width on demand — for a fair number of
    older uploads the rewritten path simply 404s, while the original width works
    fine. Sharpness is not worth losing the image, so the original is used and
    the proxy handles scaling.
    """
    return url


def _loads(body: bytes) -> Any:
    import json

    return json.loads(body.decode("utf-8", "replace"))


def _stable_index(seed: str, size: int) -> int:
    if size <= 1:
        return 0
    return int(hashlib.sha1(seed.encode()).hexdigest(), 16) % size


def safe_filename(name: str, fallback: str = "model.safetensors") -> str:
    name = (name or "").strip()
    if not name:
        name = fallback
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    name = name.strip(" .")
    return name or fallback


def pick_folder_key(model_type: str | None) -> str:
    return TYPE_TO_FOLDER.get(model_type or "", config.get("fallback_folder") or "loras")


client = CivitaiClient()
