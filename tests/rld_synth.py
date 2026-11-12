"""Builds small synthetic RDWorks (.rld) files for tests.

Only the parts that xs2lightburn reads are written: header with the bed size, the vector layer
table (black + 256 palette colors, 1151-byte records), the bitmap layer table (1009-byte records)
and the object list (paths made of line / Bezier segments, and 8-bit BMP bitmaps).
"""
import struct

A_REC = 1151
B_REC = 1009
A0 = 1236
PALETTE = ["0000ff", "ff0000", "00ff00", "fa8072", "ffff00", "1e90ff", "8a360f", "808000"]


def palette(k):
    return PALETTE[k] if k < len(PALETTE) else "%02x%02x%02x" % (k, 255 - k, (k * 7) % 256)


def default_layer():
    return {"speed": 100.0, "min": 30.0, "max": 30.0, "out": 1, "air": 1, "passes": 1,
            "scan": False, "mode": None, "interval": 0.1, "flags": (0, 0, 0)}


def build(bed=(1200.0, 800.0), vector=None, bitmap=None, objects=()):
    """vector / bitmap: {palette index (-1 = black): overrides of default_layer()}.
    objects: ("path", "rrggbb", closed, [(type, (8 floats))]) or ("bitmap", "rrggbb",
    (x0, x1, y0, y1), bmp_bytes)."""
    vector = vector or {}
    bitmap = bitmap or {}
    b0 = A0 + 256 * A_REC + 26
    size = b0 + 256 * B_REC + 64
    d = bytearray(size)
    head = b"\x0fRDFILEVER8.0.01a"
    d[0:len(head)] = head
    struct.pack_into("<dd", d, 20, bed[0], bed[1])
    for k in range(-1, 256):
        base = A0 + A_REC * k
        lay = dict(default_layer(), **vector.get(k, {}))
        d[base + 327:base + 330] = bytes.fromhex("000000" if k < 0 else palette(k))
        d[base + 331] = lay["out"]
        struct.pack_into("<ddd", d, base + 335, lay["speed"], lay["min"], lay["max"])
        d[base + 375] = lay["air"]
        d[base + 619] = 1 if lay["passes"] > 1 else 0
        struct.pack_into("<i", d, base + 623, lay["passes"])
        d[base + A_REC] = lay["mode"] if lay["mode"] is not None else (0 if lay["scan"] else 1)
        struct.pack_into("<d", d, base + A_REC + 17, lay["interval"])
    for k in range(256):
        base = b0 + B_REC * k
        lay = dict(default_layer(), **bitmap.get(k, {}))
        d[base + 159:base + 162] = bytes.fromhex(palette(k))
        d[base + 163] = lay["out"]
        struct.pack_into("<ddd", d, base + 167, lay["speed"], lay["min"], lay["max"])
        d[base + 207] = lay["air"]
        d[base + 451] = 1 if lay["passes"] > 1 else 0
        struct.pack_into("<i", d, base + 455, lay["passes"])
        struct.pack_into("<3i", d, base + 983, *lay["flags"])
        if base + B_REC + 9 <= len(d):
            struct.pack_into("<d", d, base + B_REC + 1, lay["interval"])
    for obj in objects:
        if obj[0] == "path":
            _, col, closed, segs = obj
            xs = [v for _t, s in segs for v in s[0:8:2]]
            ys = [v for _t, s in segs for v in s[1:8:2]]
            rec = bytearray(b"\x01\x00\x01\x00\x00\x01\x00\x00\x00" + bytes.fromhex(col) + b"\x00")
            rec += struct.pack("<4d", min(xs), max(xs), min(ys), max(ys))
            rec += struct.pack("<ii", 1 if closed else 0, len(segs))
            for t, s in segs:
                rec += struct.pack("<HI8dI", t, 0, *s, 0)
            d += rec + b"\x00" * 40
        else:
            _, col, (x0, x1, y0, y1), bmp = obj
            rec = bytearray(b"\x02\x00\x02\x00\x00\x03\x00\x00\x00" + bytes.fromhex(col) + b"\x00")
            rec += struct.pack("<4d", x0, x1, y0, y1) + bmp
            d += rec + b"\x00" * 40
    return bytes(d)


def line(x0, y0, x1, y1):
    return (0, (x0, y0, x1, y1, 0.0, 0.0, 0.0, 0.0))


def curve(p0, p1, c0, c1):
    return (3, (p0[0], p0[1], p1[0], p1[1], c0[0], c0[1], c1[0], c1[1]))


def rect(x, y, w, h):
    return [line(x + w, y + h, x, y + h), line(x, y + h, x, y), line(x, y, x + w, y),
            line(x + w, y, x + w, y + h)]


def bmp(bpp, w, rows, palette=None, top_down=False, comp=0, masks=None, header=None):
    """Uncompressed BMP. rows top to bottom: palette indices (1/4/8 bit) or (r, g, b[, a])
    tuples (24/32 bit), always written as B, G, R(, A) bytes. palette: list of (r, g, b).
    masks: (r, g, b) or (r, g, b, a) BI_BITFIELDS masks (comp=3); 4 masks give a 56-byte
    header. header: header size to write (e.g. 108 = V4, 124 = V5), padded with zeros."""
    h = len(rows)
    stride = ((w * bpp + 31) // 32) * 4
    lines = []
    for r in rows:
        if bpp >= 24:
            line = b"".join(bytes((px[2], px[1], px[0]) + ((px[3],) if bpp == 32 else ()))
                            for px in r)
        else:
            bits = "".join(format(v, "0%db" % bpp) for v in r)
            bits += "0" * (-len(bits) % 8)
            line = bytes(int(bits[i:i + 8], 2) for i in range(0, len(bits), 8))
        lines.append(line + b"\x00" * (stride - len(line)))
    pix = b"".join(lines if top_down else reversed(lines))
    pal = b""
    if bpp <= 8:
        palette = palette or [(i * 255 // ((1 << bpp) - 1),) * 3 for i in range(1 << bpp)]
        pal = b"".join(bytes((b, g, r, 0)) for r, g, b in palette)
    extra = struct.pack("<%dI" % len(masks), *masks) if masks else b""
    hsz = header or (56 if masks and len(masks) == 4 else 40)
    extra = extra.ljust(hsz - 40, b"\x00") if header else extra
    off = 14 + 40 + len(extra) + len(pal)
    return (b"BM" + struct.pack("<IHHI", off + len(pix), 0, 0, off)
            + struct.pack("<IiiHHIIiiII", hsz, w, -h if top_down else h, 1, bpp, comp, len(pix),
                          3780, 3780, len(palette) if bpp <= 8 else 0, 0)
            + extra + pal + pix)


def bmp8(w, h, rows):
    """8-bit grayscale BMP, rows given top to bottom (stored bottom-up like RDWorks)."""
    stride = (w + 3) & ~3
    pal = b"".join(bytes((i, i, i, 0)) for i in range(256))
    pix = b"".join(bytes(r) + b"\x00" * (stride - w) for r in reversed(rows))
    off = 14 + 40 + len(pal)
    return (b"BM" + struct.pack("<IHHI", off + len(pix), 0, 0, off)
            + struct.pack("<IiiHHIIiiII", 40, w, h, 1, 8, 0, len(pix), 3780, 3780, 256, 256)
            + pal + pix)
