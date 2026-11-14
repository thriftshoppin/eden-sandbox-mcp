"""Linux desktop tools for this agent's X display (DISPLAY=:4).

Served only on the secret /d/<key>/mcp path. Action tools (everything except
status, screenshot, windows, pixels, and crop_b64) require an armed session.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import time
from io import BytesIO
from pathlib import Path

import mss
from mcp.server.mcpserver import Image
from PIL import Image as PILImage

import browser_tools
import crop_util

DISPLAY = os.environ.get("DESK_DISPLAY") or os.environ.get("DISPLAY") or ":4"
if not DISPLAY.startswith(":"):
    DISPLAY = ":" + DISPLAY.lstrip(":")

DIR = Path("/workspace/mcp-test")
KEY_FILE = DIR / "desktop.key"
ARM_STATE = DIR / "desk_arm.state"
ACTION_LOG = DIR / "desk_actions.log"
SHOTS_DIR = DIR / "desk_shots"
OCR_TSV = DIR / "desk_last_ocr.tsv"
IDLE_S = 300
MAX_ARM_MIN = 60
DEFAULT_ARM_MIN = 15
OCR_CAP = 2000

# Human aliases -> argv. launch() only starts these.
LAUNCH_ALLOW: dict[str, list[str]] = {
    "browser": ["google-chrome-stable", "google-chrome", "chromium"],
    "chrome": ["google-chrome-stable", "google-chrome"],
    "editor": ["mousepad"],
    "text editor": ["mousepad"],
    "mousepad": ["mousepad"],
    "files": ["thunar"],
    "file manager": ["thunar"],
    "thunar": ["thunar"],
    "terminal": ["xfce4-terminal"],
    "xfce4-terminal": ["xfce4-terminal"],
}

_env = dict(os.environ)
_env["DISPLAY"] = DISPLAY


def _now_cst() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime())
    # localtime follows the box TZ; force Chicago for the log:
    # (done below in log via TZ isn't available; use explicit)


def _cst_stamp() -> str:
    env = dict(os.environ)
    env["TZ"] = "America/Chicago"
    out = subprocess.check_output(["date", "+%Y-%m-%d %H:%M:%S %Z"], env=env, text=True)
    return out.strip()


def _load_key() -> str:
    if not KEY_FILE.is_file():
        return ""
    return KEY_FILE.read_text(encoding="utf-8").strip()


def _public_origin() -> str:
    return browser_tools._public_origin()


def _secret_shot_url(name: str) -> str:
    key = _load_key()
    if not key:
        return f"{_public_origin()}/shots/{name}"
    return f"{_public_origin()}/d/{key}/shots/{name}"


def _log_action(tool: str, detail: str) -> None:
    line = f"{_cst_stamp()} tool={tool} {detail}\n"
    try:
        with ACTION_LOG.open("a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass


def _run(args: list[str], timeout: float = 8) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        env=_env,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=timeout,
    )


def _read_arm() -> dict:
    if not ARM_STATE.is_file():
        return {"armed": False}
    try:
        data = json.loads(ARM_STATE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"armed": False}
    return data if isinstance(data, dict) else {"armed": False}


def _write_arm(data: dict) -> None:
    tmp = ARM_STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data) + "\n", encoding="utf-8")
    tmp.replace(ARM_STATE)


def disarm(reason: str = "hangup") -> dict:
    data = _read_arm()
    data["armed"] = False
    data["disarmed_reason"] = reason
    data["disarmed_at"] = int(time.time())
    data.pop("expires", None)
    _write_arm(data)
    _log_action("disarm", f"reason={reason}")
    return data


def _eval_arm() -> dict:
    """Return current arm status, applying expiry and idle disarm."""
    data = _read_arm()
    if not data.get("armed"):
        return {"armed": False, "reason": data.get("disarmed_reason") or "disarmed"}
    now = time.time()
    expires = float(data.get("expires") or 0)
    last = float(data.get("last_activity") or 0)
    if expires and now >= expires:
        disarm("expired")
        return {"armed": False, "reason": "expired"}
    if last and now - last >= IDLE_S:
        disarm("idle")
        return {"armed": False, "reason": "idle"}
    return data


def is_armed() -> bool:
    return bool(_eval_arm().get("armed"))


def touch_activity() -> None:
    data = _eval_arm()
    if not data.get("armed"):
        return
    data["last_activity"] = int(time.time())
    _write_arm(data)


def _require_armed() -> str | None:
    st = _eval_arm()
    if st.get("armed"):
        return None
    reason = st.get("reason") or "disarmed"
    return (
        f"DISARMED ({reason}). Action tools are off until the session is armed. "
        "Ask in chat to 'go', or run desk_arm.sh <minutes>."
    )


def _time_left(data: dict) -> tuple[int, int]:
    now = time.time()
    expires = float(data.get("expires") or 0)
    last = float(data.get("last_activity") or 0)
    until_exp = max(0, int(expires - now)) if expires else 0
    until_idle = max(0, int((last + IDLE_S) - now)) if last else 0
    return until_exp, until_idle


def _screen_size() -> tuple[int, int]:
    try:
        r = _run(["xdotool", "getdisplaygeometry"], timeout=4)
        parts = (r.stdout or "").split()
        if len(parts) >= 2:
            return int(parts[0]), int(parts[1])
    except Exception:
        pass
    return 1920, 1200


def _active_window() -> dict:
    info = {"id": "", "title": "", "geometry": ""}
    try:
        r = _run(["xdotool", "getactivewindow"], timeout=4)
        wid = (r.stdout or "").strip()
        info["id"] = wid
        if wid:
            n = _run(["xdotool", "getwindowname", wid], timeout=4)
            info["title"] = (n.stdout or "").strip()
            g = _run(["xdotool", "getwindowgeometry", wid], timeout=4)
            info["geometry"] = " ".join((g.stdout or "").split())
    except Exception as exc:
        info["error"] = str(exc)
    return info


def _window_list() -> list[dict]:
    rows: list[dict] = []
    try:
        r = _run(["wmctrl", "-l", "-p", "-G"], timeout=5)
    except Exception:
        return rows
    for line in (r.stdout or "").splitlines():
        # id desktop pid x y w h host title...
        parts = line.split(None, 8)
        if len(parts) < 8:
            continue
        title = parts[8] if len(parts) > 8 else ""
        rows.append({
            "id": parts[0],
            "desktop": parts[1],
            "pid": parts[2],
            "x": parts[3],
            "y": parts[4],
            "w": parts[5],
            "h": parts[6],
            "title": title,
        })
    return rows


def _grab_png() -> tuple[bytes, int, int]:
    with mss.mss(display=DISPLAY) as sct:
        mon = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
        raw = sct.grab(mon)
        img = PILImage.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
    w, h = img.size
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue(), w, h


def _ocr_image(path: Path) -> tuple[str, str]:
    """Return (plain text, tsv). Empty strings if tesseract is missing."""
    if not shutil.which("tesseract"):
        return "", ""
    try:
        plain = subprocess.run(
            ["tesseract", str(path), "stdout", "--psm", "6", "-l", "eng"],
            capture_output=True, text=True, errors="replace", timeout=20, env=_env,
        )
        tsv = subprocess.run(
            ["tesseract", str(path), "stdout", "tsv", "--psm", "6", "-l", "eng"],
            capture_output=True, text=True, errors="replace", timeout=20, env=_env,
        )
        return (plain.stdout or "").strip(), (tsv.stdout or "")
    except Exception:
        return "", ""


def _save_desk_shot(data: bytes, fmt: str) -> str:
    SHOTS_DIR.mkdir(parents=True, exist_ok=True)
    ext = "jpg" if fmt in {"jpeg", "jpg"} else (fmt or "png")
    name = f"{secrets.token_hex(8)}.{ext}"
    (SHOTS_DIR / name).write_bytes(data)
    files = sorted(
        [f for f in SHOTS_DIR.iterdir() if f.is_file()],
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )
    for old in files[30:]:
        try:
            old.unlink()
        except OSError:
            pass
    return _secret_shot_url(name)


def _window_lines(wins: list[dict], limit: int = 12) -> str:
    if not wins:
        return "(no windows)"
    parts = []
    for w in wins[:limit]:
        parts.append(f"{w.get('title') or '(untitled)'} [{w.get('id','')}]")
    more = f" (+{len(wins)-limit} more)" if len(wins) > limit else ""
    return "; ".join(parts) + more


def desk_status() -> str:
    """Armed state, time left, screen size, and the active window."""
    data = _eval_arm()
    w, h = _screen_size()
    active = _active_window()
    armed = bool(data.get("armed"))
    until_exp, until_idle = _time_left(data) if armed else (0, 0)
    out = {
        "armed": armed,
        "reason": None if armed else (data.get("reason") or data.get("disarmed_reason") or "disarmed"),
        "expires_in_s": until_exp if armed else 0,
        "idle_disarm_in_s": until_idle if armed else 0,
        "idle_timeout_s": IDLE_S,
        "display": DISPLAY,
        "screen": {"width": w, "height": h},
        "active_window": active,
    }
    _log_action("status", f"armed={armed}")
    return json.dumps(out, indent=2)


def desk_windows() -> str:
    """List top-level windows on this agent's display."""
    wins = _window_list()
    active = _active_window()
    _log_action("windows", f"count={len(wins)}")
    return json.dumps({"display": DISPLAY, "active": active, "windows": wins}, indent=2)


