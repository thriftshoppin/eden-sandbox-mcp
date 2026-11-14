# Exposing a local Streamable HTTP MCP server over public HTTPS

Claude custom connectors cannot talk to `localhost`. They also cannot set custom headers, which rules out the usual "put a Bearer token on every request" pattern. If you want Claude — desktop, mobile, or voice mode — to call *your* MCP tools, you need a **public HTTPS URL** and an auth story that does not depend on headers: either no auth (harmless tools only) or a **secret path**.

This guide teaches that piece: how to take a local server on port 8000 and make it reachable at `https://<your-static-domain>.ngrok-free.dev/mcp`. It is the companion to [BUILD-YOUR-OWN.md](BUILD-YOUR-OWN.md). Build your own server and your own tunnel. Do not attempt to access the author's deployed instances; those URLs are not published here.

## Why Claude needs a public HTTPS URL

- The connector field in Claude is a URL the Claude cloud (and the mobile app) will POST to. `http://127.0.0.1:8000/mcp` is your machine, not theirs.
- There is no place to attach custom headers, so `Authorization: Bearer …` will never be sent. Design for **no auth** or **secret-path auth** (`/d/<key>/mcp`).
- The transport is **Streamable HTTP**. Responses are often SSE. A GET that waits on a stream will look like a hang; return a short JSON body on GET `/mcp` instead.
- If your server answers OAuth discovery documents, Claude may try OAuth. Return **404** on `/.well-known/oauth*` unless you actually implement it.

TLS has to terminate somewhere in front of your process. The rest of this document is about that "somewhere."

## Options compared

These are the options this project looked at, or considered and left on the table. Pick for *your* box; the tradeoffs are the lesson.

### Cloudflare quick tunnels (`cloudflared tunnel --url`, trycloudflare)

A one-liner gives you a public `https://*.trycloudflare.com` URL with no account required.

- **Pros:** Fast to try, real HTTPS, no DNS record to create.
- **Cons:** A **new random URL on every restart**, so you must re-add the connector in Claude each time. In this project the tunnels also **dropped**, often with Cloudflare **530** / **1033** errors. On a restricted sandbox whose DNS rewrites `*.argotunnel.com` to `198.18.0.1`, the agent could not even reach the edge without a DoH + bind-mount workaround.
- **Verdict here:** Useful for a ten-minute test. Retired as soon as a stable URL mattered.

### Cloudflare named tunnels (your own domain)

A named tunnel, a connector token, and a CNAME (or Cloudflare-proxied record) on a domain you control.

- **Pros:** A hostname you choose, that survives restarts; access policies if you want them; no ngrok interstitial.
- **Cons:** You need a domain and a Cloudflare account. Access policies that require a browser login will block Claude, which cannot complete them. Same sandbox-DNS caveat as quick tunnels if you are on a locked-down box.
- **Verdict:** A strong production choice if you already have a domain and you keep the public hostname pointed at an unauthenticated, or secret-path, MCP path.

### ngrok free static domain (what this project chose)

Reserve a free `*.ngrok-free.dev` hostname, authenticate the agent once, and bind that hostname to port 8000.

- **Pros:** The URL **does not change** across restarts. Setup is short. Agent heartbeats can be tightened (`heartbeat_interval: 10s`, the minimum; `heartbeat_tolerance: 45s`). Claude's POSTs are not browsers, so the free-tier **browser interstitial** (`ERR_NGROK_6024`) does not block them.
- **Cons:** The interstitial *does* appear if a human opens the URL in a browser. Free-tier limits apply. A long stall on the host (around 16 minutes in this environment) can drop the **cloud** session even when the local `ngrok` process is still alive — you need a watchdog that probes the public URL or the local agent API, not just the PID. Same class of DNS-rewrite issues as Cloudflare on some sandboxes.
- **Verdict:** The best fit here for "stable URL, no domain of our own, Claude as the only real client."

Setup sketch (use *your* reserved hostname and *your* token; never commit either):

```bash
# Run this yourself. Do not paste the entire command line into NGROK_AUTHTOKEN.
ngrok config add-authtoken <token>

# Optional: in ~/.config/ngrok/ngrok.yml (mode 600)
# version: "3"
# agent:
#   heartbeat_interval: 10s
#   heartbeat_tolerance: 45s
#   authtoken: <token>

ngrok http --url=your-static-domain.ngrok-free.dev 8000
```

