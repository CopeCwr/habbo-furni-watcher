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
MAX_NOTIFY = int(os.environ.get("MAX_NOTIFY") or 200)                 # tope de avisos por ejecución
MAX_RUNTIME = float(os.environ.get("MAX_RUNTIME_MIN") or 80) * 60     # segundos antes de parar con calma
SAVE_EVERY = 10                                                        # guardar el estado cada N avisos


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


_render_cache = {}  # (sin uso: no se guardan renders para no gastar memoria)
FOLLOWUPS = os.environ.get("FOLLOWUPS", "1").strip().lower() not in ("0", "false", "no", "")


def get_render(furnis):
    """Descarga el .swf y lo convierte a WebP. `furnis` = el mismo furni visto en distintos hoteles.
    Prueba la revisión de cada hotel hasta que una funcione. Cacheado por (clase, revisiones)."""
    first = furnis[0]
    cls = first["classname"]
    base = cls.split("*")[0]  # variantes de color comparten swf
    revs = []
    for f in furnis:
        if f["revision"] and f["revision"] not in revs:
            revs.append(f["revision"])
    result = None
    for rev in revs:
        for tpl in SWF_URLS:
            url = tpl.format(rev=rev, cls=base)
            try:
                r = requests.get(url, headers=HEADERS, timeout=60)
                if r.status_code == 200 and r.content[:3] in (b"FWS", b"CWS", b"ZWS"):
                    dd = first.get("defaultdir")
                    data = render_webp(r.content, base, direction=int(dd) if dd and dd.isdigit() else None,
                                       shadow=first.get("kind") != "wall",
                                       color_id=_color_id(cls),
                                       partcolors=first.get("partcolors"))
                    result = (data, url)
                    break
            except Exception as e:  # noqa: BLE001
                print(f"  ! error con {url}: {e}", file=sys.stderr)
        if result:
            break
    return result


def _post(payload, files=None):
    for _ in range(5):
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


def _hotel_tags(entries):
    return ", ".join(f"{flag} {hotel.upper()}" for hotel, flag, _ in entries)


def notify_new(entries):
    """Un único aviso (con imagen) para un furni nuevo. entries = [(hotel, bandera, datos_furni), ...]."""
    furnis = [f for _, _, f in entries]
    furni = furnis[0]
    quoted = ", ".join(f'**"{h.upper()}"**' for h, _, _ in entries)
    where = f"en el hotel {quoted}" if len(entries) == 1 else f"en los hoteles {quoted}"
    render = get_render(furnis)
    fields = [
        ("Hoteles", _hotel_tags(entries), False),
        ("ID", furni["id"], True), ("Classname", f"`{furni['classname']}`", True),
        ("Revisión", furni["revision"] or "N/D", True), ("Furniline", furni["furniline"], True),
        *([("Colores", " ".join(f"`{c}`" for c in furni["partcolors"]), True)] if furni.get("partcolors") else []),
        ("Se puede pisar", furni["canstandon"], True), ("Se puede sentar", furni["cansiton"], True),
        ("Se puede tumbar", furni["canlayon"], True), ("Admite objetos encima", furni["canputstuffon"], True),
        ("Intercambiable", furni["tradeable"], True), ("Reciclable", furni["recyclable"], True),
    ]
    names = {f["name"] for f in furnis}
    if len(names) > 1:  # el nombre cambia según el idioma del hotel
        fields.insert(1, ("Nombre por hotel", "\n".join(f"{fl} {h.upper()}: {f['name']}" for h, fl, f in entries), False))
    embed = {
        "title": f"🆕 Nuevo furni: {furni['name']}",
        "description": f"Se ha encontrado el nuevo furni **\"{furni['name']}\"** {where}.\n\n> {furni['description']}",
        "color": 0x2ECC71,
        "fields": [{"name": n, "value": str(v), "inline": inl} for n, v, inl in fields],
    }
    files = None
    if render:
        data, url = render
        embed["image"] = {"url": "attachment://furni.webp"}
        embed["footer"] = {"text": url}
        files = {"file": ("furni.webp", data, "image/webp")}
    _post({"embeds": [embed]}, files)


