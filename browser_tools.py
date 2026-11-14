"""Sandboxed headless browser tools for the dummy MCP server.

One shared Playwright Chromium context, reused across tool calls. The profile
is created only under /workspace/mcp-test and never touches the box desktop
browser. All destinations are restricted to public http(s) addresses.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import re
import secrets
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.error import HTTPError, URLError

from mcp.server.mcpserver import Image
from PIL import Image as PILImage

import crop_util

TIMEOUT_MS = 20_000
TIMEOUT_S = 20
BROWSE_TEXT_LIMIT = 4_000
PAGE_TEXT_LIMIT = 24_000
MAX_IMAGE_BYTES = 1_000_000
MAX_DOWNLOAD_BYTES = 8_000_000
DEFAULT_VIEWPORT = (1280, 800)
SHOTS_DIR = Path("/workspace/mcp-test/shots")
MAX_SHOTS = 50
VOICE_TEXT_LIMIT = 3_000
CHROME_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-sync",
    "--disable-features=Translate,MediaRouter",
]

_lock = asyncio.Lock()
_playwright = None
_browser = None
_context = None
_page = None
_dns_cache: dict[str, list[str]] = {}


def _is_blocked_ip(ip_str: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return True
    if addr.version == 6 and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    if addr.version == 6 and getattr(addr, "teredo", None):
        # Teredo embeds an IPv4 address; check the embedded one too.
        try:
            if _is_blocked_ip(str(addr.teredo[1])):
                return True
        except Exception:
            pass
    return bool(
        addr.is_loopback
        or addr.is_link_local
        or addr.is_private
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
        or addr.is_multicast
    )


def _host_as_ip(host: str) -> ipaddress._BaseAddress | None:
    host = host.strip("[]")
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    if host.isdigit():
        try:
            return ipaddress.IPv4Address(int(host))
        except Exception:
            return None
    return None


def _doh_ips(host: str) -> list[str]:
    """Resolve A/AAAA via Cloudflare DoH.

    This box's system DNS rewrites public names to 198.18.0.1 (sandbox egress
    intercept). 198.18.0.0/15 is RFC 2544 reserved, so trusting getaddrinfo
    would block every public website. DoH returns the real addresses used for
    the SSRF allow/deny decision. The browser still uses system DNS to fetch.
    """
    import json
    from urllib.request import Request, urlopen

    ips: list[str] = []
    for rr, typ in (("A", 1), ("AAAA", 28)):
        url = f"https://1.1.1.1/dns-query?name={host}&type={rr}"
        req = Request(url, headers={"Accept": "application/dns-json"})
        try:
            with urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode())
        except Exception:
            continue
        for ans in data.get("Answer") or []:
            if ans.get("type") == typ and ans.get("data"):
                ip = ans["data"]
                if ip not in ips:
                    ips.append(ip)
    return ips


def _resolve_ips(host: str) -> list[str]:
    if host in _dns_cache:
        return _dns_cache[host]
    ips = _doh_ips(host)
    if not ips:
        raise ValueError(
            f"could not resolve hostname {host!r} via public DNS (DoH). "
            "The name may not exist, or DNS lookup failed."
        )
    _dns_cache[host] = ips
    return ips


def validate_public_http_url(url: str) -> str | None:
    """Return an error string if the URL is not a public http(s) address, else None."""
    if not url or not str(url).strip():
        return "Error: a URL is required."
    url = str(url).strip()
    parsed = urlparse(url)
    if parsed.scheme in ("file", "ftp", "ws", "wss", "javascript", "data", "about", "chrome", "chrome-extension"):
        return f"Error: only http and https URLs are allowed, not {parsed.scheme}://."
    if parsed.scheme not in ("http", "https"):
        return "Error: only http and https URLs are allowed."
    if parsed.username or parsed.password:
        return "Error: URLs with credentials are not allowed."
    host = parsed.hostname
    if not host:
        return "Error: URL is missing a hostname."
    lowered = host.lower().rstrip(".")
    if lowered in {"localhost", "ip6-localhost", "ip6-loopback"} or lowered.endswith(".localhost"):
        return "Error: requests to localhost are not allowed."

    as_ip = _host_as_ip(host)
    if as_ip is not None:
        if _is_blocked_ip(str(as_ip)):
            return f"Error: requests to private or local addresses are not allowed ({host})."
        return None

    try:
        ips = _resolve_ips(host)
    except ValueError as exc:
        return f"Error: {exc}"
    if not ips:
        return f"Error: could not resolve hostname {host!r}."
    for ip in ips:
        if _is_blocked_ip(ip):
            return (
                f"Error: hostname {host} resolves to a private or local address "
                f"({ip}), which is not allowed."
            )
    return None


def _url_allowed_for_request(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme in ("data", "blob", "about", "chrome", ""):
        return True
    return validate_public_http_url(url) is None


async def _filter_route(route: Any) -> None:
    url = route.request.url
    if _url_allowed_for_request(url):
        await route.continue_()
    else:
        await route.abort("blockedbyclient")


async def _close_browser() -> None:
    global _playwright, _browser, _context, _page
    for obj, method in (
        (_context, "close"),
        (_browser, "close"),
        (_playwright, "stop"),
    ):
        if obj is None:
            continue
        try:
            maybe = getattr(obj, method)()
            if asyncio.iscoroutine(maybe):
                await maybe
        except Exception:
            pass
    _playwright = _browser = _context = _page = None


async def ensure_browser():
    """Return the shared page, launching Chromium if needed."""
    global _playwright, _browser, _context, _page
    if _page is not None and not _page.is_closed():
        return _page
    from playwright.async_api import async_playwright

    await _close_browser()
    pw = await async_playwright().start()
    _playwright = pw
    last_err: Exception | None = None
    for kwargs in (
        {"headless": True, "args": CHROME_ARGS, "timeout": TIMEOUT_MS},
        {
            "headless": True,
            "executable_path": "/usr/bin/google-chrome",
            "args": CHROME_ARGS,
            "timeout": TIMEOUT_MS,
        },
    ):
        try:
            _browser = await pw.chromium.launch(**kwargs)
            break
        except Exception as exc:
            last_err = exc
            _browser = None
    if _browser is None:
        await _close_browser()
        raise RuntimeError(f"could not launch Chromium: {last_err}")
    _context = await _browser.new_context(
        viewport={"width": DEFAULT_VIEWPORT[0], "height": DEFAULT_VIEWPORT[1]},
        accept_downloads=False,
        java_script_enabled=True,
        bypass_csp=False,
    )
    await _context.route("**/*", _filter_route)
    _page = await _context.new_page()
    _page.set_default_timeout(TIMEOUT_MS)
    _page.set_default_navigation_timeout(TIMEOUT_MS)
    return _page


async def prewarm_browser() -> None:
    try:
        async with _lock:
            await ensure_browser()
    except Exception:
        pass


def _readable_text_from_htmlish(text: str, limit: int) -> str:
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = text.strip()
    if len(text) > limit:
        return text[: limit - 1].rstrip() + "…"
    return text


async def _page_readable_text(page, limit: int) -> str:
    try:
        raw = await page.evaluate(
            """() => {
                const root = document.body || document.documentElement;
                if (!root) return document.title || "";
                const clone = root.cloneNode(true);
                clone.querySelectorAll("script,style,noscript,template,svg,iframe").forEach(
                    (el) => el.remove()
                );
                return clone.innerText || "";
            }"""
        )
    except Exception:
        try:
            raw = await page.inner_text("body")
        except Exception:
            raw = ""
    return _readable_text_from_htmlish(raw or "", limit)


async def _goto(page, url: str) -> str | None:
    err = validate_public_http_url(url)
    if err:
        return err
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=TIMEOUT_MS)
    except Exception as exc:
        msg = str(exc)
        if "net::ERR_BLOCKED" in msg or "blockedbyclient" in msg:
            return f"Error: navigation was blocked because the URL is not a public http(s) address ({url})."
        return f"Error: failed to load {url}: {exc}"
    final = page.url
    err = validate_public_http_url(final) if final.startswith("http") else (
        None if final.startswith("about:") else f"Error: landed on a disallowed URL ({final})."
    )
    if err:
        try:
            await page.goto("about:blank")
        except Exception:
            pass
        return err
    return None


def _compress_image(raw: bytes) -> tuple[bytes, str]:
    if len(raw) <= MAX_IMAGE_BYTES:
        try:
            kind = PILImage.open(BytesIO(raw)).format or "PNG"
        except Exception:
            return raw, "png"
        fmt = "jpeg" if kind.upper() in {"JPEG", "JPG"} else kind.lower()
        if fmt == "jpg":
            fmt = "jpeg"
        return raw, fmt
    im = PILImage.open(BytesIO(raw))
    if im.mode in {"RGBA", "LA", "P"}:
        rgba = im.convert("RGBA")
        bg = PILImage.new("RGB", rgba.size, (255, 255, 255))
        bg.paste(rgba, mask=rgba.split()[-1])
        im = bg
    elif im.mode != "RGB":
        im = im.convert("RGB")
    for scale in (1.0, 0.85, 0.7, 0.55, 0.4, 0.28):
        work = im
        if scale != 1.0:
            w, h = im.size
            work = im.resize(
                (max(1, int(w * scale)), max(1, int(h * scale))),
                PILImage.Resampling.LANCZOS,
            )
        for quality in (80, 70, 60, 50, 40, 30):
            buf = BytesIO()
            work.save(buf, format="JPEG", quality=quality, optimize=True)
            data = buf.getvalue()
            if len(data) <= MAX_IMAGE_BYTES:
                return data, "jpeg"
    buf = BytesIO()
    tiny = im.copy()
    tiny.thumbnail((640, 400), PILImage.Resampling.LANCZOS)
    tiny.save(buf, format="JPEG", quality=30, optimize=True)
    return buf.getvalue(), "jpeg"



def _public_origin() -> str:
    for candidate in (
        Path("/workspace/mcp-test/current_url.txt"),
        Path("/workspace/mcp-test/public-url.txt"),
    ):
        if candidate.is_file():
            u = candidate.read_text(encoding="utf-8", errors="replace").strip().rstrip("/")
            if u.endswith("/mcp"):
                u = u[:-4]
            if u:
                return u.rstrip("/")
    return "http://127.0.0.1:8000"


def _clip_text(text: str, limit: int = VOICE_TEXT_LIMIT) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _cleanup_shots() -> None:
    SHOTS_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(
        [f for f in SHOTS_DIR.iterdir() if f.is_file()],
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )
    for old in files[MAX_SHOTS:]:
        try:
            old.unlink()
        except OSError:
            pass


def _save_shot(data: bytes, fmt: str) -> str:
    """Write image bytes under shots/ and return the public URL."""
    SHOTS_DIR.mkdir(parents=True, exist_ok=True)
    ext = "jpg" if fmt in {"jpeg", "jpg"} else (fmt or "png")
    name = f"{secrets.token_hex(8)}.{ext}"
    path = SHOTS_DIR / name
    path.write_bytes(data)
    _cleanup_shots()
    return f"{_public_origin()}/shots/{name}"


def _css_color_to_hex(css: str) -> str:
    if not css:
        return ""
    css = css.strip()
    if css.lower() in {"transparent", "none"}:
        return "transparent"
    if css.startswith("#") and len(css) in {4, 7, 9}:
        if len(css) == 4:
            return "#" + "".join(c * 2 for c in css[1:4]).upper()
        return css[:7].upper()
    m = re.match(
        r"rgba?\(\s*(\d+(?:\.\d+)?)\s*[, ]\s*(\d+(?:\.\d+)?)\s*[, ]\s*(\d+(?:\.\d+)?)(?:\s*[,/]\s*([\d.]+%?))?",
        css,
        re.I,
    )
    if m:
        r, g, b, a = m.group(1), m.group(2), m.group(3), m.group(4)
        if a is not None:
            av = float(a[:-1]) / 100.0 if str(a).endswith("%") else float(a)
            if av <= 0.01:
                return "transparent"
        r, g, b = (max(0, min(255, int(float(x)))) for x in (r, g, b))
        return f"#{r:02X}{g:02X}{b:02X}"
    return css


def _dominant_colors(
    raw: bytes,
    n: int = 6,
    *,
    crop_left_frac: float | None = None,
    skip_near_white: bool = False,
) -> list[tuple[str, float]]:
    """Return up to n (hex, percent) pairs via Pillow quantize."""
    try:
        im = PILImage.open(BytesIO(raw))
    except Exception:
        return []
    if im.mode in {"RGBA", "LA", "P"}:
        rgba = im.convert("RGBA")
        bg = PILImage.new("RGB", rgba.size, (255, 255, 255))
        bg.paste(rgba, mask=rgba.split()[-1])
        im = bg
    elif im.mode != "RGB":
        im = im.convert("RGB")
    if crop_left_frac and 0 < crop_left_frac < 1:
        w, h = im.size
        left_w = max(1, int(w * crop_left_frac))
        im = im.crop((0, 0, left_w, h))
    # Downscale so quantize is fast and stable
    im = im.copy()
    im.thumbnail((160, 160), PILImage.Resampling.BOX)
    if skip_near_white:
        px = list(im.getdata())
        kept = [c for c in px if not (c[0] >= 245 and c[1] >= 245 and c[2] >= 245)]
        if len(kept) >= 8:
            im = PILImage.new("RGB", (len(kept), 1))
            im.putdata(kept)
    ncolors = max(8, min(n + 2, 16))
    ncolors = min(ncolors, im.size[0] * im.size[1])
    try:
        q = im.quantize(colors=max(2, ncolors), method=PILImage.Quantize.MEDIANCUT)
    except Exception:
        q = im.quantize(colors=max(2, ncolors))
    counts = q.getcolors() or []
    palette = q.getpalette() or []
    total = sum(c for c, _ in counts) or 1
    items: list[tuple[str, float]] = []
    for count, idx in sorted(counts, key=lambda x: x[0], reverse=True):
        base = idx * 3
        if base + 2 >= len(palette):
            continue
        r, g, b = palette[base], palette[base + 1], palette[base + 2]
        if skip_near_white and r >= 245 and g >= 245 and b >= 245:
            continue
        items.append((f"#{r:02X}{g:02X}{b:02X}", 100.0 * count / total))
        if len(items) >= n:
            break
    return items


def _format_colors(pairs: list[tuple[str, float]]) -> str:
    if not pairs:
        return "(none)"
    return ", ".join(f"{hx} {pct:.0f}%" for hx, pct in pairs)


def _image_dims(raw: bytes) -> tuple[int, int]:
    try:
        im = PILImage.open(BytesIO(raw))
        return im.size
    except Exception:
        return (0, 0)


class _ValidatingRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        err = validate_public_http_url(newurl)
        if err:
            raise URLError(err)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _fetch_bytes(url: str) -> tuple[bytes, str]:
    err = validate_public_http_url(url)
    if err:
        raise ValueError(err)
    opener = build_opener(_ValidatingRedirectHandler)
    req = Request(
        url,
        headers={
            "User-Agent": "mcp-test-browser/1.0",
            "Accept": "image/*,*/*;q=0.8",
        },
        method="GET",
    )
    try:
        with opener.open(req, timeout=TIMEOUT_S) as resp:
            final = resp.geturl()
            err = validate_public_http_url(final)
            if err:
                raise ValueError(err)
            ctype = resp.headers.get("Content-Type", "application/octet-stream").split(";")[0].strip()
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_DOWNLOAD_BYTES:
                    raise ValueError("Error: image is larger than the 8MB download limit.")
                chunks.append(chunk)
            return b"".join(chunks), ctype
    except HTTPError as exc:
        raise ValueError(f"Error: failed to fetch image ({exc.code} {exc.reason}).") from exc
    except URLError as exc:
        reason = exc.reason if not isinstance(exc.reason, str) else exc.reason
        raise ValueError(f"Error: failed to fetch image: {reason}") from exc


async def browse(url: str) -> str:
    """Open a public http(s) webpage and return its title, final URL, and a short text excerpt."""
    try:
        async with _lock:
            page = await ensure_browser()
            err = await _goto(page, url)
            if err:
                return err
            title = await page.title()
            final = page.url
            excerpt = await _page_readable_text(page, BROWSE_TEXT_LIMIT)
            return f"Title: {title}\nURL: {final}\n\n{excerpt}"
    except Exception as exc:
        return f"Error: browse failed: {exc}"


async def _page_visual_bits(page) -> dict:
    try:
        return await page.evaluate(
            """() => {
                const vw = window.innerWidth, vh = window.innerHeight;
                const vis = (el) => {
                    const r = el.getBoundingClientRect();
                    if (r.width < 2 || r.height < 2) return null;
                    if (r.bottom < 0 || r.right < 0 || r.top > vh || r.left > vw) return null;
                    const st = getComputedStyle(el);
                    if (st.visibility === "hidden" || st.display === "none" || st.opacity === "0") return null;
                    return r;
                };
                const images = [];
                for (const el of document.querySelectorAll("img, svg, [role=img]")) {
                    const r = vis(el);
                    if (!r) continue;
                    images.push({
                        tag: el.tagName.toLowerCase(),
                        src: el.currentSrc || el.src || el.getAttribute("src") || el.getAttribute("href") || "",
                        alt: el.getAttribute("alt") || el.getAttribute("aria-label") || "",
                        w: Math.round(r.width),
                        h: Math.round(r.height),
                        x: Math.round(r.left),
                        y: Math.round(r.top),
                    });
                    if (images.length >= 12) break;
                }
                const headings = [];
                for (const el of document.querySelectorAll("h1,h2,h3,[role=heading]")) {
                    const r = vis(el);
                    if (!r) continue;
                    const t = (el.innerText || "").trim();
                    if (!t) continue;
                    headings.push({
                        level: (el.tagName || "H").toUpperCase(),
                        text: t.slice(0, 120),
                        y: Math.round(r.top),
                    });
                    if (headings.length >= 12) break;
                }
                return { images, headings };
            }"""
        )
    except Exception:
        return {"images": [], "headings": []}


async def _find_locator(page, selector_or_text):
    target = str(selector_or_text).strip()
    loc = page.locator(target)
    try:
        count = await loc.count()
    except Exception:
        count = 0
    if count > 0:
        return loc.first, target
    loc = page.get_by_text(target, exact=False)
    try:
        count = await loc.count()
    except Exception:
        count = 0
    if count > 0:
        return loc.first, target
    # Common-name fallbacks (e.g. "Google logo")
    lowered = target.lower()
    if "logo" in lowered:
        for sel in (
            'img[alt*="Google" i]',
            "img[alt*='logo' i]",
            "#hplogo",
            'a#logo img',
            'img[src*="logo"]',
            '[aria-label*="logo" i]',
            "svg[aria-label]",
        ):
            loc = page.locator(sel)
            try:
                if await loc.count() > 0:
                    return loc.first, sel
            except Exception:
                continue
    raise RuntimeError(f"no element matched {target!r}")


async def screenshot(
    url: str | None = None,
    full_page: bool = False,
    width: int = 1280,
    height: int = 800,
) -> list:
    """Screenshot the current page, or a given public URL. Returns an image plus a text description (URL, colors, headings, visible images) for voice clients."""
    try:
        width = int(width) if width else DEFAULT_VIEWPORT[0]
        height = int(height) if height else DEFAULT_VIEWPORT[1]
        width = max(200, min(width, 1920))
        height = max(200, min(height, 1600))
        async with _lock:
            page = await ensure_browser()
            if url:
                err = await _goto(page, url)
                if err:
                    return [err]
            elif not page.url or page.url == "about:blank":
                return ["Error: no page is open. Pass a url or call browse() first."]
            try:
                await page.set_viewport_size({"width": width, "height": height})
            except Exception:
                pass
            raw = await page.screenshot(type="png", full_page=bool(full_page), timeout=TIMEOUT_MS)
            data, fmt = _compress_image(raw)
            title = await page.title()
            final = page.url
            bits = await _page_visual_bits(page)
            iw, ih = _image_dims(data)
            shot_url = _save_shot(data, fmt)
            colors = _format_colors(_dominant_colors(data, 6))
            raw_w, raw_h = _image_dims(raw)
            lines = [
                f"Screenshot {iw}×{ih} of {final} — “{title or '(no title)'}”"
                + (" (full page)" if full_page else ""),
                f"Coordinates: {raw_w}×{raw_h} capture pixels, origin (0,0) top-left of the "
                + ("full page" if full_page else f"viewport ({width}×{height} CSS)"),
                "Use pixels(x,y,w,h) or crop_b64(x,y,w,h) in this page space (not the maybe-downscaled preview).",
                f"Image: {shot_url} ({len(data) // 1024} KB {fmt.upper()})",
                f"Colors: {colors}",
            ]
            headings = bits.get("headings") or []
            if headings:
                lines.append(
                    "Headings: "
                    + "; ".join(f"{h.get('level','?')} {h.get('text','')}" for h in headings[:8])
                )
            images = bits.get("images") or []
            if images:
                parts = []
                for im in images[:8]:
                    alt = im.get("alt") or "(no alt)"
                    src = im.get("src") or ""
                    if len(src) > 80:
                        src = src[:77] + "…"
                    parts.append(
                        f"“{alt}” {im.get('w')}×{im.get('h')} at ({im.get('x')},{im.get('y')})"
                        + (f" src={src}" if src else "")
                    )
                lines.append("Visible images: " + " | ".join(parts))
            caption = _clip_text("\n".join(lines))
            return [Image(data=data, format=fmt), caption]
    except Exception as exc:
        return [f"Error: screenshot failed: {exc}"]


async def view_image(url: str) -> list:
    """Fetch a public image URL and return it as an image plus a text description (public URL, size, colors) for voice clients."""
    try:
        err = validate_public_http_url(url)
        if err:
            return [err]
        raw, ctype = await asyncio.to_thread(_fetch_bytes, url)
        try:
            PILImage.open(BytesIO(raw)).verify()
        except Exception:
            return [f"Error: the URL did not contain a usable image (content-type {ctype})."]
        data, fmt = _compress_image(raw)
        iw, ih = _image_dims(data)
        shot_url = _save_shot(data, fmt)
        colors = _format_colors(_dominant_colors(data, 6))
        chroma = _format_colors(_dominant_colors(data, 6, skip_near_white=True))
        lines = [
            f"Image {iw}×{ih} from {url}",
            f"Saved: {shot_url} ({len(data) // 1024} KB {fmt.upper()})",
            f"Colors: {colors}",
        ]
        if chroma and chroma != colors:
            lines.append(f"Chroma (ignoring near-white): {chroma}")
        caption = _clip_text("\n".join(lines))
        return [Image(data=data, format=fmt), caption]
    except ValueError as exc:
        msg = str(exc)
        return [msg if msg.startswith("Error:") else f"Error: {msg}"]
    except Exception as exc:
        return [f"Error: view_image failed: {exc}"]


async def click(selector_or_text: str) -> str:
    """Click an element on the current page, by CSS selector or visible text."""
    try:
        if not selector_or_text or not str(selector_or_text).strip():
            return "Error: selector_or_text is required."
        target = str(selector_or_text).strip()
        async with _lock:
            page = await ensure_browser()
            if not page.url or page.url == "about:blank":
                return "Error: no page is open. Call browse() first."
            loc = page.locator(target)
            try:
                count = await loc.count()
            except Exception:
                count = 0
            if count > 0:
                await loc.first.click(timeout=TIMEOUT_MS)
            else:
                await page.get_by_text(target, exact=False).first.click(timeout=TIMEOUT_MS)
            return f"Clicked {target!r} on {page.url}"
    except Exception as exc:
        return f"Error: could not click {selector_or_text!r}: {exc}"


async def type_text(selector: str, text: str, submit: bool = False) -> str:
    """Type text into a field on the current page (CSS selector). Set submit=true to press Enter."""
    try:
        if not selector or not str(selector).strip():
            return "Error: selector is required."
        async with _lock:
            page = await ensure_browser()
            if not page.url or page.url == "about:blank":
                return "Error: no page is open. Call browse() first."
            await page.locator(selector).first.fill(str(text), timeout=TIMEOUT_MS)
            if submit:
                await page.keyboard.press("Enter")
            action = "Typed and submitted" if submit else "Typed"
            return f"{action} into {selector!r} on {page.url}"
    except Exception as exc:
        return f"Error: could not type into {selector!r}: {exc}"


async def scroll(direction: str, amount: int = 400) -> str:
    """Scroll the current page. direction is up, down, left, or right; amount is pixels (default 400)."""
    try:
        direction = (direction or "").strip().lower()
        if direction not in {"up", "down", "left", "right"}:
            return "Error: direction must be one of: up, down, left, right."
        try:
            amount = int(amount)
        except (TypeError, ValueError):
            return "Error: amount must be an integer number of pixels."
        amount = max(1, min(amount, 10_000))
        dx = dy = 0
        if direction == "down":
            dy = amount
        elif direction == "up":
            dy = -amount
        elif direction == "right":
            dx = amount
        else:
            dx = -amount
        async with _lock:
            page = await ensure_browser()
            if not page.url or page.url == "about:blank":
                return "Error: no page is open. Call browse() first."
            await page.evaluate(
                "({dx, dy}) => window.scrollBy(dx, dy)",
                {"dx": dx, "dy": dy},
            )
            return f"Scrolled {direction} by {amount}px on {page.url}"
    except Exception as exc:
        return f"Error: scroll failed: {exc}"


async def page_text() -> str:
    """Return the current page's readable text."""
    try:
        async with _lock:
            page = await ensure_browser()
            if not page.url or page.url == "about:blank":
                return "Error: no page is open. Call browse() first."
            title = await page.title()
            final = page.url
            text = await _page_readable_text(page, PAGE_TEXT_LIMIT)
            return f"Title: {title}\nURL: {final}\n\n{text}"
    except Exception as exc:
        return f"Error: page_text failed: {exc}"