The scripts in this repo read `EXPECTED_HOST` from a gitignored `.env` and pass `--url=$EXPECTED_HOST`.

### Tailscale Funnel

Funnel publishes a local port on your Tailnet's HTTPS name.

- **Pros:** Nice security story if every client is on your tailnet or you are comfortable with Funnel's public path; stable hostname; no third-party URL randomizer.
- **Cons:** Claude's servers are not on your tailnet. Funnel can expose a public HTTPS endpoint, but you must read the current Funnel access model carefully and still cannot attach custom headers. Requires the Tailscale daemon on the box.
- **Verdict:** Worth it if you already run Tailscale and have confirmed that a non-browser, no-header client can POST to the Funnel URL. Not the path this project took.

### Self-hosted reverse proxy (Caddy, nginx) with Let's Encrypt on a VPS

Run the MCP server on a VPS you control, or tunnel to one, and put Caddy (or nginx + certbot) in front with a hostname on your domain.

- **Pros:** Maximum control, no interstitial, no random URLs, logs and rate limits you define, certificates from Let's Encrypt automatically with Caddy.
- **Cons:** You need a VPS and a domain. You are now responsible for patching and uptime. If the MCP process stays on a laptop behind NAT, you still need a separate tunnel or a VPN to the VPS.
- **Verdict:** The right long-term shape for something you intend to keep on a domain you own. Overkill for a first connector test.

## A working recipe, from zero

This is a complete first path: install MCP, run a tiny server, start a tunnel, verify with `curl`, add the URL in Claude. It uses ngrok's free static domain because that is what this project ended up with. Substitute another option from the table if you prefer.

### 1. Install the library and write a tiny server

```bash
python3 -m venv venv
./venv/bin/pip install 'mcp==2.3.0' uvicorn
```

Use the minimal server in [BUILD-YOUR-OWN.md](BUILD-YOUR-OWN.md) (echo tool, stateless Streamable HTTP on `0.0.0.0:8000`, JSON GET `/mcp`, 404 on `/.well-known/oauth*`). Save it as `server.py` and start it:

```bash
./venv/bin/python server.py
```

Confirm it answers locally:

```bash
curl -sS -D- http://127.0.0.1:8000/mcp -o -
# HTTP/1.1 200 …  {"ok":true,"name":"learn-mcp","transport":"streamable-http"}
```

If this GET hangs, your GET handler is probably falling through to SSE. Fix that before you open a tunnel.

### 2. Reserve a hostname and start the tunnel

Install ngrok, reserve a free static domain in the ngrok dashboard, and authenticate the agent with `ngrok config add-authtoken <token>`. If you copy from a doc, copy **only the token**, not the words `ngrok config add-authtoken`. Pasting the whole command into a token field (or into `NGROK_AUTHTOKEN`) is a common failure mode.

```bash
ngrok http --url=your-static-domain.ngrok-free.dev 8000
```

Leave that running. Optionally tighten heartbeats in `~/.config/ngrok/ngrok.yml` as shown above.

### 3. Verify through the public URL

Health check (JSON, not SSE):

```bash
curl -sS https://your-static-domain.ngrok-free.dev/mcp
```

Initialize and list tools. Streamable HTTP may return `text/event-stream`; `curl` will still print the event body.

```bash
curl -sS https://your-static-domain.ngrok-free.dev/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"curl","version":"1.0"}}}'

curl -sS https://your-static-domain.ngrok-free.dev/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}'
```

You want a JSON-RPC result that includes `echo` (and any other tools you added). If you instead see an ngrok HTML error page, the agent has lost its session — restart it. If you see the ngrok interstitial HTML, you used a browser User-Agent; retry with curl's default.

### 4. Add it in Claude as a custom connector

In Claude, add a custom connector and paste:

```
https://your-static-domain.ngrok-free.dev/mcp
```

No auth, no headers. Start a chat (or voice mode) and ask it to call `echo`. If voice mode can say the echoed text back, the connector is actually loaded — that was the original test this project existed to run.

