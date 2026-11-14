"""Zero-dependency pure-Python ISO/IEC 18004 QR Code SVG generator & animated callout builder.

Supports Byte-mode QR versions 1..10 (Error Correction Level L — up to 271 UTF-8 bytes) with:
- GF(256) Reed-Solomon error correction (`0x11D` primitive polynomial)
- Finder, separator, timing, alignment, and dark-module patterns
- BCH(15, 5) format bits & BCH(18, 6) version bits (v7..10)
- Automatic mask selection (masks 0..7 scored via ISO penalty rules)
- Clean single-`<path>` vector `<svg>` output with 4-module white quiet zone!
"""

from __future__ import annotations

import html
import re
from urllib.parse import urlparse

_QR_SPECS_L: dict[int, tuple[int, int, int, tuple[int, ...]]] = {
    1: (19, 7, 1, ()),
    2: (34, 10, 1, (6, 18)),
    3: (55, 15, 1, (6, 22)),
    4: (80, 20, 1, (6, 26)),
    5: (108, 26, 1, (6, 30)),
    6: (136, 18, 2, (6, 34)),
    7: (156, 20, 2, (6, 22, 38)),
    8: (194, 24, 2, (6, 24, 42)),
    9: (232, 30, 2, (6, 26, 46)),
    10: (274, 18, 4, (6, 28, 50)),
}

_GF_EXP = [0] * 512
_GF_LOG = [0] * 256
_x = 1
for _i in range(255):
    _GF_EXP[_i] = _x
    _GF_LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, 512):
    _GF_EXP[_i] = _GF_EXP[_i - 255]