async def inspect(selector_or_text: str) -> list:
    """Inspect an element by CSS selector or visible text. Returns computed styles, box, text, and dominant pixel colors (including a left/first-letter sample) plus an image."""
    try:
        if not selector_or_text or not str(selector_or_text).strip():
            return ["Error: selector_or_text is required."]
        async with _lock:
            page = await ensure_browser()
            if not page.url or page.url == "about:blank":
                return ["Error: no page is open. Call browse() first."]
            try:
                loc, matched = await _find_locator(page, selector_or_text)
            except Exception as exc:
                return [f"Error: could not find {selector_or_text!r}: {exc}"]
            try:
                await loc.scroll_into_view_if_needed(timeout=TIMEOUT_MS)
            except Exception:
                pass
            info = await loc.evaluate(
                """(el) => {
                    const r = el.getBoundingClientRect();
                    const st = getComputedStyle(el);
                    const attrs = {};
                    for (const a of el.attributes) {
                        if (attrs && Object.keys(attrs).length < 16) attrs[a.name] = (a.value || "").slice(0, 200);
                    }
                    const tag = el.tagName.toLowerCase();
                    let src = "", alt = "", naturalW = null, naturalH = null;
                    if (tag === "img") {
                        src = el.currentSrc || el.src || "";
                        alt = el.alt || "";
                        naturalW = el.naturalWidth;
                        naturalH = el.naturalHeight;
                    } else if (tag === "svg" || tag === "image") {
                        src = el.getAttribute("href") || el.getAttribute("src") || "";
                        alt = el.getAttribute("aria-label") || el.getAttribute("alt") || "";
                        try {
                            const bb = el.getBBox ? el.getBBox() : null;
                            if (bb) { naturalW = Math.round(bb.width); naturalH = Math.round(bb.height); }
                        } catch (e) {}
                    }
                    return {
                        tag, id: el.id || "",
                        className: (el.className && el.className.toString) ? String(el.className).slice(0, 160) : "",
                        text: (el.innerText || "").trim().slice(0, 400),
                        box: { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) },
                        color: st.color,
                        backgroundColor: st.backgroundColor,
                        font: st.font,
                        fontSize: st.fontSize,
                        fontFamily: (st.fontFamily || "").split(",")[0].replace(/['"]/g, ""),
                        fontWeight: st.fontWeight,
                        attrs, src, alt, naturalW, naturalH
                    };
                }"""
            )
            raw = await loc.screenshot(type="png", timeout=TIMEOUT_MS)
            data, fmt = _compress_image(raw)
            shot_url = _save_shot(data, fmt)
            iw, ih = _image_dims(data)
            colors = _dominant_colors(data, 6)
            chroma = _dominant_colors(data, 6, skip_near_white=True)
            left = _dominant_colors(data, 4, crop_left_frac=0.28, skip_near_white=True)
            box = info.get("box") or {}
            tag = info.get("tag") or "?"
            ident = tag
            if info.get("id"):
                ident += f"#{info['id']}"
            if info.get("alt"):
                ident += f" alt=\"{info['alt']}\""
            color_hex = _css_color_to_hex(info.get("color") or "")
            bg_hex = _css_color_to_hex(info.get("backgroundColor") or "")
            vp = page.viewport_size or {}
            vw = int(vp.get("width") or DEFAULT_VIEWPORT[0])
            vh = int(vp.get("height") or DEFAULT_VIEWPORT[1])
            bx, by = box.get("x"), box.get("y")
            bw, bh = box.get("w"), box.get("h")
            lines = [
                f"{ident} {bw}×{bh} at ({bx},{by})",
                f"Coordinates: page viewport {vw}×{vh}, element box {bw}×{bh} at ({bx},{by}). "
                f"Use pixels({bx},{by},{bw},{bh}) or crop_b64({bx},{by},{bw},{bh}) in this page space.",
                f"Pixels: {_format_colors(chroma or colors)}",
            ]
            if left:
                lines.append(f"Left / first-letter region: {_format_colors(left)}")
            lines.append(
                f"CSS color {color_hex or info.get('color')}, "
                f"background {bg_hex or info.get('backgroundColor')}, "
                f"{info.get('fontWeight') or ''} {info.get('fontSize') or ''} {info.get('fontFamily') or ''}".strip()
            )
            if info.get("text"):
                lines.append(f"Text: {info['text'][:240]}")
            if info.get("src") or info.get("naturalW"):
                nat = ""
                if info.get("naturalW"):
                    nat = f", natural {info.get('naturalW')}×{info.get('naturalH')}"
                src = info.get("src") or ""
                if len(src) > 100:
                    src = src[:97] + "…"
                lines.append(f"src={src}{nat}")
            lines.append(f"Crop: {shot_url} ({iw}×{ih}, {len(data) // 1024} KB {fmt.upper()})")
            caption = _clip_text("\n".join(lines))
            return [Image(data=data, format=fmt), caption]
    except Exception as exc:
        return [f"Error: inspect failed: {exc}"]