def desk_screenshot() -> list:
    """Capture the desktop. Returns an image plus voice-friendly text (shot URL, windows, palette, OCR)."""
    raw, w, h = _grab_png()
    data, fmt = browser_tools._compress_image(raw)
    shot_url = _save_desk_shot(data, fmt)
    # OCR from the uncompressed PNG so small text stays readable.
    SHOTS_DIR.mkdir(parents=True, exist_ok=True)
    ocr_src = SHOTS_DIR / "_last_ocr.png"
    ocr_src.write_bytes(raw)
    text, tsv = _ocr_image(ocr_src)
    if tsv:
        OCR_TSV.write_text(tsv, encoding="utf-8")
    ocr_text = browser_tools._clip_text(text, OCR_CAP) if text else "(no OCR text)"
    active = _active_window()
    wins = _window_list()
    colors = browser_tools._format_colors(browser_tools._dominant_colors(data, 6))
    lines = [
        f"Desktop screenshot on {DISPLAY} ({w}x{h}).",
        f"Coordinates: {w}×{h} screen pixels, origin (0,0) top-left. "
        "Use desk_pixels(x,y,w,h) or desk_crop_b64(x,y,w,h) in this space.",
        f"Image: {shot_url} ({len(data) // 1024} KB {fmt.upper()})",
        f"Active window: {active.get('title') or '(none)'} {active.get('id') or ''}".strip(),
        f"Windows: {_window_lines(wins)}",
        f"Colors: {colors}" if colors else "Colors: (none)",
        "OCR text:",
        ocr_text,
    ]
    caption = "\n".join(lines)
    _log_action("screenshot", f"bytes={len(data)} ocr_chars={len(text)}")
    return [Image(data=data, format=fmt), caption]


