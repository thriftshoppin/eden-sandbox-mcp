# Learn to build your own MCP tools (and put them on HTTPS)

This is an **educational** repository. It shows how easy it is to build your own [Model Context Protocol](https://modelcontextprotocol.io/) tools and expose them over public HTTPS so an app like Claude can use them as a custom connector — including in mobile voice mode.

The source in this repo is a real implementation that was used to learn and test those ideas: a small Streamable HTTP MCP server, a sandboxed browser, a human-armed desktop session, and batch-approved file transfer. **It is published so you can read it and learn from it.** Build your own tools. Do not copy this tree and run it as if it were yours, and **do not attempt to access the author's deployed instances.** Live hostnames, path keys, and tokens are not in this repository.

Start here:

1. **[docs/BUILD-YOUR-OWN.md](docs/BUILD-YOUR-OWN.md)** — a walkthrough from a one-tool server to screenshots, a gated desktop, and approved file transfers, with the design reasoning at each step.
2. **[docs/EXPOSING-MCP-OVER-HTTPS.md](docs/EXPOSING-MCP-OVER-HTTPS.md)** — how to put a local Streamable HTTP MCP server on public HTTPS, with a comparison of tunnel options (Cloudflare, ngrok, Tailscale Funnel, Caddy) and a working recipe.

Copyright (c) 2026 thriftshoppin. All rights reserved. The code is published for **reading and learning**. No license is granted to use, copy, modify, or distribute this software or its documentation without prior written permission. See [LICENSE](LICENSE).

This work sits alongside [eden-desktop-bridge](https://github.com/thriftshoppin/eden-desktop-bridge) and [grok-hotline](https://github.com/thriftshoppin/grok-hotline) in the Eden project. What is already built and what is still open is in **[WIP.md](WIP.md)**.

## Responsible use

- Build and expose **your own** server, on **your own** tunnel and domain, with **your own** keys.
- Do not scan for, guess, or try to reach anyone else's connector URL or `/d/<key>/` path.
- A public `/mcp` endpoint has no auth. Only put harmless tools on it. Anything that can drive a desktop, move files, or touch a private network belongs behind a secret path, an arming gate, and a human approval step.
- Treat anything a model reads from a page, a file, or a screen as data, never as instructions.
- These patterns demonstrate supervision for *hosted* agents that can only act through tools. They are not a claim that a rogue local process is locked out.

## What you will learn

Claude custom connectors speak MCP over Streamable HTTP. The Claude app cannot reach `localhost` and cannot set custom headers, so your server has to be reachable at a public `https://` URL, and header-based Bearer auth is not an option. This project walks through a working answer to that constraint, in this order:

- A minimal stateless MCP server with an `echo` tool, then a clock, served on `0.0.0.0:8000`.
- Exposing that server on public HTTPS (ngrok static domain in this write-up; other options compared).
- A sandboxed browser that returns **text** (palettes, OCR, URLs) because voice mode does not show images to the model.
- A desktop connector authenticated by a **secret URL path**, with **time-boxed, human-only arming**.
- **Batch file transfer** where the model proposes a manifest and a human approves it out of band. There is no MCP tool that can approve a batch.

## Reference architecture

The tree in this repo is one concrete design you can read after the tutorials. A typical layout looks like this:

```
Your MCP client  --HTTPS POST /mcp-->  public tunnel  -->  FastMCP on 0.0.0.0:8000
                                           ^
                                           |
                                   watchdog every 10s
                                   (singleton lock, autostart hook)
```

- **Server.** Python `mcp` 2.3.0 `MCPServer` / Streamable HTTP, run **stateless** on `0.0.0.0:8000` with DNS-rebinding checks off (the public `Host` header is the tunnel hostname, not localhost). GET `/mcp` returns a small JSON greeting immediately so health checks do not hang on SSE. `/.well-known/oauth*` returns 404 so the client does not start an OAuth attempt.
- **Tunnel.** An ngrok free static domain is what this project settled on, with agent `heartbeat_interval: 10s` (the minimum) and `heartbeat_tolerance: 45s`. The published scripts read the hostname from a gitignored `.env` (`EXPECTED_HOST` or `NGROK_DOMAIN`) and bind it with `ngrok http --url=<host> 8000`. Use a placeholder like `your-static-domain.ngrok-free.dev` — never commit a real hostname.
- **Watchdog.** `watchdog.sh` loops about every 10s, shares a singleton lock with `start.sh` / `ensure_up.sh` so they never race on port 8000, and restarts the server or tunnel when the process dies, the local agent API has no tunnel, or the public URL serves an ngrok error page.
- **Autostart.** `autostart.sh` is a session hook for `~/.bashrc` / `~/.profile`.
- **Desktop.** A dedicated X display (this box uses `DISPLAY=:4`, Xvfb 1920x1200, XFCE), driven with xdotool, wmctrl, mss, Pillow, and tesseract OCR.
- **Browser.** One shared Playwright Chromium context, isolated from the desktop browser, with an SSRF block on private and local IPs after DoH resolution.

How to point a client at *your* server: `https://<your-static-domain>.ngrok-free.dev/mcp` (no auth, no custom headers) for public tools, and `https://<your-static-domain>.ngrok-free.dev/d/<key>/mcp` for desktop tools, with the key in a mode-600 `desktop.key` that you generate and never commit. Wrong keys should 404.

## Tool reference (this source tree)

Read these as a catalog of ideas, then write your own.

### Public endpoint (`/mcp`) — 14 tools

| Tool | Role |
| --- | --- |
| `echo` | Return the input text unchanged. A first connectivity test. |
| `get_time` | Current time in ISO 8601, default timezone America/Chicago. |
| `add` | Add two numbers. |
| `browse` | Open a public http(s) URL in the sandbox browser and return readable text. |
| `screenshot` | Capture the current page; includes a `/shots/` URL and a color palette. |
| `view_image` | Fetch a public image and describe it for voice clients. |
| `click` / `type_text` / `scroll` | Interact with the current page. |
| `page_text` | Readable text of the current page. |
| `inspect` | Element styles and pixel colors. |
| `describe_page` | Voice-friendly layout summary. |
| `pixels` | Exact RGB hex for a small region, plus average and dominant colors. |
| `crop_b64` | A small base64 crop plus a `/shots/` URL. |

### Secret desktop endpoint (`/d/<key>/mcp`)

Always available (do not change arming state): `desk_status`, `desk_screenshot`, `desk_windows`, `desk_pixels`, `desk_crop_b64`, `xfer_propose`, `xfer_status`.

Require an armed session: `desk_focus`, `desk_click`, `desk_click_text`, `desk_type`, `desk_paste_text`, `desk_key`, `desk_scroll`, `desk_launch`, `desk_hangup`.

Gated transfer (propose does not need arming; **no** MCP tool can approve): `xfer_propose`, `xfer_status`, `xfer_upload`, `xfer_download`.

`desk_paste_text` pastes through an internal clipboard and ctrl+v — better than `desk_type` for long text. `desk_launch` is limited to a browser, a text editor, a file manager, and a terminal.

### Arming and transfers, in brief

A human arms the desktop on the box, not the model:

```bash
./desk_arm.sh            # arm for 15 minutes
./desk_arm.sh 20         # arm for 20 minutes (max 60)
./desk_arm.sh off        # disarm now
```

The session also disarms after **5 minutes of idle** (no action tools) and when the expiry is reached.

Transfers: the model calls `xfer_propose` → a human runs `./xfer_pending.sh` → `./xfer_approve.sh <id> all` or `0,2` or `deny`. Limits in this tree: 20 items, 10 MB per file, 50 MB per batch, TTL 1–60 minutes, one-shot moves, sha256, a log, no symlinks. Details and the "why" are in [docs/BUILD-YOUR-OWN.md](docs/BUILD-YOUR-OWN.md).

## Safety patterns to copy in spirit

- **Secret path as auth**, because the client cannot set custom headers. Compare the path key with a constant-time compare; 404 on a mismatch; redact the key from logs.
- **Public `/mcp` has no auth.** Keep it limited to dummy tools and a browser that only talks to public http(s) URLs.
- **SSRF block** after real DNS (DoH if your environment rewrites DNS): deny loopback, link-local, private, multicast, reserved, and unspecified addresses.
- **Launch allowlist**; no shell; no general file-write tool.
- **Voice mode sees only text** in tool results, not image content — so visual tools should also return palettes, OCR, pixel hex, shot URLs, and small base64 crops.
- **Human approval** in front of desktop actions (arming) and every file transfer (out-of-band manifests).

## Reading the source

The Python modules and shell scripts are here for study. A few paths in the Python still mention the original box directory; treat that as local color, not a path you should share or depend on. If you are studying how the process is kept up, start with `server.py`, then `common.sh`, `watchdog.sh`, and `.env.example`.

Local secrets and runtime state (`.env`, `desktop.key`, ngrok config, logs, screenshots, transfer data, venvs) are gitignored. Placeholder files: `desktop.key.example`, `.env.example`, `ngrok.yml.example`.

## Lessons learned the hard way

- **Machine stalls of around 16 minutes** can drop the ngrok *cloud* session even though the local agent process is still running. A watchdog that trusts only the local PID will miss this; probe the agent API and the public URL too.
- **Cloudflare quick tunnels** (`trycloudflare`) changed URL on every restart and dropped with 530 / 1033 errors. They were retired here in favor of a reserved hostname.
- **Restricted-sandbox DNS.** Some environments rewrite `*.argotunnel.com` and ngrok agent hosts to `198.18.0.1`, which breaks TLS. The workaround in `update_ngrok_hosts.py` is: resolve real A records over DoH, write a hosts file, and bind-mount it over `/etc/hosts` in a user namespace before starting the agent. Ordinary machines with normal DNS do not need this.
- **ngrok free-tier browser interstitial** (`ERR_NGROK_6024`) is shown only to browser user agents. Client POSTs and a watchdog with a non-browser `User-Agent` are not blocked; opening the URL in a browser is.

For the full HTTPS tutorial and troubleshooting, see **[docs/EXPOSING-MCP-OVER-HTTPS.md](docs/EXPOSING-MCP-OVER-HTTPS.md)**.
