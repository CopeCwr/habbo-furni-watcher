"""Compone un furni .swf de Habbo en una cuadrícula con todos sus estados (WebP animado)."""
import io
import math
import re
import sys
import xml.etree.ElementTree as ET

from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageOps

from .swf import parse_swf

TICK_MS = 125            # duración de cada paso de animación
MAX_FRAMES = 120         # frames máximos del WebP
BG = (58, 63, 75, 255)   # fondo de cada celda (necesario para el brillo aditivo)
MAX_BYTES = 7_500_000    # límite de seguridad (Discord admite ~8-25 MB)


def _xml(blob: bytes) -> ET.Element:
    text = blob.decode("latin-1").replace("\r", "")
    text = re.sub(r"^<\?xml[^>]*\?>", "", text.strip())
    return ET.fromstring(text)


def _find_blob(binaries, symbols, suffix):
    for cid, name in symbols.items():
        if name.endswith("_" + suffix) and cid in binaries:
            return binaries[cid]
    return None


# --------------------------------------------------------------------------- visualización
def _parse_visualization(blob, size):
    """-> layers{idx:{ink,alpha,z}}, dir_z{dir:{idx:z}}, states{id:{idx:[frames]|None}}"""
    layers, dir_z, states = {}, {}, {}
    if blob is None:
        return layers, dir_z, states
    node = None
    for v in _xml(blob).iter("visualization"):
        if v.get("size") == str(size):
            node = v
    if node is None:
        return layers, dir_z, states
    lay = node.find("layers")
    if lay is not None:
        for l in lay.findall("layer"):
            a = l.get("alpha")
            layers[int(l.get("id"))] = {
                "ink": (l.get("ink") or "COPY").upper(),
                "alpha": int(a) / 255 if a else 1.0,
                "z": int(l.get("z")) if l.get("z") else None,
            }
    dirs = node.find("directions")
    if dirs is not None:
        for d in dirs.findall("direction"):
            dir_z[int(d.get("id"))] = {int(l.get("id")): int(l.get("z"))
                                       for l in d.findall("layer") if l.get("z")}
    anims = node.find("animations")
    if anims is not None:
        for a in anims.findall("animation"):
            seqs = {}
            for al in a.findall("animationLayer"):
                idx = int(al.get("id"))
                fs = al.find("frameSequence")
                if fs is None:
                    seqs[idx] = None
                    continue
                rep = int(al.get("frameRepeat", "1"))
                frames = []
                for f in fs.findall("frame"):
                    frames += [int(f.get("id"))] * rep
                seqs[idx] = frames or None
            states[int(a.get("id"))] = seqs
    return layers, dir_z, states


# --------------------------------------------------------------------------- composición
def _compose_cell(items, assets, box, scale):
    """items: [(asset, ink, alpha)] en orden de dibujo. Devuelve imagen RGB de la celda."""
    x0, y0, W, H = box
    cell = Image.new("RGBA", (W, H), BG)
    for name, ink, alpha in items:
        img, x, y, flip = assets[name]
        left = (x - img.width if flip else -x) - x0
        top = -y - y0
        im = img
        if alpha < 1:
            im = img.copy()
            im.putalpha(im.getchannel("A").point(lambda v, a=alpha: int(v * a)))
        if ink == "ADD":
            layer = Image.new("RGBA", (W, H), (0, 0, 0, 255))
            layer.alpha_composite(im, (left, top)) if left >= 0 and top >= 0 else layer.paste(im, (left, top), im)
            cell = Image.merge("RGBA", (*ImageChops.add(cell.convert("RGB"), layer.convert("RGB")).split(), cell.getchannel("A")))
        else:
            cell.alpha_composite(im, (left, top)) if left >= 0 and top >= 0 else cell.paste(im, (left, top), im)
    cell = cell.convert("RGB")
    if scale > 1:
        cell = cell.resize((W * scale, H * scale), Image.NEAREST)
    return cell


