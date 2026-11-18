"""Pure-Python ISO/IEC 18004 QR Code generator and terminal/SVG renderers for Feishu web login.

Produces scannable QR matrices (`size` x `size` rows of ``"0"`` / ``"1"``, matching
``lark-desk``'s ``connector.QRCode``), compact Unicode half-block terminal art, and
inline ``data:image/svg+xml;base64,...`` URIs with zero external dependencies.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any

# Version table for ECC Level L (Versions 1..10):
# (total_data_codewords, ec_codewords_per_block, ((num_blocks, data_codewords_per_block), ...), alignment_coords)
_VERSION_SPECS_L: dict[int, tuple[int, int, tuple[tuple[int, int], ...], tuple[int, ...]]] = {
    1: (19, 7, ((1, 19),), ()),
    2: (34, 10, ((1, 34),), (6, 18)),
    3: (55, 15, ((1, 55),), (6, 22)),
    4: (80, 20, ((1, 80),), (6, 26)),
    5: (108, 26, ((1, 108),), (6, 30)),
    6: (136, 18, ((2, 68),), (6, 34)),
    7: (156, 20, ((2, 78),), (6, 22, 38)),
    8: (194, 24, ((2, 97),), (6, 24, 42)),
    9: (232, 30, ((2, 116),), (6, 26, 46)),
    10: (274, 18, ((2, 68), (2, 69)), (6, 28, 50)),
}

# GF(256) primitive polynomial 0x11D
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
        next_poly = [0] * (len(poly) + 1)
        root = _GF_EXP[i]
        for j, coeff in enumerate(poly):
            next_poly[j] ^= coeff
            next_poly[j + 1] ^= _gf_mul(coeff, root)
        poly = next_poly
    return poly


def _rs_remainder(data: list[int], degree: int) -> list[int]:
    gen = _rs_generator_poly(degree)
    rem = [0] * degree
    for b in data:
        factor = b ^ rem[0]
        rem = rem[1:] + [0]
        if factor != 0:
            for j in range(degree):
                rem[j] ^= _gf_mul(gen[j + 1], factor)
    return rem


def _format_bits_ecc_l(mask: int) -> int:
    # ECC Level L indicator is 0b01
    data = (0b01 << 3) | (mask & 0x7)
    rem = data << 10
    for i in range(4, -1, -1):
        if rem & (1 << (i + 10)):
            rem ^= 0x537 << i
    return ((data << 10) | rem) ^ 0x5412


def _version_bits(version: int) -> int:
    rem = version << 12
    for i in range(5, -1, -1):
        if rem & (1 << (i + 12)):
            rem ^= 0x1F25 << i
    return (version << 12) | rem


def _mask_bit(mask: int, r: int, c: int) -> bool:
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
        return (r * c) % 2 + (r * c) % 3 == 0
    if mask == 6:
        return ((r * c) % 2 + (r * c) % 3) % 2 == 0
    return ((r + c) % 2 + (r * c) % 3) % 2 == 0


@dataclass(frozen=True)
class QRCodeMatrix:
    """QR code matrix matching ``lark-desk``'s ``QRCode`` structure (`size` and `"0"`/`"1"` `rows`)."""

    size: int
    rows: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"size": self.size, "rows": list(self.rows)}