def desk_pixels(x: float, y: float, w: float = 1, h: float = 1, source: str = "desktop") -> str:
    """Exact RGB hex for a small region of the desktop (capped at 32×32), plus average and dominant colors. Works while disarmed; does not change arming."""
    source = (source or "desktop").strip().lower()
    if source not in {"page", "desktop"}:
        return "Error: source must be 'page' or 'desktop'."
    if source == "page":
        return (
            "Error: on this connector, desk_pixels() reads the desktop. "
            "Use pixels() on the open browser connector."
        )
    try:
        raw, sw, sh = _grab_png()
        im = PILImage.open(BytesIO(raw)).convert("RGB")
        crop, meta = crop_util.crop_rgb(im, x, y, w, h, cap_side=crop_util.MAX_PIXEL_SIDE)
        if meta.get("error") or crop is None:
            return meta.get("error") or "Error: could not crop the desktop region."
        buf = BytesIO()
        crop.save(buf, format="PNG")
        dominant = browser_tools._format_colors(browser_tools._dominant_colors(buf.getvalue(), 6))
        _log_action("pixels", f"x={meta['x']} y={meta['y']} w={meta['w']} h={meta['h']}")
        return crop_util.format_pixels(
            crop,
            origin=(meta["x"], meta["y"]),
            src_w=meta["src_w"],
            src_h=meta["src_h"],
            space=f"desktop {DISPLAY}",
            dominant=dominant,
        )
    except Exception as exc:
        return f"Error: desk_pixels failed: {exc}"


