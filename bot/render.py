"""Compone un furni .swf de Habbo en una cuadrícula con todos sus estados (WebP animado)."""
import io
import math
import os
import re
import sys
import xml.etree.ElementTree as ET

from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageOps

from .swf import parse_swf

TICK_MS = 42             # duración de cada paso (24 fps) - igual que el cliente de Habbo
MAX_FRAMES = 1000        # pasos máximos de la animación (1000 x 42 ms = 42 s)
SHADOW_ALPHA = 48 / 255   # opacidad de la sombra (negro) en el cliente de Habbo
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
                fss = al.findall("frameSequence")
                if not fss:
                    seqs[idx] = None
                    continue
                rep = int(al.get("frameRepeat", "1"))
                frames = []
                # Con random="1" el juego elige una secuencia al azar; aquí se encadenan todas
                for fs in fss:
                    for f in fs.findall("frame"):
                        frames += [int(f.get("id"))] * rep
                seqs[idx] = frames or None
            states[int(a.get("id"))] = seqs
    return layers, dir_z, states


# --------------------------------------------------------------------------- composición
_scaled_cache = {}


def _scaled(img, scale, key):
    k = (key, scale)
    if k not in _scaled_cache:
        _scaled_cache[k] = img if scale == 1 else img.resize((img.width * scale, img.height * scale), Image.NEAREST)
    return _scaled_cache[k]


def _compose_cell(items, assets, box, scale, bg):
    """items: [(asset, ink, alpha)] en orden de dibujo. bg ya viene a la escala final."""
    x0, y0, W, H = box
    W, H = W * scale, H * scale
    cell = bg.copy()
    for name, ink, alpha in items:
        img, x, y, flip = assets[name]
        im = _scaled(img, scale, name)
        left = ((x - img.width if flip else -x) - x0) * scale
        top = (-y - y0) * scale
        if alpha < 1:
            im = im.copy()
            im.putalpha(im.getchannel("A").point(lambda v, a=alpha: int(v * a)))
        if ink == "ADD":
            layer = Image.new("RGBA", (W, H), (0, 0, 0, 255))
            layer.paste(im, (left, top), im)
            cell = Image.merge("RGBA", (*ImageChops.add(cell.convert("RGB"), layer.convert("RGB")).split(), cell.getchannel("A")))
        else:
            cell.paste(im, (left, top), im) if im.mode == "RGBA" else cell.paste(im, (left, top))
    return cell.convert("RGB")


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


# --------------------------------------------------------------------------- fondos
def _gradient(W, H, top, bottom):
    col = Image.new("RGB", (1, H))
    col.putdata([tuple(int(top[k] + (bottom[k] - top[k]) * y / max(1, H - 1)) for k in range(3)) for y in range(H)])
    return col.resize((W, H)).convert("RGBA")


def _vignette(img, strength=0.55):
    W, H = img.size
    mask = Image.radial_gradient("L").resize((W, H), Image.BILINEAR)           # 0 centro -> 255 borde
    mask = mask.point(lambda v: int(max(0, v - 70) * strength * 1.4))
    return Image.composite(Image.new("RGBA", (W, H), (12, 13, 18, 255)), img, mask)


DEFAULT_BG = Path(__file__).resolve().parent.parent / "assets" / "background.png"


def _image_background(path, W, H):
    """Recorta el centro de la imagen a resolución original (sin ampliarla salvo que sea más pequeña)."""
    im = Image.open(path).convert("RGBA")
    if im.width < W or im.height < H:
        k = max(W / im.width, H / im.height)
        im = im.resize((math.ceil(im.width * k), math.ceil(im.height * k)), Image.NEAREST)
    l, t = (im.width - W) // 2, (im.height - H) // 2
    return im.crop((l, t, l + W, t + H))