def encode_qr_matrix(text: str, *, mask: int = 0) -> QRCodeMatrix:
    """Encode ``text`` into an ISO/IEC 18004 QR Code matrix (Byte mode, ECC Level L)."""
    payload = text.encode("utf-8")
    version = 0
    for v, (cap, _, _, _) in _VERSION_SPECS_L.items():
        len_bits = 8 if v <= 9 else 16
        needed_bytes = (4 + len_bits + len(payload) * 8 + 7) // 8
        if needed_bytes <= cap:
            version = v
            break
    if version == 0:
        raise ValueError(f"QR payload too long ({len(payload)} bytes; max 271 bytes)")

    total_data, ec_len, block_groups, align_coords = _VERSION_SPECS_L[version]
    len_bits = 8 if version <= 9 else 16

    bits: list[int] = []

    def append_bits(val: int, count: int) -> None:
        for i in range(count - 1, -1, -1):
            bits.append((val >> i) & 1)

    # Byte mode indicator (0100) + character count
    append_bits(0b0100, 4)
    append_bits(len(payload), len_bits)
    for b in payload:
        append_bits(b, 8)

    capacity_bits = total_data * 8
    terminator = min(4, capacity_bits - len(bits))
    append_bits(0, terminator)
    while len(bits) % 8 != 0:
        bits.append(0)

    data_bytes = [sum(bits[i + j] << (7 - j) for j in range(8)) for i in range(0, len(bits), 8)]
    pad_seq = (0xEC, 0x11)
    pad_idx = 0
    while len(data_bytes) < total_data:
        data_bytes.append(pad_seq[pad_idx % 2])
        pad_idx += 1

    # Split into blocks and compute Reed-Solomon EC bytes
    data_blocks: list[list[int]] = []
    ec_blocks: list[list[int]] = []
    offset = 0
    for num_blocks, block_data_len in block_groups:
        for _ in range(num_blocks):
            block = data_bytes[offset : offset + block_data_len]
            offset += block_data_len
            data_blocks.append(block)
            ec_blocks.append(_rs_remainder(block, ec_len))

    interleaved: list[int] = []
    max_data_len = max(len(blk) for blk in data_blocks)
    for i in range(max_data_len):
        for blk in data_blocks:
            if i < len(blk):
                interleaved.append(blk[i])
    for i in range(ec_len):
        for eb in ec_blocks:
            interleaved.append(eb[i])

    final_bits: list[int] = []
    for byte_val in interleaved:
        for i in range(7, -1, -1):
            final_bits.append((byte_val >> i) & 1)

    size = 17 + 4 * version
    grid: list[list[int]] = [[0] * size for _ in range(size)]
    is_func: list[list[bool]] = [[False] * size for _ in range(size)]

    def set_func(r: int, c: int, val: int) -> None:
        if 0 <= r < size and 0 <= c < size:
            grid[r][c] = 1 if val else 0
            is_func[r][c] = True

    def place_finder(top_r: int, left_c: int) -> None:
        for dr in range(-1, 8):
            for dc in range(-1, 8):
                r, c = top_r + dr, left_c + dc
                if 0 <= r < size and 0 <= c < size:
                    in_outer = 0 <= dr <= 6 and (dr in {0, 6} or dc in {0, 6})
                    in_inner = 2 <= dr <= 4 and 2 <= dc <= 4
                    in_box = 0 <= dr <= 6 and 0 <= dc <= 6
                    set_func(r, c, 1 if (in_box and (in_outer or in_inner)) else 0)

    place_finder(0, 0)
    place_finder(0, size - 7)
    place_finder(size - 7, 0)

    # Timing patterns
    for i in range(8, size - 8):
        bit = 1 if i % 2 == 0 else 0
        set_func(6, i, bit)
        set_func(i, 6, bit)

    # Alignment patterns
    for ar in align_coords:
        for ac in align_coords:
            if (
                (ar == 6 and ac == 6)
                or (ar == 6 and ac == size - 7)
                or (ar == size - 7 and ac == 6)
            ):
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    dist = max(abs(dr), abs(dc))
                    set_func(ar + dr, ac + dc, 1 if dist in {0, 2} else 0)

    # Dark module & reserved format areas
    set_func(4 * version + 9, 8, 1)
    for i in range(9):
        if not is_func[8][i]:
            set_func(8, i, 0)
        if not is_func[i][8]:
            set_func(i, 8, 0)
    for i in range(8):
        if not is_func[8][size - 1 - i]:
            set_func(8, size - 1 - i, 0)
        if not is_func[size - 1 - i][8]:
            set_func(size - 1 - i, 8, 0)

    if version >= 7:
        vbits = _version_bits(version)
        for i in range(18):
            bit = (vbits >> i) & 1
            r, c = i // 3, size - 11 + (i % 3)
            set_func(r, c, bit)
            set_func(c, r, bit)

    # Zigzag data bit placement
    bit_idx = 0
    col = size - 1
    upward = True
    while col > 0:
        if col == 6:
            col -= 1
        row_iter = range(size - 1, -1, -1) if upward else range(size)
        for r in row_iter:
            for dc in (0, -1):
                c = col + dc
                if not is_func[r][c]:
                    b = final_bits[bit_idx] if bit_idx < len(final_bits) else 0
                    bit_idx += 1
                    if _mask_bit(mask, r, c):
                        b ^= 1
                    grid[r][c] = b
        upward = not upward
        col -= 2

    # Format info bits
    fbits = _format_bits_ecc_l(mask)
    for i in range(6):
        set_func(8, i, (fbits >> (14 - i)) & 1)
    set_func(8, 7, (fbits >> 8) & 1)
    set_func(8, 8, (fbits >> 7) & 1)
    set_func(7, 8, (fbits >> 6) & 1)
    for i in range(6):
        set_func(5 - i, 8, (fbits >> (5 - i)) & 1)

    for i in range(7):
        set_func(size - 1 - i, 8, (fbits >> (14 - i)) & 1)
    for i in range(8):
        set_func(8, size - 8 + i, (fbits >> (7 - i)) & 1)

    rows = tuple("".join("1" if cell else "0" for cell in row) for row in grid)
    return QRCodeMatrix(size=size, rows=rows)


