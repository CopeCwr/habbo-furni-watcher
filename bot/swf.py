"""Lector mínimo de SWF: extrae imágenes (DefineBitsLossless/2), binarios y símbolos."""
import io
import lzma
import struct
import zlib

from PIL import Image


def _body(data: bytes) -> bytes:
    sig = data[:3]
    if sig == b"FWS":
        return data[8:]
    if sig == b"CWS":
        return zlib.decompress(data[8:])
    if sig == b"ZWS":
        props = data[12:17]
        return lzma.decompress(props + b"\xff" * 8 + data[17:], format=lzma.FORMAT_ALONE)
    raise ValueError("No es un archivo SWF válido")


def _cstr(buf: bytes, pos: int):
    end = buf.index(b"\x00", pos)
    return buf[pos:end].decode("utf-8", "replace"), end + 1


def _lossless(payload: bytes, alpha: bool):
    cid, fmt = struct.unpack_from("<HB", payload, 0)
    w, h = struct.unpack_from("<HH", payload, 3)
    pos = 7
    table = 0
    if fmt == 3:
        table = payload[pos] + 1
        pos += 1
    raw = zlib.decompress(payload[pos:])
    if fmt == 3:
        step = 4 if alpha else 3
        pal = raw[: table * step]
        idx = raw[table * step:]
        stride = (w + 3) & ~3
        out = bytearray()
        for y in range(h):
            row = idx[y * stride: y * stride + w]
            for i in row:
                if i >= table:
                    out += b"\x00\x00\x00\x00"
                    continue
                e = pal[i * step: i * step + step]
                if alpha:
                    r, g, b, a = e
                    if a and a < 255:  # alfa premultiplicado
                        r, g, b = (min(255, c * 255 // a) for c in (r, g, b))
                    out += bytes((r, g, b, a))
                else:
                    out += bytes((e[0], e[1], e[2], 255))
        return cid, Image.frombytes("RGBA", (w, h), bytes(out))
    if fmt == 5:
        px = bytearray(raw[: w * h * 4])
        out = bytearray(len(px))
        for i in range(0, len(px), 4):
            a, r, g, b = px[i], px[i + 1], px[i + 2], px[i + 3]
            if not alpha:
                a = 255
            elif 0 < a < 255:
                r, g, b = (min(255, c * 255 // a) for c in (r, g, b))
            out[i:i + 4] = bytes((r, g, b, a))
        return cid, Image.frombytes("RGBA", (w, h), bytes(out))
    raise ValueError(f"Formato lossless {fmt} no soportado")


def parse_swf(data: bytes):
    """Devuelve (images{id: PIL}, binaries{id: bytes}, symbols{id: nombre})."""
    body = _body(data)
    nbits = body[0] >> 3
    pos = (5 + 4 * nbits + 7) // 8 + 4
    images, binaries, symbols = {}, {}, {}
    while pos + 2 <= len(body):
        (hdr,) = struct.unpack_from("<H", body, pos)
        pos += 2
        code, ln = hdr >> 6, hdr & 0x3F
        if ln == 0x3F:
            (ln,) = struct.unpack_from("<I", body, pos)
            pos += 4
        payload = body[pos: pos + ln]
        pos += ln
        if code == 0:
            break
        try:
            if code in (20, 36):
                cid, img = _lossless(payload, alpha=(code == 36))
                images[cid] = img
            elif code == 87:
                (cid,) = struct.unpack_from("<H", payload, 0)
                binaries[cid] = payload[6:]
            elif code == 76:
                (n,) = struct.unpack_from("<H", payload, 0)
                p = 2
                for _ in range(n):
                    (cid,) = struct.unpack_from("<H", payload, p)
                    name, p = _cstr(payload, p + 2)
                    symbols[cid] = name
        except Exception:
            continue
    return images, binaries, symbols
