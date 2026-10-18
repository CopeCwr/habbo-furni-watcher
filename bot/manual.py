"""Envía a Discord un furni concreto buscándolo por ID o classname (para pruebas)."""
import os
import sys

from . import watcher as w


def main():
    query = (os.environ.get("FURNI_QUERY") or (sys.argv[1] if len(sys.argv) > 1 else "")).strip()
    hotel = (os.environ.get("FURNI_HOTEL") or (sys.argv[2] if len(sys.argv) > 2 else "auto")).strip()
    if not query:
        sys.exit("Falta el ID o classname del furni.")
    if not w.WEBHOOK:
        sys.exit("Falta el secret DISCORD_WEBHOOK_URL.")

    hotels = list(w.HOTELS) if hotel == "auto" else [hotel]
    for h in hotels:
        if h not in w.HOTELS:
            sys.exit(f"Hotel desconocido: {h}")
        url, flag = w.HOTELS[h]
        print(f"Buscando '{query}' en {h}...")
        try:
            furnis = w.fetch_furnis(url)
        except Exception as e:  # noqa: BLE001
            print(f"  error leyendo {h}: {e}", file=sys.stderr)
            continue
        for f in furnis.values():
            if f["id"] == query or f["classname"] == query:
                print(f"Encontrado: {f['name']} ({f['classname']}) rev {f['revision']}")
                w.notify(h, flag, f)
                print("Enviado a Discord.")
                return
    sys.exit(f"No se encontró '{query}' en: {', '.join(hotels)}")


if __name__ == "__main__":
    main()