A secret desktop endpoint, once you have one, is the same hostname with a path key you never publish: `https://your-static-domain.ngrok-free.dev/d/<key>/mcp`.

## Keeping it alive

A tunnel that only works while you watch the terminal is not a connector you can use tomorrow. This repo's approach, which you can reimplement for your own process:

- **Watchdog** (`watchdog.sh`) every ~10 seconds: is the server answering locally, is the ngrok agent API showing a tunnel, does the public GET still look like MCP (and not an ngrok error page)? Restart only the part that is down. Require two failed public checks before yanking a tunnel, so a single blip does not flap the session.
- **Singleton lock** (`flock` on a lock file) so `start.sh`, `ensure_up.sh`, and the watchdog never race on port 8000.
- **Autostart hook** (`autostart.sh` from `~/.bashrc` / `~/.profile`) that starts the watchdog if it is not running and then returns, so a new shell does not block.
- **Agent heartbeats** at the 10s / 45s settings above, so a dead cloud session is noticed faster.
- A **periodic external probe** (a small routine or cron that hits GET `/mcp`) still helps, because a multi-minute host stall can drop the cloud session while the local process looks healthy.

## Troubleshooting

**The tunnel cannot start on a restricted sandbox (DNS rewritten to `198.18.0.1`).** Some environments intercept DNS for `*.ngrok-agent.com`, `*.ngrok.com`, or `*.argotunnel.com`. TLS and CRL checks then fail. The workaround used here: resolve the real A records with Cloudflare DoH (`https://1.1.1.1/dns-query`), write them to a hosts file (`update_ngrok_hosts.py`), and bind-mount that file over `/etc/hosts` inside `unshare --user --map-root-user --mount` before exec-ing the agent. If your laptop's DNS is normal, you will never need this.

**`ngrok config add-authtoken` rejects the token, or the agent says the token is invalid.** Check that the value is only the token. If a script or a password manager pasted the entire command, the value may start with `ngrok config add-authtoken`. The right fix is to run the command yourself, once, on that machine.

**GET `/mcp` hangs.** The GET is being handled as an SSE stream. Short-circuit GET/HEAD `/mcp` with a JSON body, as in the minimal server.

**Claude starts talking about OAuth or "authorization required."** Something is still serving a `/.well-known/oauth-*` document, or a 401 with a WWW-Authenticate challenge. Make those paths 404, and do not require headers the client cannot send.

**Voice mode "can't see the screenshot."** The image may have been returned as image content, which voice mode does not surface. Also return text: a description, a palette, OCR, pixel hex, a shot URL, a small crop. See [BUILD-YOUR-OWN.md](BUILD-YOUR-OWN.md#3-a-browser-screenshot-that-voice-mode-can-use).

**The public URL works in curl and fails in a browser.** On ngrok's free tier that is often `ERR_NGROK_6024`, the interstitial. Claude is not a browser; this is expected. Use curl or the Claude app to test the connector.

**curl works locally, the public URL serves an ngrok error page, but `ngrok` is still running.** The cloud session is gone (this happened here after ~16 minute host stalls). Restart the agent, or let a watchdog do it when the agent API reports `no_tunnel` or the body matches `ERR_NGROK_`.

## Security notes

- Use placeholders in docs and in git: `your-static-domain.ngrok-free.dev`, `YOUR_NGROK_AUTHTOKEN`, `/d/<key>/mcp`. Never commit a real domain, authtoken, or path key. This repository's `.gitignore` already excludes `.env`, `desktop.key`, `ngrok.yml`, URL files, and logs.
- Prefer `ngrok config add-authtoken` over leaving `NGROK_AUTHTOKEN` in a shell history file. If a script must read a token, read it from a mode-600 config, not from a value hard-coded in the repo.
- The public `/mcp` path is visible to anyone who knows or guesses the hostname. Put only harmless tools there.
- A secret path is "auth" only in the sense that it is unguessable and unlisted. It is still a capability URL. Treat it like a password: long, random, rotated when leaked, never logged in full, never pasted into a public issue.
- Do not put the author's (or anyone else's) live hostname into a client "to try the demo." There is no public demo instance documented here. Build your own.
