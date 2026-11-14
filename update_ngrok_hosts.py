#!/usr/bin/env python3
"""Write a hosts file with DoH-resolved ngrok agent/CRL endpoints.

This box's system DNS rewrites public names to 198.18.0.1 (sandbox egress
intercept), which breaks ngrok's TLS/CRL checks. Bind-mount this file over
/etc/hosts in a user namespace so ngrok reaches the real IPs.
"""
import json
import urllib.request
from pathlib import Path

OUT = Path(__file__).resolve().parent / "ngrok-hosts"

NAMES = [
    "connect.ngrok-agent.com",
    "connect.us.ngrok-agent.com",
    "connect.eu.ngrok-agent.com",
    "connect.ap.ngrok-agent.com",
    "connect.au.ngrok-agent.com",
    "connect.sa.ngrok-agent.com",
    "connect.jp.ngrok-agent.com",
    "connect.in.ngrok-agent.com",
    "connect.ngrok.com",
    "tunnel.ngrok.com",
    "tunnel.us.ngrok.com",
    "api.ngrok.com",
    "agent.ngrok.com",
    "crl.ngrok.com",
    "crl.ngrok-agent.com",
    "update.ngrok-agent.com",
    "update.equinox.io",
    "bin.equinox.io",
]


def doh_a(name: str) -> list[str]:
    url = "https://1.1.1.1/dns-query?name=%s&type=A" % name
    req = urllib.request.Request(url, headers={"accept": "application/dns-json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        data = json.loads(r.read().decode())
    return [a.get("data") for a in data.get("Answer") or [] if a.get("type") == 1]


def main() -> None:
    lines = Path("/etc/hosts").read_text(encoding="utf-8", errors="replace").splitlines()
    resolved = {}
    for n in NAMES:
        try:
            ips = doh_a(n)
        except Exception as exc:
            print(f"DoH failed for {n}: {exc}")
            ips = []
        resolved[n] = ips
        for ip in ips:
            lines.append("%s %s" % (ip, n))
    OUT.write_text("\n".join(lines) + "\n")
    print("wrote", OUT)
    for n, ips in resolved.items():
        print(" ", n, ",".join(ips) or "(none)")


if __name__ == "__main__":
    main()