def notify_followup(entries, seen_in):
    """Aviso corto (sin imagen): un furni ya anunciado ha llegado a otro hotel."""
    furni = entries[0][2]
    before = ", ".join(f"{HOTELS[h][1]} {h.upper()}" for h in seen_in) or "—"
    embed = {
        "title": f"🔔 {furni['name']}: ya disponible en otro hotel",
        "description": (f"El furni **\"{furni['name']}\"** (`{furni['classname']}`) ya se había anunciado "
                        f"(visto antes en: {before}).\nAhora también aparece en: **{_hotel_tags(entries)}**."),
        "color": 0x3498DB,
    }
    _post({"embeds": [embed]})


def notify(hotel, flag, furni):
    """Compatibilidad (prueba manual): aviso completo de un furni en un solo hotel."""
    notify_new([(hotel, flag, furni)])


def main():
    first_run = not STATE_FILE.exists()
    state = {} if first_run else json.loads(STATE_FILE.read_text())
    announced = set(state.pop("_announced", []))
    if not announced:  # estado antiguo (o primera vez): todo lo ya conocido cuenta como anunciado
        for lst in state.values():
            announced |= set(lst)

    # 1) leer los hoteles
    data = {}
    for hotel, (url, _) in HOTELS.items():
        try:
            data[hotel] = fetch_furnis(url)
        except Exception as e:  # noqa: BLE001
            print(f"[{hotel}] ERROR al leer furnidata: {e}", file=sys.stderr)

    # 2) hoteles nuevos: línea base sin avisar
    baselined = set()
    for hotel, cur in data.items():
        if hotel not in state:
            state[hotel] = sorted(cur)
            announced |= set(cur)
            baselined.add(hotel)
            print(f"[{hotel}] línea base guardada ({len(cur)} furnis)")

    # 3) novedades agrupadas por furni (en el orden en que aparecen)
    known = {h: set(v) for h, v in state.items()}
    pending = {}
    for hotel, cur in data.items():
        if hotel in baselined:
            continue
        new = [c for c in cur if c not in known[hotel]]
        print(f"[{hotel}] {len(cur)} furnis, {len(new)} nuevos")
        for c in new:
            pending.setdefault(c, []).append(hotel)
    print(f"{len(pending)} furnis con novedades")

    # 4) avisar (un mensaje por furni, no uno por hotel)
    def save_state():
        out = {h: sorted(v) for h, v in known.items()}
        out["_announced"] = sorted(announced)
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(out, indent=0, sort_keys=True))
        tmp.replace(STATE_FILE)  # escritura atómica: nunca queda un archivo a medias

    sent, since_save, started = 0, 0, time.time()
    try:
        for cls, hotels in pending.items():
            if sent >= MAX_NOTIFY or not WEBHOOK:
                break  # quedará pendiente para la siguiente ejecución
            if time.time() - started > MAX_RUNTIME:
                print(f"Tiempo máximo ({MAX_RUNTIME/60:.0f} min) alcanzado: el resto queda para la siguiente ejecución.")
                break
            entries = [(h, HOTELS[h][1], data[h][cls]) for h in hotels]
            try:
                if cls not in announced:
                    notify_new(entries)
                    announced.add(cls)
                    sent += 1
                elif FOLLOWUPS:
                    notify_followup(entries, [h for h in known if cls in known[h]])
                    sent += 1
                for h in hotels:
                    known[h].add(cls)
                since_save += 1
                if since_save >= SAVE_EVERY:
                    save_state()  # si la ejecución se corta, no se repiten los avisos ya enviados
                    since_save = 0
            except Exception as e:  # noqa: BLE001
                print(f"fallo al avisar de {cls}: {e}", file=sys.stderr)
    finally:
        save_state()
    left = sum(1 for c, hs in pending.items() if any(c not in known[h] for h in hs))
    print(f"{sent} avisos enviados, {left} pendientes para la siguiente ejecución.")
    if first_run:
        print("Primera ejecución: estado inicial guardado sin enviar avisos.")


if __name__ == "__main__":
    main()
