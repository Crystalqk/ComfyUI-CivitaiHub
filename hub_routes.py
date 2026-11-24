"""aiohttp routes backing the Civitai Hub panel."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from pathlib import Path
from datetime import datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import quote

from aiohttp import web

from .hub_client import ChannelError, client, pick_folder_key, safe_filename
from .hub_config import THUMB_DIR, config
from .hub_downloader import manager, resolve_target_dir

ALLOWED_TAGS = {
    "p", "br", "b", "strong", "i", "em", "u", "s", "ul", "ol", "li", "hr",
    "h1", "h2", "h3", "h4", "h5", "h6", "a", "blockquote", "code", "pre",
    "span", "div", "table", "thead", "tbody", "tr", "td", "th", "img",
}


class _Sanitizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self._depth: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in ALLOWED_TAGS:
            return
        kept = []
        for key, value in attrs:
            if not value:
                continue
            if key in ("href", "src"):
                if value.startswith(("http://", "https://", "/")):
                    kept.append(f'{key}="{_escape(value)}"')
            elif key in ("alt", "title"):
                kept.append(f'{key}="{_escape(value)}"')
        if tag in ("img", "br", "hr"):
            self.out.append(f"<{tag} {' '.join(kept)}>" if kept else f"<{tag}>")
            return
        self._depth.append(tag)
        self.out.append(f"<{tag} {' '.join(kept)}>" if kept else f"<{tag}>")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._depth:
            while self._depth:
                open_tag = self._depth.pop()
                self.out.append(f"</{open_tag}>")
                if open_tag == tag:
                    break

    def handle_data(self, data: str) -> None:
        self.out.append(_escape(data))


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


_TAG_RE = re.compile(r"<[^>]+>")


def sanitize_html(raw: str) -> str:
    if not raw:
        return ""
    parser = _Sanitizer()
    try:
        parser.feed(raw)
        parser.close()
    except Exception:  # noqa: BLE001
        return ""
    return "".join(parser.out)


def plain_text(raw: str, limit: int = 400) -> str:
    text = _TAG_RE.sub(" ", raw or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def slim_model(model: dict[str, Any], *, full: bool = False) -> dict[str, Any]:
    versions = []
    for ver in (model.get("modelVersions") or [])[:50]:
        images = []
        for img in (ver.get("images") or []):
            url = img.get("url") or ""
            if not url:
                continue
            # Same rule as _slim_snapshot: the plugin cannot render a clip, and
            # slicing the raw list first meant a version whose leading entries are
            # all clips produced an empty gallery in the detail pane while the
            # grid (built from the snapshot) still showed its stills. Babes is
            # exactly that case: 20 entries, the first 12 all .mp4.
            low = url.lower().split("?")[0]
            if low.endswith((".mp4", ".webm", ".mov")):
                continue
            images.append(
                {
                    "url": url,
                    # Level 1-2 is PG/PG-13; only R and above counts as explicit,
                    # matching the snapshot's own threshold.
                    "nsfw": int(img.get("nsfwLevel") or 0) >= 4,
                    "width": img.get("width"),
                    "height": img.get("height"),
                    "id": img.get("id"),
                    "hasMeta": bool(img.get("hasMeta")),
                    "hash": img.get("hash"),
                }
            )
            if len(images) >= 12:
                break
        files = []
        for f in ver.get("files") or []:
            if (f.get("type") or "Model") not in ("Model", "Archive"):
                continue
            files.append(
                {
                    "name": f.get("name"),
                    "sizeKB": f.get("sizeKB"),
                    "type": f.get("type"),
                    "primary": f.get("primary"),
                    "sha256": (f.get("hashes") or {}).get("SHA256"),
                    "downloadUrl": f.get("downloadUrl"),
                }
            )
        files.sort(key=lambda f: (not f.get("primary"), -(f.get("sizeKB") or 0)))
        versions.append(
            {
                "id": ver.get("id"),
                "name": ver.get("name"),
                "baseModel": ver.get("baseModel"),
                "baseModelType": ver.get("baseModelType"),
                "publishedAt": ver.get("publishedAt"),
                "createdAt": ver.get("createdAt"),
                "earlyAccessEndsAt": ver.get("earlyAccessEndsAt"),
                "availability": ver.get("availability"),
                "stats": ver.get("stats"),
                # Trainers often put the trigger words only here, not in the
                # description, so this must be surfaced separately.
                "trainedWords": [w for w in (ver.get("trainedWords") or []) if w],
                "images": images,
                "files": files,
            }
        )

    creator = model.get("creator") or {}
    data: dict[str, Any] = {
        "id": model.get("id"),
        "name": model.get("name"),
        "type": model.get("type"),
        "nsfw": model.get("nsfw"),
        "creator": {
            "username": creator.get("username"),
            "image": creator.get("image"),
        },
        "stats": model.get("stats"),
        "tags": (model.get("tags") or [])[:24],
        "baseModels": model.get("baseModels") or [],
        "versions": versions,
    }
    desc = model.get("description") or ""
    data["summary"] = plain_text(desc, 260)
    if full:
        data["description"] = sanitize_html(desc)
    return data


def _snapshot_row(model_id: int) -> dict[str, Any] | None:
    for m in _SNAPSHOT["items"]:
        if int(m.get("id") or 0) == model_id:
            return m
    return None


def _restore_stills(data: dict[str, Any], model_id: int) -> int:
    """Put back the example stills a live payload no longer carries.

    Trainers swap a version's stills for clips, and the live payload then reports
    that version with no images at all. Those stills stay on the CDN, so the pane
    showed them from the snapshot, the background refresh replaced them with the
    empty live list, and the gallery looked like it had dropped its images. The
    snapshot still holds them; merging them back here — server-side, so the open
    path and the refresh path both get them — is what stops the flicker.
    """
    row = _snapshot_row(model_id)
    if not row:
        return 0
    by_id = {v.get("id"): v for v in (row.get("versions") or [])}
    restored = 0
    for ver in data.get("versions") or []:
        if ver.get("images"):
            continue
        src = by_id.get(ver.get("id"))
        if not src:
            continue
        imgs = [dict(im) for im in (src.get("images") or []) if im.get("url")]
        if imgs:
            ver["images"] = imgs
            restored += len(imgs)
    return restored


# One merged list per query signature. Paging must slice a *stable* list —
# rebuilding it per request shifted the offsets and produced duplicates.
_MERGE_CACHE: dict[str, dict[str, Any]] = {}
# Filtered+sorted snapshot results, keyed by the filter combination. Browsing
# re-asks for the same combination on every page, and each rebuild walked all 55k
# rows; this makes page 2..N a list slice instead.
_QUERY_CACHE: dict[str, dict[str, Any]] = {}
# Pool entries already filtered+converted for a given (types, bases) pair.
_ADULT_FILTER_CACHE: dict[str, list[dict[str, Any]]] = {}
_ADULT_FILTER_TTL = 300.0
_ADULT_FILTER_TS: dict[str, float] = {}
# Kept long so switching filters back and forth is instant. Freshness is handled
# separately by _prepend_new_arrivals, which only fetches what is genuinely new.
_MERGE_TTL = 3600.0
# The index costs roughly one second per 100 rows server-side, so the first
# page only asks for 100 — about two seconds — and later pages extend the same
# cached list by offset instead of rebuilding it.
_MEILI_WINDOW = 100


def _merge_key(term: str, nsfw: bool, types: str, bases: list[str], sort_choice: str) -> str:
    return json.dumps([term, nsfw, types, sorted(bases), sort_choice], ensure_ascii=False)


# Cache for the deep paging fallback (past the 1000-hit index ceiling).
_DEEP_CACHE: dict[str, dict[str, Any]] = {}
_DEEP_TTL = 600.0
_DEEP_MAX = 2000


async def _deep_items(nsfw: bool, want: int = 0, types: str = "All",
                      bases: list[str] | None = None,
                      sort: str = "Most Liked",
                      period: str = "AllTime") -> list[dict[str, Any]]:
    """Walk the v1 cursor chain, but only as far as the caller needs.

    The cache key includes the type filter: a shared pool would leak LoRA cards
    into a Checkpoint search once the index window ran out. Sort and period are
    part of the key too — the same combination ordered by downloads and by likes,
    or limited to a week and to all time, are different result chains, and
    sharing one cache mixed them together.
    """
    key = (f"{'nsfw' if nsfw else 'sfw'}|{types}|"
           f"{','.join(sorted(bases or []))}|{sort}|{period}")
    st = _DEEP_CACHE.get(key)
    # The cache must expire: a single bad fill (partial page, wrong sort) used to
    # stick forever and serve the same broken ordering on every request.
    if st and time.time() - st.get("ts", 0.0) > 900:
        st = None
        _DEEP_CACHE.pop(key, None)
    if st is None:
        st = {"items": [], "cursor": "", "done": False, "ts": time.time()}
        _DEEP_CACHE[key] = st
    if st.get("done") and want and len(st["items"]) < want:
        return st["items"]
    guard = 0
    while (not want or len(st["items"]) < want) and not st.get("done") and guard < 25:
        guard += 1
        try:
            raw = await client.search(
                "", limit=100, nsfw=nsfw, sort=sort, period=period,
                types=types, base_models=bases or None,
                cursor=st.get("cursor") or "",
            )
        except Exception:  # noqa: BLE001
            break
        batch = raw.get("items") or []
        seen = {m.get("id") for m in st["items"]}
        for m in batch:
            if m.get("id") not in seen:
                seen.add(m.get("id"))
                st["items"].append(m)
        st["cursor"] = (raw.get("metadata") or {}).get("nextCursor") or ""
        if not st["cursor"] or not batch:
            st["done"] = True
    return st["items"]


# The adult pool does not depend on the query, so fetch it once and reuse it
# across every filter combination. The first request only waits for a small
# slice; the rest is topped up in the background.
_NSFW_POOL: dict[str, Any] = {"ts": 0.0, "items": [], "cursor": "", "done": False}
_NSFW_TTL = 900.0
_NSFW_LOCK = asyncio.Lock()
_NSFW_FIRST_BATCHES = 3
# Must stay at the full depth: the adult models are merged by like count, so
# stopping early silently drops the high-liked ones and shifts every later
# position. The cost is paid once by the warm-up, not per request.
_NSFW_MAX_BATCHES = 10


async def _nsfw_fill(batches: int) -> None:
    """Extend the shared pool by at most `batches` cursor pages."""
    for _ in range(batches):
        if _NSFW_POOL["done"]:
            return
        try:
            raw = await client.search(
                "", limit=100, nsfw=True, cursor=_NSFW_POOL["cursor"] or "",
            )
        except Exception as exc:  # noqa: BLE001
            # Do not bail silently: an unset ``done`` made every later request
            # restart the walk, so the panel never finished loading. Give up
            # after a few consecutive failures and keep what we already have.
            _NSFW_POOL["errors"] = _NSFW_POOL.get("errors", 0) + 1
            print(f"[CivitaiHub] adult pool batch failed ({_NSFW_POOL['errors']}): {exc}")
            if _NSFW_POOL["errors"] >= 3 or _NSFW_POOL["items"]:
                _NSFW_POOL["done"] = True
            return
        _NSFW_POOL["errors"] = 0
        batch = raw.get("items") or []
        seen = {m.get("id") for m in _NSFW_POOL["items"]}
        for m in batch:
            if m.get("id") not in seen:
                seen.add(m.get("id"))
                _NSFW_POOL["items"].append(m)
        _NSFW_POOL["cursor"] = (raw.get("metadata") or {}).get("nextCursor") or ""
        if not _NSFW_POOL["cursor"] or not batch:
            _NSFW_POOL["done"] = True
        _NSFW_POOL["ts"] = time.time()


async def _nsfw_pool_all(min_batches: int = _NSFW_FIRST_BATCHES) -> list[dict[str, Any]]:
    """Return the adult pool, growing it only as deep as the caller needs.

    The feed is ordered by likes, so the first N entries are always the top N.
    Fetching more later can only append lower-liked models — it can never push
    a higher one into a position that was already rendered — which makes
    incremental growth safe for the merged ordering.
    """
    want = max(min_batches, _NSFW_FIRST_BATCHES) * 100
    if _NSFW_POOL["items"] and (len(_NSFW_POOL["items"]) >= want or _NSFW_POOL["done"]):
        return _NSFW_POOL["items"]
    async with _NSFW_LOCK:
        if _NSFW_POOL["items"] and (len(_NSFW_POOL["items"]) >= want or _NSFW_POOL["done"]):
            return _NSFW_POOL["items"]
        if not _NSFW_POOL["items"]:
            _NSFW_POOL.update(cursor="", done=False)
        missing = want - len(_NSFW_POOL["items"])
        if missing > 0:
            await _nsfw_fill(min(_NSFW_MAX_BATCHES, (missing + 99) // 100))
    return _NSFW_POOL["items"]


def _nsfw_pool_complete() -> bool:
    """True once the pool has been walked to the end (or filled to the cap)."""
    return bool(_NSFW_POOL["done"]) or len(_NSFW_POOL["items"]) >= _NSFW_MAX_BATCHES * 100


_NEW_ARRIVAL_TS: dict[str, float] = {}
_NEW_ARRIVAL_INTERVAL = 120.0   # how often a cached list checks for new uploads

# The whole point of persisting this is that a restart currently throws away
# every warm result, so the first query after each restart pays full price
# again. A few hundred KB on disk removes that.
_CACHE_FILE = None


def _cache_path():
    global _CACHE_FILE
    if _CACHE_FILE is None:
        try:
            from .hub_config import THUMB_DIR
            _CACHE_FILE = THUMB_DIR.parent / "merge_cache.json"
        except Exception:  # noqa: BLE001
            _CACHE_FILE = False
    return _CACHE_FILE or None


def _load_merge_cache() -> None:
    path = _cache_path()
    if not path or not path.exists():
        return
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return
    now = time.time()
    for key, entry in (raw.items() if isinstance(raw, dict) else []):
        try:
            ts = float(entry.get("ts") or 0)
            if now - ts > _MERGE_TTL:
                continue
            items = entry.get("items") or []
            if isinstance(items, list) and items:
                _MERGE_CACHE[key] = {"ts": ts, "items": items}
        except Exception:  # noqa: BLE001
            continue


_BG_FILLING: set[str] = set()

# ---------------------------------------------------------------------------
# Snapshot store
#
# Every filter used to be its own server round trip, which is why switching cost
# 2-6s and the combination space could never be pre-warmed. Instead a single
# like-ordered crawl is kept locally (no type/base filter), and filtering,
# sorting and paging all happen in memory. One fetch serves every combination.
_SNAPSHOT: dict[str, Any] = {"items": [], "ts": 0.0, "crawling": False,
                             "cursor": "", "done": False}
_SNAPSHOT_FILE = None
# Bumped whenever the stored shape changes. Without this an old file kept being
# reused after a fix, so the fix looked like it did nothing.
# Bumped whenever the stored shape changes. Without this an old file kept being
# reused after a fix, so the fix looked like it did nothing.
_SNAPSHOT_VERSION = 10
# Deep enough that every type x base-model combination has more than one page.
# 8 types x 1500 rows = 12000, crawled once in the background while the panel is
# already usable from whatever has arrived.
_SNAPSHOT_TARGET = 12000
# Types the UI can filter by. Each gets its own slice of the snapshot; LoRA
# alone would otherwise crowd out every other type.
# Must match the type values the UI offers (web/civitai_hub_v141.js TYPES).
# Missing one means that dropdown entry always returns nothing.
_SNAPSHOT_TYPES = ["LORA", "LoCon", "DoRA", "Checkpoint", "TextualInversion",
                   "Controlnet", "Hypernetwork", "AestheticGradient", "Poses",
                   "Wildcards", "Workflows", "MotionModule", "VAE", "Upscaler"]
_SNAPSHOT_TTL = 6 * 3600.0


def _snapshot_path():
    global _SNAPSHOT_FILE
    if _SNAPSHOT_FILE is None:
        try:
            from .hub_config import THUMB_DIR
            _SNAPSHOT_FILE = THUMB_DIR.parent / "models_snapshot.json"
        except Exception:  # noqa: BLE001
            _SNAPSHOT_FILE = False
    return _SNAPSHOT_FILE or None


def _seed_snapshot_if_missing() -> None:
    """Copy the shipped snapshot into cache/ on first run.

    A cold crawl takes about two minutes, which makes the very first open look
    broken. A gzipped snapshot ships inside the plugin (105 MB of JSON compresses
    to 27 MB), so the list is usable the moment ComfyUI starts; the background
    crawl still runs and refreshes it.
    """
    path = _snapshot_path()
    if not path or path.exists():
        return
    seed = Path(__file__).resolve().parent / "data" / "models_snapshot.json.gz"
    if not seed.exists():
        return
    try:
        import gzip
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = gzip.decompress(seed.read_bytes())
        path.write_bytes(raw)
        print(f"[CivitaiHub] snapshot seeded from bundle "
              f"({len(raw) // 1024 // 1024} MB)")
    except Exception as exc:  # noqa: BLE001
        print(f"[CivitaiHub] snapshot seed failed: {exc}")


def _load_snapshot() -> None:
    path = _snapshot_path()
    if not path or not path.exists():
        return
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return
        # Pinned ids are independent of the item shape: keep them even when the
        # rest of the file is stale, otherwise the pinned block is lost and the
        # ordering silently degrades to pure likes.
        pinned = raw.get("pinned")
        if isinstance(pinned, list) and pinned:
            _SNAPSHOT_PINNED.clear()
            _SNAPSHOT_PINNED.extend(pinned)
        if raw.get("v") != _SNAPSHOT_VERSION:
            print(f"[CivitaiHub] snapshot v{raw.get('v')} != v{_SNAPSHOT_VERSION};"
                  f" rebuilding (kept {len(_SNAPSHOT_PINNED)} pinned)")
            return
        items = raw.get("items")
        ts = float(raw.get("ts") or 0)
        # Load whenever the file has content and the version matches. The old
        # freshness window meant an aged snapshot was ignored and rebuilt from
        # scratch, which is how a complete 55k file turned into 17k rows.
        if isinstance(items, list) and items:
            _SNAPSHOT.update(items=items, ts=ts)
            print(f"[CivitaiHub] snapshot loaded: {len(items)} models, "
                  f"{len(_SNAPSHOT_PINNED)} pinned")
    except Exception:  # noqa: BLE001
        pass


def _save_snapshot() -> None:
    path = _snapshot_path()
    if not path:
        return
    try:
        # Write-then-rename. A plain overwrite of this 115 MB file leaves a
        # truncated snapshot if the process dies or the disk fills mid-write, and
        # a truncated snapshot is unparseable — the whole catalogue would be lost
        # until the bundled copy is unpacked again. os.replace is atomic on NTFS.
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps({"v": _SNAPSHOT_VERSION,
                                   "ts": _SNAPSHOT["ts"],
                                   "pinned": list(_SNAPSHOT_PINNED),
                                   "items": _SNAPSHOT["items"]},
                                  ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001
        pass


def _snapshot_index(items: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    return {m.get("id"): m for m in items if m.get("id") is not None}


# Ids of the pinned block, in site order. The snapshot is like-ordered, so
# without this the pinned block (FLUX etc., moderate likes) sinks below
# high-like models — the exact inversion the site avoids.
_SNAPSHOT_PINNED: list = []


async def _snapshot_learn_pinned() -> None:
    """Read the pinned block from the index and remember its order."""
    try:
        head = await client.search_meili("", limit=100, offset=0, nsfw=False,
                                         types="All")
    except Exception:  # noqa: BLE001
        return
    rows = list(head.get("items") or [])
    if not rows:
        return
    end = len(rows)
    for i in range(1, len(rows)):
        prev = (rows[i - 1].get("stats") or {}).get("thumbsUpCount") or 0
        cur = (rows[i].get("stats") or {}).get("thumbsUpCount") or 0
        if cur > prev:
            end = i
            break
    ids = [r.get("id") for r in rows[:end] if r.get("id") is not None]
    if ids:
        _SNAPSHOT_PINNED.clear()
        _SNAPSHOT_PINNED.extend(ids)
        print(f"[CivitaiHub] pinned block: {len(ids)} models")


async def _snapshot_calibrate_bases() -> None:
    """Ask the site index which models it considers to belong to each base.

    v1's ``modelVersions`` order does not match the index's ``versions`` array,
    so deriving the base locally kept disagreeing with the website (models like
    Babes showed up under Anima). The index is the only authority, so its verdict
    is recorded once and filtering then runs locally.
    """
    bases = []
    try:
        data = await client.api("/api/v1/models", {"limit": 1, "nsfw": "true"})
        # base list comes from the live feed via base_models(); reuse it
        bm = await self_base_models()
        bases = [b["name"] for b in bm[:24]]
    except Exception:  # noqa: BLE001
        return
    if not bases:
        return
    membership: dict[str, list[int]] = {}
    for base in bases:
        ids: list[int] = []
        offset = 0
        while offset < 1000:
            try:
                res = await client.search_meili(
                    "", limit=100, offset=offset, nsfw=False,
                    types="All", base_models=[base],
                )
            except Exception:  # noqa: BLE001
                break
            hits = res.get("items") or []
            for h in hits:
                if h.get("id") is not None:
                    ids.append(h["id"])
            if len(hits) < 100:
                break
            offset += 100
        if ids:
            membership[base] = ids
    if not membership:
        return
    by_id = {m.get("id"): m for m in _SNAPSHOT["items"]}
    # Seed every row with an empty verdict first. Without this, models the index
    # does NOT associate with any base kept ``_official = None`` and fell back to
    # the local guess, so calibration appeared to have no effect on exactly the
    # models it was meant to exclude.
    for row in by_id.values():
        row["_official"] = []
    for base, ids in membership.items():
        for mid in ids:
            row = by_id.get(mid)
            if row is None:
                continue
            if base not in row["_official"]:
                row["_official"].append(base)
    _save_snapshot()
    print(f"[CivitaiHub] base membership calibrated: {sum(len(v) for v in membership.values())} rows")


async def self_base_models() -> list[dict[str, Any]]:
    try:
        return await client.base_models()
    except Exception:  # noqa: BLE001
        return []


async def _snapshot_crawl(target: int = _SNAPSHOT_TARGET) -> None:
    """Crawl the like-ordered feed, per type, into the local snapshot.

    A single unfiltered crawl is dominated by LoRA (far more of them exist), so
    Checkpoint / Controlnet / VAE searches came back empty. Each type gets its
    own slice instead, which is what makes every filter combination answerable
    from memory.
    """
    if _SNAPSHOT["crawling"]:
        return
    _SNAPSHOT["crawling"] = True
    # LoRA dwarfs the rest in volume, so it gets a bigger share of the budget;
    # the others only need enough to fill a few pages per base model.
    weights = {t: (3 if t == "LORA" else 1) for t in _SNAPSHOT_TYPES}
    total_weight = sum(weights.values())
    try:
        have = _snapshot_index(_SNAPSHOT["items"])
        # The pinned block must be present or the ordering silently degrades: its
        # models sink out of the list and the sort falls back to pure likes. The
        # per-type crawl does not reach all of them (Anima is a Checkpoint that a
        # 200-row slice missed), so fetch the block by id first.
        for mid in list(_SNAPSHOT_PINNED):
            if mid in have:
                continue
            try:
                raw = await client.api(f"/api/v1/models/{mid}", {})
            except Exception:  # noqa: BLE001
                continue
            if isinstance(raw, dict) and raw.get("id"):
                have[mid] = True
                _SNAPSHOT["items"].append(_slim_snapshot(raw))
        # Also pull the genuinely newest uploads. The per-type crawl collects the
        # hot list, so without this the Newest sort would only reorder old models
        # instead of showing what was published today.
        cursor = ""
        fresh_added = 0
        while fresh_added < 1500:
            try:
                raw = await client.search(
                    "", limit=100, nsfw=True, sort="Newest", cursor=cursor,
                )
            except Exception:  # noqa: BLE001
                break
            batch = raw.get("items") or []
            added = 0
            for m in batch:
                mid = m.get("id")
                if mid in have:
                    continue
                have[mid] = True
                _SNAPSHOT["items"].append(_slim_snapshot(m))
                added += 1
            fresh_added += added
            cursor = (raw.get("metadata") or {}).get("nextCursor") or ""
            if not cursor or not batch:
                break
        # Types are independent of each other, so crawl them concurrently. The
        # cursor chain inside one type stays serial (nextCursor is a dependency),
        # but 14 types in parallel cuts wall time from minutes to seconds.
        sem = asyncio.Semaphore(6)

        async def crawl_type(mtype: str) -> None:
            per_type = max(200, int(target * weights[mtype] / total_weight))
            cursor = ""
            grown = 0
            misses = 0
            async with sem:
                while grown < per_type:
                    try:
                        raw = await client.search(
                            "", limit=100, nsfw=True, sort="Most Liked",
                            types=mtype, cursor=cursor,
                        )
                        misses = 0
                    except Exception:  # noqa: BLE001
                        # One timeout used to abandon the whole type, leaving it
                        # with zero rows and forcing live queries later.
                        misses += 1
                        if misses >= 3:
                            print(f"[CivitaiHub] snapshot {mtype} gave up after "
                                  f"{grown} rows")
                            break
                        await asyncio.sleep(1.5 * misses)
                        continue
                    batch = raw.get("items") or []
                    for m in batch:
                        mid = m.get("id")
                        if mid in have:
                            continue
                        have[mid] = True
                        _SNAPSHOT["items"].append(_slim_snapshot(m))
                        grown += 1
                    cursor = (raw.get("metadata") or {}).get("nextCursor") or ""
                    if not cursor or not batch:
                        break

        await asyncio.gather(*(crawl_type(t) for t in _SNAPSHOT_TYPES))
        _SNAPSHOT["ts"] = time.time()
        _save_snapshot()

        # Crawl per base model as well. A type-based crawl only reaches whatever
        # ranks high overall, so a base like Anima ended up with 223 rows even
        # though the site has thousands — the base filter looked broken.
        try:
            bases = [b["name"] for b in (await client.base_models())[:26]]
        except Exception:  # noqa: BLE001
            bases = []

        async def crawl_base(base: str) -> None:
            cursor = ""
            grown = 0
            per_base = max(150, int(target * 0.6 / max(1, len(bases))))
            async with sem:
                while grown < per_base:
                    try:
                        raw = await client.search(
                            "", limit=100, nsfw=True, sort="Most Downloaded",
                            types="All", base_models=[base], cursor=cursor,
                        )
                    except Exception:  # noqa: BLE001
                        break
                    batch = raw.get("items") or []
                    for m in batch:
                        mid = m.get("id")
                        if mid in have:
                            continue
                        have[mid] = True
                        _SNAPSHOT["items"].append(_slim_snapshot(m))
                        grown += 1
                    cursor = (raw.get("metadata") or {}).get("nextCursor") or ""
                    if not cursor or not batch:
                        break

        await asyncio.gather(*(crawl_base(b) for b in bases))
        _SNAPSHOT["ts"] = time.time()
        _save_snapshot()

        _SNAPSHOT["items"].sort(
            key=lambda x: -((x.get("stats") or {}).get("thumbsUpCount") or 0))
        _SNAPSHOT["ts"] = time.time()
        _SNAPSHOT["done"] = True
        _save_snapshot()
        print(f"[CivitaiHub] snapshot ready: {len(_SNAPSHOT['items'])} models")
    finally:
        _SNAPSHOT["crawling"] = False


def _slim_snapshot(m: dict[str, Any]) -> dict[str, Any]:
    """Trim a v1 model to what browsing needs, keeping filter fields."""
    vers = m.get("modelVersions") or []
    v0 = vers[0] if vers else {}

    def usable(url: str) -> bool:
        # Some models use a .mp4 clip as their cover. An image proxy returns 404
        # for those, which showed up as a broken card with a retry button.
        low = (url or "").lower().split("?")[0]
        return low.endswith((".jpeg", ".jpg", ".png", ".webp"))

    def _collect(sources) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for ver in sources:
            for im in (ver.get("images") or []):
                url = im.get("url")
                if not usable(url):
                    continue
                out.append({
                    "url": url, "width": im.get("width"),
                    "height": im.get("height"),
                    "nsfw": (im.get("nsfwLevel") or 0) >= 4,
                    "hasMeta": im.get("hasMeta"), "id": im.get("id"),
                    "hash": im.get("hash"),
                })
                # Keep up to 20: the first six are often all explicit while a
                # clean screenshot sits further down the list, and the NSFW-off
                # cover has to come from one of those.
                if len(out) >= 20:
                    return out
        return out

    imgs = _collect([v0])
    if not imgs:
        imgs = _collect([v for v in vers if v is not v0])

    # Two cover URLs per model: the site swaps the cover to a clean screenshot
    # when NSFW is off, so both have to be known ahead of time. coverSfw equals
    # cover when the first image is already clean, which lets the frontend skip
    # the lookup for the majority of models.
    cover_nsfw = imgs[0]["url"] if imgs else ""
    cover_sfw = ""
    if imgs:
        first_clean = next((im for im in imgs if not im.get("nsfw")), None)
        cover_sfw = (first_clean or imgs[0]).get("url") or ""
    # The cover is always images[0], whatever the NSFW state. Swapping in a clean
    # screenshot changed the URL, and every swapped model lost its bundled cover
    # (that is why a few cards went black with the filter off). Image filtering
    # belongs to the detail gallery, not to the list thumbnail.
    # The site's index only carries a model's *recent* versions, so its base-model
    # filter sees the newest one. v1 returns ``modelVersions[0]`` as the primary
    # (index 0), which can be an old upload — using it pulled in models the site
    # never shows for that base. Match the index: newest by publishedAt.
    newest = None
    for v in vers:
        stamp = v.get("publishedAt") or v.get("createdAt") or ""
        if newest is None or str(stamp) > str(newest.get("publishedAt") or newest.get("createdAt") or ""):
            newest = v
    base_newest = (newest or {}).get("baseModel")
    # Every version needs its own publish date: the detail page shows them all so
    # the user can tell the current release from old experiments. The slim payload
    # keeps version ids/names only, so the full list is carried here.
    slim_versions = []
    for v in (vers or [])[:60]:
        # Carry a few screenshots per version, not just the primary one. Switching
        # versions used to land on a blank gallery because only index 0 had images
        # stored, and a model's older versions are exactly what people compare.
        v_imgs = [im for im in _collect([v])][:4]
        slim_versions.append({
            "id": v.get("id"),
            "name": v.get("name"),
            "baseModel": v.get("baseModel"),
            "publishedAt": v.get("publishedAt") or v.get("createdAt"),
            "images": v_imgs,
            "hasFiles": bool(v.get("files")),
        })
    return {
        "id": m.get("id"),
        "name": m.get("name"),
        "type": m.get("type"),
        # Sorting fields, so Newest / Most Downloaded can be answered locally
        # instead of hitting v1 again for every sort switch.
        "_created": str(m.get("createdAt") or m.get("publishedAt") or ""),
        "_published": str(v0.get("publishedAt") or v0.get("createdAt") or ""),
        "nsfw": bool(m.get("nsfw")),
        "coverNsfw": cover_nsfw,
        "coverSfw": cover_sfw,
        "creator": {"username": (m.get("creator") or {}).get("username")},
        "stats": m.get("stats") or {},
        "tags": m.get("tags") or [],
        "summary": "",
        # All versions, each with its own publish date and a few screenshots.
        # The full gallery is still fetched on demand via /version_detail; these
        # four are enough that switching versions shows something immediately.
        "versions": [
            {
                **{k: val for k, val in sv.items() if k != "images"},
                "images": sv.get("images") or (imgs if i == 0 else []),
                "trainedWords": (v0.get("trainedWords") or []) if i == 0 else [],
                "files": [],
            }
            for i, sv in enumerate(slim_versions)
        ] or [{
            "id": v0.get("id"), "name": v0.get("name"),
            "baseModel": v0.get("baseModel"),
            "publishedAt": v0.get("publishedAt") or v0.get("createdAt"),
            "images": imgs,
            "trainedWords": v0.get("trainedWords") or [],
            "files": [],
        }],
        # Only the primary version's base model. Collecting every version pulled
        # in models that merely *have* an Anima variant somewhere in their
        # history (e.g. Mistoon_Anime), which is wrong for a filter.
        "_bases": [base_newest] if base_newest else [],
        # ...but that reasoning applies to *local derivation*, not to the site's
        # filter. The site filters by every version's base model, and v1 exposes
        # exactly that as a top-level array: verified on 100 items, item.baseModels
        # equals the union of all modelVersions bases. Carrying it here means rows
        # pulled from v1 match the site's filter without the old meilisearch
        # calibration pass, and without falling back to the primary-version guess.
        "_official": [b for b in (m.get("baseModels") or []) if b],
    }


def _snapshot_query(term: str, types: str, bases: list[str], nsfw_on: bool,
                    sort_choice: str) -> list[dict[str, Any]]:
    """Filter + sort the snapshot entirely in memory."""
    out: list[dict[str, Any]] = []
    needle = term.strip().lower()
    bset = set(bases or [])
    # One lookup per query, not per row: this walks 55k models.
    have = _bundled_hashes()
    for m in _SNAPSHOT["items"]:
        if not nsfw_on:
            # A model whose every screenshot is explicit is effectively an adult
            # model regardless of its own rating, so treat it as one. That is
            # cleaner than keeping it and blurring the cover: there is no clean
            # image to show, so the card could never be previewed safely.
            if m.get("nsfw"):
                continue
            _imgs = ((m.get("versions") or [{}])[0] or {}).get("images") or []
            if _imgs and all(im.get("nsfw") for im in _imgs):
                continue
        if types and types != "All" and (m.get("type") or "") != types:
            continue
        if bset:
            # Prefer the index's own verdict when calibration has run; fall back
            # to the locally derived bases otherwise.
            if "_official" in m:
                mb = set(m.get("_official") or [])
            else:
                mb = set(m.get("_bases") or [])
            if not (mb & bset):
                continue
        if needle:
            hay = (m.get("name") or "").lower() + " " + " ".join(m.get("tags") or []).lower()
            if needle not in hay:
                continue
        # Compute both cover URLs on the fly instead of storing them in the
        # snapshot: the snapshot format is expensive to rebuild and this is two
        # cheap lookups over a list already in memory.
        m = _with_covers(m, have)
        out.append(m)

    def likes(x):
        return (x.get("stats") or {}).get("thumbsUpCount") or 0

    def downloads(x):
        return (x.get("stats") or {}).get("downloadCount") or 0

    if sort_choice == "Newest":
        # Matches the site: Newest orders by when the model was first created, so
        # an old model that merely published a new version does not jump to the
        # top. _published is only the fallback for rows that lack a create stamp.
        out.sort(key=lambda x: str(x.get("_created") or x.get("_published") or ""),
                 reverse=True)
    elif sort_choice == "Most Downloaded":
        out.sort(key=lambda x: -((x.get("stats") or {}).get("downloadCount") or 0))
    else:
        # Site order: the pinned block first, in its own order, then likes.
        pinned_pos = {mid: i for i, mid in enumerate(_SNAPSHOT_PINNED)}

        def rank(m):
            pos = pinned_pos.get(m.get("id"))
            if pos is not None:
                return (0, pos)
            # Likes, then downloads, then stored position.
            # The site sorts Most Liked by like count and breaks ties by download
            # count — verified on live data, 6 of 6 adjacent equal-like pairs
            # came back in download order.
            # Position is only the last resort, and it is deliberately not used
            # to partition the list: cursor paging sometimes drops a row whole.
            # One model with 9305 likes (true rank 200) was missing from a
            # 100-page walk and ended up in the tail; sorting by counter puts it
            # back where it belongs, sorting by stored position cannot.
            r = m.get("_r")
            return (1, -likes(m), -downloads(m), 0 if r is None else r)

        out.sort(key=rank)
    return out


def _with_covers(m: dict[str, Any], have: set[str]) -> dict[str, Any]:
    """Attach the cover fields a card needs.

    Shared by the snapshot path and the v1 row fill: rows pulled from v1 arrive in
    a different shape and would otherwise render as initial-letter placeholders.

    Lives after _snapshot_query on purpose — inserting it in the middle of that
    function silently cut the sort block and its return off, so every query came
    back None.
    """
    vers0 = (m.get("versions") or [{}])[0] or {}
    imgs0 = vers0.get("images") or []
    if not imgs0:
        return m
    primary = imgs0[0]
    clean = next((im for im in imgs0 if not im.get("nsfw")), None) or primary
    k_primary = hashlib.sha1(
        f"{primary.get('url')}|{_THUMB_STORE_WIDTH}".encode()).hexdigest()
    k_clean = hashlib.sha1(
        f"{clean.get('url')}|{_THUMB_STORE_WIDTH}".encode()).hexdigest()

    def _cover_url(key: str, origin: str) -> str:
        # Only claim a bundled file when it is really on disk. Otherwise point
        # straight at the proxy route, which serves from cache/thumbs when the
        # cover has already been fetched.
        if key in have:
            return f"/extensions/ComfyUI-CivitaiHub/covers/{key}.jpg"
        if not origin:
            return ""
        return (f"/civitai_hub/thumb?url={quote(origin, safe='')}"
                f"&w={int(_THUMB_STORE_WIDTH)}")

    return {**m,
            "coverNsfw": primary.get("url") or "",
            "coverSfw": clean.get("url") or "",
            "coverNsfwKey": k_primary,
            "coverSfwKey": k_clean,
            "coverNsfwUrl": _cover_url(k_primary, primary.get("url") or ""),
            "coverSfwUrl": _cover_url(k_clean, clean.get("url") or ""),
            # "Every screenshot is explicit" cannot be inferred by comparing the
            # two URLs: for a model whose first image is clean they are equal.
            "allExplicit": not any(not im.get("nsfw") for im in imgs0)}


# A hundred-row probe cannot keep the order honest. Like counts drift by a few
# dozen, and around rank 1000 that is enough to swap one model in two: measured
# on 2026-11-23, refreshing the top 2000 counters still left half of the slots
# between rank 1000 and 1500 out of order, and only a full pass fixed them.
#
# There is no such thing as a complete walk: the site holds well over 70000
# models and the cursor still returned rows at rank 70000 with 166 likes. Only
# the head decides what users see, so the pass stops at rank 10000. Measured
# coverage there is 9998 of 10000, and the two missing rows get added by this
# same pass. That head is ~25x deeper than anyone actually scrolls.
_REFRESH_PAGES = 100
# 100 pages at 1.42s each is about 2.5 minutes, run in the background.
_REFRESH_INTERVAL = 6 * 3600.0
# A small gap between pages. This is one long sequential walk; hammering the
# mirror with it is how the image proxy got us rate-limited once already.
_REFRESH_PAGE_GAP = 0.25
_REFRESH_NEW_COVERS = 60
# Target rows per base model. Required: 300, the same figure the cover prefetch
# uses, so a base-model filter has three pages of rows locally instead of one.
# A base model with fewer rows than this never reached the Most Liked head, so the
# snapshot cannot serve it locally. A newly released base model is exactly that
# case: its first Checkpoints have no download history. Measured 2026-11-23, a
# top-10000 member has a median age of 1014 days and only 75 of 9998 are under a
# week old, so waiting for popularity means waiting days.
_THIN_BASE_ROWS = 300
# Three pages per base = 300 rows. One page (100) left most combinations short.
# v1's baseModels filter was unreliable once; re-verified 2026-11-23 and every
# returned row carried the requested base, so paging it makes a base filter local.
_BASE_FILL_PAGES = 3
# Bases handled per pass. 60 bases × 3 pages = 180 requests ≈ 4.5 minutes at the
# page gap, which stays inside the step's own timeout. The cursor rotates, so the
# long tail is covered over successive passes instead of one very long walk.
# Combinations handled per pass. 20 × 3 pages = 60 requests ≈ 90 seconds at the
# page gap, which leaves room inside the step's own timeout. The cursor is
# persisted, because it now walks ~500 combinations: a memory-only cursor restarts
# at zero on every launch and the tail would never be reached.
_BASE_FILL_MAX = 20
_BASE_FILL_CURSOR_FILE = Path(__file__).resolve().parent / "cache" / "base_fill_cursor.json"


def _load_base_cursor() -> int:
    try:
        raw = _BASE_FILL_CURSOR_FILE.read_text(encoding="utf-8-sig")
        return max(0, int(json.loads(raw).get("cursor") or 0))
    except Exception:  # noqa: BLE001
        return 0


def _save_base_cursor(value: int) -> None:
    try:
        _BASE_FILL_CURSOR_FILE.parent.mkdir(parents=True, exist_ok=True)
        _BASE_FILL_CURSOR_FILE.write_text(json.dumps({"cursor": int(value)}),
                                          encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
_REFRESH_LOG = Path(__file__).resolve().parent / "cache" / "refresh.log"
# Present only while a pass is incomplete. Written before the work, deleted after
# all of it succeeded, so a shutdown mid-pass leaves it behind.
_REFRESH_STATE = Path(__file__).resolve().parent / "cache" / "refresh_state.json"


def _refresh_note(msg: str) -> None:
    """Record a refresh step.

    print() never arrives: ComfyUI routes stdout through its own logger, which
    drops plugin prints at the default level, so a failing refresh was invisible
    and looked like a refresh that never ran.
    """
    try:
        _REFRESH_LOG.parent.mkdir(parents=True, exist_ok=True)
        with _REFRESH_LOG.open("a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except Exception:  # noqa: BLE001
        pass


def _mark_refresh(pending: bool, reason: str = "") -> None:
    """Record whether a refresh pass is still in flight.

    Written before the work starts and removed only once every step succeeded, so
    a shutdown in the middle leaves the marker on disk. The next start reads it and
    runs its pass straight away instead of waiting 90 seconds for data it already
    knows is stale. It replaces that delayed pass — it does not add a second one.
    """
    try:
        if pending:
            _REFRESH_STATE.parent.mkdir(parents=True, exist_ok=True)
            _REFRESH_STATE.write_text(
                json.dumps({"pending": True, "reason": reason, "ts": time.time()}),
                encoding="utf-8")
        else:
            _REFRESH_STATE.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass


def _refresh_pending() -> str:
    """Reason the previous pass never finished, or '' when there is nothing owed.

    The mere existence of the marker means the last pass did not clean up, so an
    unreadable one counts as pending too. Treating a corrupt file as "no debt" made
    the marker silently useless — it waited the full 90 seconds anyway. Read with
    utf-8-sig because a hand-written file may carry a BOM, which json rejects.
    """
    if not _REFRESH_STATE.exists():
        return ""
    try:
        data = json.loads(_REFRESH_STATE.read_text(encoding="utf-8-sig"))
        return str((data or {}).get("reason") or "incomplete")
    except Exception:  # noqa: BLE001
        return "unreadable state"


async def _refresh_ranking() -> bool:
    """Rewrite the snapshot from the site's own Most Liked ranking.

    The order that comes back *is* the site's order, tie-breaks included, so the
    snapshot becomes a copy of it instead of a local re-sort. That is also the
    cheap path: the list endpoint already carries the counters the ordering uses,
    so one page walk replaces 55107 per-model detail requests.
    """
    have = _snapshot_index(_SNAPSHOT["items"])
    order: list[int] = []
    stats: dict[int, dict] = {}
    bases_map: dict[int, list[str]] = {}
    fresh_raw: dict[int, dict] = {}
    seen_order: set = set()
    cursor = ""
    pages = 0
    repeats = 0
    try:
        while pages < _REFRESH_PAGES:
            # types must be "All". client.search defaults to types="LORA", so
            # omitting it walks the LoRA-only ranking: the first page then starts
            # at rank 7 of the real list and every Checkpoint is missing, which
            # put the site's top model 2263 rows down the stored file.
            # period must stay empty for the same reason: the site's Most Liked
            # carries no period, and only sort + cursor produce its exact order.
            raw = await client.search("", limit=100, nsfw=True, types="All",
                                      period="", sort="Most Liked", cursor=cursor)
            batch = raw.get("items") or []
            if not batch:
                break
            for m in batch:
                mid = m.get("id")
                if mid is None:
                    continue
                # Cursor paging repeats a row across page boundaries. Keeping the
                # repeat would shift every stored rank after it, and the stored
                # rank is the whole point: measured 2 repeats in 100 pages, which
                # moved the order 2 slots off the site from row 287 onwards.
                if mid in seen_order:
                    repeats += 1
                    continue
                seen_order.add(mid)
                order.append(mid)
                st = m.get("stats")
                if st:
                    stats[mid] = st
                bm = m.get("baseModels")
                if bm:
                    # The site's filter is the union of every version's base
                    # model, and this array carries exactly that. Refreshing it on
                    # each pass keeps the head's filter current as models gain new
                    # versions — the bundled calibration is frozen at pack time.
                    bases_map[mid] = [b for b in bm if b]
                if mid not in have:
                    fresh_raw[mid] = m
            cursor = (raw.get("metadata") or {}).get("nextCursor") or ""
            pages += 1
            if not cursor:
                break
            await asyncio.sleep(_REFRESH_PAGE_GAP)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        # Not print(): ComfyUI routes stdout through its logger and drops plugin
        # output at the default level, so a failed walk looked identical to a
        # walk that never started.
        _refresh_note(f"ranking walk failed after {pages} pages: "
                      f"{type(exc).__name__}: {exc}")
        return False

    # A short walk means the cursor chain broke, not that the site shrank. The
    # head is spliced into the snapshot below, so a truncated head would put
    # low-ranked rows above high-ranked ones. Keep what is on disk instead.
    floor = _REFRESH_PAGES * 90
    if len(order) < floor:
        _refresh_note(f"ranking refresh skipped: {len(order)} rows "
                      f"(floor {floor}); snapshot left untouched")
        return False

    merged: list[dict[str, Any]] = []
    new_rows: list[dict[str, Any]] = []
    seen: set = set()
    changed = 0
    for mid in order:
        if mid in seen:
            continue
        seen.add(mid)
        old = have.get(mid)
        if old is None:
            slim = _slim_snapshot(fresh_raw.get(mid) or {})
            if slim.get("id") is None:
                continue
            merged.append(slim)
            new_rows.append(slim)
            continue
        st = stats.get(mid)
        bm = bases_map.get(mid)
        if (st and st != (old.get("stats") or {})) or (
                bm is not None and bm != list(old.get("_official") or [])):
            patch: dict[str, Any] = {}
            if st:
                patch["stats"] = st
            if bm is not None:
                patch["_official"] = bm
            merged.append({**old, **patch})
            changed += 1
        else:
            merged.append(old)

    # Rows the ranking never returned (unavailable, taken down, made private)
    # keep their place at the end rather than disappearing from the catalogue.
    tail = [m for m in _SNAPSHOT["items"] if m.get("id") not in seen]
    # Store the site rank on every row. Below the refreshed head the counters are
    # stale — a model that lost likes keeps its old number — so re-deriving the
    # order from counters lifts it back among the top rows. Measured after a
    # 100-page refresh: 3 rows above the rank-10000 threshold, which was enough
    # to shift page 20 against the site.
    for i, m in enumerate(merged):
        m["_r"] = i
    tail.sort(key=lambda m: -int((m.get("stats") or {}).get("thumbsUpCount") or 0))
    for i, m in enumerate(tail):
        m["_r"] = len(merged) + i
    _SNAPSHOT["items"] = merged + tail
    _SNAPSHOT["ts"] = time.time()
    # Cached pages still hold the previous order. Dropping them costs one rebuild
    # on the next filter switch; keeping them would serve a stale ranking for
    # hours, which is the exact problem this function exists to fix.
    _QUERY_CACHE.clear()
    _save_snapshot()
    _refresh_note(f"refreshed: {len(merged)} rows / {pages} pages "
                  f"({repeats} cursor repeats dropped), "
                  f"{changed} counters updated, +{len(new_rows)} new, "
                  f"{len(tail)} kept aside")
    if new_rows:
        # Bounded on purpose. _warm_new_covers waits on the shared prefetch
        # semaphore, so a busy scroll can starve it; without a timeout that
        # stall would hold the whole refresh loop and the next pass six hours
        # later would never start.
        try:
            got = await asyncio.wait_for(_warm_new_covers(new_rows), timeout=120)
            _refresh_note(f"covers adopted: {got} of {len(new_rows)} new rows")
        except asyncio.TimeoutError:
            _refresh_note("cover adoption timed out after 120s; next pass retries")
        except Exception as exc:  # noqa: BLE001
            _refresh_note(f"cover adoption failed: {type(exc).__name__}: {exc}")
    # Newly released base models cannot reach the Most Liked head, so they are
    # topped up separately. Cheap: one page per thin base, a few bases per pass.
    try:
        got = await asyncio.wait_for(_fill_thin_bases(), timeout=600)
        # Logged even when zero: the pass always costs one request per thin base,
        # so "nothing new" and "never ran" must not look the same in the log.
        _refresh_note(f"thin base pass: +{got} rows")
    except asyncio.TimeoutError:
        _refresh_note("thin base fill timed out after 600s; next pass continues")
        return False
    except Exception as exc:  # noqa: BLE001
        _refresh_note(f"thin base fill failed: {type(exc).__name__}: {exc}")
        return False
    return True


async def _fill_thin_bases() -> int:
    """Top up (base model × type) combinations the snapshot barely covers.

    The refresh only walks the site's Most Liked head. Anything outside it — and
    every model of a base model released this week — is served over the network
    instead of locally, which is the one promise this plugin can still break.

    Granularity is the combination, not the base model: topping up "Pony" with one
    mixed page does nothing for the LoRA filter, which is the one people actually
    use. Each combination gets up to _BASE_FILL_PAGES pages = _THIN_BASE_ROWS rows.

    **Data only.** This never fetches a cover. Cover files are downloaded by the
    thumb route when a row is actually displayed, and only when that exact file is
    not already on disk — topping up thousands of rows must not pull thousands of
    images with them.
    """
    counts: dict[tuple[str, str], int] = {}
    for m in _SNAPSHOT["items"]:
        kind = (m.get("type") or "").strip() or "All"
        row = m["_official"] if "_official" in m else m.get("_bases")
        for base in row or []:
            name = (base or "").strip()
            if name:
                key = (name, kind)
                counts[key] = counts.get(key, 0) + 1
    try:
        for item in await client.base_models():
            name = (item.get("name") or "").strip()
            if name:
                # A base model with no rows yet: fetch it without a type filter,
                # which is enough for something that only just shipped.
                counts.setdefault((name, "All"), 0)
    except Exception:  # noqa: BLE001
        pass
    thin = sorted((c, b, t) for (b, t), c in counts.items() if c < _THIN_BASE_ROWS)
    if not thin:
        return 0
    start = _load_base_cursor() % len(thin)
    picked = thin[start:start + _BASE_FILL_MAX]
    if not picked:
        picked = thin[:1]
    _save_base_cursor((start + len(picked)) % len(thin))

    # A plain id set, not the snapshot index: those two hold different shapes and
    # mixing them in one dict made the values meaningless.
    have = _snapshot_index(_SNAPSHOT["items"])
    seen = set(have)
    fresh: list[dict[str, Any]] = []
    refitted = 0
    per_combo: dict[str, int] = {}
    for _have, base, kind in picked:
        label = f"{base} × {kind}"
        before = len(fresh)
        for page in range(1, _BASE_FILL_PAGES + 1):
            try:
                raw = await client.search("", limit=100, nsfw=True, types=kind,
                                          sort="Most Liked", period="", page=page,
                                          base_models=[base])
            except Exception:  # noqa: BLE001
                break
            batch = raw.get("items") or []
            for m in batch:
                mid = m.get("id")
                if mid is None:
                    continue
                bm = [b for b in (m.get("baseModels") or []) if b]
                if mid in seen:
                    # Rows added before this field existed carry only the locally
                    # derived _bases, which filters by the primary version alone.
                    # Repair them in place from the site's own array.
                    old = have.get(mid)
                    if bm and old is not None and list(old.get("_official") or []) != bm:
                        old["_official"] = bm
                        refitted += 1
                    continue
                seen.add(mid)
                fresh.append(_slim_snapshot(m))
            if len(batch) < 100:
                break
            await asyncio.sleep(_REFRESH_PAGE_GAP)
        if len(fresh) > before:
            per_combo[label] = len(fresh) - before
    if not fresh and not refitted:
        _refresh_note(f"thin pass: {len(picked)} combinations checked, nothing to add "
                      f"(cursor {start}->{_load_base_cursor()} of {len(thin)})")
        return 0
    if fresh:
        # No stored rank: these rows sort by counter, which is where they belong.
        _SNAPSHOT["items"].extend(fresh)
    _SNAPSHOT["ts"] = time.time()
    _QUERY_CACHE.clear()
    _save_snapshot()
    # Per-combination detail, because "+0 rows" cannot tell a combination that is
    # already complete apart from one the loop skipped.
    if per_combo:
        top = sorted(per_combo.items(), key=lambda kv: -kv[1])[:12]
        _refresh_note("combinations filled: " + ", ".join(f"{k} +{n}" for k, n in top))
    return len(fresh) + refitted


async def _warm_new_covers(rows: list[dict[str, Any]]) -> int:
    """Fetch one cover per newly ranked model and adopt it as a bundled cover.

    Not a cache write: the file lands in web/covers and index.json gains its
    entry, so _bundled_hashes still finds it after a restart instead of falling
    back to the proxy. That is the difference between "downloaded once" and
    "shipped with the plugin".

    Returns how many were adopted, and records why the rest were not: a silent
    zero here is indistinguishable from the step never running.
    """
    global _BUNDLED_TS
    if not rows:
        return 0
    index_path = _COVERS_DIR / "index.json"
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
        if not isinstance(index, dict):
            index = {}
    except Exception:  # noqa: BLE001
        index = {}
    # Its own two slots, not the shared prefetch semaphore. At startup that one
    # is saturated by the base-model cover prefetch, so a new row's single cover
    # sits at the back of that queue and never gets a slot — which looks exactly
    # like "saving the cover is broken".
    sem = asyncio.Semaphore(2)
    lock = asyncio.Lock()
    adopted = 0

    async def one(m: dict[str, Any]) -> str:
        nonlocal adopted
        mid = m.get("id")
        vers = (m.get("versions") or [{}])[0] or {}
        for im in (vers.get("images") or [])[:4]:
            url = im.get("url") or ""
            # Video covers are 404 through the image proxy, so move on to the
            # first real screenshot instead of burning a request on a dead URL.
            if not url.split("?")[0].lower().endswith((".jpeg", ".jpg", ".png", ".webp")):
                continue
            ikey = f"{url}|{_THUMB_STORE_WIDTH}"
            key = hashlib.sha1(ikey.encode()).hexdigest()
            dest = _COVERS_DIR / f"{key}.jpg"
            if dest.exists():
                async with lock:
                    index[ikey] = key
                return f"{mid} already bundled as {key[:8]}"
            async with sem:
                try:
                    got = await client.fetch_thumb(url, _THUMB_STORE_WIDTH)
                except Exception as exc:  # noqa: BLE001
                    return f"{mid} fetch {type(exc).__name__}: {exc}"
            if not got or not got[0]:
                return f"{mid} fetch returned nothing"
            try:
                dest.write_bytes(got[0])
            except Exception as exc:  # noqa: BLE001
                return f"{mid} write {type(exc).__name__}: {exc}"
            async with lock:
                index[ikey] = key
                adopted += 1
            return f"{mid} adopted {len(got[0])}B"
        return f"{mid} has no static image"

    notes = await asyncio.gather(*(one(m) for m in rows[:_REFRESH_NEW_COVERS]))
    for n in notes:
        _refresh_note(f"cover {n}")
    if not adopted:
        return 0
    try:
        # Same reason as the snapshot: this index is what makes 23k bundled covers
        # resolve instantly, so a half-written one is not acceptable.
        idx_tmp = index_path.with_name(index_path.name + ".tmp")
        idx_tmp.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
        os.replace(idx_tmp, index_path)
    except Exception as exc:  # noqa: BLE001
        _refresh_note(f"cover index write failed: {exc}")
        return 0
    # Expire the 10-minute memo so the next render already sees the new hashes.
    _BUNDLED_TS = 0.0
    return adopted


async def _snapshot_refresh() -> None:
    """Keep the shipped ranking honest: one full pass, then every six hours.

    Delayed at the start so it does not compete with the first panel open, which
    is the one moment the user is actually waiting on the mirror. The delay is
    skipped when the previous pass never finished — that is exactly the case where
    the data is stale and the user would otherwise wait another 90 seconds for
    nothing.
    """
    try:
        pending = _refresh_pending()
        if pending:
            _refresh_note(f"previous pass did not finish ({pending}); "
                          "running the next one immediately")
        else:
            await asyncio.sleep(90)
        while True:
            # Marked before the work, cleared only after all of it succeeded, so a
            # shutdown in the middle leaves the marker behind for the next start.
            _mark_refresh(True, "pass in progress")
            ok = False
            try:
                ok = await _refresh_ranking()
            except asyncio.CancelledError:
                return
            except Exception as exc:  # noqa: BLE001
                _refresh_note(f"refresh failed: {type(exc).__name__}: {exc}")
            if ok:
                _mark_refresh(False)
            await asyncio.sleep(_REFRESH_INTERVAL)
    except asyncio.CancelledError:
        return


_THUMB_PREFETCH_PER_COMBO = 300
# The default combination's covers are permanent; anything loaded while
# browsing other filters is temporary and released when the filter changes.
_BASE_COMBOS = {("All", ""), ("LORA", "")}

# Per-combination cover floor. The first N covers of a filter are cached so
# returning to it is instant; only the default browse views keep them forever.
_THUMB_BASE_PER_COMBO = 300
# Stored at 240px: a ~300px card looks the same but each cover is ~12KB instead
# of ~32KB, which matters when thousands live on disk.
_THUMB_STORE_WIDTH = 240
# Refill once the user has scrolled through a third of what is already cached.
# At 0.5 the refill only started on page 8 of 12, leaving no headroom for fast
# scrolling; 0.3 starts around page 4 and stays well ahead.
_THUMB_REFILL_RATIO = 0.3
_COMBO_WARMED: set[str] = set()
# Scroll-driven refill state, per filter combination.
#   _COMBO_WARMED_TO  how far down covers have been fetched (starts at the floor)
#   _COMBO_TRIGGER    the row that queues the next block; each block advances it
#                     by _THUMB_BASE_PER_COMBO * _THUMB_REFILL_RATIO
#   _COMBO_WARMING    combination whose block is still running, so a fast scroll
#                     cannot stack one block per page request
_COMBO_WARMED_TO: dict[str, int] = {}
_COMBO_TRIGGER: dict[str, int] = {}
_COMBO_WARMING: set[str] = set()
# Extra rows pulled from v1 behind the snapshot's edge. These are runtime only:
# they live in _QUERY_CACHE next to the snapshot result and go away with it, so
# switching filters or reopening the panel drops them again.
_DEEP_FILLING: set[str] = set()
# One block of extra rows per trigger. A v1 page is 100 rows at ~1.1s, so a
# block costs three requests — far cheaper than covers, which take ~1s each.
_DEEP_BLOCK = 300
# One semaphore for every prefetch task. It used to be created inside
# _prefetch_combo_thumbs, so each page request spawned a task with its own slots
# and the real concurrency was that limit times the number of open pages.
_PREFETCH_SEM: asyncio.Semaphore | None = None


def _prefetch_sem() -> asyncio.Semaphore:
    global _PREFETCH_SEM
    if _PREFETCH_SEM is None:
        _PREFETCH_SEM = asyncio.Semaphore(6)
    return _PREFETCH_SEM


# Bundled covers ship as web/covers/<sha1>.jpg. The index is the only authority
# on what was actually packed: the snapshot's image URLs are collected at a
# different time, so a row can point at a cover the bundle never received.
_COVERS_DIR = Path(__file__).resolve().parent / "web" / "covers"
_BUNDLED_HASHES: set[str] = set()
_BUNDLED_TS = 0.0


def _bundled_hashes() -> set[str]:
    """Hashes that really exist in the bundled cover set.

    Handing the frontend /covers/<hash>.jpg without this check produced one 404
    per unpacked card, and the browser then re-requested the same cover through
    the proxy as a fallback — two round trips per cover.
    """
    global _BUNDLED_HASHES, _BUNDLED_TS
    now = time.time()
    if _BUNDLED_HASHES and now - _BUNDLED_TS < 600:
        return _BUNDLED_HASHES
    try:
        raw = json.loads((_COVERS_DIR / "index.json").read_text(encoding="utf-8"))
        _BUNDLED_HASHES = set(raw.values())
        _BUNDLED_TS = now
        print(f"[CivitaiHub] bundled covers indexed: {len(_BUNDLED_HASHES)}")
    except Exception as exc:  # noqa: BLE001
        print(f"[CivitaiHub] bundled index unavailable: {exc}")
    return _BUNDLED_HASHES


def _prune_cache(scope: str = "temp", max_age_days: float = 0) -> tuple[int, int]:
    """Delete cached thumbnails according to ``scope``.

    scope="temp"    every file not marked core (survives a crash mid-scroll)
    scope="detail"  non-core files above 120 KB (the big previews)
    max_age_days    additionally drop anything untouched for that long, core
                    included — covers for a model nobody opened in months are
                    dead weight regardless of their tag
    Returns (removed, freed_bytes).
    """
    try:
        from .hub_config import THUMB_DIR
    except Exception:  # noqa: BLE001
        return 0, 0
    if not THUMB_DIR.exists():
        return 0, 0
    cutoff = time.time() - max_age_days * 86400 if max_age_days else 0
    removed = freed = 0
    for f in list(THUMB_DIR.glob("*.bin")):
        try:
            st = f.stat()
        except OSError:
            continue
        kind_f = f.with_suffix(".kind")
        try:
            kind = kind_f.read_text(encoding="utf-8").strip() if kind_f.exists() else ""
        except OSError:
            kind = ""
        stale = bool(cutoff) and st.st_mtime < cutoff
        if not stale:
            if scope == "temp":
                if kind == "core":
                    continue
            elif scope == "detail":
                if kind == "core" or st.st_size < 120 * 1024:
                    continue
            elif kind == "core":
                continue
        f.unlink(missing_ok=True)
        f.with_suffix(".ct").unlink(missing_ok=True)
        kind_f.unlink(missing_ok=True)
        removed += 1
        freed += st.st_size
    return removed, freed


def _prune_download_temp(max_age_hours: float = 6.0) -> tuple[int, int]:
    """Delete abandoned model-download fragments.

    Closing ComfyUI mid-download kills the process before the cleanup in
    DownloadManager._run (a finally block) can run, so the partial write stays in
    cache/tmp. Fragment names carry the task's uuid, so a later attempt at the same
    file cannot reuse them — those bytes are dead weight forever. Only fragments
    untouched for a while are removed, so a second ComfyUI instance downloading at
    the same time is not disturbed.
    """
    try:
        from .hub_config import TMP_DIR
    except Exception:  # noqa: BLE001
        return 0, 0
    if not TMP_DIR.exists():
        return 0, 0
    cutoff = time.time() - max_age_hours * 3600
    removed = freed = 0
    for f in list(TMP_DIR.iterdir()):
        try:
            if not f.is_file() or f.suffix not in (".part", ".parts"):
                continue
            st = f.stat()
        except OSError:
            continue
        if st.st_mtime >= cutoff:
            continue
        try:
            f.unlink()
        except OSError:
            continue
        removed += 1
        freed += st.st_size
    return removed, freed


async def _prefetch_base_covers() -> None:
    """Fill the default views' cover floors at boot.

    Without this the first paint shows placeholders until the per-combination
    prefetch catches up, even though the covers are the ones users see most.
    """
    for types, bases in (("All", []), ("LORA", [])):
        try:
            items = await _browse_unlimited("", True, types, bases,
                                            _THUMB_BASE_PER_COMBO)
            got = await _prefetch_combo_thumbs(
                items, _THUMB_BASE_PER_COMBO, "core")
            print(f"[CivitaiHub] base covers cached for {types}: {got}")
        except Exception as exc:  # noqa: BLE001
            print(f"[CivitaiHub] base cover prefetch failed for {types}: {exc}")


def _is_base_combo(types: str, bases: list[str], sort_choice: str = "") -> bool:
    """True only for the default browse views.

    Their covers are worth keeping forever because the user returns to them
    constantly. Every other combination — and any time-sorted view, whose first
    page changes with every upload — is treated as temporary.
    """
    if sort_choice:
        return False
    return (types, ",".join(sorted(bases))) in _BASE_COMBOS


async def _prefetch_combo_thumbs(items: list[dict[str, Any]],
                                 limit: int = _THUMB_BASE_PER_COMBO,
                                 kind: str = "core") -> int:
    """Cache the first ``limit`` covers of one filter combination.

    Three covers in flight, no byte-range splitting: a 240px cover is ~14 KB, so
    ranges save nothing and every extra segment is another concurrent request to
    the proxy. Six covers × six segments was 36 parallel connections, which is
    how the IP got rate-limited before.
    """
    sem = _prefetch_sem()
    done = 0
    skipped = 0
    # Bundled covers live in web/covers, indexed by the same key. Check them before
    # touching the network: fetch_thumb_range only knows cache/thumbs, so without
    # this the prefetch re-downloaded all 300 covers that were already shipped
    # with the plugin.
    bundled_path = Path(__file__).resolve().parent / "web" / "covers"

    async def one(m: dict[str, Any]) -> None:
        nonlocal done, skipped
        ver = (m.get("versions") or [{}])[0] or {}
        imgs = ver.get("images") or []
        if not imgs:
            return
        url = imgs[0].get("url")
        if not url:
            return
        bkey = hashlib.sha1(
            f"{url}|{int(_THUMB_STORE_WIDTH)}".encode()).hexdigest()
        if (bundled_path / f"{bkey}.jpg").exists():
            skipped += 1
            return
        async with sem:
            try:
                got = await client.fetch_thumb_range(url, _THUMB_STORE_WIDTH, 1)
                if got:
                    done += 1
                    # Tag as requested so pruning can tell floors from leftovers.
                    key = hashlib.sha1(
                        f"{url}|{int(_THUMB_STORE_WIDTH)}".encode()).hexdigest()
                    kf = None
                    try:
                        from .hub_config import THUMB_DIR
                        kf = THUMB_DIR / f"{key}.kind"
                    except Exception:  # noqa: BLE001
                        kf = None
                    if kf and not kf.exists():
                        try:
                            kf.write_text(kind, encoding="utf-8")
                        except Exception:  # noqa: BLE001
                            pass
            except Exception:  # noqa: BLE001
                return

    await asyncio.gather(*(one(m) for m in items[:limit]))
    if skipped:
        print(f"[CivitaiHub] prefetch {kind}: {done} downloaded, "
              f"{skipped} already bundled")
    return done


async def _prefetch_block(key: str, items: list[dict[str, Any]],
                          limit: int) -> None:
    """Fetch one block of covers, then clear its in-flight mark.

    The mark is what paces the chain: while a block is still running no new
    block is queued for that combination, so scrolling fast cannot stack one
    block per page request.
    """
    try:
        await _prefetch_combo_thumbs(items, limit, "temp")
    except Exception:  # noqa: BLE001
        pass
    finally:
        _COMBO_WARMING.discard(key)


async def _deep_fill(key: str, types: str, bases: list[str],
                     want_nsfw: bool, target: int,
                     sort: str = "Most Liked",
                     period: str = "AllTime") -> None:
    """Append rows the snapshot is missing, straight into the cached list.

    The snapshot is a fixed 55k file, so a narrow combination runs out of rows
    well before the site does — Anima alone has 830 locally against thousands on
    the site. v1's cursor chain has no such ceiling, and a row costs ~11ms
    against ~1s for a cover, so this is the cheap half of the prefetch.

    What lands here is temporary by design: it is appended to the _QUERY_CACHE
    entry, so releasing that entry (filter change, eviction, restart) drops the
    extra rows with it.
    """
    try:
        # Ask for more than the list currently holds, measured against what v1
        # has already handed over. Using len(list) + a fixed step stalled after
        # the second round: almost every row v1 returns is already in the list,
        # so the target grew by 900 while the useful additions went to zero.
        _dkey = (f"{'nsfw' if want_nsfw else 'sfw'}|{types}|"
                 f"{','.join(sorted(bases or []))}|{sort}|{period}")
        _pulled = len((_DEEP_CACHE.get(_dkey) or {}).get("items") or [])
        target = max(target, _pulled + _DEEP_BLOCK * 3)
        deep = await _deep_items(want_nsfw, target, types, bases or None,
                                 sort, period)
        # Re-read after the await: the entry can be evicted while the network
        # call is in flight, and appending to a detached list would silently do
        # nothing while still reporting success.
        entry = _QUERY_CACHE.get(key)
        if not entry:
            return
        have = {m.get("id") for m in entry["items"]}
        added = 0
        for m in deep:
            mid = m.get("id")
            if mid in have:
                continue
            imgs = ((m.get("versions") or [{}])[0] or {}).get("images") or []
            if not want_nsfw:
                if m.get("nsfw"):
                    continue
                # Same rule as the snapshot path: a model whose every sample is
                # explicit has no cover that can be shown, so it does not belong
                # in the adult-off view.
                if imgs and all(im.get("nsfw") for im in imgs):
                    continue
            have.add(mid)
            # _slim_snapshot, not slim_model: the extra rows have to carry the
            # same fields as snapshot rows (_created, _published, nsfw, _bases),
            # otherwise a time filter drops them as timestamp 0 and the NSFW
            # badge disappears.
            entry["items"].append(
                _with_covers(_slim_snapshot(m), _bundled_hashes()))
            added += 1
        if added:
            # Touch the entry: the cache is evicted oldest-first, and a list that
            # just grew is precisely the one worth keeping.
            entry["ts"] = time.time()
            print(f"[CivitaiHub] deep fill +{added} models ({types or 'All'})")
    except Exception:  # noqa: BLE001
        pass
    finally:
        _DEEP_FILLING.discard(key)


_NEWEST_CACHE: dict[str, Any] = {"items": [], "ts": 0.0}
# Ten minutes: the newest view exists to show fresh uploads, so its cache has to
# be short, but long enough that repeated opens reuse one fetch.
_NEWEST_TTL = 600.0
_NEWEST_DEPTH = 1200


async def _newest_fetch(target: int = _NEWEST_DEPTH) -> list[dict[str, Any]]:
    """Fetch the newest models plus their covers, once, on demand.

    Ran at startup so the first open of the newest tab is a paint instead of a
    cold network wait. Covers are downloaded with the ranged fetcher (several
    parallel byte ranges per image), which is the same path model files use.
    """
    items: list[dict[str, Any]] = []
    cursor = ""
    while len(items) < target:
        try:
            raw = await client.search("", limit=100, nsfw=True,
                                      sort="Newest", cursor=cursor)
        except Exception:  # noqa: BLE001
            break
        batch = raw.get("items") or []
        for m in batch:
            items.append(_slim_snapshot(m))
        cursor = (raw.get("metadata") or {}).get("nextCursor") or ""
        if not cursor or not batch:
            break
    if not items:
        return []
    _NEWEST_CACHE["items"] = items
    _NEWEST_CACHE["ts"] = time.time()

    async def warm(m: dict[str, Any]) -> None:
        ver = (m.get("versions") or [{}])[0] or {}
        imgs = ver.get("images") or []
        if not imgs:
            return
        url = imgs[0].get("url")
        if not url:
            return
        try:
            await client.fetch_thumb_range(url, _THUMB_STORE_WIDTH, 6)
        except Exception:  # noqa: BLE001
            pass

    sem = asyncio.Semaphore(4)

    async def guarded(m: dict[str, Any]) -> None:
        async with sem:
            await warm(m)

    await asyncio.gather(*(guarded(m) for m in items[:120]))
    print(f"[CivitaiHub] newest warm: {len(items)} models")
    return items


async def _bg_extend(key: str, term: str, want_nsfw: bool, types: str,
                     bases: list[str], sort_field: str, page_no: int,
                     page_size: int) -> None:
    """Top up a cached list in the background, ahead of the user reaching it.

    Keeps every filter combination a few hundred rows deep on disk, so paging is
    served from memory and only a background task ever waits on the network.
    """
    if key in _BG_FILLING:
        return
    _BG_FILLING.add(key)
    try:
        entry = _MERGE_CACHE.get(key)
        if not entry:
            return
        target = len(entry["items"]) + 200
        try:
            extra = await _build_browse(
                term, want_nsfw, types, bases, sort_field,
                page_no + 8, page_size, offset=len(entry["items"]), limit=200,
            )
        except Exception:  # noqa: BLE001
            return
        have = {i.get("id") for i in entry["items"]}
        grew = False
        for m in extra:
            if m.get("id") in have:
                continue
            have.add(m.get("id"))
            entry["items"].append(m)
            grew = True
        if grew:
            entry["ts"] = time.time()
            _save_merge_cache()
    finally:
        _BG_FILLING.discard(key)


def _save_merge_cache() -> None:
    path = _cache_path()
    if not path:
        return
    try:
        now = time.time()
        keep = {k: v for k, v in _MERGE_CACHE.items()
                if now - v.get("ts", 0) < _MERGE_TTL and v.get("items")}
        # Cap the file: 12 combinations is plenty and keeps it under a few MB.
        if len(keep) > 12:
            keep = dict(sorted(keep.items(), key=lambda kv: -kv[1]["ts"])[:12])
        path.write_text(json.dumps(keep, ensure_ascii=False), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


async def _prepend_new_arrivals(items: list[dict[str, Any]], want_nsfw: bool,
                                types: str, bases: list[str]) -> bool:
    """Fetch the newest uploads and splice any the cached list is missing.

    Separates freshness from caching: the list itself can live for an hour, while
    this cheap createdAt-ordered probe (one index query, no cursor walk) keeps the
    top of the list current.
    """
    key = json.dumps([want_nsfw, types, sorted(bases)], ensure_ascii=False)
    now = time.time()
    if now - _NEW_ARRIVAL_TS.get(key, 0.0) < _NEW_ARRIVAL_INTERVAL:
        return False
    _NEW_ARRIVAL_TS[key] = now
    try:
        fresh = await client.search_meili(
            "", limit=30, offset=0, nsfw=False, types=types, base_models=bases,
            sort_field="createdAt:desc",
        )
    except Exception:  # noqa: BLE001
        return False
    have = {i.get("id") for i in items}
    added = [m for m in (fresh.get("items") or []) if m.get("id") not in have]
    if not added:
        return False
    # Insert after the pinned block, never before it: splicing at index 0 put
    # zero-like uploads above FLUX and made the list look reordered.
    end = 0
    for idx in range(1, len(items)):
        prev = (items[idx - 1].get("stats") or {}).get("thumbsUpCount") or 0
        cur = (items[idx].get("stats") or {}).get("thumbsUpCount") or 0
        if cur > prev:
            end = idx
            break
    items[end:end] = added
    return True


_WARM_STARTED = False


def _ensure_warm() -> None:
    """Kick off the background adult-pool warm-up exactly once.

    Must be called from inside a running request handler: at import time
    ComfyUI's event loop does not exist yet, and a task created on a loop that
    never runs is silently dropped — which is why warming appeared not to work.
    """
    global _WARM_STARTED
    if _WARM_STARTED:
        return
    _WARM_STARTED = True
    try:
        asyncio.create_task(_nsfw_pool_warm())
        # Keeps the shipped ranking from going stale. Six-hour full pass; the
        # first one is delayed inside the task so it cannot fight the panel open.
        asyncio.create_task(_snapshot_refresh())
        client.start_probe()
        print("[CivitaiHub] warm-up + mirror probe started")
    except RuntimeError as exc:
        _WARM_STARTED = False
        print(f"[CivitaiHub] warm-up could not start: {exc}")


_WARM_COMBOS = [
    ("All", []),
    ("LORA", []),
    ("Checkpoint", []),
    ("LORA", ["Anima"]),
    ("LORA", ["Pony"]),
]
# Sort modes are separate queries too (Newest hits the index with a sort field,
# Most Downloaded goes through the v1 ranker), so each pays its own first-hit
# cost unless warmed.
_WARM_SORTS = ["Newest", "Most Downloaded"]

# Combinations the user actually used, most-recent first. The space is far too
# large to enumerate (14 types × 31 bases × sorts × periods), so the set is
# learned from real traffic and warmed on the next start.
_USED_COMBOS: dict[str, int] = {}
_USED_FILE = None


def _used_path():
    global _USED_FILE
    if _USED_FILE is None:
        try:
            from .hub_config import THUMB_DIR
            _USED_FILE = THUMB_DIR.parent / "used_combos.json"
        except Exception:  # noqa: BLE001
            _USED_FILE = False
    return _USED_FILE or None


def _load_used_combos() -> dict[str, int]:
    path = _used_path()
    if path and path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {str(k): int(v) for k, v in data.items()}
        except Exception:  # noqa: BLE001
            pass
    return {}


def _note_combo(types: str, bases: list[str]) -> None:
    """Remember that this (type, bases) pair was actually requested."""
    if _USED_COMBOS.get("_loaded") is None:
        _USED_COMBOS.update(_load_used_combos())
        _USED_COMBOS["_loaded"] = 1
    key = json.dumps([types, sorted(bases)], ensure_ascii=False)
    _USED_COMBOS[key] = _USED_COMBOS.get(key, 0) + 1
    path = _used_path()
    if path:
        try:
            clean = {k: v for k, v in _USED_COMBOS.items() if not k.startswith("_")}
            path.write_text(json.dumps(clean, ensure_ascii=False), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass


async def _prefetch_combos() -> None:
    """Warm the combos people open first, learned ones included.

    Each costs one server-side query (2-6s). They run concurrently, so wall
    time is roughly the slowest single one rather than the sum.
    """
    learned: list[tuple[str, list[str]]] = []
    try:
        raw = _load_used_combos()
        for key in sorted(raw, key=lambda k: -raw[k])[:6]:
            try:
                types, bases = json.loads(key)
                learned.append((str(types), [str(b) for b in bases]))
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        learned = []

    # Dedupe by a hashable key: the entries are (type, list-of-bases) and a list
    # cannot be a dict key, which made the whole prefetch abort with
    # "unhashable type: 'list'" and left every combination cold.
    seen_keys: set[str] = set()
    targets: list[tuple[str, list[str]]] = []
    for item in [*_WARM_COMBOS, *learned]:
        t, bs = item[0], list(item[1])
        k = f"{t}|{','.join(sorted(bs))}"
        if k in seen_keys:
            continue
        seen_keys.add(k)
        targets.append((t, bs))

    async def one(types: str, bases: list[str]) -> None:
        try:
            # 400 rows per combination: enough for ~16 pages straight from disk,
            # which is what makes filter switching feel instant.
            items = await _browse_unlimited("", True, types, bases, 400)
            if items:
                ck = _merge_key("", True, types, bases, "")
                _MERGE_CACHE[ck] = {"ts": time.time(), "items": items}
                _save_merge_cache()
                # Every warmed combination keeps a 300-cover floor, but only the
                # default browse views keep it permanently. A "newest" view rotates
                # its whole first page on every upload, so marking those core let
                # the cache grow without bound.
                try:
                    await _prefetch_combo_thumbs(
                        items, _THUMB_BASE_PER_COMBO,
                        "core" if _is_base_combo(types, bases, "") else "temp")
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            pass

    async def one_sort(mode: str) -> None:
        try:
            if mode == "Newest":
                await client.search_meili(
                    "", limit=_MEILI_WINDOW, offset=0, nsfw=False,
                    types="All", sort_field="createdAt:desc",
                )
            else:
                await client.search(
                    "", limit=100, nsfw=True, sort="Most Downloaded",
                )
        except Exception:  # noqa: BLE001
            pass

    await asyncio.gather(
        *(one(t, b) for t, b in targets),
        *(one_sort(m) for m in _WARM_SORTS),
    )
    print(f"[CivitaiHub] filter combinations prefetched ({len(targets)} combos)")


async def _nsfw_pool_warm() -> None:
    """Take the pool to the shallow depth only.

    Deep entries are pulled on demand as the user pages down (see
    _nsfw_pool_all). Warming the whole chain cost ~30s of startup for rows that
    most sessions never reach.
    """
    await asyncio.sleep(0.5)
    # Establish the mirror connection and DNS/TLS state early; the first real
    # query otherwise pays several seconds just for the handshake.
    try:
        await client.search_meili("", limit=10, offset=0, nsfw=False, types="All")
    except Exception:  # noqa: BLE001
        pass
    async with _NSFW_LOCK:
        if not _NSFW_POOL["items"] or time.time() - _NSFW_POOL["ts"] > _NSFW_TTL:
            _NSFW_POOL.update(items=[], cursor="", done=False, ts=0.0)
        if len(_NSFW_POOL["items"]) < _NSFW_FIRST_BATCHES * 100:
            await _nsfw_fill(_NSFW_FIRST_BATCHES)
    # The snapshot is a shipped asset, not something to regenerate at boot.
    # Crawling on every start replaced a complete 55k file with whatever the
    # crawl reached before timing out, and _snapshot_refresh re-sorted the list,
    # which shifted every page and invalidated the cover index. Boot only reads
    # it now; refreshing is an explicit action.
    try:
        asyncio.create_task(_prefetch_base_covers())
        # Warm the newest view too: fresh uploads rotate constantly, so this tab
        # can never be served from the main snapshot.
        asyncio.create_task(_newest_fetch())
    except RuntimeError:
        pass
    try:
        await _prefetch_combos()
        print("[CivitaiHub] filter combinations prefetched")
    except Exception as exc:  # noqa: BLE001
        print(f"[CivitaiHub] combo prefetch skipped: {exc}")


async def _browse_unlimited(term: str, want_nsfw: bool, types: str,
                            bases: list[str], need: int) -> list[dict[str, Any]]:
    """Site order, unlimited: pinned block from the index, then the v1 feed.

    The website's rule is "official block first, then likes descending". The
    index only knows its first 1000 rows, but the v1 feed is like-ordered and
    has no ceiling — so the pinned block comes from the index and everything
    after it comes from v1. Together they reproduce the site order while
    covering the entire catalogue.
    """
    items: list[dict[str, Any]] = []

    # 1) Pinned block: the site index's leading run, which is strictly
    #    decreasing by likes. The first rise marks its end.
    if not term.strip():
        try:
            head = await client.search_meili(
                "", limit=100, offset=0, nsfw=False, types=types, base_models=bases,
            )
            rows = list(head.get("items") or [])
            end = len(rows)
            for i in range(1, len(rows)):
                prev = (rows[i - 1].get("stats") or {}).get("thumbsUpCount") or 0
                cur = (rows[i].get("stats") or {}).get("thumbsUpCount") or 0
                if cur > prev:
                    end = i
                    break
            items = rows[:end]
        except Exception:  # noqa: BLE001
            items = []

    # 2) Everything after the pinned block, in like order, as deep as needed.
    #    Only top up what this page actually needs: pulling the whole depth at
    #    once turned a single scroll into a 35s stall.
    seen = {i.get("id") for i in items}
    try:
        shortfall = need - len(items)
        want = len(items) + max(shortfall, 0) + 100
        deep = await _deep_items(want_nsfw, want, types, bases)
        for row in deep:
            if row.get("id") in seen:
                continue
            seen.add(row.get("id"))
            items.append(slim_model(row))
    except Exception:  # noqa: BLE001
        pass

    return items


async def _build_browse(term: str, want_nsfw: bool, types: str,
                        bases: list[str], sort_field: str,
                        page_no: int = 1, page_size: int = 24,
                        offset: int = 0, limit: int = 0) -> list[dict[str, Any]]:
    """Build (or extend) the browse list; callers cache the result."""
    # Browsing (no keyword) uses the unlimited path: pinned block + like-ordered
    # v1 feed, so depth is limited only by how far the user pages.
    if not term.strip() and not sort_field:
        _note_combo(types, bases)
        need = max(_MEILI_WINDOW, (page_no + 2) * page_size)
        return await _browse_unlimited(term, want_nsfw, types, bases, need)

    pool = limit or _MEILI_WINDOW
    if offset:
        pool = min(1000, offset + pool)
    meili = await client.search_meili(
        term, limit=pool, offset=offset, nsfw=False,
        types=types, base_models=bases, sort_field=sort_field,
    )
    items: list[dict[str, Any]] = list(meili["items"])
    if not want_nsfw:
        return items
    if offset:
        # The adult pool is merged into the prefix on the first build; a later
        # slice only needs the fresh tail.
        return items
    adult_rows = await _nsfw_pool_all(max(1, page_no // 4 + 1))

    # v1's baseModels filter is unreliable: it misses models whose *first*
    # version is Anima (e.g. "Bondage Suspension", 11152 likes). So the pool is
    # filtered locally instead — on type and on every version's baseModel.

    def _matches(m: dict[str, Any]) -> bool:
        if not m.get("nsfw"):
            return False
        if types and types != "All" and (m.get("type") or "") != types:
            return False
        if bases:
            vers = m.get("modelVersions") or []
            if not any((v.get("baseModel") or "") in bases for v in vers):
                return False
        return True

    # Filtering + converting 500-odd pool entries per query was the bulk of the
    # latency on a new filter combination. The result only depends on
    # (types, bases), so keep it.
    af_key = json.dumps([types, sorted(bases)], ensure_ascii=False)
    cached_adults = _ADULT_FILTER_CACHE.get(af_key)
    if cached_adults is not None and time.time() - _ADULT_FILTER_TS.get(af_key, 0) > _ADULT_FILTER_TTL:
        cached_adults = None
        _ADULT_FILTER_CACHE.pop(af_key, None)
    if cached_adults is not None:
        adults = list(cached_adults)
    else:
        needle = term.strip().lower()
        seen = {i.get("id") for i in items}
        adults = []
        for row in adult_rows:
            if not _matches(row):
                continue
            slim = slim_model(row)
            if slim["id"] in seen:
                continue
            if needle:
                hay = (slim["name"] + " " + " ".join(slim.get("tags") or [])).lower()
                if needle not in hay:
                    continue
            seen.add(slim["id"])
            adults.append(slim)
        if not term.strip():
            _ADULT_FILTER_CACHE[af_key] = list(adults)
            _ADULT_FILTER_TS[af_key] = time.time()

    if not adults:
        return items

    adults.sort(key=lambda m: -((m.get("stats") or {}).get("thumbsUpCount") or 0))
    # The pinned block's signature is strictly decreasing likes; the first rise
    # marks its end. Account name alone misses partners like circlestone_labs.
    pinned_end = 0
    for idx in range(1, len(items)):
        prev_likes = (items[idx - 1].get("stats") or {}).get("thumbsUpCount") or 0
        cur_likes = (items[idx].get("stats") or {}).get("thumbsUpCount") or 0
        if cur_likes > prev_likes:
            pinned_end = idx
            break

    merged = list(items[:pinned_end])
    ai = 0
    for itm in items[pinned_end:]:
        likes = (itm.get("stats") or {}).get("thumbsUpCount") or 0
        while ai < len(adults) and (
            (adults[ai].get("stats") or {}).get("thumbsUpCount") or 0
        ) > likes:
            merged.append(adults[ai])
            ai += 1
        merged.append(itm)
    merged.extend(adults[ai:])
    return merged


async def _extend_deep(items: list[dict[str, Any]], want_nsfw: bool,
                       target: int, types: str = "All",
                       bases: list[str] | None = None) -> bool:
    """Append v1-only rows until `items` reaches `target`. Returns True if grown.

    `types` and `bases` are parameters, not free variables: the body used to read
    names that only existed in the caller's scope, so any call raised NameError.
    """
    if len(items) >= target:
        return False
    have = {i.get("id") for i in items}
    grew = False
    fetched = 0
    for _ in range(8):
        need = max(fetched + 100, target)
        deep = await _deep_items(want_nsfw, need, types, bases or None)
        for m in deep:
            if m.get("id") in have:
                continue
            have.add(m.get("id"))
            items.append(slim_model(m))
            grew = True
        if len(items) >= target or len(deep) <= fetched:
            break
        fetched = len(deep)
    return grew


# --------------------------------------------------------------------- routes
_VERSION_TTL = 1800.0
_VERSION_CACHE: dict[int, dict[str, Any]] = {}


async def _version_detail(version_id: int, force: bool = False) -> dict[str, Any]:
    """Full version payload, including every image and its generation meta.

    Cached because a detail page asks for it once for the gallery and again for
    each image the user clicks; without this every click waited 2-5s on the
    network before the parameters appeared.
    """
    key = int(version_id)
    hit = _VERSION_CACHE.get(key)
    if hit and not force and time.time() - hit["ts"] < _VERSION_TTL:
        return hit["data"]
    data = await client.api(f"/api/v1/model-versions/{key}")
    if isinstance(data, dict):
        _VERSION_CACHE[key] = {"ts": time.time(), "data": data}
    return data


def _is_loopback(request: web.Request) -> bool:
    """True only for a request that came from this machine.

    Needed because the config endpoints read and write credentials. ComfyUI can
    be started with --listen, and then every host on the LAN reaches these routes
    too; the old code trusted a comment claiming the endpoint was local-only
    while performing no check at all.
    """
    peer = (request.remote or "").strip()
    if peer in ("127.0.0.1", "::1", "localhost"):
        return True
    # IPv4-mapped IPv6 form used by dual-stack sockets.
    return peer.startswith("::ffff:127.")


def _require_local(request: web.Request) -> None:
    if not _is_loopback(request):
        raise web.HTTPForbidden(text="only available from localhost")


def register_routes() -> None:
    from server import PromptServer  # imported lazily so tests can import us

    routes = PromptServer.instance.routes

    # Warm the adult pool while the aiohttp application is starting, and block
    # until it is ready. Async warming looked fine but the server accepted
    # requests before the pool finished, so the first panel open still paid the
    # full fetch. Waiting here trades startup time for an instant first click.
    try:
        _app = getattr(PromptServer.instance, "app", None)
        if _app is not None:
            _load_merge_cache()
            _seed_snapshot_if_missing()
            _load_snapshot()
            async def _warm_on_startup(_application) -> None:
                # Housekeeping first, before anything starts writing again:
                # drop everything that is not marked core (crash leftovers, covers
                # for one-off scrolls). Core entries are kept regardless of age —
                # an old cover is still the cover the user wants to see instantly,
                # so age alone is never a reason to throw it away.
                try:
                    r1, f1 = _prune_cache("temp")
                    r2, f2 = _prune_download_temp()
                    if r1 or r2:
                        print(f"[CivitaiHub] cache pruned: {r1 + r2} files, "
                              f"{(f1 + f2) // 1024 // 1024} MB")
                except Exception as exc:  # noqa: BLE001
                    print(f"[CivitaiHub] cache prune failed: {exc}")
                try:
                    await asyncio.wait_for(_nsfw_pool_warm(), timeout=90)
                    print("[CivitaiHub] adult pool ready")
                except asyncio.TimeoutError:
                    # Do not give up: keep filling in the background so the pool
                    # still converges even when the mirror is slow right now.
                    print("[CivitaiHub] warm-up timed out; continuing in background")
                    asyncio.create_task(_nsfw_pool_warm())
                except Exception as exc:  # noqa: BLE001
                    print(f"[CivitaiHub] adult pool warm-up failed: {exc}")
                try:
                    client.start_probe()
                except Exception:  # noqa: BLE001
                    pass

            _app.on_startup.append(_warm_on_startup)
    except Exception:  # noqa: BLE001 - warming is an optimisation, never fatal
        pass

    @routes.get("/civitai_hub/ping")
    async def _ping(request: web.Request) -> web.Response:
        # The panel calls this as soon as the ComfyUI page loads, which is well
        # before the user opens it. Starting the warm-up here means the adult
        # pool is ready by the time a search actually happens.
        _ensure_warm()
        return web.json_response({"ok": True, "plugin": "ComfyUI-CivitaiHub"})

    @routes.get("/civitai_hub/config")
    async def _get_config(request: web.Request) -> web.Response:
        _require_local(request)
        return web.json_response(config.as_dict())

    @routes.post("/civitai_hub/config")
    async def _set_config(request: web.Request) -> web.Response:
        _require_local(request)
        try:
            patch = await request.json()
        except Exception:
            raise web.HTTPBadRequest(text="invalid json")
        if not isinstance(patch, dict):
            raise web.HTTPBadRequest(text="expected an object")
        if patch.get("civitai_api_key") == "***":
            patch.pop("civitai_api_key")
        for list_key in ("api_mirrors", "image_proxies", "b2_endpoints"):
            if list_key in patch:
                value = patch[list_key]
                if isinstance(value, str):
                    value = [v.strip() for v in re.split(r"[\s,]+", value) if v.strip()]
                patch[list_key] = [str(v).strip() for v in (value or []) if str(v).strip()]
        for num_key in ("thumb_width", "max_concurrent_downloads"):
            if num_key in patch:
                try:
                    patch[num_key] = int(patch[num_key])
                except (TypeError, ValueError):
                    patch.pop(num_key)
        if "nsfw" in patch:
            patch["nsfw"] = bool(patch["nsfw"])
        config.update(patch)
        return web.json_response(config.as_dict())

    @routes.get("/civitai_hub/health")
    async def _health(request: web.Request) -> web.Response:
        try:
            return web.json_response(await client.health())
        except Exception as exc:  # noqa: BLE001
            return web.json_response({"error": f"{type(exc).__name__}: {exc}"},
                                     status=500)

    @routes.get("/civitai_hub/search")
    async def _search(request: web.Request) -> web.Response:
        _ensure_warm()
        q = request.query
        site = (q.get("site") or "civitai").lower()
        if site != "civitai":
            from .hub_providers import PROVIDERS

            provider = PROVIDERS.get(site)
            if provider is None:
                return web.json_response({"error": f"unknown site: {site}"}, status=400)
            try:
                result = await asyncio.wait_for(
                    provider.search(
                        q.get("query", ""),
                        page=int(q.get("page", 1)),
                        limit=int(q.get("limit", 24)),
                        types=q.get("types", "All"),
                    ),
                    timeout=40,
                )
            except asyncio.TimeoutError:
                return web.json_response(
                    {"error": "镜像响应超时（HuggingFace 通道限流），稍后重试",
                     "items": []}, status=200)
            except Exception as exc:  # noqa: BLE001
                return web.json_response({"error": f"{type(exc).__name__}: {exc}",
                                          "items": []}, status=502)
            result.setdefault("items", [])
            return web.json_response(result)
        try:
            want_nsfw = q.get("nsfw") == "true"
            sort_choice = (q.get("sort") or "").strip()
            # Every sort except "Newest" is answered from the snapshot; that one
            # needs live data because it exists to show the last few minutes.
            # The old condition was `not sort_choice or sort_choice == "Newest"`,
            # which silently pushed "Most Downloaded" onto the network path even
            # though _snapshot_query already sorts by download count — that view
            # returned total_items=None and paid a live query per page.
            # Parsed before the branch below: the branch condition reads `term`,
            # and leaving the assignment inside it crashed with UnboundLocalError
            # the moment the first clause was false (i.e. for every newest view).
            page_no = max(1, int(q.get("page", 1)))
            page_size = max(1, min(int(q.get("limit", 24)), 100))
            bases = [b for b in (q.get("base_models") or "").split(",") if b]
            term = q.get("query", "")
            types = q.get("types", "All")
            sort_field = "createdAt:desc" if sort_choice == "Newest" else ""
            key = _merge_key(term, want_nsfw, types, bases, sort_choice)

            # "Newest" with no search term enters this block so its own cached
            # branch below is reachable at all; it used to sit inside a condition
            # that excluded "Newest", so that branch was dead code and every
            # newest request fell through to a live v1 call with no total.
            if sort_choice != "Newest" or not term.strip():

                # Snapshot path: every combination, sort and period is answered
                # from memory. "Newest" is excluded on purpose: a snapshot is
                # minutes stale by design, and that view exists to show what was
                # uploaded in the last few minutes. It queries the live index and
                # fetches covers concurrently instead.
                # "Newest" is served from its own short-lived cache, warmed at
                # startup with covers already on disk. Falling through to the
                # live v1 query means a cold wait of several seconds per open.
                # Only the unfiltered view uses the cache. With a base model or
                # type filter, the 1200-row cache would be cut down to a handful
                # of rows, so those fall through to v1, which filters server-side
                # and returns the full set.
                if (sort_choice == "Newest" and not term.strip()
                        and not bases and (not types or types == "All")):
                    fresh = time.time() - float(_NEWEST_CACHE.get("ts") or 0) < _NEWEST_TTL
                    cached_items = list(_NEWEST_CACHE.get("items") or [])
                    if not (fresh and cached_items):
                        cached_items = await _newest_fetch()
                    if cached_items:
                        picked = cached_items
                        if types and types != "All":
                            picked = [m for m in picked if m.get("type") == types]
                        days = {"Newest": 7}.get(q.get("period") or "Newest", 7)
                        cutoff = time.time() - days * 86400

                        def _st(m):
                            iso = m.get("_published") or m.get("_created") or ""
                            try:
                                return datetime.fromisoformat(
                                    iso.replace("Z", "+00:00")).timestamp()
                            except Exception:  # noqa: BLE001
                                return 0.0

                        picked = [m for m in picked if _st(m) >= cutoff]
                        start = (page_no - 1) * page_size
                        window = picked[start:start + page_size]
                        # Return the empty tail instead of falling through: a
                        # fallthrough switched the response to a different data
                        # source mid-scroll, so continuing down the list suddenly
                        # showed unrelated rows (it looked like a big skipped gap).
                        if window or start < len(picked) or page_no > 1:
                            return web.json_response({
                                "items": window,
                                "page": page_no,
                                "total_pages": (len(picked) + page_size - 1) // page_size,
                                "total_items": len(picked),
                                "next_cursor": "",
                                "has_more": start + page_size < len(picked),
                                "channel": "newest",
                            })

                if _SNAPSHOT["items"] and sort_choice != "Newest":
                    _t_query = time.time()
                    # Reuse the filtered + sorted list across pages. Rebuilding it
                    # for every page cost 0.4s each time and made paging feel like
                    # a network round trip even though nothing was fetched.
                    _qk = f"{term}|{types}|{','.join(sorted(bases))}|{want_nsfw}|{sort_choice}"
                    _qhit = _QUERY_CACHE.get(_qk)
                    if _qhit and time.time() - _qhit["ts"] < 300:
                        merged = _qhit["items"]
                        _cache_state = "hit"
                    else:
                        merged = _snapshot_query(term, types, bases, want_nsfw,
                                                 sort_choice)
                        _QUERY_CACHE[_qk] = {"ts": time.time(), "items": merged}
                        _cache_state = "miss"
                        # Three combinations at most. The list carries the rows
                        # pulled from v1 behind the snapshot's edge, and those are
                        # meant to be temporary: switching to a fourth filter
                        # drops the oldest one, so browsing never accumulates a
                        # growing pile of borrowed rows.
                        if len(_QUERY_CACHE) > 3:
                            for _k in sorted(_QUERY_CACHE,
                                             key=lambda k: _QUERY_CACHE[k]["ts"])[:-3]:
                                _QUERY_CACHE.pop(_k, None)
                                # Drop the fill mark with the entry, otherwise the
                                # combination can never be topped up again: the
                                # guard would still be set and every later request
                                # would skip the fill.
                                _DEEP_FILLING.discard(_k)
                    period = q.get("period", "AllTime")
                    # "Newest" is the site's newest-first view, which only spans
                    # recent uploads. Treating it as a pure sort returned the whole
                    # catalogue in reverse order, so the result count looked wrong.
                    days = {"Newest": 7, "Day": 1, "Week": 7,
                            "Month": 30, "Year": 365}.get(period, 0)
                    if days:
                        cutoff = time.time() - days * 86400

                        def _stamp(m):
                            iso = m.get("_published") or m.get("_created") or ""
                            try:
                                return datetime.fromisoformat(
                                    iso.replace("Z", "+00:00")).timestamp()
                            except Exception:  # noqa: BLE001
                                return 0.0

                        merged = [m for m in merged if _stamp(m) >= cutoff]
                    start = (page_no - 1) * page_size
                    window = merged[start:start + page_size]
                    if window or start < len(merged):
                        # Row prefetch: same idea as the cover refill below, but
                        # on data. The snapshot is a fixed 55k file, so a narrow
                        # combination runs out of rows well before the site does;
                        # once the scroll nears the end of what is loaded, the
                        # next block is pulled from v1's cursor chain and
                        # appended to this cached list. A row costs ~11ms against
                        # ~1s for a cover, so this one keeps up easily.
                        #
                        # Only for an empty search term: _deep_items queries v1
                        # with q="" and keys its cache without the term, so under
                        # a search it would append models that do not match.
                        #
                        # The appended rows are temporary: they belong to this
                        # _QUERY_CACHE entry and go away with it.
                        try:
                            if (not term.strip()
                                    and len(merged)
                                    and start + page_size * 4 >= len(merged)
                                    and _qk not in _DEEP_FILLING):
                                _DEEP_FILLING.add(_qk)
                                # Follow the view's own ordering and time window:
                                # "Most Downloaded" walks a different chain than
                                # likes, and a Day/Week view must not pull
                                # all-time rows that the filter then discards.
                                asyncio.create_task(_deep_fill(
                                    _qk, types, bases or [], want_nsfw,
                                    len(merged) + _DEEP_BLOCK * 3,
                                    "Most Downloaded"
                                    if sort_choice == "Most Downloaded"
                                    else "Most Liked",
                                    period))
                        except RuntimeError:
                            _DEEP_FILLING.discard(_qk)
                        # Scroll-driven cover refill, one _THUMB_BASE_PER_COMBO block
                        # at a time, starting where the bundled floor ends. The
                        # next block is queued when the scroll reaches
                        # _THUMB_REFILL_RATIO of the block that is downloading
                        # now, i.e. its own first 30%:
                        #   301-600  -> next trigger 301 + 90 = 391
                        #   601-900  -> next trigger 601 + 90 = 691
                        #   901-1200 -> next trigger 901 + 90 = 991
                        # _COMBO_WARMING keeps a fast scroll from queueing one
                        # block per page request while a block is still running.
                        try:
                            _warmed_to = _COMBO_WARMED_TO.get(
                                _qk, _THUMB_BASE_PER_COMBO)
                            _trigger = _COMBO_TRIGGER.get(
                                _qk, _THUMB_BASE_PER_COMBO)
                            if (_qk not in _COMBO_WARMING
                                    and start + page_size >= _trigger):
                                _next = _warmed_to + _THUMB_BASE_PER_COMBO
                                _COMBO_WARMED_TO[_qk] = _next
                                # 30% into the block that was just queued.
                                _COMBO_TRIGGER[_qk] = _warmed_to + int(
                                    _THUMB_BASE_PER_COMBO * _THUMB_REFILL_RATIO)
                                _COMBO_WARMING.add(_qk)
                                asyncio.create_task(_prefetch_block(
                                    _qk, merged[_warmed_to:_next],
                                    _THUMB_BASE_PER_COMBO))
                        except RuntimeError:
                            _COMBO_WARMING.discard(_qk)
                        return web.json_response({
                            "items": window,
                            "page": page_no,
                            "total_pages": (len(merged) + page_size - 1) // page_size,
                            "total_items": len(merged),
                            "next_cursor": "",
                            "has_more": start + page_size < len(merged),
                            "channel": "snapshot",
                        }, headers={
                            "X-Snap-Cache": _cache_state,
                            "X-Snap-Ms": f"{(time.time() - _t_query) * 1000:.1f}",
                        })

                entry = _MERGE_CACHE.get(key)
                need = page_no * page_size
                fresh = entry and time.time() - entry["ts"] < _MERGE_TTL
                if fresh and len(entry["items"]) >= need:
                    items = entry["items"]
                    # Cached list, but check occasionally for uploads it predates.
                    if page_no == 1 and not term.strip():
                        if await _prepend_new_arrivals(items, want_nsfw, types, bases):
                            _MERGE_CACHE[key] = {"ts": entry["ts"], "items": items}
                else:
                    # Grow by appending: rebuilding from scratch would reorder
                    # the prefix the client already rendered, which reads as
                    # duplicated cards further down.
                    if fresh and entry["items"]:
                        items = entry["items"]
                        extra = await _build_browse(
                            term, want_nsfw, types, bases, sort_field,
                            page_no, page_size, offset=len(items),
                            limit=max(need - len(items), 100),
                        )
                        have = {i.get("id") for i in items}
                        for m in extra:
                            if m.get("id") not in have:
                                have.add(m.get("id"))
                                items.append(m)
                    else:
                        items = await _build_browse(term, want_nsfw, types, bases,
                                                    sort_field, page_no, page_size)
                    _MERGE_CACHE[key] = {"ts": time.time(), "items": items}

                page_no = max(1, int(q.get("page", 1)))
                start = (page_no - 1) * page_size
                # Cursor paging by id: the client sends the last id it already
                # has, and we resume right after it. Index offsets can shift when
                # the merged list grows, but an id either exists or it does not —
                # so this cannot hand back a card the client already rendered.
                after = (q.get("after") or "").strip()
                if after:
                    pos = None
                    for idx, itm in enumerate(items):
                        if str(itm.get("id")) == after:
                            pos = idx
                            break
                    if pos is None:
                        return web.json_response({
                            "items": [], "page": page_no, "total_pages": None,
                            "total_items": len(items), "next_cursor": "",
                            "has_more": False, "channel": "site", "reset": True,
                        })
                    start = pos + 1
                # Past the index ceiling keep appending v1-only rows. The list
                # only ever grows, so earlier offsets stay valid.
                if start + page_size > len(items) and not term.strip():
                    # First try the index itself (keeps the site ordering), then
                    # fall back to the v1 feed once it is exhausted.
                    grown = False
                    try:
                        more = await _build_browse(
                            term, False, types, bases, sort_field,
                            page_no, page_size, offset=len(items),
                            limit=max(page_size * 4, 100),
                        )
                        have = {i.get("id") for i in items}
                        for m in more:
                            if m.get("id") not in have:
                                have.add(m.get("id"))
                                items.append(m)
                                grown = True
                    except Exception:  # noqa: BLE001
                        pass
                    if grown or await _extend_deep(items, want_nsfw,
                                                   start + page_size,
                                                   types, bases):
                        _MERGE_CACHE[key] = {"ts": time.time(), "items": items}

                window = items[start:start + page_size]
                # Only the default view's floor is permanent. "Newest" in
                # particular rotates its whole first page every few hours, so
                # marking those core would let the cache grow forever with covers
                # nobody looks at again.
                permanent = _is_base_combo(types, bases, sort_choice)
                kind = "core" if permanent else "temp"
                if page_no == 1 and key not in _COMBO_WARMED:
                    _COMBO_WARMED.add(key)
                    try:
                        asyncio.create_task(_prefetch_combo_thumbs(
                            list(items), _THUMB_BASE_PER_COMBO, kind))
                    except RuntimeError:
                        pass
                elif start >= _THUMB_BASE_PER_COMBO * _THUMB_REFILL_RATIO:
                    try:
                        asyncio.create_task(_prefetch_combo_thumbs(
                            list(items[start:start + page_size * 2]),
                            page_size * 2, "temp"))
                    except RuntimeError:
                        pass
                # Past 70% of what we hold, refill in the background so the next
                # pages never wait on the network.
                if page_no * page_size > len(items) * 0.7 and not term.strip():
                    try:
                        asyncio.create_task(_bg_extend(
                            key, term, want_nsfw, types, bases, sort_field,
                            page_no, page_size,
                        ))
                    except RuntimeError:
                        pass
                return web.json_response({
                    "items": window,
                    "page": page_no,
                    "total_pages": (len(items) + page_size - 1) // page_size,
                    "total_items": len(items),
                    "next_cursor": "",
                    "has_more": start + page_size < len(items),
                    "channel": "site",
                })
            raw = await client.search(
                q.get("query", ""),
                types=q.get("types", "All"),
                sort=q.get("sort") or ("Most Liked" if want_nsfw else ""),
                period=q.get("period", "AllTime"),
                page=int(q.get("page", 1)),
                limit=int(q.get("limit", 24)),
                nsfw=None if q.get("nsfw") is None else q.get("nsfw") == "true",
                base_models=[b for b in (q.get("base_models") or "").split(",") if b],
                username=q.get("username", ""),
                tag=q.get("tag", ""),
                cursor=q.get("cursor", ""),
                sort_by=q.get("sortBy", "models_v9"),
            )
        except ChannelError as exc:
            return web.json_response({"error": str(exc)}, status=502)
        except Exception as exc:  # noqa: BLE001
            return web.json_response({"error": f"{type(exc).__name__}: {exc}"},
                                     status=500)
        items = [slim_model(m) for m in (raw.get("items") or [])]
        meta = raw.get("metadata") or {}
        return web.json_response(
            {
                "items": items,
                "page": meta.get("currentPage", q.get("page", 1)),
                "total_pages": meta.get("totalPages"),
                "total_items": meta.get("totalItems"),
                "next_cursor": meta.get("nextCursor") or "",
            }
        )

    @routes.get("/civitai_hub/model")
    @routes.get("/civitai_hub/model/{model_id}")
    async def _model(request: web.Request) -> web.Response:
        site = (request.query.get("site") or "civitai").lower()
        model_id = request.query.get("id") or request.match_info.get("model_id") or ""
        if site != "civitai":
            from .hub_providers import PROVIDERS

            provider = PROVIDERS.get(site)
            if provider is None:
                return web.json_response({"error": f"unknown site: {site}"}, status=400)
            try:
                return web.json_response(await provider.detail(model_id))
            except Exception as exc:  # noqa: BLE001
                return web.json_response({"error": f"{type(exc).__name__}: {exc}"},
                                         status=502)
        try:
            mid = int(model_id)
            raw = await client.model(mid)
        except ChannelError as exc:
            return web.json_response({"error": str(exc)}, status=502)
        except Exception as exc:  # noqa: BLE001
            return web.json_response({"error": f"{type(exc).__name__}: {exc}"},
                                     status=500)
        data = slim_model(raw, full=True)
        _restore_stills(data, mid)
        return web.json_response(data)

    @routes.get("/civitai_hub/base_models")
    async def _base_models(request: web.Request) -> web.Response:
        """Base models for the filter dropdown.

        Live probes alone are far too narrow: measured 2026-11-23, four probes
        covered 32 of the 96 base models the snapshot actually holds, so two
        thirds of the catalogue could not be filtered and a newly released base
        model never showed up at all. The snapshot is the wider source — every
        row carries its base model — so the dropdown is the union of the two,
        with counts taken from the snapshot because the probe numbers are only
        sample sizes.

        The probes still matter: they are the only thing that notices a base
        model before any of its models reach the snapshot.
        """
        refresh = request.query.get("refresh") == "1"
        counts: dict[str, int] = {}
        for m in _SNAPSHOT["items"]:
            row = m["_official"] if "_official" in m else m.get("_bases")
            for base in row or []:
                name = (base or "").strip()
                if name:
                    counts[name] = counts.get(name, 0) + 1
        error = ""
        probe = client.base_models_cached()
        for item in probe:
            name = (item.get("name") or "").strip()
            if name:
                counts.setdefault(name, 0)
        if not probe or refresh:
            # Never make the request wait for the probes: they cost six round
            # trips (measured 4.4s cold) while the snapshot half above already
            # answers. Warm them for the next call instead.
            try:
                asyncio.create_task(client.base_models(refresh=refresh))
            except RuntimeError as exc:
                error = f"probe not started: {exc}"
        items = [{"name": n, "count": c} for n, c in counts.items()]
        items.sort(key=lambda kv: (-kv["count"], kv["name"]))
        payload: dict[str, Any] = {"items": items, "source": "snapshot+probe"}
        if error:
            payload["error"] = error
        return web.json_response(payload)

    @routes.get("/civitai_hub/sites")
    async def _sites(request: web.Request) -> web.Response:
        # Only Civitai is exposed. The HuggingFace/Liblib adapters are kept in
        # hub_providers.py for future use but are intentionally not listed.
        return web.json_response({"items": [
            {"key": "civitai", "label": "Civitai", "configured": True,
             "needs_account": False},
        ]})

    @routes.post("/civitai_hub/site_test")
    async def _site_test(request: web.Request) -> web.Response:
        from .hub_providers import PROVIDERS, SiteNotConfigured

        try:
            body = await request.json()
        except Exception:
            raise web.HTTPBadRequest(text="invalid json")
        site = str(body.get("site") or "")
        provider = PROVIDERS.get(site)
        if provider is None:
            return web.json_response({"ok": False, "error": f"unknown site: {site}"},
                                     status=400)
        try:
            result = await asyncio.wait_for(
                provider.search(str(body.get("query") or "lora"), limit=5), timeout=30
            )
        except SiteNotConfigured as exc:
            return web.json_response({"ok": False, "error": str(exc)})
        except asyncio.TimeoutError:
            return web.json_response({"ok": False, "error": "响应超时"})
        except Exception as exc:  # noqa: BLE001
            return web.json_response({"ok": False,
                                      "error": f"{type(exc).__name__}: {exc}"})
        items = result.get("items") or []
        return web.json_response({
            "ok": bool(items),
            "count": len(items),
            "sample": [i.get("name") for i in items[:3]],
            "error": "" if items else "鉴权通过但没有解析到模型列表",
        })

    @routes.get("/civitai_hub/version_detail")
    async def _version_detail_route(request: web.Request) -> web.Response:
        """All images of one version, for the detail page's gallery.

        The snapshot only carries a handful of images per model (six, from the
        primary version), so switching to another version showed an almost empty
        strip. This returns the requested version's full image list; the frontend
        calls it for the version currently selected and nothing else, so only what
        is on screen is ever fetched.
        """
        try:
            version_id = int(request.query.get("version_id", 0))
        except ValueError:
            raise web.HTTPBadRequest(text="bad version_id")
        if not version_id:
            raise web.HTTPBadRequest(text="version_id required")
        try:
            data = await _version_detail(version_id)
        except Exception as exc:  # noqa: BLE001
            return web.json_response({"error": str(exc), "images": []}, status=200)
        imgs = []
        for im in (data.get("images") or []):
            url = im.get("url") or ""
            low = url.lower().split("?")[0]
            if not low.endswith((".jpeg", ".jpg", ".png", ".webp")):
                continue
            # id/hash are usually absent from this payload; the url is the only
            # stable handle, and the meta matcher keys on it.
            imgs.append({
                "url": url,
                "id": im.get("id"),
                "hash": im.get("hash"),
                "nsfw": (im.get("nsfwLevel") or 0) >= 4,
                "hasMeta": bool(im.get("meta")) or bool(im.get("hasMeta")),
                "width": im.get("width"),
                "height": im.get("height"),
            })
        return web.json_response({
            "images": imgs,
            "baseModel": data.get("baseModel"),
            "trainedWords": data.get("trainedWords") or [],
        })

    @routes.get("/civitai_hub/image_meta")
    async def _image_meta(request: web.Request) -> web.Response:
        """Fetch per-image generation data.

        ``/api/v1/images`` strips ``meta`` for anonymous callers, but
        ``/api/v1/model-versions/{id}`` returns its images *with* meta intact —
        no login needed. So the version payload is the primary source and the
        images endpoint is only a fallback.
        """
        version_id = request.query.get("version_id")
        want_hash = request.query.get("hash") or ""
        want_url = request.query.get("url") or ""
        image_id = request.query.get("image_id")
        try:
            item: dict[str, Any] | None = None
            pool: list[dict[str, Any]] = []

            if version_id:
                try:
                    data = await _version_detail(int(version_id))
                    pool = [i for i in (data.get("images") or []) if i.get("meta")]
                except Exception:  # noqa: BLE001
                    pool = []

            if not pool and image_id:
                data = await client.api("/api/v1/images", {"imageId": int(image_id)})
                pool = [i for i in (data.get("items") or []) if i.get("meta")]
            if not pool and version_id:
                data = await client.api(
                    "/api/v1/images",
                    {"modelVersionId": int(version_id), "limit": 100, "nsfw": "X"},
                )
                pool = [i for i in (data.get("items") or []) if i.get("meta")]

            if pool:
                if want_hash:
                    item = next((i for i in pool if i.get("hash") == want_hash), None)
                if item is None and want_url:
                    tail = want_url.split("?")[0].rsplit("/", 1)[-1]
                    item = next((i for i in pool
                                 if tail and tail in (i.get("url") or "")), None)
                # No fallback to pool[0]: returning the first image's parameters
                # for a different picture is worse than saying there are none.
                # The caller already knows which image it asked about.
        except ChannelError as exc:
            return web.json_response({"meta": None, "error": str(exc)}, status=200)
        except Exception as exc:  # noqa: BLE001
            return web.json_response({"meta": None,
                                      "error": f"{type(exc).__name__}: {exc}"})

        if not item:
            return web.json_response({"meta": None, "has_meta": False,
                                      "needs_key": False, "image": {}})
        meta = item.get("meta")
        return web.json_response({
            "meta": meta,
            "has_meta": bool(item.get("hasMeta")),
            "needs_key": False,
            "image": {
                "width": item.get("width"),
                "height": item.get("height"),
                "username": item.get("username"),
                "nsfwLevel": item.get("nsfwLevel"),
                "baseModel": item.get("baseModel"),
                "hash": item.get("hash"),
                "id": item.get("id"),
            },
        })

    @routes.get("/civitai_hub/thumb")
    async def _thumb(request: web.Request) -> web.Response:
        origin = request.query.get("url", "")
        if not origin.startswith(("http://", "https://")):
            raise web.HTTPBadRequest(text="url must be http(s)")
        try:
            width = int(request.query.get("w", 0)) or None
        except ValueError:
            width = None
        kind = "core" if request.query.get("core") == "1" else "temp"
        # Large previews go through the ranged fetch: wsrv serialises one request
        # per image, so a 900px hero is the same bottleneck as a model file.
        seg = int(request.query.get("seg", 0) or 0)
        if seg > 1 and (width or 0) >= 600:
            result = await client.fetch_thumb_range(origin, width, seg)
        else:
            result = await client.fetch_thumb(origin, width, kind)
        if result is None:
            raise web.HTTPNotFound(text="thumbnail unavailable")
        data, ctype = result
        return web.Response(
            body=data,
            content_type=ctype.split(";")[0].strip() or "image/jpeg",
            headers={"Cache-Control": "public, max-age=604800"},
        )

    @routes.post("/civitai_hub/thumb_prune")
    async def _thumb_prune(request: web.Request) -> web.Response:
        """Release cached thumbnails according to ``scope``.

        temp    everything not marked core (what a filter change loads)
        detail  non-core previews above 120 KB (a detail page's big images)
        all     same as temp, kept for older callers
        """
        scope = "all"
        max_age = 0.0
        try:
            body = await request.json()
            scope = str(body.get("scope") or "all")
            max_age = float(body.get("max_age_days") or 0)
        except Exception:  # noqa: BLE001
            pass
        try:
            removed, freed = _prune_cache(scope, max_age_days=max_age)
            total = sum(f.stat().st_size for f in THUMB_DIR.glob("*.bin")
                        if f.exists())
            return web.json_response({
                "removed": removed,
                "freed_mb": freed // (1024 * 1024),
                "remaining_mb": total // (1024 * 1024),
            })
        except Exception as exc:  # noqa: BLE001
            return web.json_response({"error": str(exc)}, status=200)

    @routes.post("/civitai_hub/download")
    async def _download(request: web.Request) -> web.Response:
        try:
            payload = await request.json()
        except Exception:
            raise web.HTTPBadRequest(text="invalid json")
        try:
            task = await manager.submit(payload)
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        return web.json_response(task)

    @routes.get("/civitai_hub/downloads")
    async def _downloads(request: web.Request) -> web.Response:
        return web.json_response({"tasks": manager.list()})

    @routes.post("/civitai_hub/downloads/{task_id}/cancel")
    async def _cancel(request: web.Request) -> web.Response:
        ok = manager.cancel(request.match_info["task_id"])
        return web.json_response({"cancelled": ok})

    @routes.post("/civitai_hub/downloads/clear")
    async def _clear(request: web.Request) -> web.Response:
        return web.json_response({"removed": manager.clear_finished()})

    @routes.get("/civitai_hub/local")
    async def _local(request: web.Request) -> web.Response:
        folder_key = request.query.get("folder") or pick_folder_key(
            request.query.get("type") or "LORA"
        )
        try:
            directory = resolve_target_dir(folder_key, (config.get("save_subdir") or "").strip())
        except ValueError as exc:
            # A rejected folder key: the caller asked for a path outside the
            # models tree.
            return web.json_response({"error": str(exc)}, status=400)
        except Exception as exc:  # noqa: BLE001
            return web.json_response({"error": str(exc)}, status=500)
        files = []
        try:
            for entry in sorted(directory.iterdir(), key=lambda p: p.name.lower()):
                if entry.is_file() and entry.suffix.lower() in (
                    ".safetensors", ".ckpt", ".pt", ".bin", ".pth"
                ):
                    files.append(
                        {
                            "name": entry.name,
                            "size": entry.stat().st_size,
                            "mtime": entry.stat().st_mtime,
                        }
                    )
        except FileNotFoundError:
            pass
        return web.json_response(
            {
                "folder": folder_key,
                "path": str(directory),
                "files": files,
                "cache_dir": str(THUMB_DIR.parent),
                "safe_filename_hint": safe_filename("x.safetensors"),
            }
        )