def _font(px):
    for p in ("DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "arialbd.ttf", "Arial Bold.ttf"):
        try:
            return ImageFont.truetype(p, px)
        except OSError:
            continue
    try:
        return ImageFont.load_default(px)
    except TypeError:
        return ImageFont.load_default()


def _label(cell, text):
    d = ImageDraw.Draw(cell, "RGBA")
    f = _font(max(11, cell.width // 9))
    l, t, r, b = d.textbbox((0, 0), text, font=f)
    pad = 4
    d.rounded_rectangle((4, 4, 4 + (r - l) + pad * 2, 4 + (b - t) + pad * 2), 5, fill=(0, 0, 0, 170))
    d.text((4 + pad - l, 4 + pad - t), text, font=f, fill=(255, 255, 255, 255))


def _lcm(a, b):
    return a * b // math.gcd(a, b)


def render_swf(swf_bytes: bytes, classname: str, size: int = 64, direction=None, labels=True):
    images, binaries, symbols = parse_swf(swf_bytes)
    by_name = {sym: images[cid] for cid, sym in symbols.items() if cid in images}

    name_re = re.compile(r"^(.*_\d+_[a-z]+)_(\d+)_(\d+)$")

    def get_img(asset):
        for key in (f"{classname}_{asset}", asset):
            if key in by_name:
                return by_name[key]
        # Algunos SWF solo guardan la imagen de una dirección y las demás la reutilizan
        m = name_re.match(asset)
        if m:
            for d in (0, 2, 4, 6):
                alt = f"{m.group(1)}_{d}_{m.group(3)}"
                for key in (f"{classname}_{alt}", alt):
                    if key in by_name:
                        return by_name[key]

    assets_blob = _find_blob(binaries, symbols, "assets")
    if assets_blob is None:
        raise RuntimeError("El SWF no contiene el XML de assets")

    assets = {}
    for a in _xml(assets_blob).iter("asset"):
        name = a.get("name")
        img = get_img(a.get("source") or name)
        if img is None:
            continue
        flip = a.get("flipH") == "1"
        assets[name] = (ImageOps.mirror(img) if flip else img, int(a.get("x", 0)), int(a.get("y", 0)), flip)

    pat = re.compile(rf"^{re.escape(classname)}_{size}_([a-z]+)_(\d+)_(\d+)$")
    by_dir = {}  # dir -> idx -> {frame: asset}
    for name in assets:
        m = pat.match(name)
        if m:
            idx = ord(m.group(1)) - 97 if len(m.group(1)) == 1 else None
            if idx is not None:
                by_dir.setdefault(int(m.group(2)), {}).setdefault(idx, {})[int(m.group(3))] = name
    if not by_dir:
        raise RuntimeError(f"No hay assets de tamaño {size}")
    if direction not in by_dir:
        direction = 2 if 2 in by_dir else sorted(by_dir)[0]
    frames_of = by_dir[direction]            # idx -> {frame: asset}

    layers, dir_z, states = _parse_visualization(_find_blob(binaries, symbols, "visualization"), size)
    if not states:
        states = {0: {i: None for i in frames_of}}
    state_ids = sorted(states)

    # orden de dibujo: z de la dirección > z de la capa > hereda el anterior
    z_dir = dir_z.get(direction, {})
    order, prev = [], 0
    for idx in sorted(frames_of):
        z = z_dir.get(idx, layers.get(idx, {}).get("z"))
        if z is None:
            z = prev + 1 if (z_dir or any(v.get("z") is not None for v in layers.values())) else 0
        prev = z
        order.append((z, idx))
    order = [i for _, i in sorted(order, key=lambda t: t[0])]

    def lens(seqs):
        return [len(s) for s in seqs.values() if s]

    periods = {s: max(1, math.lcm(*lens(states[s])) if lens(states[s]) else 1) for s in state_ids}
    total = 1
    for p in periods.values():
        total = _lcm(total, p)
    if total > MAX_FRAMES:
        total = min(max(periods.values()), MAX_FRAMES)

    def items_at(state, t):
        out = []
        for idx in order:
            seq = states[state].get(idx)
            frames = frames_of[idx]
            # Sin secuencia -> frame 0. Si el frame 0 no tiene imagen (hueco vacío), la capa está oculta.
            fid = seq[t % len(seq)] if seq else 0
            if fid not in frames:
                continue
            L = layers.get(idx, {})
            out.append((frames[fid], L.get("ink", "COPY"), L.get("alpha", 1.0)))
        return out

    plan = {s: [items_at(s, t) for t in range(total)] for s in state_ids}

    # caja común para que todas las celdas cuadren (el halo tenue de los brillos no cuenta)
    sig_cache = {}

    def sig_box(name, ink, alpha):
        key = (name, ink, alpha)
        if key not in sig_cache:
            img = assets[name][0]
            a = img.getchannel("A")
            if ink == "ADD":
                r, g, b, _ = img.split()
                m = ImageChops.multiply(a, ImageChops.lighter(ImageChops.lighter(r, g), b))
                mask = m.point(lambda v: 255 if v * alpha > 90 else 0)
            else:
                mask = a.point(lambda v: 255 if v * alpha > 16 else 0)
            sig_cache[key] = mask.getbbox()
        return sig_cache[key]

    x0 = y0 = 10 ** 9
    x1 = y1 = -10 ** 9
    for s_ in state_ids:
        for items in plan[s_]:
            for name, ink, alpha in items:
                bb = sig_box(name, ink, alpha)
                if not bb:
                    continue
                img, x, y, flip = assets[name]
                left = x - img.width if flip else -x
                x0, y0 = min(x0, left + bb[0]), min(y0, -y + bb[1])
                x1, y1 = max(x1, left + bb[2]), max(y1, -y + bb[3])
    pad = 6
    box = (x0 - pad, y0 - pad, x1 - x0 + pad * 2, y1 - y0 + pad * 2)
    W, H = box[2], box[3]

    n = len(state_ids)
    cols = n if n <= 3 else 3 if n <= 6 else 4 if n <= 8 else 5
    cols = max(1, cols)
    rows = math.ceil(n / cols)
    min_cell = 150 if labels and n > 1 else 0   # que quepa la etiqueta
    scale = max(1, min(3, 960 // (cols * W)))
    if W * scale < min_cell:
        scale = math.ceil(min_cell / W)
    gap = 4

    def build(scale):
        gw, gh = cols * W * scale + (cols + 1) * gap, rows * H * scale + (rows + 1) * gap
        out = []
        cache = {}
        for t in range(total):
            sheet = Image.new("RGB", (gw, gh), (30, 32, 38))
            for k, s in enumerate(state_ids):
                key = (s, tuple(plan[s][t]))
                if key not in cache:
                    cell = _compose_cell(plan[s][t], assets, box, scale)
                    if labels and n > 1:
                        _label(cell, f"Estado {s}")
                    cache[key] = cell
                r, c = divmod(k, cols)
                sheet.paste(cache[key], (gap + c * (W * scale + gap), gap + r * (H * scale + gap)))
            out.append(sheet)
        return out

    return build(scale)


def save_webp(frames, target):
    """Guarda como WebP (sin pérdida; si pesa demasiado, con pérdida mínima)."""
    def write(buf, **kw):
        if len(frames) == 1:
            frames[0].save(buf, "WEBP", **kw)
        else:
            frames[0].save(buf, "WEBP", save_all=True, append_images=frames[1:],
                           duration=TICK_MS, loop=0, **kw)
    probe = io.BytesIO()
    write(probe, lossless=True, minimize_size=True)
    kw = dict(lossless=True, minimize_size=True)
    if probe.tell() > MAX_BYTES:
        kw = dict(quality=88, method=4)
    if hasattr(target, "write"):
        write(target, **kw)
    else:
        with open(target, "wb") as f:
            write(f, **kw)


if __name__ == "__main__":
    swf, out = sys.argv[1], sys.argv[2]
    cls = sys.argv[3] if len(sys.argv) > 3 and not sys.argv[3].isdigit() else re.sub(r"\.swf$", "", swf.split("/")[-1])
    dr = int(sys.argv[-1]) if sys.argv[-1].isdigit() and len(sys.argv) > 3 else None
    fr = render_swf(open(swf, "rb").read(), cls, direction=dr)
    save_webp(fr, out)
    print(f"{len(fr)} frames {fr[0].size} -> {out}")
