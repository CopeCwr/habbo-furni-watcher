"""Vigila el furnidata de los hoteles de Habbo y avisa por Discord de furnis nuevos."""
import io
import json
import os
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

from .render import render_webp

HOTELS = {
    "com":     ("https://www.habbo.com/gamedata/furnidata_xml/1",     "🇺🇸"),
    "sandbox": ("https://sandbox.habbo.com/gamedata/furnidata_xml/1", "🧪"),
    "es":      ("https://www.habbo.es/gamedata/furnidata_xml/1",      "🇪🇸"),
    "fi":      ("https://www.habbo.fi/gamedata/furnidata_xml/1",      "🇫🇮"),
    "com.br":  ("https://www.habbo.com.br/gamedata/furnidata_xml/1",  "🇧🇷"),
    "nl":      ("https://www.habbo.nl/gamedata/furnidata_xml/1",      "🇳🇱"),
    "de":      ("https://www.habbo.de/gamedata/furnidata_xml/1",      "🇩🇪"),
    "com.tr":  ("https://www.habbo.com.tr/gamedata/furnidata_xml/1",  "🇹🇷"),
    "it":      ("https://www.habbo.it/gamedata/furnidata_xml/1",      "🇮🇹"),
    "fr":      ("https://www.habbo.fr/gamedata/furnidata_xml/1",      "🇫🇷"),
}
SWF_URLS = ["http://images.habbo.com/dcr/hof_furni/{rev}/{cls}.swf",
            "https://images.habbo.com/dcr/hof_furni/{rev}/{cls}.swf"]
STATE_FILE = Path(os.environ.get("STATE_FILE", "state/known.json"))
WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL", "")
HEADERS = {"User-Agent": "Mozilla/5.0 (FurniWatcher; +github-actions)"}
MAX_NOTIFY = int(os.environ.get("MAX_NOTIFY", "25"))  # tope de avisos por ejecución


def _txt(el, tag):
    """Lee <tag>valor</tag> o tag="valor"; devuelve None si no existe."""
    child = el.find(tag)
    if child is not None:
        return (child.text or "").strip()
    return el.get(tag)


def _flag(v):
    if v is None:
        return "N/D"
    return "Sí" if v.strip().lower() in ("1", "true", "yes") else "No"


def _partcolors(el):
    """<partcolors><color>#RRGGBB</color>...</partcolors> -> ['#RRGGBB', ...]"""
    pc = el.find("partcolors")
    if pc is None:
        return []
    return [(c.text or "").strip() for c in pc.findall("color") if (c.text or "").strip()]


def _color_id(classname):
    """rare_dragonlamp*12 -> 12 (None si no es una variante de color)."""
    tail = classname.split("*", 1)[1] if "*" in classname else ""
    return int(tail) if tail.isdigit() else None


def fetch_furnis(url):
    r = requests.get(url, headers=HEADERS, timeout=60)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    out = {}
    pairs = []
    for tag, kind in (("roomitemtypes", "floor"), ("wallitemtypes", "wall")):
        for sec in root.iter(tag):
            pairs += [(el, kind) for el in sec.iter("furnitype")]
    if not pairs:  # estructura inesperada: leer todo como furni de suelo
        pairs = [(el, "floor") for el in root.iter("furnitype")]
    for el, kind in pairs:
        cls = el.get("classname")
        if not cls:
            continue
        out[cls] = {
            "id": el.get("id"),
            "kind": kind,
            "classname": cls,
            "revision": _txt(el, "revision"),
            "defaultdir": _txt(el, "defaultdir"),
            "partcolors": _partcolors(el),
            "name": _txt(el, "name") or cls,
            "description": _txt(el, "description") or "—",
            "canstandon": _flag(_txt(el, "canstandon")),
            "cansiton": _flag(_txt(el, "cansiton")),
            "canlayon": _flag(_txt(el, "canlayon")),
            "canputstuffon": _flag(_txt(el, "canputstuffon")),
            "furniline": _txt(el, "furniline") or "—",
            "tradeable": _flag(_txt(el, "tradeable")),
            "recyclable": _flag(_txt(el, "recyclable")),
        }
    return out


_render_cache = {}