def _gf_mul(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    return _GF_EXP[_GF_LOG[a] + _GF_LOG[b]]


def _rs_generator_poly(degree: int) -> list[int]:
    poly = [1]
    for i in range(degree):
        nxt = [0] * (len(poly) + 1)
        root = _GF_EXP[i]
        for j, coef in enumerate(poly):
            nxt[j] ^= coef
            nxt[j + 1] ^= _gf_mul(coef, root)
        poly = nxt
    return poly


def _rs_remainder(data: list[int], ec_len: int) -> list[int]:
    gen = _rs_generator_poly(ec_len)
    rem = [0] * ec_len
    for byte in data:
        factor = byte ^ rem[0]
        rem = [*rem[1:], 0]
        if factor:
            for j in range(ec_len):
                rem[j] ^= _gf_mul(gen[j + 1], factor)
    return rem


def _encode_codewords(payload: bytes) -> tuple[int, list[int]]:
    n = len(payload)
    ver = 0
    for v, (cap_cw, _, _, _) in _QR_SPECS_L.items():
        len_bits = 8 if v <= 9 else 16
        max_bytes = (cap_cw * 8 - 4 - len_bits) // 8
        if n <= max_bytes:
            ver = v
            break
    if ver == 0:
        raise ValueError(f"QR payload too long ({n} bytes; max 271 bytes)")

    total_data_cw, ec_per_blk, num_blks, _ = _QR_SPECS_L[ver]
    len_bits = 8 if ver <= 9 else 16

    bits: list[int] = [0, 1, 0, 0]
    for shift in range(len_bits - 1, -1, -1):
        bits.append((n >> shift) & 1)
    for b in payload:
        for shift in range(7, -1, -1):
            bits.append((b >> shift) & 1)

    cap_bits = total_data_cw * 8
    for _ in range(min(4, cap_bits - len(bits))):
        bits.append(0)
    while len(bits) % 8 != 0:
        bits.append(0)

    data_cw = [sum(bits[i + k] << (7 - k) for k in range(8)) for i in range(0, len(bits), 8)]
    pad_seq = (0xEC, 0x11)
    pad_idx = 0
    while len(data_cw) < total_data_cw:
        data_cw.append(pad_seq[pad_idx % 2])
        pad_idx += 1

    short_len = total_data_cw // num_blks
    num_long = total_data_cw % num_blks
    num_short = num_blks - num_long

    data_blocks: list[list[int]] = []
    ec_blocks: list[list[int]] = []
    pos = 0
    for b_idx in range(num_blks):
        blen = short_len + (1 if b_idx >= num_short else 0)
        blk = data_cw[pos : pos + blen]
        pos += blen
        data_blocks.append(blk)
        ec_blocks.append(_rs_remainder(blk, ec_per_blk))

    interleaved: list[int] = []
    max_dlen = max(len(b) for b in data_blocks)
    for col in range(max_dlen):
        for blk in data_blocks:
            if col < len(blk):
                interleaved.append(blk[col])
    for col in range(ec_per_blk):
        for eblk in ec_blocks:
            interleaved.append(eblk[col])

    return ver, interleaved


def _format_bits_level_l(mask: int) -> int:
    data = (0b01 << 3) | mask
    rem = data << 10
    for i in range(14, 9, -1):
        if (rem >> i) & 1:
            rem ^= 0x537 << (i - 10)
    return ((data << 10) | rem) ^ 0x5412


def _version_bits(ver: int) -> int:
    rem = ver << 12
    for i in range(17, 11, -1):
        if (rem >> i) & 1:
            rem ^= 0x1F25 << (i - 12)
    return (ver << 12) | rem


def _mask_fn(mask: int, r: int, c: int) -> bool:
    if mask == 0:
        return (r + c) % 2 == 0
    if mask == 1:
        return r % 2 == 0
    if mask == 2:
        return c % 3 == 0
    if mask == 3:
        return (r + c) % 3 == 0
    if mask == 4:
        return (r // 2 + c // 3) % 2 == 0
    if mask == 5:
        return ((r * c) % 2 + (r * c) % 3) == 0
    if mask == 6:
        return (((r * c) % 2 + (r * c) % 3) % 2) == 0
    return (((r + c) % 2 + (r * c) % 3) % 2) == 0


def _penalty_score(grid: list[list[int]]) -> int:
    size = len(grid)
    score = 0
    for r in range(size):
        run_val, run_len = grid[r][0], 1
        for c in range(1, size):
            if grid[r][c] == run_val:
                run_len += 1
            else:
                if run_len >= 5:
                    score += 3 + (run_len - 5)
                run_val, run_len = grid[r][c], 1
        if run_len >= 5:
            score += 3 + (run_len - 5)

    for c in range(size):
        run_val, run_len = grid[0][c], 1
        for r in range(1, size):
            if grid[r][c] == run_val:
                run_len += 1
            else:
                if run_len >= 5:
                    score += 3 + (run_len - 5)
                run_val, run_len = grid[r][c], 1
        if run_len >= 5:
            score += 3 + (run_len - 5)

    for r in range(size - 1):
        for c in range(size - 1):
            v = grid[r][c]
            if v == grid[r][c + 1] == grid[r + 1][c] == grid[r + 1][c + 1]:
                score += 3
    return score


def encode_qr_matrix(text: str) -> list[list[int]]:
    """Encode `text` into a square 2D matrix of `0`/`1` modules (ISO/IEC 18004 Level L)."""
    ver, codewords = _encode_codewords(text.encode("utf-8"))
    size = 17 + 4 * ver
    _, _, _, align_centers = _QR_SPECS_L[ver]

    base = [[0] * size for _ in range(size)]
    is_func = [[False] * size for _ in range(size)]

    def _set_func(r: int, c: int, val: int) -> None:
        if 0 <= r < size and 0 <= c < size:
            base[r][c] = val
            is_func[r][c] = True

    def _place_finder(top: int, left: int) -> None:
        for dr in range(-1, 8):
            for dc in range(-1, 8):
                val = (
                    1
                    if (0 <= dr <= 6 and 0 <= dc <= 6)
                    and (dr in (0, 6) or dc in (0, 6) or (2 <= dr <= 4 and 2 <= dc <= 4))
                    else 0
                )
                _set_func(top + dr, left + dc, val)

    _place_finder(0, 0)
    _place_finder(0, size - 7)
    _place_finder(size - 7, 0)

    for i in range(8, size - 8):
        _set_func(6, i, 1 if i % 2 == 0 else 0)
        _set_func(i, 6, 1 if i % 2 == 0 else 0)

    for ar in align_centers:
        for ac in align_centers:
            if (ar == 6 and ac == 6) or (ar == 6 and ac == size - 7) or (ar == size - 7 and ac == 6):
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    val = 1 if max(abs(dr), abs(dc)) in (0, 2) else 0
                    _set_func(ar + dr, ac + dc, val)

    for i in range(9):
        if i != 6:
            _set_func(8, i, 0)
            _set_func(i, 8, 0)
    for i in range(8):
        _set_func(8, size - 1 - i, 0)
        _set_func(size - 1 - i, 8, 0)
    _set_func(size - 8, 8, 1)

    if ver >= 7:
        vbits = _version_bits(ver)
        for i in range(18):
            bit = (vbits >> i) & 1
            r, c = i // 3, size - 11 + (i % 3)
            _set_func(r, c, bit)
            _set_func(c, r, bit)

    bit_stream = [(cw >> shift) & 1 for cw in codewords for shift in range(7, -1, -1)]
    bit_idx = 0
    upward = True
    col = size - 1
    while col > 0:
        if col == 6:
            col -= 1
        rows = range(size - 1, -1, -1) if upward else range(size)
        for r in rows:
            for dc in (0, -1):
                c = col + dc
                if not is_func[r][c]:
                    base[r][c] = bit_stream[bit_idx] if bit_idx < len(bit_stream) else 0
                    bit_idx += 1
        upward = not upward
        col -= 2

    def _apply_mask_and_format(mask: int) -> list[list[int]]:
        g = [row[:] for row in base]
        for r in range(size):
            for c in range(size):
                if not is_func[r][c] and _mask_fn(mask, r, c):
                    g[r][c] ^= 1
        fbits = _format_bits_level_l(mask)
        tl_coords = [
            (8, 0), (8, 1), (8, 2), (8, 3), (8, 4), (8, 5), (8, 7), (8, 8),
            (7, 8), (5, 8), (4, 8), (3, 8), (2, 8), (1, 8), (0, 8),
        ]
        for idx, (r, c) in enumerate(tl_coords):
            g[r][c] = (fbits >> idx) & 1
        for idx in range(7):
            g[size - 1 - idx][8] = (fbits >> idx) & 1
        for idx in range(7, 15):
            g[8][size - 15 + idx] = (fbits >> idx) & 1
        return g

    best_grid: list[list[int]] | None = None
    best_score = 10**9
    for m in range(8):
        cand = _apply_mask_and_format(m)
        sc = _penalty_score(cand)
        if sc < best_score:
            best_score = sc
            best_grid = cand
    assert best_grid is not None
    return best_grid


def generate_qr_svg(
    url: str,
    *,
    border: int = 4,
    dark: str = "#0b0e14",
    light: str = "#ffffff",
    size_px: int = 260,
) -> str:
    """Return a crisp, self-contained `<svg>` QR code with `border`-module white quiet zone."""
    matrix = encode_qr_matrix(url)
    n = len(matrix)
    dim = n + 2 * border
    rects = [
        f"M{c + border},{r + border}h1v1h-1z"
        for r in range(n)
        for c in range(n)
        if matrix[r][c]
    ]
    path_d = "".join(rects)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {dim} {dim}" '
        f'width="{size_px}" height="{size_px}" shape-rendering="crispEdges">'
        f'<rect width="{dim}" height="{dim}" rx="2" fill="{light}"/>'
        f'<path d="{path_d}" fill="{dark}"/>'
        f"</svg>"
    )


def short_display_url(url: str) -> str:
    parsed = urlparse(url.strip())
    host = (parsed.netloc or "").removeprefix("www.")
    path = (parsed.path or "").rstrip("/")
    frag = f"#{parsed.fragment}" if parsed.fragment else ""
    disp = f"{host}{path}{frag}" if host else url.strip()
    return disp[:48] + ("…" if len(disp) > 48 else "")


def build_qr_callout_html(
    *,
    title: str,
    url: str,
    kicker: str = "SCAN OR CLICK LINK IN DESCRIPTION",
    position: str = "bottom-right",
    accent: str = "#4cc9f0",
) -> str:
    """Build a responsive 1080p/4K animated HTML/SVG/JS QR callout card for `motion.render_overlay`."""
    svg = generate_qr_svg(url, border=3, size_px=240)
    disp_url = html.escape(short_display_url(url))
    esc_title = html.escape(title.strip() or "Explore the Code & Notebook")
    esc_kicker = html.escape(kicker.strip())
    esc_accent = re.sub(r"[^#a-zA-Z0-9(),.% -]", "", accent) or "#4cc9f0"

    pos_css = {
        "bottom-right": "bottom: 6.5vh; right: 4.5vw;",
        "bottom-left": "bottom: 6.5vh; left: 4.5vw;",
        "top-right": "top: 6.5vh; right: 4.5vw;",
        "top-left": "top: 6.5vh; left: 4.5vw;",
    }.get(position, "bottom: 6.5vh; right: 4.5vw;")

    return f"""<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; font-family: 'Segoe UI', system-ui, -apple-system, sans-serif; }}
  #qr-card {{
    position: absolute; {pos_css}
    display: flex; align-items: center; gap: 1.6vw;
    padding: 1.4vw 1.9vw 1.4vw 1.4vw;
    max-width: 44vw;
    background: rgba(11, 15, 25, 0.94);
    border: 0.14vw solid rgba(255, 255, 255, 0.14);
    border-left: 0.45vw solid {esc_accent};
    border-radius: 1.1vw;
    box-shadow: 0 1.4vw 3.5vw rgba(0, 0, 0, 0.72);
    color: #f8fafc;
  }}
  .qr-box {{
    width: 7.4vw; height: 7.4vw; min-width: 132px; min-height: 132px;
    background: #ffffff; border-radius: 0.55vw; padding: 0.32vw;
    display: flex; align-items: center; justify-content: center; flex-shrink: 0;
  }}
  .qr-box svg {{ width: 100%; height: 100%; display: block; }}
  .qr-meta {{ display: flex; flex-direction: column; gap: 0.42vw; min-width: 0; }}
  .qr-kicker {{ font-size: 0.85vw; font-weight: 800; letter-spacing: 0.11em; color: {esc_accent}; text-transform: uppercase; }}
  .qr-title {{ font-size: 1.65vw; font-weight: 850; line-height: 1.18; color: #ffffff; }}
  .qr-url {{
    font-family: ui-monospace, 'Cascadia Code', Consolas, monospace;
    font-size: 1.05vw; color: #cbd5e1; background: rgba(255, 255, 255, 0.07);
    padding: 0.3vw 0.65vw; border-radius: 0.4vw; width: fit-content;
  }}
  .qr-prog {{ width: 100%; height: 0.22vw; background: rgba(255,255,255,0.12); border-radius: 99px; margin-top: 0.3vw; overflow: hidden; }}
  .qr-prog-bar {{ height: 100%; background: {esc_accent}; width: 0%; }}
</style>
<div id="qr-card">
  <div class="qr-box">{svg}</div>
  <div class="qr-meta">
    <div class="qr-kicker">{esc_kicker}</div>
    <div class="qr-title">{esc_title}</div>
    <div class="qr-url">{disp_url}</div>
    <div class="qr-prog"><div class="qr-prog-bar" id="qr-bar"></div></div>
  </div>
</div>
<script>
  window.renderFrame = (f, t, dur) => {{
    const enter = Math.min(1, t / 0.38);
    const exit = t > dur - 0.42 ? Math.max(0, (dur - t) / 0.42) : 1;
    const ease = 1 - Math.pow(1 - enter, 3);
    const card = document.getElementById('qr-card');
    card.style.opacity = (ease * exit).toFixed(3);
    card.style.transform = `translateY(${{((1 - ease) * 36).toFixed(1)}}px) scale(${{(0.95 + 0.05 * ease).toFixed(3)}})`;
    document.getElementById('qr-bar').style.width = `${{Math.min(100, (t / Math.max(0.1, dur)) * 100).toFixed(1)}}%`;
  }};
</script>"""