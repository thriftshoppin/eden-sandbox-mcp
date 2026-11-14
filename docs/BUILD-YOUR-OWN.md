# Build your own MCP tools

The point of this repository is to show that a useful MCP connector is a small, ordinary program. You can start with one function, add tools as you need them, and only then worry about tunnels, supervision, and voice clients.

Read the source in the repo after you have tried the steps below. **Write your own server.** This tree is published for reading and learning, under an all-rights-reserved license. Do not treat it as a drop-in product, and do not attempt to access the author's deployed instances.

When you are ready to put *your* server on a public HTTPS URL, continue with [EXPOSING-MCP-OVER-HTTPS.md](EXPOSING-MCP-OVER-HTTPS.md).

## The path

Each step adds one idea:

1. A minimal Streamable HTTP server with an `echo` tool.
2. A clock tool, so you can see how a second tool is just another function.
3. A browser screenshot tool that returns **text** as well as an image, because voice mode only sees text.
4. A desktop action behind a **secret URL path** and a **time-boxed arming gate** that only a human can open.
5. A **batch transfer** pattern: the model proposes a manifest, a human approves it out of band, and only then do bytes move.

The snippets are written fresh for this document. They are illustrations, not a second copy of the repo.

## Prerequisites

- Python 3.11 or newer
- A virtualenv and `pip install 'mcp==2.3.0' uvicorn`
- For step 3, Playwright and a Chromium build
- For steps 4–5, a Linux desktop or Xvfb display you control, plus basic CLI tools

Run everything on a machine you own. Bind to localhost while you are learning; expose the port only after you have read the HTTPS guide.

## 1. A minimal server and an echo tool

Streamable HTTP is the transport Claude custom connectors use. Run the server **stateless**, on `0.0.0.0` if a tunnel will need to reach it, and turn DNS-rebinding protection off if the public `Host` header will be your tunnel hostname rather than `localhost`.

Two small extras matter in practice:

- Answer **GET `/mcp` with JSON immediately**. If GET falls through to the SSE stream, health checks and some clients appear to hang.
- Return **404** on `/.well-known/oauth-protected-resource` and `/.well-known/oauth-authorization-server`. Otherwise a client may try to start an OAuth flow you never implemented.

```python
#!/usr/bin/env python3
"""Minimal Streamable HTTP MCP server with one tool. For learning."""

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
import uvicorn

mcp = MCPServer(
    name="learn-mcp",
    instructions="A tiny learning server. Only an echo tool.",
)

@mcp.tool()
def echo(text: str) -> str:
    """Return the input text unchanged."""
    return text

@mcp.custom_route("/.well-known/oauth-protected-resource", methods=["GET", "HEAD"])
@mcp.custom_route("/.well-known/oauth-authorization-server", methods=["GET", "HEAD"])
@mcp.custom_route("/mcp/.well-known/oauth-protected-resource", methods=["GET", "HEAD"])
@mcp.custom_route("/mcp/.well-known/oauth-authorization-server", methods=["GET", "HEAD"])
async def no_oauth(request: Request) -> Response:
    return Response(status_code=404, content=b"Not Found")

security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
app = mcp.streamable_http_app(
    streamable_http_path="/mcp",
    stateless_http=True,
    host="0.0.0.0",
    transport_security=security,
)

async def asgi(scope, receive, send):
    if scope.get("type") == "http":
        method = scope.get("method") or ""
        path = (scope.get("path") or "").rstrip("/")
        if method in ("GET", "HEAD") and path == "/mcp":
            await JSONResponse(
                {"ok": True, "name": "learn-mcp", "transport": "streamable-http"},
                headers={"Allow": "GET, POST, DELETE", "Cache-Control": "no-store"},
            )(scope, receive, send)
            return
    await app(scope, receive, send)

if __name__ == "__main__":
    uvicorn.run(asgi, host="0.0.0.0", port=8000)
```

You now have a program you can talk to locally:

```bash
curl -sS http://127.0.0.1:8000/mcp
# {"ok":true,"name":"learn-mcp","transport":"streamable-http"}
```

A `tools/list` POST (see the HTTPS guide for a full initialize + list example) should show `echo`. If that works, the hard part of "is this even MCP?" is already behind you.

## 2. Add a clock

A second tool is another decorated function. Nothing else about the server has to change.

```python
from datetime import datetime
from zoneinfo import ZoneInfo

@mcp.tool()
def get_time(timezone: str = "America/Chicago") -> str:
    """Return the current time in ISO 8601 for an IANA timezone."""
    return datetime.now(ZoneInfo(timezone)).isoformat()
```

That is the whole pattern: name the tool, describe it in the docstring (clients show that to the model), return a string or a number. Add `add(a, b)` the same way if you want a third confidence check.

Restart the process and list tools again. You should see `echo` and `get_time`. This is also a good moment to put the server behind a tunnel, still with no auth, and add it in Claude as a custom connector. The HTTPS tutorial covers that recipe end to end.

## 3. A browser screenshot that voice mode can use

If you return only an image, **Claude voice mode will not see it**. Voice clients receive the text of a tool result. So a screenshot tool has to describe what it saw in words: a short caption, dominant colors, OCR if you have it, maybe a public URL to the file, maybe a tiny base64 crop.

Keep the browser in its own profile, and do not let it fetch private addresses. Resolve the host (DoH is useful if your environment rewrites DNS), then refuse loopback, link-local, private, multicast, reserved, and unspecified IPs.