def make_background(style, W, H, origin, image_path=None, scale=1):
    """Fondo de una celda. W, H y origin en píxeles base; el resultado sale a W*scale x H*scale."""
    style = (style or "").lower()
    if not style:
        style = "image" if DEFAULT_BG.exists() else "floor"
    if style == "image":
        path = image_path or DEFAULT_BG
        try:
            return _image_background(path, W * scale, H * scale)
        except Exception as e:  # noqa: BLE001
            print(f"  ! no se pudo cargar el fondo {path}: {e}", file=sys.stderr)
            style = "floor"
    img = _procedural_background(style, W, H, origin)
    return img if scale == 1 else img.resize((W * scale, H * scale), Image.NEAREST)


def _procedural_background(style, W, H, origin):
    if style == "solid":
        return Image.new("RGBA", (W, H), BG)
    if style == "gradient":
        return _vignette(_gradient(W, H, (34, 38, 52), (78, 86, 108)))
    if style == "dark":
        return _vignette(_gradient(W, H, (18, 19, 25), (40, 43, 54)), 0.4)
    # "floor": suelo isométrico de baldosas 64x32 alineado con el punto 0,0 del furni
    img = _gradient(W, H, (30, 33, 45), (62, 68, 88))
    d = ImageDraw.Draw(img)
    ox, oy = origin
    c1, c2, edge = (104, 113, 140, 255), (90, 98, 124, 255), (66, 72, 96, 255)
    n = (W + H) // 16 + 4
    for i in range(-n, n):
        for j in range(-n, n):
            cx, cy = ox + (i - j) * 32, oy + (i + j) * 16
            if cx < -40 or cx > W + 40 or cy < -20 or cy > H + 20:
                continue
            d.polygon([(cx, cy - 16), (cx + 32, cy), (cx, cy + 16), (cx - 32, cy)],
                      fill=c1 if (i + j) % 2 == 0 else c2, outline=edge)
    return _vignette(img, 0.5)


def _lcm(a, b):
    return a * b // math.gcd(a, b)