def desk_crop_b64(
    x: float,
    y: float,
    w: float,
    h: float,
    scale: float = 1.0,
    format: str = "png",
) -> str:
    """Return a crop of the desktop as base64 text (capped at ~12 KB) plus a /shots/ URL and a one-line decode snippet. Works while disarmed; does not change arming."""
    try:
        raw, sw, sh = _grab_png()
        im = PILImage.open(BytesIO(raw)).convert("RGB")
        crop, meta = crop_util.crop_rgb(im, x, y, w, h, cap_side=None)
        if meta.get("error") or crop is None:
            return meta.get("error") or "Error: could not crop the desktop region."
        try:
            data, fmt, b64, ow, oh, applied = crop_util.encode_crop_b64(
                crop, fmt=format, scale=scale
            )
        except ValueError as exc:
            msg = str(exc)
            return msg if msg.startswith("Error:") else f"Error: {msg}"
        shot_url = _save_desk_shot(data, fmt)
        _log_action("crop_b64", f"x={meta['x']} y={meta['y']} w={meta['w']} h={meta['h']} b64={len(b64)}")
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
            space=f"desktop {DISPLAY}",
            scale=applied,
            shot_url=shot_url,
        )
    except Exception as exc:
        return f"Error: desk_crop_b64 failed: {exc}"



def desk_focus(window: str) -> str:
    """Focus a window by title substring or window id (0x…)."""
    err = _require_armed()
    if err:
        return err
    window = (window or "").strip()
    if not window:
        return "Error: give a window title substring or id."
    if re.fullmatch(r"0x[0-9a-fA-F]+", window) or window.isdigit():
        r = _run(["wmctrl", "-i", "-a", window])
    else:
        r = _run(["wmctrl", "-a", window])
    if r.returncode != 0:
        # try case-insensitive match via -x? wmctrl -a is already substring
        _log_action("focus", f"window={window!r} rc={r.returncode}")
        return f"Error: could not focus {window!r}: {(r.stderr or r.stdout or 'unknown').strip()[:200]}"
    time.sleep(0.15)
    active = _active_window()
    touch_activity()
    _log_action("focus", f"window={window!r} now={active.get('title')!r}")
    return json.dumps({"ok": True, "requested": window, "active": active}, indent=2)


def desk_click(x: float, y: float, button: str = "left", double: bool = False) -> str:
    """Click at screen pixels (x, y). button is left, right, or middle."""
    err = _require_armed()
    if err:
        return err
    btn_map = {"left": "1", "middle": "2", "right": "3"}
    b = btn_map.get((button or "left").lower())
    if not b:
        return "Error: button must be left, right, or middle."
    xi, yi = int(round(x)), int(round(y))
    sw, sh = _screen_size()
    if xi < 0 or yi < 0 or xi >= sw or yi >= sh:
        return f"Error: click ({xi},{yi}) is outside the {sw}x{sh} screen."
    args = ["xdotool", "mousemove", "--sync", str(xi), str(yi)]
    r = _run(args)
    if r.returncode != 0:
        return f"Error: mousemove failed: {(r.stderr or '').strip()[:200]}"
    if double:
        r = _run(["xdotool", "click", "--repeat", "2", "--delay", "80", b])
    else:
        r = _run(["xdotool", "click", b])
    if r.returncode != 0:
        return f"Error: click failed: {(r.stderr or '').strip()[:200]}"
    touch_activity()
    _log_action("click", f"x={xi} y={yi} button={button} double={bool(double)}")
    return json.dumps({"ok": True, "x": xi, "y": yi, "button": button, "double": bool(double)})


def _parse_tsv(tsv: str) -> list[dict]:
    words = []
    lines = tsv.splitlines()
    if not lines:
        return words
    header = lines[0].split("\t")
    for line in lines[1:]:
        cols = line.split("\t")
        if len(cols) < 12:
            continue
        row = dict(zip(header, cols)) if len(header) >= 12 else {}
        try:
            left, top, width, height = int(row.get("left", cols[6])), int(row.get("top", cols[7])), int(row.get("width", cols[8])), int(row.get("height", cols[9]))
            conf = float(row.get("conf", cols[10]))
        except ValueError:
            continue
        text = (row.get("text") if row else cols[11]).strip()
        if not text or conf < 0:
            continue
        words.append({"text": text, "left": left, "top": top, "width": width, "height": height, "conf": conf})
    return words