```python
from mcp.server.mcpserver import Image

@mcp.tool()
async def screenshot() -> list:
    """Capture the current page. Returns an image plus voice-friendly text."""
    png_bytes, width, height, palette, ocr, shot_url = await capture_current_page()
    summary = (
        f"Screenshot {width}x{height}. Colors: {palette}. "
        f"Text: {ocr or '(none)'}. File: {shot_url}"
    )
    return [
        Image(data=png_bytes, format="png"),
        summary,
    ]
```

`capture_current_page()` is left for you: Playwright Chromium with a tight timeout, a viewport you choose, compression so the payload stays small, and a shots directory with a cap on how many files you keep. The important design choice is already in the return value — **always include the text**. The same idea applies to `view_image`, `describe_page`, `pixels`, and `crop_b64`.

Put browser tools on the *public* endpoint only if they are limited to public http(s) URLs. The moment a tool can see your LAN or your logged-in desktop browser, it no longer belongs on an unauthenticated path.

## 4. A gated desktop tool, with arming

Desktop control is a different class of power. Three rules showed up immediately in this project:

**Secret path as auth.** Claude cannot set custom headers, so a Bearer token is not workable. A long random key in the URL — `/d/<key>/mcp` — is the auth. Load the key from a mode-600 file, compare it with `hmac.compare_digest`, and return 404 for every other `/d/...` path. Redact the key from logs. Never commit the file.

**A human arms the session.** The model should not be able to turn its own desktop tools on. A small CLI on the box (`desk_arm.sh 20`) writes a state file with an expiry. Action tools (`click`, `type`, `launch`, …) refuse unless that state is armed. Status and screenshot can stay available so you can see the screen while disarmed.

**Time-box and idle-out.** An armed session should die of old age (for example 15 minutes, max 60) and also after a few minutes with no actions (five, in this project). A `hangup` tool can disarm immediately. Launch only allowlisted apps. Do not expose a shell or a general file-write tool.

```python
import hmac, time
from pathlib import Path

KEY = Path("desktop.key").read_text().strip()

def require_armed() -> str | None:
    state = read_arm_state()  # your JSON file
    now = int(time.time())
    if not state.get("armed"):
        return "DISARMED. Ask a human to run desk_arm.sh."
    if now >= state.get("expires", 0):
        disarm("expired")
        return "DISARMED (expired)."
    if now - state.get("last_activity", now) >= 300:
        disarm("idle")
        return "DISARMED (idle)."
    return None

@desk.tool()
def desk_click(x: float, y: float) -> str:
    """Click at screen pixels. Requires an armed session."""
    if err := require_armed():
        return err
    touch_activity()
    click_at(x, y)  # xdotool, etc.
    return f"clicked {x},{y}"

def path_key_ok(segment: str) -> bool:
    return bool(segment) and hmac.compare_digest(segment, KEY)
```

Mount the desktop MCP app only under `/d/<key>/mcp`. Serve desktop screenshots under that same prefix, not on the public `/shots/` path. The public `/mcp` server and the desktop server can share one process; they should not share one URL.

## 5. Batch-approved file transfer

If a model can move files, it should not be able to approve its own request. The pattern used here is:

1. The model calls `xfer_propose` with every item, direction (`to_box` / `from_box`), size, destination, a reason, and a TTL.
2. A human reads the manifest with a CLI (`xfer_pending.sh`) and sees the full destination path for every file.
3. The human approves all, trims the list, or denies (`xfer_approve.sh <id> all|0,2|deny`).
4. Only then can upload or download tools move bytes, in small chunks, **once per item**, with a sha256 check and a log line. There is **no** MCP tool that approves a batch.

Caps keep a bad manifest from becoming a bad surprise. The numbers in this repo are 20 items, 10 MB per file, 50 MB per batch, and a TTL between 1 and 60 minutes. Reject symlinks. Confine both sides of the transfer to a single directory you chose in advance.

```python
# Illustrative shape of a manifest — not a complete implementation.
manifest = {
    "id": "xf_example",
    "reason": "send the session notes to the box",
    "ttl_minutes": 15,
    "expires": 1763140000,
    "items": [
        {
            "index": 0,
            "direction": "to_box",
            "name": "notes.txt",
            "dest": "xfer/box/notes.txt",
            "size_bytes": 1842,
            "sha256": "ab…",
            "state": "pending",
        }
    ],
}

def xfer_propose(items: list, reason: str, ttl_minutes: int = 15) -> str:
    """Propose a batch. Does not move any bytes. Cannot approve itself."""
    validate_limits(items, ttl_minutes)
    mid = save_pending(items, reason, ttl_minutes)
    return f"PENDING {mid}. Waiting for a human to approve on the CLI."
```

Approvals outrank anything the model types or requests later. If you are designing a console that also has a kill switch (the Eden Link idea in [WIP.md](../WIP.md)), keep that same ranking: a pending approval is not something an agent can talk its way around.

## What to do next

- Expose *your* server: [EXPOSING-MCP-OVER-HTTPS.md](EXPOSING-MCP-OVER-HTTPS.md).
- Read `server.py`, `browser_tools.py`, `desktop_tools.py`, and `xfer_tools.py` in this repo as a worked example of the same ideas, with more edge cases filled in (OCR, crop caps, logging, SSRF, watchdog integration).
- Keep secrets out of git: a `.gitignore`, a `desktop.key.example`, and a `.env.example` are part of the lesson.

Build your own. Keep your keys and hostnames to yourself.