def render_qr_unicode(matrix: QRCodeMatrix, *, border: int = 2) -> str:
    """Render a ``QRCodeMatrix`` using compact Unicode half-blocks for terminal scanning."""
    size = matrix.size
    pad = max(1, border)
    padded: list[list[int]] = []
    for _ in range(pad):
        padded.append([0] * (size + 2 * pad))
    for row in matrix.rows:
        padded.append([0] * pad + [1 if ch == "1" else 0 for ch in row] + [0] * pad)
    for _ in range(pad):
        padded.append([0] * (size + 2 * pad))
    if len(padded) % 2 != 0:
        padded.append([0] * (size + 2 * pad))

    lines: list[str] = []
    # Invert visually (white quiet zone = full block █, dark module = space) or standard dark-on-light:
    # Using dark background / light foreground compatible blocks where 1=dark and 0=light:
    # We render dark modules as spaces inside a white block frame (`█`, `▀`, `▄`, ` `) so it scans
    # reliably on both dark and light terminal themes!
    for r in range(0, len(padded), 2):
        top_row = padded[r]
        bot_row = padded[r + 1]
        chars: list[str] = []
        for top, bot in zip(top_row, bot_row):
            if top == 0 and bot == 0:
                chars.append("█")
            elif top == 0 and bot == 1:
                chars.append("▀")
            elif top == 1 and bot == 0:
                chars.append("▄")
            else:
                chars.append(" ")
        lines.append("".join(chars))
    return "\n".join(lines)


def render_qr_svg_data_url(matrix: QRCodeMatrix, *, border: int = 2, scale: int = 6) -> str:
    """Render a ``QRCodeMatrix`` as a self-contained ``data:image/svg+xml;base64,...`` URI."""
    pad = max(1, border)
    total = (matrix.size + 2 * pad) * max(1, scale)
    rects: list[str] = []
    for r, row in enumerate(matrix.rows):
        for c, ch in enumerate(row):
            if ch == "1":
                x = (c + pad) * scale
                y = (r + pad) * scale
                rects.append(f'<rect x="{x}" y="{y}" width="{scale}" height="{scale}"/>')
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {total} {total}" '
        f'width="{total}" height="{total}" shape-rendering="crispEdges">'
        f'<rect width="100%" height="100%" fill="#ffffff"/>'
        f'<g fill="#000000">{"".join(rects)}</g></svg>'
    )
    b64 = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    return f"data:image/svg+xml;base64,{b64}"