def desk_click_text(text: str) -> str:
    """Find on-screen text with OCR and click the first match."""
    err = _require_armed()
    if err:
        return err
    needle = (text or "").strip()
    if not needle:
        return "Error: give the text to click."
    raw, w, h = _grab_png()
    SHOTS_DIR.mkdir(parents=True, exist_ok=True)
    ocr_src = SHOTS_DIR / "_last_ocr.png"
    ocr_src.write_bytes(raw)
    _, tsv = _ocr_image(ocr_src)
    if tsv:
        OCR_TSV.write_text(tsv, encoding="utf-8")
    words = _parse_tsv(tsv)
    if not words:
        _log_action("click_text", "no_ocr")
        return "Error: OCR found no words on screen. Try screenshot first or a larger target."
    tokens = needle.split()
    found = None
    low_tokens = [t.lower() for t in tokens]
    # Prefer a multi-word sequence, then a single word containing the phrase.
    for i in range(len(words)):
        chunk = words[i:i + len(low_tokens)]
        if len(chunk) < len(low_tokens):
            break
        if [c["text"].lower() for c in chunk] == low_tokens:
            x1 = min(c["left"] for c in chunk)
            y1 = min(c["top"] for c in chunk)
            x2 = max(c["left"] + c["width"] for c in chunk)
            y2 = max(c["top"] + c["height"] for c in chunk)
            found = {"x": (x1 + x2) // 2, "y": (y1 + y2) // 2, "matched": " ".join(c["text"] for c in chunk)}
            break
    if not found:
        q = needle.lower()
        for c in words:
            if q in c["text"].lower():
                found = {
                    "x": c["left"] + c["width"] // 2,
                    "y": c["top"] + c["height"] // 2,
                    "matched": c["text"],
                }
                break
    if not found:
        _log_action("click_text", f"not_found query_len={len(needle)}")
        return f"Error: could not find {needle!r} on screen."
    result = desk_click(found["x"], found["y"], "left", False)
    _log_action("click_text", f"matched={found['matched']!r} x={found['x']} y={found['y']}")
    return json.dumps({"ok": True, "matched": found["matched"], "x": found["x"], "y": found["y"], "click": result})


TYPE_CHUNK = 80
TYPE_DELAY_MS = 8
TYPE_SETTLE_S = 0.25
TYPE_MAX_CHARS = 100_000
PASTE_MAX_CHARS = 2_000_000


def _ensure_focus() -> tuple[dict, str | None]:
    """Activate the current window and wait briefly so the first keystrokes land."""
    active = _active_window()
    wid = (active.get("id") or "").strip()
    if not wid:
        return active, "Error: no focused window to type into. Focus a window first."
    r = _run(["xdotool", "windowactivate", "--sync", wid], timeout=4)
    if r.returncode != 0:
        _run(["xdotool", "windowfocus", "--sync", wid], timeout=4)
    else:
        _run(["xdotool", "windowfocus", "--sync", wid], timeout=4)
    time.sleep(TYPE_SETTLE_S)
    now = _active_window()
    if now.get("id") and now.get("id") != wid:
        _run(["xdotool", "windowactivate", "--sync", wid], timeout=4)
        _run(["xdotool", "windowfocus", "--sync", wid], timeout=4)
        time.sleep(0.15)
        now = _active_window()
    return now or active, None


def _xdotool_type_chunk(chunk: str) -> subprocess.CompletedProcess:
    # xdotool type treats leading '-' as flags; always pass -- .
    timeout = max(8, (len(chunk) * TYPE_DELAY_MS) / 1000 + 4)
    return _run(
        ["xdotool", "type", "--clearmodifiers", "--delay", str(TYPE_DELAY_MS), "--", chunk],
        timeout=timeout,
    )


def _type_literal(s: str) -> str | None:
    """Type a newline-free, tab-free string in chunks. Returns an error or None."""
    for i in range(0, len(s), TYPE_CHUNK):
        chunk = s[i : i + TYPE_CHUNK]
        r = _xdotool_type_chunk(chunk)
        if r.returncode != 0:
            return f"Error: type failed: {(r.stderr or '').strip()[:200]}"
    return None


def desk_type(text: str) -> str:
    """Type text into the focused window as real keystrokes. Newlines become Return, tabs become Tab. Returns the character count sent."""
    err = _require_armed()
    if err:
        return err
    if text is None:
        return "Error: give the text to type."
    if not isinstance(text, str):
        text = str(text)
    if len(text) > TYPE_MAX_CHARS:
        return f"Error: text is longer than {TYPE_MAX_CHARS} characters; use desk_paste_text for long documents."
    active, ferr = _ensure_focus()
    if ferr:
        return ferr
    # Preserve a trailing newline; treat CRLF as a single break.
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    sent = 0
    returns = 0
    tabs = 0
    for i, line in enumerate(lines):
        segs = line.split("\t")
        for j, seg in enumerate(segs):
            if seg:
                err = _type_literal(seg)
                if err:
                    _log_action("type", f"len={len(text)} sent={sent} rc=fail")
                    return err
                sent += len(seg)
            if j < len(segs) - 1:
                r = _run(["xdotool", "key", "--clearmodifiers", "Tab"], timeout=4)
                if r.returncode != 0:
                    return f"Error: Tab key failed: {(r.stderr or '').strip()[:200]}"
                tabs += 1
                sent += 1
        if i < len(lines) - 1:
            r = _run(["xdotool", "key", "--clearmodifiers", "Return"], timeout=4)
            if r.returncode != 0:
                return f"Error: Return key failed: {(r.stderr or '').strip()[:200]}"
            returns += 1
            sent += 1
    touch_activity()
    _log_action("type", f"len={len(text)} sent={sent} newlines={returns} tabs={tabs}")
    return json.dumps(
        {
            "ok": True,
            "typed_chars": sent,
            "newlines": returns,
            "tabs": tabs,
            "window": {"id": active.get("id"), "title": active.get("title")},
        }
    )


def _clipboard_owner(text: str) -> tuple[subprocess.Popen | None, str | None]:
    """Own the X clipboard long enough for one paste. Do not wait for the
    process here: xclip/xsel must stay alive until ctrl+v reads the selection.
    Using capture_output would also hang, because the forked owner keeps
    the pipes open.
    """
    exe = shutil.which("xclip") or shutil.which("xsel")
    if not exe:
        return None, "Error: no clipboard tool (xclip/xsel) is installed."
    # Stay in the foreground (-quiet) and do not use -loops 1: GTK paste
    # first asks for TARGETS, then the text, so a one-shot owner exits too early.
    if Path(exe).name == "xclip":
        args = [exe, "-quiet", "-selection", "clipboard", "-i"]
    else:
        args = [exe, "--clipboard", "--input"]
    try:
        proc = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=_env,
        )
        assert proc.stdin is not None
        proc.stdin.write(text.encode("utf-8"))
        proc.stdin.close()
    except Exception as exc:
        return None, f"Error: clipboard set failed: {exc}"
    time.sleep(0.05)
    if proc.poll() not in (None, 0):
        return None, f"Error: clipboard set failed (exit {proc.returncode})."
    return proc, None


