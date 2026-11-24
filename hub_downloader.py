"""Resumable download manager for Civitai model files."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import struct
import time
import uuid
from pathlib import Path
from typing import Any

import aiohttp

from .hub_client import UA, ChannelError, client, pick_folder_key, safe_filename
from .hub_config import TMP_DIR, config

# Folder keys are ComfyUI directory names: letters, digits, underscore, dash.
# Anything else (dots, slashes, drive letters) is a path traversal attempt.
_FOLDER_KEY_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")

# Kept free on top of the file itself: filesystem metadata, the segment index, and
# enough head-room that a download does not die on the last block.
_FREE_MARGIN_BYTES = 256 * 1024 * 1024


def _free_bytes(path: Path) -> int:
    """Free space on the volume that will hold ``path``, or -1 if unprobeable."""
    probe = Path(path)
    try:
        while not probe.exists():
            parent = probe.parent
            if parent == probe:
                return -1
            probe = parent
        return shutil.disk_usage(probe).free
    except OSError:
        return -1


def _size_text(n: float) -> str:
    if n >= 1024 ** 4:
        return f"{n / 1024 ** 4:.1f} TB"
    if n >= 1024 ** 3:
        return f"{n / 1024 ** 3:.1f} GB"
    return f"{max(n, 0) / 1024 ** 2:.0f} MB"


def _space_problem(need: int, targets: list[tuple[str, Path]]) -> str:
    """Return a user-facing message if any target volume is too small.

    Bytes are written into the temp dir and then renamed into place. When both live
    on one volume that is a single allocation, so it must only be counted once; a
    split install (models on another drive) needs the room twice. Refusing up front
    beats downloading gigabytes and then failing on the last block.
    """
    margin = max(_FREE_MARGIN_BYTES, int(need * 0.02))
    seen: set[str] = set()
    for label, path in targets:
        try:
            drive = os.path.splitdrive(str(Path(path).resolve()))[0].upper()
        except OSError:
            drive = ""
        key = drive or str(path)
        if key in seen:
            continue
        seen.add(key)
        free = _free_bytes(path)
        if 0 <= free < need + margin:
            return (
                f"磁盘空间不够：这个文件 {_size_text(need)}，"
                f"{drive or label} 只剩 {_size_text(free)}。"
                "请先清理磁盘，或在「通道设置」里把这个类型的保存目录改到别的盘。"
            )
    return ""


def _resolve_dest(spec: str) -> Path:
    """Turn a destination setting into an absolute directory.

    Accepts a bare folder name (resolved under models/), a relative path, or a
    full absolute path. Users often keep a loras folder on another drive, so an
    absolute value must be honoured verbatim rather than nested under models/.
    """
    raw = (spec or "").strip().strip('"').strip("'")
    if not raw:
        raw = "loras"
    p = Path(os.path.expandvars(os.path.expanduser(raw)))
    if p.is_absolute() or (len(raw) > 1 and raw[1] == ":"):
        return p
    return _models_root() / "models" / raw.strip("/\\")


def _models_root() -> Path:
    try:
        import folder_paths  # type: ignore

        base = Path(folder_paths.base_path)
    except Exception:  # noqa: BLE001 - standalone import / tests
        base = Path(__file__).resolve().parents[2]
    return base


def resolve_target_dir(folder_key: str, subdir: str = "") -> Path:
    """Resolve a model directory for a folder key.

    The key arrives from a query parameter, so it is validated before use: it used
    to be joined raw, which let `/civitai_hub/local?folder=../../..` walk out of
    the models tree and report absolute paths for arbitrary directories.
    """
    key = str(folder_key or "").strip()
    if not _FOLDER_KEY_RE.fullmatch(key):
        raise ValueError(f"invalid folder key: {folder_key!r}")
    base: Path | None = None
    try:
        import folder_paths  # type: ignore

        paths = folder_paths.get_folder_paths(folder_key)
        if paths:
            base = Path(paths[0])
    except Exception:  # noqa: BLE001
        base = None
    if base is None:
        base = _models_root() / "models" / folder_key
    target = base / subdir if subdir else base
    target.mkdir(parents=True, exist_ok=True)
    return target


def verify_safetensors(path: Path) -> tuple[bool, str]:
    """Cheap structural check: header length + parseable JSON header."""
    try:
        size = path.stat().st_size
        if size < 16:
            return False, "file too small"
        with path.open("rb") as fh:
            raw = fh.read(8)
            (header_len,) = struct.unpack("<Q", raw)
            if header_len <= 0 or header_len > min(size - 8, 100 * 1024 * 1024):
                return False, f"bad header length {header_len}"
            header = json.loads(fh.read(header_len).decode("utf-8"))
        if not isinstance(header, dict):
            return False, "header is not an object"
        if "__metadata__" not in header and not any(
            k != "__metadata__" for k in header
        ):
            return False, "no tensor entries"
        return True, "ok"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


class DownloadTask:
    __slots__ = (
        "id", "model_id", "model_name", "version_id", "version_name", "model_type",
        "file_name", "base_model", "preview_url", "status", "downloaded", "total",
        "speed", "error", "target", "created_at", "started_at", "finished_at",
        "checksum", "resumed", "direct_url", "_task", "_cancel", "_samples",
    )

    def __init__(self, **kw: Any) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.model_id = kw.get("model_id")
        self.model_name = kw.get("model_name") or "model"
        self.version_id = kw.get("version_id")
        self.version_name = kw.get("version_name") or ""
        self.model_type = kw.get("model_type") or "LORA"
        self.file_name = kw.get("file_name") or "model.safetensors"
        self.base_model = kw.get("base_model") or ""
        self.preview_url = kw.get("preview_url") or ""
        self.checksum = kw.get("checksum") or ""
        self.direct_url = kw.get("direct_url") or ""
        self.status = "queued"
        self.downloaded = 0
        self.total = kw.get("size_kb") and int(float(kw["size_kb"]) * 1024) or 0
        self.speed = 0.0
        self.error = ""
        self.target = ""
        self.created_at = time.time()
        self.started_at = 0.0
        self.finished_at = 0.0
        self.resumed = False
        self._task: asyncio.Task | None = None
        self._cancel = asyncio.Event()
        self._samples: list[tuple[float, int]] = []

    def to_dict(self) -> dict[str, Any]:
        pct = (self.downloaded / self.total * 100) if self.total else 0.0
        eta = None
        if self.speed > 1024 and self.total > self.downloaded:
            eta = int((self.total - self.downloaded) / self.speed)
        return {
            "id": self.id,
            "model_id": self.model_id,
            "model_name": self.model_name,
            "version_id": self.version_id,
            "version_name": self.version_name,
            "model_type": self.model_type,
            "file_name": self.file_name,
            "base_model": self.base_model,
            "preview_url": self.preview_url,
            "status": self.status,
            "downloaded": self.downloaded,
            "total": self.total,
            "percent": round(min(pct, 100.0), 1),
            "speed": round(self.speed, 1),
            "eta": eta,
            "error": self.error,
            "target": self.target,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "resumed": self.resumed,
        }


class DownloadManager:
    def __init__(self) -> None:
        self.tasks: dict[str, DownloadTask] = {}
        self._sem: asyncio.Semaphore | None = None
        self._sem_size = 0

    def _semaphore(self) -> asyncio.Semaphore:
        size = max(1, int(config.get("max_concurrent_downloads") or 2))
        if self._sem is None or self._sem_size != size:
            self._sem = asyncio.Semaphore(size)
            self._sem_size = size
        return self._sem

    def list(self) -> list[dict[str, Any]]:
        order = {"downloading": 0, "queued": 1, "completed": 2, "failed": 3, "cancelled": 4}
        items = sorted(
            self.tasks.values(),
            key=lambda t: (order.get(t.status, 9), -t.created_at),
        )
        return [t.to_dict() for t in items]

    def get(self, task_id: str) -> dict[str, Any] | None:
        task = self.tasks.get(task_id)
        return task.to_dict() if task else None

    def cancel(self, task_id: str) -> bool:
        task = self.tasks.get(task_id)
        if not task or task.status not in ("queued", "downloading"):
            return False
        task._cancel.set()
        return True

    def clear_finished(self) -> int:
        gone = [k for k, t in self.tasks.items()
                if t.status in ("completed", "failed", "cancelled")]
        for k in gone:
            self.tasks.pop(k, None)
        return len(gone)

    async def submit(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Create a task from a model-version descriptor produced by the UI."""
        version_id = payload.get("version_id")
        if not version_id:
            raise ValueError("version_id is required")
        files = payload.get("files") or []
        chosen = None
        if payload.get("file_name"):
            chosen = next((f for f in files if f.get("name") == payload["file_name"]), None)
        if chosen is None and files:
            chosen = max(files, key=lambda f: float(f.get("sizeKB") or 0))
        file_name = safe_filename(
            (chosen or {}).get("name") or f"{payload.get('model_name','model')}.safetensors"
        )
        # A double click, or a click the UI delivered twice, used to queue two
        # writers for the same path. Hand back the job that is already moving
        # instead of racing a second one against it.
        for existing in self.tasks.values():
            if (existing.status in ("queued", "downloading")
                    and str(existing.version_id) == str(version_id)
                    and existing.file_name == file_name):
                return existing.to_dict()

        # Refuse before writing anything when the disk cannot hold the file.
        size_kb = float((chosen or {}).get("sizeKB") or payload.get("sizeKB") or 0)
        if size_kb > 0:
            try:
                dest_dir: Path | None = resolve_target_dir(
                    pick_folder_key(payload.get("model_type")),
                    (config.get("save_subdir") or "").strip(),
                )
            except Exception:  # noqa: BLE001 - an unusable destination fails later
                dest_dir = None
            targets: list[tuple[str, Path]] = [("临时目录", TMP_DIR)]
            if dest_dir is not None:
                targets.append(("保存目录", dest_dir))
            problem = _space_problem(int(size_kb * 1024), targets)
            if problem:
                raise ValueError(problem)

        task = DownloadTask(
            model_id=payload.get("model_id"),
            model_name=payload.get("model_name"),
            version_id=version_id,
            version_name=payload.get("version_name"),
            model_type=payload.get("model_type"),
            file_name=file_name,
            base_model=payload.get("base_model"),
            preview_url=payload.get("preview_url"),
            checksum=(chosen or {}).get("sha256") or payload.get("sha256") or "",
            size_kb=(chosen or {}).get("sizeKB") or payload.get("sizeKB"),
            direct_url=(chosen or {}).get("downloadUrl") or payload.get("download_url") or "",
        )
        self.tasks[task.id] = task
        task._task = asyncio.create_task(self._run(task))
        return task.to_dict()

    async def _run(self, task: DownloadTask) -> None:
        async with self._semaphore():
            if task._cancel.is_set():
                task.status = "cancelled"
                return
            task.status = "downloading"
            task.started_at = time.time()
            try:
                await self._download(task)
            except asyncio.CancelledError:
                task.status = "cancelled"
                raise
            except Exception as exc:  # noqa: BLE001
                task.status = "failed"
                task.error = f"{type(exc).__name__}: {exc}"
            finally:
                task.finished_at = time.time()
                task.speed = 0.0
                # On any non-success ending, remove what was written. A failed
                # 6 GB partial left in models/ looks like a usable model to
                # ComfyUI and to the user.
                if task.status in ("failed", "cancelled"):
                    try:
                        for leftover in (TMP_DIR / f"{task.id}_{task.file_name}.part",
                                         TMP_DIR / f"{task.id}.parts",
                                         TMP_DIR / "verify.part"):
                            leftover.unlink(missing_ok=True)
                        if task.target:
                            Path(task.target).unlink(missing_ok=True)
                            task.target = ""
                    except Exception:  # noqa: BLE001
                        pass

    async def _download(self, task: DownloadTask) -> None:
        # Per-type destination: one row per model type in the settings panel, so a
        # LoRA and a Checkpoint never share a folder unless the user says so. A
        # value may be a relative folder under models/ or a full absolute path
        # (useful when the loras live outside the ComfyUI tree). Empty falls back
        # to save_subdir, then to "loras".
        dirs = config.get("save_dirs") or {}
        subdir = str(dirs.get(task.model_type or "") or "").strip()
        if not subdir:
            subdir = (config.get("save_subdir") or "loras").strip()
        target_dir = _resolve_dest(subdir)
        target_dir.mkdir(parents=True, exist_ok=True)
        final = target_dir / task.file_name
        if final.exists() and final.stat().st_size > 0:
            stem, suffix = final.stem, final.suffix
            n = 1
            while final.exists():
                final = target_dir / f"{stem} ({n}){suffix}"
                n += 1

        part = TMP_DIR / f"{task.id}_{task.file_name}.part"
        # Always resolve through the mirror instead of trusting the UI's
        # downloadUrl. That URL is a presigned b2.civitai.com link whose signature
        # is bound to the hostname — swapping in a reachable B2 endpoint
        # invalidates it, and the original host is blocked. resolve_file asks the
        # mirror for a fresh 307 and follows it to whatever storage it names.
        url = ""
        try:
            url = (await client.resolve_file(task.version_id))["url"]
        except Exception:  # noqa: BLE001
            url = task.direct_url or ""
        if url and "b2.civitai.com" in url:
            # Last resort: an unresolved presigned link. The host rewrite at least
            # aims at a reachable endpoint and may still pass signature checks.
            host = (config.get("b2_endpoints") or ["f004.backblazeb2.com"])[0]
            url = url.replace("b2.civitai.com", host.strip())
        if not url:
            raise ChannelError("no download url resolved")
        await self._download_parallel(task, url, part)

        # Verify before it lands in models/: a 64-segment splice that wrote to
        # the wrong offset would produce a file ComfyUI silently fails to load,
        # and the mistake would only surface much later.
        expected = (task.checksum or "").strip().upper()
        if expected and len(expected) == 64:
            task.status = "verifying"
            digest = hashlib.sha256()
            with part.open("rb") as fh:
                while True:
                    block = fh.read(4 * 1024 * 1024)
                    if not block:
                        break
                    digest.update(block)
            actual = digest.hexdigest().upper()
            if actual != expected:
                part.unlink(missing_ok=True)
                task.status = "failed"
                task.error = f"sha256 mismatch (got {actual[:16]}…)"
                return

        part.replace(final)
        task.target = str(final)
        if final.suffix.lower() == ".safetensors":
            ok, msg = verify_safetensors(final)
            if not ok:
                task.error = f"warning: {msg}"
        task.status = "completed"

    async def _download_parallel(self, task: DownloadTask, url: str,
                                 part: Path, segments: int | None = None) -> None:
        """Fetch the file as concurrent byte ranges.

        A single stream from Cloudflare R2 measured ~40-220 KB/s locally, while
        64 parallel ranges reach ~2 MB/s — the per-connection limit is the
        bottleneck, not the link. Progress is tracked in a sidecar file so an
        interrupted download resumes instead of restarting.
        """
        n_seg = int(segments or config.get("download_segments") or 64)
        session = await client._get_session()

        size = 0
        for attempt in range(2):
            headers = {"User-Agent": UA, "Accept": "*/*", "Range": "bytes=0-0"}
            async with session.get(url, headers=headers) as resp:
                if resp.status == 403 and attempt == 0 and not task.direct_url:
                    # Signed URL expired between resolve and transfer.
                    url = (await client.resolve_file(task.version_id))["url"]
                    continue
                rng = resp.headers.get("Content-Range") or ""
                if "/" in rng:
                    try:
                        size = int(rng.rsplit("/", 1)[1])
                    except ValueError:
                        size = 0
                if not size:
                    size = int(resp.headers.get("Content-Length") or 0)
                break
        if size <= 0:
            raise ChannelError("cannot determine file size")

        task.total = size
        n_seg = max(1, min(n_seg, size // (256 * 1024) + 1))
        chunk = size // n_seg
        bounds = []
        for i in range(n_seg):
            start = i * chunk
            end = size - 1 if i == n_seg - 1 else (i + 1) * chunk - 1
            bounds.append((start, end))

        progress = TMP_DIR / f"{task.id}.parts"
        done_parts: set[int] = set()
        if progress.exists():
            try:
                done_parts = {int(x) for x in progress.read_text().split()}
            except Exception:  # noqa: BLE001
                done_parts = set()

        if part.exists() and not done_parts:
            part.unlink(missing_ok=True)
        with part.open("r+b" if part.exists() else "wb") as fh:
            fh.truncate(size)

        lock = asyncio.Lock()
        task.downloaded = sum(bounds[i][1] - bounds[i][0] + 1 for i in done_parts)

        async def fetch(idx: int) -> None:
            start, end = bounds[idx]
            if idx in done_parts:
                return
            for attempt in range(3):
                try:
                    hdr = {"User-Agent": UA, "Accept": "*/*",
                           "Range": f"bytes={start}-{end}"}
                    async with session.get(url, headers=hdr, timeout=aiohttp.ClientTimeout(total=600)) as resp:
                        if resp.status not in (200, 206):
                            if resp.status == 403 and attempt == 0:
                                continue
                            return
                        pos = start
                        with part.open("r+b") as fh:
                            fh.seek(pos)
                            async for data in resp.content.iter_chunked(262144):
                                if task._cancel.is_set():
                                    return
                                fh.write(data)
                                pos += len(data)
                                async with lock:
                                    task.downloaded += len(data)
                                    self._tick(task)
                        async with lock:
                            done_parts.add(idx)
                            try:
                                progress.write_text(" ".join(str(i) for i in done_parts))
                            except Exception:  # noqa: BLE001
                                pass
                        return
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    continue

        await asyncio.gather(*(fetch(i) for i in range(n_seg)))
        if task._cancel.is_set():
            raise asyncio.CancelledError
        progress.unlink(missing_ok=True)

    def _tick(self, task: DownloadTask) -> None:
        now = time.time()
        task._samples.append((now, task.downloaded))
        if len(task._samples) > 12:
            task._samples.pop(0)
        if len(task._samples) >= 2:
            t0, b0 = task._samples[0]
            dt = now - t0
            if dt >= 1.0:
                task.speed = max(0.0, (task.downloaded - b0) / dt)


manager = DownloadManager()
