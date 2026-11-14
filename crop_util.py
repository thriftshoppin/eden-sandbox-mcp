"""Shared helpers for pixels() / crop_b64() text results."""
from __future__ import annotations

import base64
from io import BytesIO

from PIL import Image as PILImage

# Max region for pixels(): 32x32 = 1024 values.
MAX_PIXEL_SIDE = 32
MAX_PIXEL_COUNT = MAX_PIXEL_SIDE * MAX_PIXEL_SIDE
# Cap encoded crop payload at ~12 KB of base64 text.
MAX_B64_CHARS = 12 * 1024


def _hex(rgb: tuple[int, int, int]) -> str:
    r, g, b = (max(0, min(255, int(c))) for c in rgb[:3])
    return f"#{r:02X}{g:02X}{b:02X}"


def clamp_region(x, y, w, h, img_w: int, img_h: int, *, cap_side: int | None = None) -> tuple[int, int, int, int, str | None]:
    """Clip a request to the image, optionally capping width/height. Returns (x,y,w,h, err)."""
    try:
        x = int(round(float(x)))
        y = int(round(float(y)))
        w = int(round(float(w if w is not None else 1)))
        h = int(round(float(h if h is not None else 1)))
    except (TypeError, ValueError):
        return 0, 0, 0, 0, "Error: x, y, w, h must be numbers."
    if w < 1 or h < 1:
        return 0, 0, 0, 0, "Error: w and h must be at least 1."
    if cap_side:
        if w > cap_side or h > cap_side:
            w = min(w, cap_side)
            h = min(h, cap_side)
        if w * h > MAX_PIXEL_COUNT:
            # Keep the origin; shrink the trailing edge.
            h = max(1, min(h, MAX_PIXEL_COUNT // w))
    if img_w < 1 or img_h < 1:
        return 0, 0, 0, 0, "Error: source image is empty."
    if x >= img_w or y >= img_h or x + w <= 0 or y + h <= 0:
        return 0, 0, 0, 0, f"Error: region ({x},{y},{w},{h}) is outside the {img_w}×{img_h} source."
    x2 = min(img_w, max(0, x) + w)
    y2 = min(img_h, max(0, y) + h)
    x1 = max(0, x)
    y1 = max(0, y)
    return x1, y1, max(1, x2 - x1), max(1, y2 - y1), None


def crop_rgb(im: PILImage.Image, x, y, w, h, *, cap_side: int | None = None) -> tuple[PILImage.Image | None, dict]:
    im = im.convert("RGB")
    iw, ih = im.size
    cx, cy, cw, ch, err = clamp_region(x, y, w, h, iw, ih, cap_side=cap_side)
    meta = {"src_w": iw, "src_h": ih, "x": cx, "y": cy, "w": cw, "h": ch, "error": err}
    if err:
        return None, meta
    return im.crop((cx, cy, cx + cw, cy + ch)), meta


def average_hex(im: PILImage.Image) -> str:
    im = im.convert("RGB")
    # resize to 1x1 with box filter = mean
    px = im.resize((1, 1), PILImage.Resampling.BOX).getpixel((0, 0))
    return _hex(px if isinstance(px, tuple) else (px, px, px))


def pixel_rows(im: PILImage.Image) -> list[str]:
    im = im.convert("RGB")
    w, h = im.size
    pix = im.load()
    rows = []
    for yy in range(h):
        rows.append(" ".join(_hex(pix[xx, yy]) for xx in range(w)))
    return rows


def format_pixels(im: PILImage.Image, *, origin: tuple[int, int], src_w: int, src_h: int, space: str, dominant) -> str:
    """dominant is a formatter that takes image bytes or we pass a preformatted string."""
    w, h = im.size
    x, y = origin
    rows = pixel_rows(im)
    avg = average_hex(im)
    lines = [
        f"Pixels {w}×{h} at ({x},{y}) of {src_w}×{src_h} {space}",
        f"Average: {avg}",
        f"Dominant: {dominant}",
        "Rows (left→right, top→bottom):",
        *rows,
    ]
    return "\n".join(lines)


def encode_crop_b64(
    im: PILImage.Image,
    *,
    fmt: str = "png",
    scale: float = 1.0,
) -> tuple[bytes, str, str, int, int, float]:
    """Return (raw_bytes, fmt, b64, out_w, out_h, applied_scale).

    Shrinks and/or switches to JPEG so the base64 text stays at or under
    MAX_B64_CHARS (~12 KB).
    """
    im = im.convert("RGB")
    fmt = (fmt or "png").lower().strip()
    if fmt in {"jpg", "jpeg"}:
        fmt = "jpeg"
    elif fmt != "png":
        raise ValueError("Error: format must be png or jpeg.")
    try:
        scale = float(scale) if scale is not None else 1.0
    except (TypeError, ValueError):
        raise ValueError("Error: scale must be a number.")
    if scale <= 0 or scale > 8:
        raise ValueError("Error: scale must be between 0 and 8.")

    src_w, src_h = im.size
    work = im
    applied = 1.0
    if scale != 1.0:
        nw = max(1, int(round(src_w * scale)))
        nh = max(1, int(round(src_h * scale)))
        work = im.resize((nw, nh), PILImage.Resampling.LANCZOS)
        applied = scale

    def pack(image: PILImage.Image, kind: str, quality: int | None = None) -> tuple[bytes, str]:
        buf = BytesIO()
        if kind == "jpeg":
            image.save(buf, format="JPEG", quality=quality or 80, optimize=True)
            return buf.getvalue(), "jpeg"
        image.save(buf, format="PNG", optimize=True)
        return buf.getvalue(), "png"

    data, kind = pack(work, fmt, 85)
    b64 = base64.b64encode(data).decode("ascii")
    if len(b64) <= MAX_B64_CHARS:
        ow, oh = work.size
        return data, kind, b64, ow, oh, applied

    # Too big: try JPEG at several qualities and scales.
    factors = [1.0, 0.85, 0.7, 0.55, 0.4, 0.3, 0.22, 0.16]
    qualities = (80, 70, 60, 50, 40, 30)
    best: tuple[bytes, str, str, int, int, float] | None = None
    for f in factors:
        if f != 1.0:
            nw = max(1, int(round(work.size[0] * f)))
            nh = max(1, int(round(work.size[1] * f)))
            scaled = work.resize((nw, nh), PILImage.Resampling.LANCZOS)
        else:
            scaled = work
        for q in qualities:
            data, kind = pack(scaled, "jpeg", q)
            b64 = base64.b64encode(data).decode("ascii")
            cand = (data, kind, b64, scaled.size[0], scaled.size[1], applied * (scaled.size[0] / max(1, work.size[0])))
            if best is None or len(b64) < len(best[2]):
                best = cand
            if len(b64) <= MAX_B64_CHARS:
                return cand
    assert best is not None
    return best


def format_crop_b64(
    *,
    b64: str,
    fmt: str,
    x: int,
    y: int,
    req_w: int,
    req_h: int,
    out_w: int,
    out_h: int,
    src_w: int,
    src_h: int,
    space: str,
    scale: float,
    shot_url: str,
) -> str:
    ext = "jpg" if fmt == "jpeg" else "png"
    snippet = (
        f"import base64;open('crop.{ext}','wb').write(base64.b64decode(\"{b64}\"))"
    )
    lines = [
        f"Crop {req_w}×{req_h} at ({x},{y}) of {src_w}×{src_h} {space}, "
        f"encoded {out_w}×{out_h} {fmt} ({len(b64)} base64 chars, scale={scale:.3f}).",
        f"Image: {shot_url}",
        f"Dimensions: {out_w}×{out_h}",
        "Decode with:",
        f"```python\n{snippet}\n```",
        "Base64:",
        f"```\n{b64}\n```",
    ]
    return "\n".join(lines)