def desk_paste_text(text: str) -> str:
    """Paste text into the focused window by setting the X clipboard internally and sending ctrl+v. Faster and more reliable than typing for long or multi-line text. Requires an armed session. This is not a standalone clipboard-set tool."""
    err = _require_armed()
    if err:
        return err
    if text is None:
        return "Error: give the text to paste."
    if not isinstance(text, str):
        text = str(text)
    if len(text) > PASTE_MAX_CHARS:
        return f"Error: text is longer than {PASTE_MAX_CHARS} characters."
    active, ferr = _ensure_focus()
    if ferr:
        return ferr
    proc, cerr = _clipboard_owner(text)
    if cerr:
        return cerr
    time.sleep(0.12)
    r = _run(["xdotool", "key", "--clearmodifiers", "ctrl+v"], timeout=6)
    # GTK typically makes several selection requests (TARGETS, then text).
    # Keep the owner alive briefly, then stop it so this is not a lingering
    # clipboard-set tool.
    time.sleep(0.25)
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except Exception:
            proc.kill()
    if r.returncode != 0:
        _log_action("paste_text", f"len={len(text)} rc={r.returncode}")
        return f"Error: paste (ctrl+v) failed: {(r.stderr or '').strip()[:200]}"
    touch_activity()
    _log_action("paste_text", f"len={len(text)}")
    return json.dumps(
        {
            "ok": True,
            "pasted_chars": len(text),
            "window": {"id": active.get("id"), "title": active.get("title")},
        }
    )