async def describe_page() -> str:
    """Return a compact, voice-friendly layout summary of the current page: title, sections, controls, images, forms, and colors."""
    try:
        async with _lock:
            page = await ensure_browser()
            if not page.url or page.url == "about:blank":
                return "Error: no page is open. Call browse() first."
            title = await page.title()
            final = page.url
            try:
                layout = await page.evaluate(
                    """() => {
                        const vw = window.innerWidth, vh = window.innerHeight;
                        const vis = (el) => {
                            const r = el.getBoundingClientRect();
                            if (r.width < 2 || r.height < 2) return null;
                            if (r.bottom < 0 || r.right < 0 || r.top > vh || r.left > vw) return null;
                            const st = getComputedStyle(el);
                            if (st.visibility === "hidden" || st.display === "none" || Number(st.opacity) === 0) return null;
                            return r;
                        };
                        const sections = [];
                        for (const el of document.querySelectorAll("h1,h2,h3,[role=heading]")) {
                            const r = vis(el);
                            if (!r) continue;
                            const t = (el.innerText || "").trim();
                            if (!t) continue;
                            sections.push({ y: Math.round(r.top), x: Math.round(r.left), kind: (el.tagName || "H").toUpperCase(), text: t.slice(0, 100) });
                        }
                        sections.sort((a,b) => a.y - b.y || a.x - b.x);
                        const controls = [];
                        const sel = "a[href], button, [role=button], input[type=submit], input[type=button]";
                        for (const el of document.querySelectorAll(sel)) {
                            const r = vis(el);
                            if (!r) continue;
                            const t = (el.innerText || el.value || el.getAttribute("aria-label") || el.getAttribute("title") || el.getAttribute("alt") || "").trim();
                            if (!t) continue;
                            const kind = (el.tagName === "A" || el.hasAttribute("href")) ? "link" : "button";
                            controls.push({ y: Math.round(r.top), x: Math.round(r.left), kind, text: t.slice(0, 80) });
                        }
                        controls.sort((a,b) => a.y - b.y || a.x - b.x);
                        const images = [];
                        for (const el of document.querySelectorAll("img, svg, [role=img]")) {
                            const r = vis(el);
                            if (!r) continue;
                            images.push({
                                alt: el.getAttribute("alt") || el.getAttribute("aria-label") || "",
                                w: Math.round(r.width), h: Math.round(r.height),
                                src: (el.currentSrc || el.src || "").slice(0, 80),
                            });
                            if (images.length >= 15) break;
                        }
                        const forms = [];
                        for (const form of document.querySelectorAll("form")) {
                            const r = vis(form);
                            if (!r && form.querySelector("input,textarea,select") === null) continue;
                            const fields = [];
                            for (const inp of form.querySelectorAll("input, textarea, select, button")) {
                                const typ = (inp.getAttribute("type") || inp.tagName.toLowerCase());
                                const name = inp.getAttribute("name") || inp.getAttribute("aria-label") || inp.getAttribute("placeholder") || typ;
                                fields.push(`${typ}:${name}`.slice(0, 40));
                                if (fields.length >= 8) break;
                            }
                            if (fields.length) forms.push(fields);
                            if (forms.length >= 5) break;
                        }
                        return { sections: sections.slice(0, 15), controls: controls.slice(0, 30), images, forms };
                    }"""
                )
            except Exception:
                layout = {"sections": [], "controls": [], "images": [], "forms": []}
            raw = await page.screenshot(type="png", full_page=False, timeout=TIMEOUT_MS)
            data, fmt = _compress_image(raw)
            shot_url = _save_shot(data, fmt)
            colors = _format_colors(_dominant_colors(data, 6))
            lines = [
                f"{title or '(no title)'} — {final}",
                f"Colors: {colors}",
                f"Shot: {shot_url}",
            ]
            sections = layout.get("sections") or []
            if sections:
                lines.append(
                    "Sections: " + " · ".join(f"{s.get('kind')} {s.get('text')}" for s in sections[:10])
                )
            controls = layout.get("controls") or []
            if controls:
                bits = []
                for c in controls[:30]:
                    bits.append(f"{c.get('kind')}: {c.get('text')}")
                lines.append("Controls: " + " | ".join(bits))
            images = layout.get("images") or []
            if images:
                bits = []
                for im in images[:10]:
                    alt = im.get("alt") or "(no alt)"
                    bits.append(f"“{alt}” {im.get('w')}×{im.get('h')}")
                lines.append("Images: " + " | ".join(bits))
            forms = layout.get("forms") or []
            if forms:
                bits = [", ".join(f) for f in forms[:4]]
                lines.append("Forms: " + " ; ".join(bits))
            return _clip_text("\n".join(lines))
    except Exception as exc:
        return f"Error: describe_page failed: {exc}"



