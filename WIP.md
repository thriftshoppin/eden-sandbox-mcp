# Work in progress

Status of this sandbox MCP server and the wider Eden work it belongs to. This is a snapshot, not a commitment to ship on a date.

## Done

- Public Streamable HTTP MCP server on `0.0.0.0:8000` with dummy tools (`echo`, `get_time`, `add`) used to prove that Claude custom connectors, including mobile voice mode, will load and call tools.
- Sandboxed Playwright Chromium tools on the public endpoint (browse, screenshot, inspect, describe, pixels, crop, and so on), with an SSRF block after DoH resolution and text-first results so voice mode has something to read.
- Secret-path desktop connector at `/d/<key>/mcp`: status, screenshot, windows, click, type, paste, keys, scroll, allowlisted launch, hangup. Arming is CLI-only (`desk_arm.sh`), time-boxed (max 60 minutes), and ends after 5 idle minutes.
- `desk_type` reliability fix, plus `desk_paste_text` for long or multi-line text.
- Gated batch file transfer: `xfer_propose` → `xfer_pending.sh` → `xfer_approve.sh <id> all|0,2|deny`, with 20-item / 10 MB / 50 MB limits, TTL 1–60 minutes, one-shot moves, sha256, and a log. There is no MCP approval tool.
- ngrok free static domain (Cloudflare quick tunnels retired), agent heartbeats at 10s / 45s tolerance, watchdog every 10s with a singleton lock, `ensure_up.sh`, and bashrc/profile autostart hooks.
- DoH + user-namespace hosts bind-mount so the tunnel agent still works on a box whose system DNS rewrites ngrok and Cloudflare names to `198.18.0.1`.
- This educational repository: source for reading and learning (all rights reserved), README, this WIP file, [AGENTS.md](AGENTS.md), [docs/SECURITY-MODEL.md](docs/SECURITY-MODEL.md), [docs/BUILD-YOUR-OWN.md](docs/BUILD-YOUR-OWN.md), and [docs/EXPOSING-MCP-OVER-HTTPS.md](docs/EXPOSING-MCP-OVER-HTTPS.md). Live hostnames and keys stay off GitHub.

## Open

- **Eden Link console.** A master console over multiple Eden bridges (PC, Cyberdeck, this sandbox) with a live traffic view and a master kill switch. Each Eden keeps its **own** local kill switch; Link only relays commands and is never the sole authority. Per-session stops are typed at the console, enforced at each Eden, and acknowledged, so a dropped link cannot leave a session running. Pending approvals are ranked over typed commands. An override for blocked content would be off by default, started only by a human at the keyboard, never requestable by an agent, and applied to one item. A design brief for this (and for watchdog border alerts, file provenance, and trust levels) was written from a spitball session and saved on the box desktop as `eden-link-grok-prompt.clean.txt`. Audit and plan against the real repos only when asked; build only after an explicit GO.
- **Hotline route to a Windows PC.** Option 2 from the earlier choice: relay through grok-hotline onto the user's machine, using eden-desktop-bridge. Pull request #1's pairing gate is still untested on the deck.
- **Optional routine to surface new transfer manifests.** Offered as a short periodic check that would present pending manifests in chat. Not created. Until then, a human says "check transfers", `xfer_pending.sh` is run, and approval happens only on an explicit word via `xfer_approve.sh`.
- **Possible ngrok token rotation.** The token used on the box can be rotated any time; it was left in place at the operator's request. Do not put a token in this repo.
- **Heartbeat routine.** A keep-alive routine for the live tunnel runs until **21 November 2026**.

## Out of scope unless asked

- Changing or restarting the already-running services on the live box from this publication work.
- Publishing the live tunnel hostname, `desktop.key`, connector URLs, or any authtoken.
- Claiming that a rogue *local* agent is covered by the Guardian. The honest claim is only that a hosted agent can act through tools that announce every call and wait for approval.