def desk_key(combo: str) -> str:
    """Press a key or combination, e.g. 'Return', 'ctrl+s', 'alt+Tab'."""
    err = _require_armed()
    if err:
        return err
    combo = (combo or "").strip()
    if not combo:
        return "Error: give a key or combination (e.g. Return, ctrl+s)."
    aliases = {
        "enter": "Return",
        "return": "Return",
        "esc": "Escape",
        "escape": "Escape",
        "space": "space",
        "tab": "Tab",
        "backspace": "BackSpace",
        "del": "Delete",
        "delete": "Delete",
        "win": "Super_L",
        "super": "Super_L",
    }
    parts = []
    for p in re.split(r"[+ ]+", combo):
        if not p:
            continue
        parts.append(aliases.get(p.lower(), p))
    keyseq = "+".join(parts)
    if not re.fullmatch(r"[A-Za-z0-9_+\-]+", keyseq):
        return "Error: combo has unexpected characters."
    r = _run(["xdotool", "key", "--clearmodifiers", keyseq], timeout=8)
    if r.returncode != 0:
        _log_action("key", f"combo={keyseq} rc={r.returncode}")
        return f"Error: key failed: {(r.stderr or '').strip()[:200]}"
    touch_activity()
    _log_action("key", f"combo={keyseq}")
    return json.dumps({"ok": True, "key": keyseq})


def desk_scroll(direction: str = "down", amount: int = 3) -> str:
    """Scroll at the current pointer. direction is up, down, left, or right."""
    err = _require_armed()
    if err:
        return err
    direction = (direction or "down").lower()
    btn = {"up": "4", "down": "5", "left": "6", "right": "7"}.get(direction)
    if not btn:
        return "Error: direction must be up, down, left, or right."
    try:
        amount = int(amount)
    except (TypeError, ValueError):
        amount = 3
    amount = max(1, min(30, amount))
    r = _run(["xdotool", "click", "--repeat", str(amount), "--delay", "30", btn])
    if r.returncode != 0:
        return f"Error: scroll failed: {(r.stderr or '').strip()[:200]}"
    touch_activity()
    _log_action("scroll", f"direction={direction} amount={amount}")
    return json.dumps({"ok": True, "direction": direction, "amount": amount})


def desk_launch(app: str) -> str:
    """Start an allowlisted app: browser, text editor, file manager, or terminal."""
    err = _require_armed()
    if err:
        return err
    name = (app or "").strip().lower()
    argv = None
    for alias, cands in LAUNCH_ALLOW.items():
        if name == alias or name == cands[0]:
            argv = cands
            break
    if not argv:
        allowed = "browser, editor (mousepad), file manager (thunar), terminal (xfce4-terminal)"
        return f"Error: {app!r} is not on the launch allowlist ({allowed})."
    exe = next((c for c in argv if shutil.which(c)), None)
    if not exe:
        return f"Error: no executable found for {app!r} (tried {argv})."
    extra: list[str] = []
    if "chrome" in exe:
        extra = [f"--display={DISPLAY}", "--no-first-run", "--disable-sync", "--new-window"]
    try:
        proc = subprocess.Popen(
            [exe] + extra,
            env=_env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        return f"Error: failed to launch {exe}: {exc}"
    time.sleep(0.8)
    touch_activity()
    _log_action("launch", f"app={exe} pid={proc.pid}")
    return json.dumps({"ok": True, "launched": exe, "pid": proc.pid, "active": _active_window()}, indent=2)


def desk_hangup() -> str:
    """Disarm the desktop session immediately."""
    disarm("hangup")
    _log_action("hangup", "disarmed")
    return json.dumps({"ok": True, "armed": False, "message": "Desktop session disarmed."})