def _page_capture_metrics(page, im: PILImage.Image) -> tuple[int, int, int, int, float, float]:
    iw, ih = im.size
    vp = page.viewport_size or {}
    vw = int(vp.get("width") or iw)
    vh = int(vp.get("height") or ih)
    sx = (iw / vw) if vw else 1.0
    sy = (ih / vh) if vh else 1.0
    return iw, ih, vw, vh, sx, sy


def _crop_from_page(im: PILImage.Image, page, x, y, w, h, *, cap_side: int | None = None):
    """Map CSS-pixel request onto the screenshot, clip, and return (crop, meta)."""
    iw, ih, vw, vh, sx, sy = _page_capture_metrics(page, im)
    try:
        x = float(x)
        y = float(y)
        w = float(w if w is not None else 1)
        h = float(h if h is not None else 1)
    except (TypeError, ValueError):
        return None, {"error": "Error: x, y, w, h must be numbers."}
    # Clamp in CSS space to the viewport first.
    cx, cy, cw, ch, err = crop_util.clamp_region(x, y, w, h, vw, vh, cap_side=None)
    if err:
        return None, {"error": err, "src_w": vw, "src_h": vh}
    # Then map to screenshot pixels and apply the 32×32 cap there (so 1024 values).
    crop, meta = crop_util.crop_rgb(
        im, cx * sx, cy * sy, cw * sx, ch * sy, cap_side=cap_side
    )
    meta["css_x"] = cx
    meta["css_y"] = cy
    meta["css_w"] = cw
    meta["css_h"] = ch
    meta["viewport"] = (vw, vh)
    return crop, meta


