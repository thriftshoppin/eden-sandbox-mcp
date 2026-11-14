#!/usr/bin/env python3
"""Streamable HTTP MCP server: public dummy/browser tools at /mcp, and
sandboxed desktop tools at a secret /d/<key>/mcp path.
"""

import asyncio
from contextlib import asynccontextmanager
import hmac
import logging
import os
import re
import secrets
from datetime import datetime
from pathlib import Path as FilePath
from zoneinfo import ZoneInfo

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

import browser_tools
import desktop_tools
import xfer_tools

DIR = FilePath("/workspace/mcp-test")
KEY_FILE = DIR / "desktop.key"
DESK_SHOTS = DIR / "desk_shots"
_SHOTS = DIR / "shots"
_SHOT_NAME = re.compile(r"^[A-Za-z0-9._-]+\.(?:png|jpe?g|webp|gif)$", re.I)


def _load_or_create_key() -> str:
    if KEY_FILE.is_file():
        key = KEY_FILE.read_text(encoding="utf-8").strip()
        if len(key) >= 32:
            try:
                os.chmod(KEY_FILE, 0o600)
            except OSError:
                pass
            return key
    key = secrets.token_urlsafe(32)
    fd = os.open(str(KEY_FILE), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(key + "\n")
    os.chmod(KEY_FILE, 0o600)
    return key


DESK_KEY = _load_or_create_key()
DESK_PREFIX = f"/d/{DESK_KEY}"


class _RedactSecret(logging.Filter):
    """Never write the desktop path key to logs."""

    @staticmethod
    def _hide(value):
        if isinstance(value, str) and DESK_KEY and DESK_KEY in value:
            return value.replace(DESK_KEY, "****")
        return value

    def filter(self, record: logging.LogRecord) -> bool:
        if not DESK_KEY:
            return True
        if isinstance(record.msg, str):
            record.msg = self._hide(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(self._hide(a) for a in record.args)
        elif isinstance(record.args, dict):
            record.args = {k: self._hide(v) for k, v in record.args.items()}
        return True


for _name in ("", "uvicorn", "uvicorn.access", "uvicorn.error", "mcp"):
    logging.getLogger(_name).addFilter(_RedactSecret())


# ---------------------------------------------------------------------------
# Public dummy + browser tools (unchanged set of 12)
# ---------------------------------------------------------------------------
mcp = MCPServer(
    name="mcp-test",
    instructions=(
        "Dummy tools for testing Claude custom connectors, including a sandboxed "
        "headless browser. No auth, no saved logins, and only public http(s) URLs. "
        "Use browse, screenshot, and view_image to look at webpages and images; "
        "use click, type_text, scroll, and page_text to interact with the current page; "
        "use inspect to read an element's styles and pixel colors, and describe_page "
        "for a voice-friendly layout summary. Screenshots include a public /shots/ URL "
        "and color palette."
    ),
)


@mcp.tool()
def echo(text: str) -> str:
    """Return the input text unchanged."""
    return text


@mcp.tool()
def get_time(timezone: str = "America/Chicago") -> str:
    """Return the current time in ISO 8601 for an optional IANA timezone (default America/Chicago)."""
    return datetime.now(ZoneInfo(timezone)).isoformat()


@mcp.tool()
def add(a: float, b: float) -> float:
    """Add two numbers and return the sum."""
    return a + b


mcp.tool()(browser_tools.browse)
mcp.tool()(browser_tools.screenshot)
mcp.tool()(browser_tools.view_image)
mcp.tool()(browser_tools.click)
mcp.tool()(browser_tools.type_text)
mcp.tool()(browser_tools.scroll)
mcp.tool()(browser_tools.page_text)
mcp.tool()(browser_tools.inspect)
mcp.tool()(browser_tools.describe_page)
mcp.tool()(browser_tools.pixels)
mcp.tool()(browser_tools.crop_b64)


@mcp.custom_route("/.well-known/oauth-protected-resource", methods=["GET", "HEAD"])
@mcp.custom_route("/.well-known/oauth-authorization-server", methods=["GET", "HEAD"])
@mcp.custom_route("/mcp/.well-known/oauth-protected-resource", methods=["GET", "HEAD"])
@mcp.custom_route("/mcp/.well-known/oauth-authorization-server", methods=["GET", "HEAD"])
async def oauth_discovery_absent(request: Request) -> Response:
    return Response(status_code=404, content=b"Not Found", media_type="text/plain")


# ---------------------------------------------------------------------------
# Desktop tools (secret path only)
# ---------------------------------------------------------------------------
desk = MCPServer(
    name="mcp-desktop",
    instructions=(
        "Desktop tools for this computer's own screen. Action tools "
        "(focus, click, click_text, type, paste_text, key, scroll, launch, hangup) refuse "
        "unless the session is armed. status, screenshot, windows, pixels, and "
        "crop_b64 always work and do not change the arming state. "
        "There is no shell and no general file-write tool. launch is limited to a "
        "browser, a text editor, a file manager, and a terminal. Screenshots include a "
        "secret /shots/ URL, window list, colors, and OCR text for voice clients. "
        "Gated file transfer uses xfer_propose, xfer_status, xfer_upload, and "
        "xfer_download. Propose does not need arming. Nothing is transferred until "
        "a human approves the batch outside MCP (xfer_approve.sh); there is no MCP "
        "tool that can approve a batch."
    ),
)


@desk.tool(name="desk_status")
def desk_status() -> str:
    """Armed state, time left, screen size, and the active window."""
    return desktop_tools.desk_status()


@desk.tool(name="desk_screenshot")
def desk_screenshot() -> list:
    """Capture the desktop. Returns an image plus voice-friendly text (shot URL, windows, palette, OCR)."""
    return desktop_tools.desk_screenshot()


@desk.tool(name="desk_windows")
def desk_windows() -> str:
    """List top-level windows on this computer's display."""
    return desktop_tools.desk_windows()


@desk.tool(name="desk_pixels")
def desk_pixels(x: float, y: float, w: float = 1, h: float = 1, source: str = "desktop") -> str:
    """Exact RGB hex for a small desktop region (capped at 32×32), plus average and dominant colors. Works while disarmed; does not change arming."""
    return desktop_tools.desk_pixels(x, y, w, h, source)


@desk.tool(name="desk_crop_b64")
def desk_crop_b64(x: float, y: float, w: float, h: float, scale: float = 1.0, format: str = "png") -> str:
    """Return a desktop crop as base64 text (capped at ~12 KB) plus a /shots/ URL. Works while disarmed; does not change arming."""
    return desktop_tools.desk_crop_b64(x, y, w, h, scale, format)


@desk.tool(name="desk_focus")
def desk_focus(window: str) -> str:
    """Focus a window by title substring or window id (0x…). Requires an armed session."""
    return desktop_tools.desk_focus(window)


@desk.tool(name="desk_click")
def desk_click(x: float, y: float, button: str = "left", double: bool = False) -> str:
    """Click at screen pixels (x, y). button is left, right, or middle. Requires an armed session."""
    return desktop_tools.desk_click(x, y, button, double)


@desk.tool(name="desk_click_text")
def desk_click_text(text: str) -> str:
    """Find on-screen text with OCR and click the first match. Requires an armed session."""
    return desktop_tools.desk_click_text(text)


@desk.tool(name="desk_type")
def desk_type(text: str) -> str:
    """Type text into the focused window as real keystrokes. Newlines become Return, tabs become Tab. Focuses the window first and returns the character count sent. Requires an armed session."""
    return desktop_tools.desk_type(text)


@desk.tool(name="desk_paste_text")
def desk_paste_text(text: str) -> str:
    """Paste text into the focused window (internal clipboard + ctrl+v). Faster and more reliable for long or multi-line text. Requires an armed session. Not a standalone clipboard-set tool."""
    return desktop_tools.desk_paste_text(text)


@desk.tool(name="desk_key")
def desk_key(combo: str) -> str:
    """Press a key or combination, e.g. 'Return', 'ctrl+s', 'alt+Tab'. Requires an armed session."""
    return desktop_tools.desk_key(combo)


@desk.tool(name="desk_scroll")
def desk_scroll(direction: str = "down", amount: int = 3) -> str:
    """Scroll at the current pointer. direction is up, down, left, or right. Requires an armed session."""
    return desktop_tools.desk_scroll(direction, amount)


@desk.tool(name="desk_launch")
def desk_launch(app: str) -> str:
    """Start an allowlisted app: browser, editor, file manager, or terminal. Requires an armed session."""
    return desktop_tools.desk_launch(app)


@desk.tool(name="desk_hangup")
def desk_hangup() -> str:
    """Disarm the desktop session immediately."""
    return desktop_tools.desk_hangup()


@desk.tool(name="xfer_propose")
def xfer_propose(items: list, reason: str, ttl_minutes: int = 15) -> str:
    """Propose a batch of file transfers (to_box or from_box) under xfer/box/. Returns PENDING and waits for approval in Grok Bot chat. Does not need an armed session. There is no MCP tool that can approve a batch."""
    return xfer_tools.xfer_propose(items, reason, ttl_minutes)


@desk.tool(name="xfer_status")
def xfer_status(id: str) -> str:
    """Status of a transfer manifest: pending, approved(subset), denied, expired, or completed, plus per-item state."""
    return xfer_tools.xfer_status(id)


@desk.tool(name="xfer_upload")
def xfer_upload(id: str, index: int, b64_chunk: str, chunk_no: int, total_chunks: int, sha256: str) -> str:
    """Upload one ~12 KB base64 chunk of an approved to_box item. Assembled into xfer/box/<name> and verified against the approved name, size, and sha256. Each item can be transferred once."""
    return xfer_tools.xfer_upload(id, index, b64_chunk, chunk_no, total_chunks, sha256)


@desk.tool(name="xfer_download")
def xfer_download(id: str, index: int, chunk_no: int) -> str:
    """Download one base64 chunk of an approved from_box item, plus the file sha256. Each item can be transferred once."""
    return xfer_tools.xfer_download(id, index, chunk_no)


@desk.custom_route("/.well-known/oauth-protected-resource", methods=["GET", "HEAD"])
@desk.custom_route("/.well-known/oauth-authorization-server", methods=["GET", "HEAD"])
async def desk_oauth_absent(request: Request) -> Response:
    return Response(status_code=404, content=b"Not Found", media_type="text/plain")


# ---------------------------------------------------------------------------
# ASGI: public /mcp + secret /d/<key>/mcp, 404 for any other /d/*
# ---------------------------------------------------------------------------
_security = TransportSecuritySettings(enable_dns_rebinding_protection=False)

public_app = mcp.streamable_http_app(
    streamable_http_path="/mcp",
    stateless_http=True,
    host="0.0.0.0",
    transport_security=_security,
)

desk_app = desk.streamable_http_app(
    streamable_http_path=f"{DESK_PREFIX}/mcp",
    stateless_http=True,
    host="0.0.0.0",
    transport_security=_security,
)


async def _serve_file(scope, receive, send, root: FilePath, name: str) -> None:
    if not _SHOT_NAME.match(name or ""):
        await Response(status_code=404, content=b"Not Found")(scope, receive, send)
        return
    path = (root / name).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError:
        await Response(status_code=404, content=b"Not Found")(scope, receive, send)
        return
    if not path.is_file():
        await Response(status_code=404, content=b"Not Found")(scope, receive, send)
        return
    suffix = path.suffix.lower()
    media = "image/jpeg" if suffix in {".jpg", ".jpeg"} else f"image/{suffix.lstrip('.')}"
    await FileResponse(
        path,
        media_type=media,
        headers={"Cache-Control": "public, max-age=3600"},
    )(scope, receive, send)


def _desk_key_from_path(path: str) -> str | None:
    """Return the /d/<segment> key if path is under /d/, else None."""
    if not path.startswith("/d/"):
        return None
    rest = path[len("/d/"):]
    seg = rest.split("/", 1)[0]
    return seg or None


async def _probe_asgi(scope, receive, send):
    if scope.get("type") != "http":
        await public_app(scope, receive, send)
        return
    method = scope.get("method") or ""
    path = scope.get("path", "") or ""

    # Secret desktop prefix.
    if path.startswith("/d/"):
        got = _desk_key_from_path(path)
        if not got or not hmac.compare_digest(got, DESK_KEY):
            await Response(status_code=404, content=b"Not Found")(scope, receive, send)
            return
        # Desktop screenshots live under the secret prefix, not the public /shots/.
        if path.startswith(f"{DESK_PREFIX}/shots/"):
            await _serve_file(scope, receive, send, DESK_SHOTS, path[len(f"{DESK_PREFIX}/shots/"):])
            return
        if method in ("GET", "HEAD") and path.rstrip("/") == f"{DESK_PREFIX}/mcp":
            await JSONResponse(
                {"ok": True, "name": "mcp-desktop", "transport": "streamable-http"},
                status_code=200,
                headers={"Allow": "GET, POST, DELETE", "Cache-Control": "no-store"},
            )(scope, receive, send)
            return
        await desk_app(scope, receive, send)
        return

    # Public browser shots and /mcp probe.
    if path.startswith("/shots/"):
        await _serve_file(scope, receive, send, _SHOTS, path[len("/shots/"):])
        return
    if method in ("GET", "HEAD") and path.rstrip("/") == "/mcp":
        await JSONResponse(
            {"ok": True, "name": "mcp-test", "transport": "streamable-http"},
            status_code=200,
            headers={"Allow": "GET, POST, DELETE", "Cache-Control": "no-store"},
        )(scope, receive, send)
        return
    await public_app(scope, receive, send)


@asynccontextmanager
async def _combined_lifespan(_app):
    # Both Streamable HTTP session managers must be running or POSTs 500.
    async with public_app.router.lifespan_context(public_app):
        async with desk_app.router.lifespan_context(desk_app):
            yield


_lifespan_app = Starlette(lifespan=_combined_lifespan)


if __name__ == "__main__":
    os.environ.setdefault("DISPLAY", desktop_tools.DISPLAY)
    _prewarm_scheduled = False

    async def app(scope, receive, send):
        global _prewarm_scheduled
        if scope.get("type") == "lifespan":
            await _lifespan_app(scope, receive, send)
            return
        if not _prewarm_scheduled:
            _prewarm_scheduled = True
            try:
                asyncio.get_running_loop().create_task(browser_tools.prewarm_browser())
            except Exception:
                pass
        await _probe_asgi(scope, receive, send)

    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