def get_render(furni):
    """Descarga el .swf y lo convierte a WebP. Cacheado por (clase, revisión)."""
    base = furni["classname"].split("*")[0]  # variantes de color comparten swf
    key = (furni["classname"], furni["revision"])  # cada color es una variante distinta
    if key in _render_cache:
        return _render_cache[key]
    result = None
    if furni["revision"]:
        for tpl in SWF_URLS:
            url = tpl.format(rev=furni["revision"], cls=base)
            try:
                r = requests.get(url, headers=HEADERS, timeout=60)
                if r.status_code == 200 and r.content[:3] in (b"FWS", b"CWS", b"ZWS"):
                    dd = furni.get("defaultdir")
                    data = render_webp(r.content, base, direction=int(dd) if dd and dd.isdigit() else None,
                                       shadow=furni.get("kind") != "wall",
                                       color_id=_color_id(furni["classname"]),
                                       partcolors=furni.get("partcolors"))
                    result = (data, url)
                    break
            except Exception as e:  # noqa: BLE001
                print(f"  ! error con {url}: {e}", file=sys.stderr)
    _render_cache[key] = result
    return result


def notify(hotel, flag, furni):
    render = get_render(furni)
    fields = [
        ("ID", furni["id"]), ("Classname", f"`{furni['classname']}`"),
        ("Revisión", furni["revision"] or "N/D"), ("Furniline", furni["furniline"]),
        *([("Colores", " ".join(f"`{c}`" for c in furni["partcolors"]))] if furni.get("partcolors") else []),
        ("Se puede pisar", furni["canstandon"]), ("Se puede sentar", furni["cansiton"]),
        ("Se puede tumbar", furni["canlayon"]), ("Admite objetos encima", furni["canputstuffon"]),
        ("Intercambiable", furni["tradeable"]), ("Reciclable", furni["recyclable"]),
    ]
    embed = {
        "title": f"{flag} Nuevo furni: {furni['name']}",
        "description": (f"Se ha encontrado el nuevo furni **\"{furni['name']}\"** "
                        f"en el hotel **\"{hotel.upper()}\"**.\n\n> {furni['description']}"),
        "color": 0x2ECC71,
        "fields": [{"name": n, "value": str(v), "inline": True} for n, v in fields],
    }
    files = None
    if render:
        data, url = render
        embed["image"] = {"url": "attachment://furni.webp"}
        embed["footer"] = {"text": url}
        files = {"file": ("furni.webp", data, "image/webp")}
    payload = {"embeds": [embed]}
    for attempt in range(5):
        if files:
            r = requests.post(WEBHOOK, data={"payload_json": json.dumps(payload)}, files=files, timeout=60)
        else:
            r = requests.post(WEBHOOK, json=payload, timeout=60)
        if r.status_code == 429:
            time.sleep(float(r.json().get("retry_after", 2)) + 0.5)
            continue
        r.raise_for_status()
        break
    time.sleep(1)


def main():
    first_run = not STATE_FILE.exists()
    state = {} if first_run else json.loads(STATE_FILE.read_text())
    sent = 0
    for hotel, (url, flag) in HOTELS.items():
        try:
            current = fetch_furnis(url)
        except Exception as e:  # noqa: BLE001
            print(f"[{hotel}] ERROR al leer furnidata: {e}", file=sys.stderr)
            continue
        if hotel not in state:  # línea base: se guarda todo sin avisar
            state[hotel] = sorted(current)
            print(f"[{hotel}] línea base guardada ({len(current)} furnis)")
            continue
        known = set(state[hotel])
        new = [c for c in current if c not in known]
        print(f"[{hotel}] {len(current)} furnis, {len(new)} nuevos")
        for cls in new:
            if sent >= MAX_NOTIFY or not WEBHOOK:
                break  # quedará pendiente para la siguiente ejecución
            try:
                notify(hotel, flag, current[cls])
                known.add(cls)
                sent += 1
            except Exception as e:  # noqa: BLE001
                print(f"[{hotel}] fallo al avisar de {cls}: {e}", file=sys.stderr)
        state[hotel] = sorted(known)
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=0, sort_keys=True))
    if first_run:
        print("Primera ejecución: estado inicial guardado sin enviar avisos.")


if __name__ == "__main__":
    main()