def _png_bytes(im: PILImage.Image) -> bytes:
    buf = BytesIO()
    im.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()


async def pixels(x: float, y: float, w: float = 1, h: float = 1, source: str = "page") -> str:
    """Exact RGB hex for a small region of the current page (capped at 32×32), plus average and dominant colors."""
    try:
        source = (source or "page").strip().lower()
        if source not in {"page", "desktop"}:
            return "Error: source must be 'page' or 'desktop'."
        if source == "desktop":
            return (
                "Error: on this connector, pixels() reads the current browser page. "
                "Use desk_pixels on the desktop connector."
            )
        async with _lock:
            page = await ensure_browser()
            if not page.url or page.url == "about:blank":
                return "Error: no page is open. Call browse() first."
            raw = await page.screenshot(type="png", full_page=False, timeout=TIMEOUT_MS)
            im = PILImage.open(BytesIO(raw)).convert("RGB")
            crop, meta = _crop_from_page(im, page, x, y, w, h, cap_side=crop_util.MAX_PIXEL_SIDE)
            if meta.get("error") or crop is None:
                return meta.get("error") or "Error: could not crop the page region."
            dominant = _format_colors(_dominant_colors(_png_bytes(crop), 6))
            return crop_util.format_pixels(
                crop,
                origin=(meta["x"], meta["y"]),
                src_w=meta["src_w"],
                src_h=meta["src_h"],
                space="page screenshot pixels (origin is the current viewport)",
                dominant=dominant,
            )
    except Exception as exc:
        return f"Error: pixels failed: {exc}"


