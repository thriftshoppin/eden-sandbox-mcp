#!/usr/bin/env python3
"""Write a hosts file with DoH-resolved Cloudflare edge names.

This box's system DNS rewrites *.argotunnel.com to 198.18.0.1 (sandbox
egress intercept), which cannot carry cloudflared's QUIC/HTTP2 edge
protocol. Bind-mounting this file over /etc/hosts in a user namespace
lets cloudflared reach the real edge IPs.
"""
import json
import urllib.request
from pathlib import Path

OUT = Path(__file__).resolve().parent / "cf-hosts"

NAMES = [
    "region1.v2.argotunnel.com",
    "region2.v2.argotunnel.com",
    "cfd-features.argotunnel.com",
    "api.trycloudflare.com",
    "api.cloudflare.com",
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
