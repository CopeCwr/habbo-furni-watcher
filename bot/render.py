"""Compone las imágenes de un furni .swf de Habbo en un WebP (animado si procede)."""
import re
import sys
import xml.etree.ElementTree as ET

from PIL import Image, ImageOps

from .swf import parse_swf

TICK_MS = 125          # duración de cada paso de animación
SCALE = 2              # escala entera para que se vea bien en Discord
MAX_FRAMES = 120


def _xml(blob: bytes) -> ET.Element:
    text = blob.decode("latin-1").replace("\r", "")
    text = re.sub(r"^<\?xml[^>]*\?>", "", text.strip())
    return ET.fromstring(text)


def _find_blob(binaries, symbols, suffix):
    for cid, name in symbols.items():
        if name.endswith("_" + suffix) and cid in binaries:
            return binaries[cid]
    return None


def _layer_sequences(vis, size, direction):
    """Devuelve {letra_capa: [frames...]} para la animación más larga y alphas por capa."""
    node = None
    for v in vis.iter("visualization"):
        if v.get("size") == str(size):
            node = v
    if node is None:
        return {}, {}, {}
    alphas, inks = {}, {}
    for l in node.iter("layer"):
        i = int(l.get("id"))
        if l.get("alpha"):
            alphas[chr(97 + i)] = int(l.get("alpha")) / 255 if int(l.get("alpha")) > 100 else int(l.get("alpha")) / 100
        if l.get("ink"):
            inks[chr(97 + i)] = l.get("ink")
    best = {}
    for anim in node.iter("animation"):
        seqs = {}
        for al in anim.iter("animationLayer"):
            fs = al.find("frameSequence")
            if fs is None:
                continue
            rep = int(al.get("frameRepeat", "1"))
            frames = []
            for f in fs.iter("frame"):
                frames += [int(f.get("id"))] * (int(f.get("repeats", "1")) * rep)
            if frames:
                seqs[chr(97 + int(al.get("id")))] = frames
        if sum(len(s) for s in seqs.values()) > sum(len(s) for s in best.values()):
            best = seqs
    return best, alphas, inks


def render_swf(swf_bytes: bytes, classname: str, size: int = 64):
    images, binaries, symbols = parse_swf(swf_bytes)

    # nombre de asset -> imagen (los símbolos vienen duplicados: "<clase>_<asset>")
    by_name = {}
    for cid, sym in symbols.items():
        if cid in images:
            by_name[sym] = images[cid]

    def get_img(asset):
        for key in (f"{classname}_{asset}", asset):
            if key in by_name:
                return by_name[key]
        return None

    assets_blob = _find_blob(binaries, symbols, "assets")
    vis_blob = _find_blob(binaries, symbols, "visualization")
    if assets_blob is None:
        raise RuntimeError("El SWF no contiene el XML de assets")

    # asset -> (imagen, x, y, flip)
    assets = {}
    for a in _xml(assets_blob).iter("asset"):
        name = a.get("name")
        src = a.get("source") or name
        img = get_img(src)
        if img is None:
            continue
        flip = a.get("flipH") == "1"
        if flip:
            img = ImageOps.mirror(img)
        assets[name] = (img, int(a.get("x", 0)), int(a.get("y", 0)), flip)

    pat = re.compile(rf"^{re.escape(classname)}_{size}_([a-z]+)_(\d+)_(\d+)$")
    layers = {}  # (dir) -> {letra: {frame: asset}}
    for name in assets:
        m = pat.match(name)
        if m:
            layers.setdefault(int(m.group(2)), {}).setdefault(m.group(1), {})[int(m.group(3))] = name
    if not layers:
        raise RuntimeError("No hay assets de tamaño %d" % size)
    direction = 0 if 0 in layers else sorted(layers)[0]
    layer_frames = layers[direction]

    seqs, alphas, inks = ({}, {}, {})
    if vis_blob is not None:
        try:
            seqs, alphas, inks = _layer_sequences(_xml(vis_blob), size, direction)
        except Exception:
            pass

    n = min(max([len(s) for s in seqs.values()] or [1]), MAX_FRAMES)
    # Habbo apila las capas por orden alfabético (a = sombra, por debajo)
    order = sorted(layer_frames, key=lambda l: (len(l), l))

    plan = []  # por frame: lista de (asset, letra)
    for t in range(n):
        items = []
        for l in order:
            frames = layer_frames[l]
            if l in seqs:
                fid = seqs[l][t % len(seqs[l])]
            else:
                fid = 0 if 0 in frames else min(frames)
            if fid in frames:
                items.append((frames[fid], l))
        plan.append(items)

    # límites comunes a todos los frames
    x0 = y0 = 10 ** 9
    x1 = y1 = -10 ** 9
    for items in plan:
        for name, _ in items:
            img, x, y, flip = assets[name]
            left = x - img.width if flip else -x
            x0, y0 = min(x0, left), min(y0, -y)
            x1, y1 = max(x1, left + img.width), max(y1, -y + img.height)
    pad = 4
    W, H = (x1 - x0 + pad * 2), (y1 - y0 + pad * 2)

    frames_out = []
    for items in plan:
        canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        for name, l in items:
            img, x, y, flip = assets[name]
            left = x - img.width if flip else -x
            im = img
            a = alphas.get(l)
            if a is not None:
                im = img.copy()
                im.putalpha(im.getchannel("A").point(lambda v, a=a: int(v * a)))
            canvas.alpha_composite(im, (left - x0 + pad, -y - y0 + pad))
        if SCALE > 1:
            canvas = canvas.resize((W * SCALE, H * SCALE), Image.NEAREST)
        frames_out.append(canvas)
    return frames_out


def save_webp(frames, path):
    if len(frames) == 1:
        frames[0].save(path, "WEBP", lossless=True)
    else:
        frames[0].save(path, "WEBP", save_all=True, append_images=frames[1:],
                       duration=TICK_MS, loop=0, lossless=True, minimize_size=True)


if __name__ == "__main__":
    swf, out = sys.argv[1], sys.argv[2]
    cls = sys.argv[3] if len(sys.argv) > 3 else re.sub(r"\.swf$", "", swf.split("/")[-1])
    fr = render_swf(open(swf, "rb").read(), cls)
    save_webp(fr, out)
    print(f"{len(fr)} frames -> {out}")