async def crop_b64(
    x: float,
    y: float,
    w: float,
    h: float,
    scale: float = 1.0,
    format: str = "png",
) -> str:
    """Return a crop of the current page as base64 text (capped at ~12 KB) plus a /shots/ URL and a one-line decode snippet."""
    try:
        async with _lock:
            page = await ensure_browser()
            if not page.url or page.url == "about:blank":
                return "Error: no page is open. Call browse() first."
            raw = await page.screenshot(type="png", full_page=False, timeout=TIMEOUT_MS)
            im = PILImage.open(BytesIO(raw)).convert("RGB")
            crop, meta = _crop_from_page(im, page, x, y, w, h, cap_side=None)
            if meta.get("error") or crop is None:
                return meta.get("error") or "Error: could not crop the page region."
            try:
                data, fmt, b64, ow, oh, applied = crop_util.encode_crop_b64(
                    crop, fmt=format, scale=scale
                )
            except ValueError as exc:
                msg = str(exc)
                return msg if msg.startswith("Error:") else f"Error: {msg}"
            shot_url = _save_shot(data, fmt)
            return crop_util.format_crop_b64(
                b64=b64,
                fmt=fmt,
                x=meta["x"],
                y=meta["y"],
                req_w=meta["w"],
                req_h=meta["h"],
                out_w=ow,
                out_h=oh,
                src_w=meta["src_w"],
                src_h=meta["src_h"],
                space="page screenshot pixels (origin is the current viewport)",
                scale=applied,
                shot_url=shot_url,
            )
    except Exception as exc:
        return f"Error: crop_b64 failed: {exc}"
