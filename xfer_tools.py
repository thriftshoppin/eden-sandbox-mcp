"""Gated, batch-approved file transfer for the secret desktop MCP endpoint.

Approval is never an MCP tool. A human approves or denies a pending manifest
with xfer_approve.sh. Transfers are limited to /workspace/mcp-test/xfer/box/.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import stat
import time
from pathlib import Path
from typing import Any

DIR = Path("/workspace/mcp-test")
XFER = DIR / "xfer"
PENDING = XFER / "pending"
APPROVED = XFER / "approved"
DENIED = XFER / "denied"
COMPLETED = XFER / "completed"
EXPIRED = XFER / "expired"
BOX = XFER / "box"
STAGING = XFER / "staging"
LOG = XFER / "xfer.log"
NOTIFY = XFER / "notify.log"
LOCK = XFER / "xfer.lock"

MAX_ITEMS = 20
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_BATCH_BYTES = 50 * 1024 * 1024
MAX_TTL_MIN = 60
DEFAULT_TTL_MIN = 15
# ~12 KB of base64 text per chunk (~9 KiB of file bytes).
CHUNK_B64_TARGET = 12 * 1024
CHUNK_RAW = (CHUNK_B64_TARGET * 3) // 4  # 9216
MAX_CHUNK_B64 = 16 * 1024
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
ID_RE = re.compile(r"^xf_[A-Za-z0-9_-]{8,64}$")

STATES = ("pending", "approved", "denied", "expired", "completed")


def _cst() -> str:
    env = dict(os.environ)
    env["TZ"] = "America/Chicago"
    import subprocess
    return subprocess.check_output(
        ["date", "+%Y-%m-%d %H:%M:%S %Z"], env=env, text=True
    ).strip()


def _ensure_dirs() -> None:
    for p in (PENDING, APPROVED, DENIED, COMPLETED, EXPIRED, BOX, STAGING, XFER):
        p.mkdir(parents=True, exist_ok=True)


def _lock_fd():
    _ensure_dirs()
    fd = os.open(LOCK, os.O_CREAT | os.O_RDWR, 0o600)
    import fcntl
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def _canonical(data: dict) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def log_event(action: str, **fields: Any) -> None:
    """Append a structured line with no file contents."""
    _ensure_dirs()
    parts = [f"{_cst()} action={action}"]
    for k, v in fields.items():
        if v is None:
            continue
        sv = str(v).replace("\n", " ").replace("\r", "")
        # never log payloads
        if k in {"b64", "b64_chunk", "contents", "data", "chunk"}:
            continue
        if len(sv) > 240:
            sv = sv[:237] + "..."
        parts.append(f"{k}={sv}")
    line = " ".join(parts) + "\n"
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line)


def notify_pending(manifest: dict) -> None:
    total = sum(int(i.get("size_bytes") or 0) for i in manifest.get("items") or [])
    reason = (manifest.get("reason") or "").replace("\n", " ")[:200]
    line = (
        f"{_cst()} pending id={manifest.get('id')} items={len(manifest.get('items') or [])} "
        f"bytes={total} ttl={manifest.get('ttl_minutes')} expires={manifest.get('expires')} "
        f"reason={reason}\n"
    )
    with NOTIFY.open("a", encoding="utf-8") as f:
        f.write(line)


def _atomic_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def _is_symlink(path: Path) -> bool:
    try:
        return path.is_symlink()
    except OSError:
        return False


def _reject_symlink_chain(path: Path) -> str | None:
    """Reject if any existing component along path is a symlink."""
    try:
        path = Path(path)
        # Walk from root toward the leaf; do not follow links.
        acc = Path(path.anchor) if path.is_absolute() else Path(".")
        parts = path.parts[1:] if path.is_absolute() else path.parts
        for part in parts:
            acc = acc / part
            if acc.exists() and acc.is_symlink():
                return f"Error: symlink not allowed: {acc}"
    except OSError as exc:
        return f"Error: cannot inspect path: {exc}"
    return None


def safe_box_path(user_path: str, *, name: str | None = None) -> Path:
    """Resolve a dest/source path under BOX. No traversal, no symlinks."""
    raw = (user_path or "").strip() if user_path is not None else ""
    if not raw and name:
        raw = name
    if not raw:
        raise ValueError("Error: path_or_dest is required.")
    if "\x00" in raw or any(ord(c) < 32 for c in raw):
        raise ValueError("Error: path contains control characters.")
    if raw.startswith("~") or raw.startswith("$"):
        raise ValueError("Error: path must stay under xfer/box/.")
    p = Path(raw)
    # Disallow Windows drives or UNC, and obvious escapes.
    if raw.startswith("//") or (len(raw) > 1 and raw[1] == ":"):
        raise ValueError("Error: path must stay under xfer/box/.")
    box_resolved = BOX.resolve()
    if p.is_absolute():
        cand = p
    else:
        # Allow "file.txt", "box/file.txt", "xfer/box/file.txt"
        parts = p.parts
        if parts[:2] == ("xfer", "box"):
            p = Path(*parts[2:]) if len(parts) > 2 else Path()
        elif parts[:1] == ("box",):
            p = Path(*parts[1:]) if len(parts) > 1 else Path()
        if not p.parts:
            raise ValueError("Error: path must stay under xfer/box/.")
        cand = BOX / p
    if ".." in cand.parts:
        raise ValueError("Error: path traversal is not allowed.")
    err = _reject_symlink_chain(cand)
    if err:
        raise ValueError(err)
    # Resolve existing parents without following a missing leaf.
    try:
        parent = cand.parent
        err = _reject_symlink_chain(parent)
        if err:
            raise ValueError(err)
        if parent.exists():
            parent_res = parent.resolve()
        else:
            # Ensure we could create it under BOX: resolve existing prefix only.
            parent_res = _resolve_existing_prefix(parent)
        leaf = cand.name
        if not leaf or leaf in {".", ".."}:
            raise ValueError("Error: invalid file name.")
        resolved = (parent_res / leaf)
    except ValueError:
        raise
    except OSError as exc:
        raise ValueError(f"Error: cannot resolve path: {exc}") from exc
    try:
        resolved.relative_to(box_resolved)
    except ValueError as exc:
        raise ValueError("Error: path must stay under xfer/box/.") from exc
    if resolved == box_resolved:
        raise ValueError("Error: path must be a file under xfer/box/.")
    return resolved


def _resolve_existing_prefix(path: Path) -> Path:
    box_resolved = BOX.resolve()
    if not path.is_absolute():
        path = BOX / path
    cur = Path(path.anchor)
    out = Path(path.anchor)
    for part in path.parts[1:]:
        nxt = cur / part
        if nxt.exists():
            if nxt.is_symlink():
                raise ValueError(f"Error: symlink not allowed: {nxt}")
            cur = nxt
            out = nxt.resolve()
        else:
            out = out / part if out != Path(path.anchor) or out.exists() else (out / part)
            # From the first missing component, just join; later check relative_to.
            rest_start = path.parts.index(part)
            for more in path.parts[rest_start + 1 :]:
                out = out / more
            break
        cur = nxt
    else:
        out = cur.resolve() if cur.exists() else out
    try:
        (out if out.exists() else Path(str(out))).resolve() if False else out
    except OSError:
        pass
    # If nothing existed, out may still be relative-joined from root.
    if path.exists():
        return path.resolve()
    # Build under box: take BOX + relative parts of path under BOX if possible
    try:
        rel = path.relative_to(BOX) if str(path).startswith(str(BOX)) else path.relative_to(box_resolved)
        return box_resolved / rel
    except ValueError:
        return path


def _valid_name(name: str) -> str:
    name = (name or "").strip()
    if not name or not NAME_RE.match(name):
        raise ValueError(
            "Error: name must be a plain filename (letters, digits, ., _, -) without a path."
        )
    if name in {".", ".."} or name.startswith("."):
        # allow hidden? safer no leading dot to avoid .ssh etc.
        raise ValueError("Error: name must not start with a dot.")
    return name


def _parse_items(items: Any) -> list[dict]:
    if items is None:
        raise ValueError("Error: items is required.")
    if isinstance(items, str):
        try:
            items = json.loads(items)
        except json.JSONDecodeError as exc:
            raise ValueError("Error: items must be a JSON list.") from exc
    if not isinstance(items, list):
        raise ValueError("Error: items must be a list.")
    if not items:
        raise ValueError("Error: items must not be empty.")
    if len(items) > MAX_ITEMS:
        raise ValueError(f"Error: at most {MAX_ITEMS} items per batch.")
    return items


def _normalize_item(raw: dict, index: int) -> dict:
    if not isinstance(raw, dict):
        raise ValueError(f"Error: item {index} must be an object.")
    direction = str(raw.get("direction") or "").strip().lower()
    if direction not in {"to_box", "from_box"}:
        raise ValueError(f"Error: item {index} direction must be 'to_box' or 'from_box'.")
    name = _valid_name(str(raw.get("name") or ""))
    path_or_dest = raw.get("path_or_dest") or raw.get("path") or raw.get("dest") or name
    dest = safe_box_path(str(path_or_dest), name=name)
    # Destination leaf should match the given name (avoid sneaking a different file).
    if dest.name != name:
        raise ValueError(
            f"Error: item {index} path_or_dest must end with the declared name {name!r}."
        )
    size = raw.get("size_bytes")
    if size is not None and size != "":
        try:
            size = int(size)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Error: item {index} size_bytes must be an integer.") from exc
        if size < 0:
            raise ValueError(f"Error: item {index} size_bytes must be >= 0.")
        if size > MAX_FILE_BYTES:
            raise ValueError(
                f"Error: item {index} exceeds the {MAX_FILE_BYTES} byte per-file limit."
            )
    else:
        size = None
    sha = raw.get("sha256") or None
    if sha:
        sha = str(sha).strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", sha):
            raise ValueError(f"Error: item {index} sha256 must be 64 hex characters.")
    if direction == "from_box":
        if not dest.exists():
            raise ValueError(f"Error: item {index} source file does not exist under xfer/box/.")
        if dest.is_symlink() or not dest.is_file():
            raise ValueError(f"Error: item {index} source must be a regular file, not a symlink.")
        actual = dest.stat().st_size
        if actual > MAX_FILE_BYTES:
            raise ValueError(
                f"Error: item {index} exceeds the {MAX_FILE_BYTES} byte per-file limit."
            )
        if size is None:
            size = actual
        elif size != actual:
            raise ValueError(
                f"Error: item {index} size_bytes {size} does not match the file ({actual})."
            )
        actual_sha = _sha256_file(dest)
        if sha and sha != actual_sha:
            raise ValueError(f"Error: item {index} sha256 does not match the file on disk.")
        sha = actual_sha
    else:
        # to_box: dest must not already exist as a symlink; existing file is refused later
        # at upload so a pending proposal doesn't block a retry of a failed name? We'll
        # refuse propose if dest already exists, to make the batch honest.
        if dest.exists():
            raise ValueError(
                f"Error: item {index} destination already exists: {dest.name}"
            )
        if size is None:
            raise ValueError(f"Error: item {index} (to_box) requires size_bytes.")
    return {
        "index": index,
        "direction": direction,
        "name": name,
        "path": str(dest),
        "size_bytes": int(size),
        "sha256": sha,
        "status": "pending",
    }


def _find_manifest(mid: str) -> tuple[Path | None, dict | None, str | None]:
    """Return (path, data, folder_status)."""
    mid = (mid or "").strip()
    if not ID_RE.match(mid):
        return None, None, None
    for status, folder in (
        ("pending", PENDING),
        ("approved", APPROVED),
        ("denied", DENIED),
        ("completed", COMPLETED),
        ("expired", EXPIRED),
    ):
        path = folder / f"{mid}.json"
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return path, None, status
            return path, data, status
    return None, None, None


def _maybe_expire(path: Path, data: dict, folder_status: str) -> tuple[Path, dict, str]:
    if folder_status not in {"pending", "approved"}:
        return path, data, folder_status
    expires = int(data.get("expires") or 0)
    if expires and time.time() >= expires:
        data = dict(data)
        data["status"] = "expired"
        data["expired_at"] = int(time.time())
        for it in data.get("items") or []:
            if it.get("status") in {"pending", "approved"}:
                it["status"] = "expired"
        dest = EXPIRED / f"{data['id']}.json"
        _atomic_write(dest, data)
        try:
            path.unlink()
        except OSError:
            pass
        log_event("expire", id=data.get("id"), from_status=folder_status)
        return dest, data, "expired"
    return path, data, folder_status


def propose(items: Any, reason: str, ttl_minutes: int = DEFAULT_TTL_MIN) -> str:
    """Create a pending manifest. Does not transfer anything and does not need arming."""
    fd = _lock_fd()
    try:
        try:
            raw_items = _parse_items(items)
            reason = (reason or "").strip()
            if not reason:
                return "Error: reason is required."
            if len(reason) > 500:
                return "Error: reason must be 500 characters or fewer."
            try:
                ttl = int(ttl_minutes) if ttl_minutes is not None else DEFAULT_TTL_MIN
            except (TypeError, ValueError):
                return "Error: ttl_minutes must be an integer."
            if ttl < 1 or ttl > MAX_TTL_MIN:
                return f"Error: ttl_minutes must be between 1 and {MAX_TTL_MIN}."
            normalized = [_normalize_item(it, i) for i, it in enumerate(raw_items)]
            batch = sum(int(it["size_bytes"]) for it in normalized)
            if batch > MAX_BATCH_BYTES:
                return f"Error: batch exceeds the {MAX_BATCH_BYTES} byte limit."
            mid = "xf_" + secrets.token_urlsafe(9).replace("-", "x")
            now = int(time.time())
            manifest = {
                "id": mid,
                "status": "pending",
                "reason": reason,
                "created_at": now,
                "expires": now + ttl * 60,
                "ttl_minutes": ttl,
                "items": normalized,
                "batch_size_bytes": batch,
            }
            # Hash of the proposal as recorded (before any approval).
            manifest["manifest_sha256"] = _sha256_text(_canonical({
                k: manifest[k] for k in ("id", "status", "reason", "created_at",
                                          "expires", "ttl_minutes", "items",
                                          "batch_size_bytes")
            }))
            dest = PENDING / f"{mid}.json"
            _atomic_write(dest, manifest)
            notify_pending(manifest)
            log_event(
                "propose",
                id=mid,
                items=len(normalized),
                bytes=batch,
                ttl=ttl,
                reason=reason[:120],
            )
        except ValueError as exc:
            msg = str(exc)
            log_event("propose_refused", reason="validation", error=msg[:200])
            return msg if msg.startswith("Error:") else f"Error: {msg}"
        except Exception as exc:
            log_event("propose_error", error=type(exc).__name__)
            return f"Error: propose failed: {exc}"
        lines = [
            f"PENDING: awaiting user approval in Grok Bot chat, manifest {mid}",
            f"expires: {_fmt_ts(manifest['expires'])} ({ttl} min)",
            f"items: {len(normalized)}, {batch} bytes",
        ]
        for it in normalized:
            arrow = "->" if it["direction"] == "to_box" else "<-"
            lines.append(
                f"[{it['index']}] {it['direction']} {it['name']} {arrow} {it['path']} "
                f"({it['size_bytes']} bytes)"
            )
        return "\n".join(lines)
    finally:
        os.close(fd)


def _fmt_ts(ts: int) -> str:
    env = dict(os.environ)
    env["TZ"] = "America/Chicago"
    import subprocess
    return subprocess.check_output(
        ["date", "-d", f"@{int(ts)}", "+%Y-%m-%d %H:%M:%S %Z"],
        env=env,
        text=True,
    ).strip()


def status(mid: str) -> str:
    fd = _lock_fd()
    try:
        mid = (mid or "").strip()
        if not ID_RE.match(mid):
            return "Error: invalid manifest id."
        path, data, folder = _find_manifest(mid)
        if not data:
            return f"Error: no manifest {mid}."
        path, data, folder = _maybe_expire(path, data, folder)
        approved_idx = data.get("approved_indexes")
        out = {
            "id": data.get("id"),
            "status": folder,
            "reason": data.get("reason"),
            "created_at": data.get("created_at"),
            "expires": data.get("expires"),
            "expires_in_s": max(0, int(data.get("expires") or 0) - int(time.time()))
            if folder in {"pending", "approved"}
            else 0,
            "approved_indexes": approved_idx,
            "denied_indexes": data.get("denied_indexes"),
            "manifest_sha256": data.get("manifest_sha256"),
            "scope_sha256": data.get("scope_sha256"),
            "batch_size_bytes": data.get("batch_size_bytes"),
            "items": [
                {
                    "index": it.get("index"),
                    "direction": it.get("direction"),
                    "name": it.get("name"),
                    "path": it.get("path"),
                    "size_bytes": it.get("size_bytes"),
                    "sha256": it.get("sha256"),
                    "status": it.get("status"),
                }
                for it in (data.get("items") or [])
            ],
        }
        if folder == "approved" and approved_idx is not None:
            out["status"] = f"approved(subset={approved_idx})"
        log_event("status", id=mid, status=out["status"])
        return json.dumps(out, indent=2)
    finally:
        os.close(fd)


def approve(mid: str, spec: str) -> str:
    """CLI-only. spec is 'all', comma-separated indexes, or 'deny'."""
    fd = _lock_fd()
    try:
        mid = (mid or "").strip()
        if not ID_RE.match(mid):
            return "Error: invalid manifest id."
        spec = (spec or "").strip().lower()
        if not spec:
            return "Error: usage: xfer_approve.sh <id> all|<comma item indexes>|deny"
        path, data, folder = _find_manifest(mid)
        if not data:
            return f"Error: no manifest {mid}."
        path, data, folder = _maybe_expire(path, data, folder)
        if folder == "expired":
            return f"Error: manifest {mid} has expired."
        if folder != "pending":
            return f"Error: manifest {mid} is already {folder}."
        now = int(time.time())
        n = len(data.get("items") or [])
        if spec in {"deny", "denied", "no", "n"}:
            data["status"] = "denied"
            data["denied_at"] = now
            for it in data["items"]:
                it["status"] = "denied"
            dest = DENIED / f"{mid}.json"
            _atomic_write(dest, data)
            try:
                path.unlink()
            except OSError:
                pass
            log_event("deny", id=mid, items=n)
            return f"DENIED manifest {mid} ({n} items)."
        if spec in {"all", "*"}:
            indexes = list(range(n))
        else:
            indexes = []
            for part in spec.split(","):
                part = part.strip()
                if not part:
                    continue
                if not part.isdigit():
                    return f"Error: invalid item index {part!r}. Use all, deny, or comma-separated indexes."
                idx = int(part)
                if idx < 0 or idx >= n:
                    return f"Error: item index {idx} is out of range (0..{n - 1})."
                if idx not in indexes:
                    indexes.append(idx)
            if not indexes:
                return "Error: no item indexes given. Use all, deny, or comma-separated indexes."
        approved_set = set(indexes)
        for it in data["items"]:
            if it["index"] in approved_set:
                it["status"] = "approved"
            else:
                it["status"] = "not_approved"
        data["status"] = "approved"
        data["approved_at"] = now
        data["approved_indexes"] = indexes
        data["denied_indexes"] = [i for i in range(n) if i not in approved_set]
        # Hash of the scoped subset the guardian will enforce.
        scope = {
            "id": mid,
            "approved_indexes": indexes,
            "expires": data["expires"],
            "items": [
                {k: it[k] for k in ("index", "direction", "name", "path", "size_bytes", "sha256")}
                for it in data["items"]
                if it["index"] in approved_set
            ],
            "manifest_sha256": data.get("manifest_sha256"),
        }
        data["scope_sha256"] = _sha256_text(_canonical(scope))
        dest = APPROVED / f"{mid}.json"
        _atomic_write(dest, data)
        try:
            path.unlink()
        except OSError:
            pass
        log_event(
            "approve",
            id=mid,
            indexes=",".join(str(i) for i in indexes),
            scope_sha256=data["scope_sha256"],
        )
        skipped = data["denied_indexes"]
        extra = f" (excluded indexes: {skipped})" if skipped else ""
        return (
            f"APPROVED manifest {mid} items {indexes}{extra}. "
            f"Expires {_fmt_ts(data['expires'])}. scope_sha256={data['scope_sha256']}"
        )
    finally:
        os.close(fd)


def _require_approved_item(mid: str, index: int, direction: str) -> tuple[dict | None, dict | None, str | None]:
    """Return (manifest, item, error). Caller holds the lock."""
    path, data, folder = _find_manifest(mid)
    if not data:
        return None, None, f"Error: no manifest {mid}."
    path, data, folder = _maybe_expire(path, data, folder)
    if folder == "expired":
        return data, None, f"Error: manifest {mid} has expired."
    if folder == "denied":
        return data, None, f"Error: manifest {mid} was denied."
    if folder == "pending":
        return data, None, f"Error: manifest {mid} is still pending approval."
    if folder == "completed":
        # may still look up item for reuse message
        pass
    if folder not in {"approved", "completed"}:
        return data, None, f"Error: manifest {mid} is {folder}."
    try:
        index = int(index)
    except (TypeError, ValueError):
        return data, None, "Error: index must be an integer."
    items = data.get("items") or []
    item = next((it for it in items if it.get("index") == index), None)
    if item is None:
        return data, None, f"Error: no item {index} in manifest {mid}."
    if item.get("status") == "completed":
        return data, item, f"Error: item {index} was already transferred and cannot be reused."
    if item.get("status") == "not_approved":
        return data, item, f"Error: item {index} was not in the approved subset."
    if item.get("status") != "approved":
        return data, item, f"Error: item {index} is {item.get('status')}, not approved."
    if folder != "approved":
        return data, item, f"Error: manifest {mid} is {folder}."
    if item.get("direction") != direction:
        return data, item, (
            f"Error: item {index} is {item.get('direction')}, not {direction}."
        )
    return data, item, None


def _staging_dir(mid: str, index: int) -> Path:
    d = STAGING / mid / str(index)
    d.mkdir(parents=True, exist_ok=True)
    return d


def upload(mid: str, index: int, b64_chunk: str, chunk_no: int, total_chunks: int, sha256: str) -> str:
    fd = _lock_fd()
    try:
        mid = (mid or "").strip()
        if not ID_RE.match(mid):
            return "Error: invalid manifest id."
        data, item, err = _require_approved_item(mid, index, "to_box")
        if err:
            log_event("upload_refused", id=mid, index=index, error=err[:160])
            return err
        try:
            chunk_no = int(chunk_no)
            total_chunks = int(total_chunks)
        except (TypeError, ValueError):
            return "Error: chunk_no and total_chunks must be integers."
        # Accept 1-based; also accept 0-based if chunk_no is 0 and total>=1.
        if total_chunks < 1 or total_chunks > 2000:
            return "Error: total_chunks is out of range."
        if chunk_no == 0:
            chunk_idx = 0
        elif 1 <= chunk_no <= total_chunks:
            chunk_idx = chunk_no - 1
        else:
            return f"Error: chunk_no must be between 1 and {total_chunks}."
        sha = (sha256 or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", sha):
            return "Error: sha256 must be 64 hex characters of the complete file."
        if item.get("sha256") and item["sha256"] != sha:
            return "Error: sha256 does not match the approved manifest."
        raw_b64 = (b64_chunk or "").strip()
        raw_b64 = re.sub(r"\s+", "", raw_b64)
        if not raw_b64 or not re.fullmatch(r"[A-Za-z0-9+/=]+", raw_b64):
            return "Error: b64_chunk is not valid base64."
        if len(raw_b64) > MAX_CHUNK_B64:
            return f"Error: b64_chunk exceeds {MAX_CHUNK_B64} characters."
        try:
            blob = base64.b64decode(raw_b64, validate=True)
        except Exception:
            return "Error: b64_chunk is not valid base64."
        expected_size = int(item["size_bytes"])
        if expected_size > MAX_FILE_BYTES:
            return "Error: approved size exceeds the per-file limit."
        dest = safe_box_path(item["path"], name=item["name"])
        if dest.name != item["name"]:
            return "Error: approved path does not match the declared name."
        if dest.exists():
            return f"Error: destination already exists: {item['name']}"
        stag = _staging_dir(mid, int(item["index"]))
        meta_path = stag / "meta.json"
        meta = {}
        if meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                meta = {}
        if meta:
            if meta.get("sha256") not in (None, sha) or meta.get("total_chunks") not in (None, total_chunks):
                return "Error: chunk metadata does not match the in-progress upload."
        meta.update({"sha256": sha, "total_chunks": total_chunks, "size_bytes": expected_size})
        _atomic_write(meta_path, meta)
        part = stag / f"chunk_{chunk_idx:04d}"
        part.write_bytes(blob)
        present = sorted(int(p.name.split("_")[1]) for p in stag.glob("chunk_*"))
        log_event(
            "upload_chunk",
            id=mid,
            index=item["index"],
            name=item["name"],
            chunk=chunk_idx + 1,
            total=total_chunks,
            bytes=len(blob),
        )
        if len(present) < total_chunks:
            return (
                f"OK: accepted chunk {chunk_idx + 1}/{total_chunks} for item {item['index']} "
                f"({item['name']}). {len(present)}/{total_chunks} chunks received."
            )
        if present != list(range(total_chunks)):
            return "Error: missing or unexpected chunk numbers; upload is incomplete."
        # Assemble
        assembled = bytearray()
        for i in range(total_chunks):
            assembled.extend((stag / f"chunk_{i:04d}").read_bytes())
            if len(assembled) > MAX_FILE_BYTES:
                return "Error: assembled file exceeds the per-file limit."
        if len(assembled) != expected_size:
            return (
                f"Error: assembled size {len(assembled)} does not match the approved "
                f"size {expected_size}."
            )
        actual_sha = _sha256_bytes(bytes(assembled))
        if actual_sha != sha:
            log_event("upload_hash_mismatch", id=mid, index=item["index"], expected=sha, actual=actual_sha)
            return "Error: sha256 of the assembled file does not match."
        # Write dest (no overwrite, no symlink)
        err = _reject_symlink_chain(dest)
        if err:
            return err
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + f".{mid}.tmp")
        tmp.write_bytes(bytes(assembled))
        os.chmod(tmp, 0o600)
        if dest.exists():
            tmp.unlink(missing_ok=True)
            return f"Error: destination already exists: {item['name']}"
        os.rename(tmp, dest)
        # mark complete
        path = APPROVED / f"{mid}.json"
        for it in data["items"]:
            if it["index"] == item["index"]:
                it["status"] = "completed"
                it["sha256"] = actual_sha
                it["completed_at"] = int(time.time())
        if all(
            it.get("status") in {"completed", "not_approved"}
            for it in data["items"]
        ):
            data["status"] = "completed"
            data["completed_at"] = int(time.time())
            _atomic_write(COMPLETED / f"{mid}.json", data)
            try:
                path.unlink()
            except OSError:
                pass
            folder_now = "completed"
        else:
            _atomic_write(path, data)
            folder_now = "approved"
        # cleanup staging
        try:
            for p in stag.glob("*"):
                p.unlink()
            stag.rmdir()
        except OSError:
            pass
        log_event(
            "upload_complete",
            id=mid,
            index=item["index"],
            name=item["name"],
            bytes=expected_size,
            sha256=actual_sha,
            manifest=folder_now,
        )
        return (
            f"OK: item {item['index']} ({item['name']}) transferred to_box, "
            f"{expected_size} bytes, sha256={actual_sha} match. "
            f"Manifest is {folder_now}."
        )
    except ValueError as exc:
        msg = str(exc)
        log_event("upload_refused", id=mid, index=index, error=msg[:160])
        return msg if msg.startswith("Error:") else f"Error: {msg}"
    except Exception as exc:
        log_event("upload_error", id=mid, index=index, error=type(exc).__name__)
        return f"Error: upload failed: {exc}"
    finally:
        os.close(fd)


def download(mid: str, index: int, chunk_no: int) -> str:
    fd = _lock_fd()
    try:
        mid = (mid or "").strip()
        if not ID_RE.match(mid):
            return "Error: invalid manifest id."
        data, item, err = _require_approved_item(mid, index, "from_box")
        if err:
            log_event("download_refused", id=mid, index=index, error=err[:160])
            return err
        src = safe_box_path(item["path"], name=item["name"])
        if src.is_symlink() or not src.is_file():
            return "Error: source is not a regular file under xfer/box/."
        raw = src.read_bytes()
        if len(raw) != int(item["size_bytes"]):
            return "Error: file size no longer matches the approved manifest."
        if len(raw) > MAX_FILE_BYTES:
            return "Error: file exceeds the per-file limit."
        actual_sha = _sha256_bytes(raw)
        if item.get("sha256") and item["sha256"] != actual_sha:
            return "Error: file sha256 no longer matches the approved manifest."
        try:
            chunk_no = int(chunk_no)
        except (TypeError, ValueError):
            return "Error: chunk_no must be an integer."
        total = max(1, (len(raw) + CHUNK_RAW - 1) // CHUNK_RAW)
        if chunk_no == 0:
            chunk_idx = 0
        elif 1 <= chunk_no <= total:
            chunk_idx = chunk_no - 1
        else:
            return f"Error: chunk_no must be between 1 and {total}."
        start = chunk_idx * CHUNK_RAW
        piece = raw[start : start + CHUNK_RAW]
        b64 = base64.b64encode(piece).decode("ascii")
        is_last = chunk_idx + 1 == total
        # Mark completed only after the last chunk is successfully issued.
        if is_last:
            path = APPROVED / f"{mid}.json"
            for it in data["items"]:
                if it["index"] == item["index"]:
                    it["status"] = "completed"
                    it["completed_at"] = int(time.time())
            if all(it.get("status") in {"completed", "not_approved"} for it in data["items"]):
                data["status"] = "completed"
                data["completed_at"] = int(time.time())
                _atomic_write(COMPLETED / f"{mid}.json", data)
                try:
                    path.unlink()
                except OSError:
                    pass
                folder_now = "completed"
            else:
                _atomic_write(path, data)
                folder_now = "approved"
        else:
            folder_now = "approved"
        log_event(
            "download_chunk",
            id=mid,
            index=item["index"],
            name=item["name"],
            chunk=chunk_idx + 1,
            total=total,
            bytes=len(piece),
            completed=is_last,
        )
        lines = [
            f"Chunk {chunk_idx + 1}/{total} of item {item['index']} ({item['name']}), "
            f"{len(piece)} bytes this chunk, file {len(raw)} bytes, sha256={actual_sha}.",
            f"Manifest is {folder_now}." + (" Item marked completed." if is_last else ""),
            "Decode this chunk with:",
            f"```python\nimport base64;open({item['name']!r},'ab' if {chunk_idx} else 'wb').write(base64.b64decode(\"{b64}\"))\n```",
            "Base64:",
            f"```\n{b64}\n```",
        ]
        return "\n".join(lines)
    except ValueError as exc:
        msg = str(exc)
        log_event("download_refused", id=mid, index=index, error=msg[:160])
        return msg if msg.startswith("Error:") else f"Error: {msg}"
    except Exception as exc:
        log_event("download_error", id=mid, index=index, error=type(exc).__name__)
        return f"Error: download failed: {exc}"
    finally:
        os.close(fd)


def format_pending() -> str:
    """Human-readable summary of every pending manifest (for xfer_pending.sh)."""
    fd = _lock_fd()
    try:
        _ensure_dirs()
        files = sorted(PENDING.glob("xf_*.json"))
        # Expire first
        summaries = []
        for path in files:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            path, data, folder = _maybe_expire(path, data, "pending")
            if folder != "pending":
                continue
            mid = data.get("id")
            items = data.get("items") or []
            total = data.get("batch_size_bytes") or sum(int(i.get("size_bytes") or 0) for i in items)
            left = max(0, int(data.get("expires") or 0) - int(time.time()))
            mins, secs = divmod(left, 60)
            lines = [
                f"=== Manifest {mid} PENDING ===",
                f"Reason: {data.get('reason') or '(none)'}",
                f"Expires: {_fmt_ts(int(data.get('expires') or 0))} (in {mins}m {secs}s)",
                f"Items ({len(items)}), total {total} bytes:",
            ]
            for it in items:
                arrow = "->" if it.get("direction") == "to_box" else "<-"
                sha = it.get("sha256") or "(sha256 pending)"
                lines.append(
                    f"  [{it.get('index')}] {it.get('direction'):8} {it.get('name')} "
                    f"{arrow} {it.get('path')}  ({it.get('size_bytes')} bytes, {sha[:16]}…)"
                )
            lines.append(f"Approve: /workspace/mcp-test/xfer_approve.sh {mid} all")
            lines.append(f"         /workspace/mcp-test/xfer_approve.sh {mid} 0,1")
            lines.append(f"         /workspace/mcp-test/xfer_approve.sh {mid} deny")
            summaries.append("\n".join(lines))
        if not summaries:
            return "No pending transfer manifests."
        return "\n\n".join(summaries)
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# MCP-facing wrappers (no arming; never approve)
# ---------------------------------------------------------------------------
def xfer_propose(items: Any, reason: str, ttl_minutes: int = DEFAULT_TTL_MIN) -> str:
    """Propose a batch of file transfers. Returns PENDING and waits for approval in Grok Bot chat. Does not need an armed session. Approval is not an MCP tool."""
    return propose(items, reason, ttl_minutes)


def xfer_status(id: str) -> str:
    """Status of a transfer manifest: pending, approved(subset), denied, expired, or completed, plus per-item state."""
    return status(id)


def xfer_upload(id: str, index: int, b64_chunk: str, chunk_no: int, total_chunks: int, sha256: str) -> str:
    """Upload one base64 chunk (~12 KB) of an approved to_box item. The file is assembled under xfer/box/ and verified against the approved name, size, and sha256. Each item can be transferred once."""
    return upload(id, index, b64_chunk, chunk_no, total_chunks, sha256)


def xfer_download(id: str, index: int, chunk_no: int) -> str:
    """Download one base64 chunk of an approved from_box item, plus the file sha256. Each item can be transferred once (completed after the last chunk)."""
    return download(id, index, chunk_no)


def main(argv: list[str] | None = None) -> int:
    import sys
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"-h", "--help", "help"}:
        print(
            "usage:\n"
            "  xfer_tools.py pending\n"
            "  xfer_tools.py approve <id> all|<indexes>|deny\n"
            "  xfer_tools.py status <id>",
            file=sys.stderr,
        )
        return 2
    cmd = args[0]
    if cmd == "pending":
        print(format_pending())
        return 0
    if cmd == "approve" and len(args) >= 3:
        print(approve(args[1], args[2]))
        return 0
    if cmd == "status" and len(args) >= 2:
        print(status(args[1]))
        return 0
    print("usage: xfer_tools.py pending|approve|status ...", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