def render_swf(swf_bytes: bytes, classname: str, size: int = 64, direction=None, labels=True,
               background=None, bg_image=None, max_ticks=None, shadow=True):
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
            # La dirección opuesta (d+4) comparte dibujo; solo después se prueban las demás
            cur = int(m.group(2))
            opp = (cur + 4) % 8
            for d in [opp] + [x for x in (0, 2, 4, 6) if x not in (opp, cur)]:
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
    shadow_asset = f"{classname}_{size}_sd_{direction}_0" if shadow else None
    if shadow_asset not in assets:
        shadow_asset = None

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
    cap = max_ticks or MAX_FRAMES
    if total > cap:
        total = min(max(periods.values()), cap)

    def items_at(state, t):
        out = [(shadow_asset, "COPY", SHADOW_ALPHA)] if shadow_asset else []
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
    any_copy = any(ink != "ADD" for s_ in state_ids for items in plan[s_] for _, ink, _ in items)

    def sig_box(name, ink, alpha):
        key = (name, ink, alpha)
        if key not in sig_cache:
            img = assets[name][0]
            a = img.getchannel("A")
            bb = None
            if ink == "ADD" and any_copy:
                # el halo tenue de un brillo no cuenta, salvo que todo el furni sea brillo
                r, g, b, _ = img.split()
                m = ImageChops.multiply(a, ImageChops.lighter(ImageChops.lighter(r, g), b))
                bb = m.point(lambda v: 255 if v * alpha > 90 else 0).getbbox()
            elif ink != "ADD":
                bb = a.point(lambda v: 255 if v * alpha > 16 else 0).getbbox()
            if bb is None:  # respaldo: cualquier píxel visible
                bb = a.point(lambda v: 255 if v > 8 else 0).getbbox()
            sig_cache[key] = bb
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
    if x1 <= x0 or y1 <= y0:  # nada visible: caja por defecto para no fallar
        x0, y0, x1, y1 = -32, -64, 32, 16
    pad = 6
    box = (x0 - pad, y0 - pad, x1 - x0 + pad * 2, y1 - y0 + pad * 2)
    W, H = box[2], box[3]

    n = len(state_ids)
    cols = n if n <= 3 else 2 if n == 4 else 3 if n <= 6 else 4 if n <= 8 else 5
    cols = max(1, cols)
    rows = math.ceil(n / cols)
    min_cell = 140 if labels and n > 1 else 0   # que quepa la etiqueta
    scale = max(1, min(3, 960 // (cols * W)))
    if W * scale < min_cell:
        scale = math.ceil(min_cell / W)
    gap = 4

    background = background or os.environ.get("BG_STYLE") or ""
    bg_image = bg_image or os.environ.get("BG_IMAGE") or None

    def build(scale):
        bg = make_background(background, W, H, (-box[0], -box[1]), bg_image, scale)
        gw, gh = cols * W * scale + (cols + 1) * gap, rows * H * scale + (rows + 1) * gap
        out, cache, sheets = [], {}, {}
        for t in range(total):
            cells = []
            for k, s in enumerate(state_ids):
                key = (s, tuple(plan[s][t]))
                if key not in cache:
                    cell = _compose_cell(plan[s][t], assets, box, scale, bg)
                    if labels and n > 1:
                        _label(cell, f"State {s}")
                    cache[key] = cell
                cells.append(cache[key])
            sk = tuple(id(c) for c in cells)
            if sk not in sheets:
                sheet = Image.new("RGB", (gw, gh), (30, 32, 38))
                for k, cell in enumerate(cells):
                    r, c = divmod(k, cols)
                    sheet.paste(cell, (gap + c * (W * scale + gap), gap + r * (H * scale + gap)))
                sheets[sk] = sheet
            out.append(sheets[sk])   # pasos idénticos comparten el mismo objeto
        return out

    return build(scale)


def _encode(frames, **kw):
    """Codifica a WebP fusionando pasos consecutivos idénticos (suma sus duraciones)."""
    groups = []
    for f in frames:
        if groups and groups[-1][0] is f:
            groups[-1][1] += 1
        else:
            groups.append([f, 1])
    buf = io.BytesIO()
    if len(groups) == 1:
        groups[0][0].save(buf, "WEBP", **kw)
    else:
        imgs = [g[0] for g in groups]
        imgs[0].save(buf, "WEBP", save_all=True, append_images=imgs[1:],
                     duration=[g[1] * TICK_MS for g in groups], loop=0, **kw)
    return buf.getvalue()


def save_webp(frames, target):
    """Guarda como WebP (sin pérdida; si pesa demasiado, con pérdida mínima)."""
    data = _encode(frames, lossless=True, minimize_size=True)
    if len(data) > MAX_BYTES:
        data = _encode(frames, quality=88, method=4)
    if hasattr(target, "write"):
        target.write(data)
    else:
        with open(target, "wb") as f:
            f.write(data)


def render_webp(swf_bytes, classname, direction=None, shadow=True):
    """Renderiza el furni y devuelve el WebP (bytes). Si pesa demasiado, acorta el bucle."""
    frames = None
    for ticks in (MAX_FRAMES, 500, 250, 120):
        frames = render_swf(swf_bytes, classname, direction=direction, max_ticks=ticks, shadow=shadow)
        data = _encode(frames, lossless=True, minimize_size=True)
        if len(data) <= MAX_BYTES:
            return data
    return _encode(frames, quality=88, method=4)


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    wall = "--wall" in sys.argv          # furni de pared: sin sombra
    swf, out = args[0], args[1]
    cls = args[2] if len(args) > 2 and not args[2].isdigit() else re.sub(r"\.swf$", "", swf.split("/")[-1])
    dr = int(args[-1]) if len(args) > 2 and args[-1].isdigit() else None
    data = render_webp(open(swf, "rb").read(), cls, direction=dr, shadow=not wall)
    open(out, "wb").write(data)
    print(f"{len(data)/1e6:.2f} MB -> {out}")
