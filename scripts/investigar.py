#!/usr/bin/env python3
"""
investigar.py — arranca una investigación completa de googleReviews con un solo comando.

Resuelve la geometría (localidad+radio, coordenada+radio o polígono de coordenadas),
crea o reutiliza la investigación en `negocios_investigacion` y encadena el pipeline:

  F1 rasterize.py      -> discovery.json (+ upsert negocios en Mongo)
  F2 build_config.py   -> config.yaml
  F3 start.py scrape   -> reseñas (SQLite + Mongo)
  F4 enrich_mongo.py   -> location/website/email
  F5 sync_negocio_reviews.py -> negocio_reviews (negocio_id)
  F6 investigaciones.py --update -> cierre (duración + conteos)

El proceso termina al finalizar la investigación (y con ello la instancia de
googleReviews lanzada por service.sh). Se invoca SIEMPRE vía service.sh:

  service.sh investigacion --nombre "Inmobiliarias SCZ" \
      --keywords "inmobiliaria,agencia inmobiliaria" \
      --localidad "Santa Cruz de la Sierra, Bolivia" --radio 15
"""

import argparse
import json
import math
import subprocess
import sys
import time
from pathlib import Path

import requests
from pymongo import MongoClient

HERE = Path(__file__).resolve().parent          # scripts/
ROOT = HERE.parent                               # raíz del repo googleReviews
APP = ROOT                                       # app + venv + start.py

DEFAULT_URI = "mongodb://admin:admin@127.0.0.1:27017/?authSource=admin"
DB = "google_reviews"
INV_COL = "negocios_investigacion"


# ----------------------------- geometría ------------------------------------

def geocode(area: str):
    """bbox (s, w, n, e) + centro desde Nominatim/OSM."""
    r = requests.get(
        "https://nominatim.openstreetmap.org/search",
        params={"q": area, "format": "json", "limit": 1},
        headers={"User-Agent": "statetty-investigar/1.0"},
        timeout=20,
    )
    r.raise_for_status()
    js = r.json()
    if not js:
        raise SystemExit(f"Sin resultados de geocodificación para: {area}")
    bb = js[0]["boundingbox"]  # [south, north, west, east]
    return (float(bb[0]), float(bb[2]), float(bb[1]), float(bb[3]))


def circle_poly(lat: float, lng: float, r_km: float, n: int = 64):
    """Polígono (lat, lng) aproximando una circunferencia de radio r_km."""
    dlat = r_km / 111.32
    dlng = r_km / (111.32 * math.cos(math.radians(lat)))
    return [(lat + dlat * math.sin(2 * math.pi * i / n),
             lng + dlng * math.cos(2 * math.pi * i / n)) for i in range(n)]


def poly_bbox(poly):
    lats = [p[0] for p in poly]
    lngs = [p[1] for p in poly]
    return (min(lats), min(lngs), max(lats), max(lngs))


def parse_poligono(s: str):
    """'lat,lng;lat,lng;...' -> [(lat, lng), ...] (≥3 puntos)."""
    poly = []
    for seg in s.split(";"):
        seg = seg.strip()
        if not seg:
            continue
        a, b = seg.split(",")
        poly.append((float(a), float(b)))
    if len(poly) < 3:
        raise SystemExit("--poligono requiere al menos 3 puntos 'lat,lng' separados por ';'")
    return poly


def write_geojson(path: Path, poly):
    ring = [[p[1], p[0]] for p in poly]  # GeoJSON: [lng, lat]
    ring.append(ring[0])
    path.write_text(json.dumps({"type": "Polygon", "coordinates": [ring]}),
                    encoding="utf-8")


# ------------------------------ util ----------------------------------------

def slugify(s: str) -> str:
    out = "".join(c.lower() if c.isalnum() else "_" for c in s.strip())
    return "_".join(x for x in out.split("_") if x)[:60] or "investigacion"


def run(cmd, cwd):
    print("+", " ".join(str(c) for c in cmd), flush=True)
    p = subprocess.run([str(c) for c in cmd], cwd=str(cwd))
    if p.returncode != 0:
        raise SystemExit(f"Fallo (exit {p.returncode}): {' '.join(str(c) for c in cmd)}")


def resolver_investigacion(args, bbox, keywords):
    """Devuelve el ObjectId (str) de la investigación: reutiliza por nombre/id o crea."""
    col = MongoClient(args.mongo_uri)[args.db][INV_COL]
    if args.id:
        from bson import ObjectId
        oid = ObjectId(args.id)
        if col.find_one({"_id": oid}) is None:
            raise SystemExit(f"No existe investigación {args.id}")
        return str(oid), False

    nombre = args.nombre.strip()
    doc = col.find_one({"nombre": nombre})
    if doc:
        print(f"Reutiliza investigación existente: {doc['_id']} ({nombre})")
        return str(doc["_id"]), False

    cmd = [sys.executable, str(HERE / "investigaciones.py"), "--new",
           "--nombre", nombre, "--keywords", ",".join(keywords),
           "--bbox", ",".join(f"{x:.6f}" for x in bbox), "--tag", "none"]
    if args.descripcion:
        cmd += ["--descripcion", args.descripcion]
    run(cmd, ROOT)
    doc = col.find_one({"nombre": nombre})
    if doc is None:
        raise SystemExit("No se pudo crear/recuperar la investigación")
    print(f"Investigación creada: {doc['_id']}")
    return str(doc["_id"]), True


# ------------------------------ main ----------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Arranca una investigación completa de googleReviews")
    ap.add_argument("--nombre", help="nombre de la investigación (clave para reusar/crear)")
    ap.add_argument("--id", help="ObjectId de una investigación existente (evita --nombre)")
    ap.add_argument("--keywords", required=True, help="keywords separadas por coma")
    ap.add_argument("--descripcion", default=None)

    g = ap.add_argument_group("área geográfica (una sola modalidad)")
    g.add_argument("--localidad", help="'ciudad, país' (geocodifica el centro)")
    g.add_argument("--lat", type=float)
    g.add_argument("--lng", type=float)
    g.add_argument("--radio", type=float, help="radio en km (círculo)")
    g.add_argument("--poligono", help="'lat,lng;lat,lng;lat,lng;...' (≥3 puntos)")

    ap.add_argument("--fases", default="123456", help="fases a ejecutar, p.ej. '1' (default: todas)")
    ap.add_argument("--zoom", type=int, default=16, help="F1: zoom base (default 16)")
    ap.add_argument("--zoom-max", type=int, default=17,
                    help="F1: zoom máximo de subdivisión (default 17; z18/z19 explotan)")
    ap.add_argument("--limit-searches", type=int, default=0,
                    help="F1: tope de búsquedas en rasterize (0 = sin tope; útil para smoke)")
    ap.add_argument("--max-reviews", type=int, default=None,
                    help="F3: tope de reseñas por negocio en start.py (útil para smoke)")
    ap.add_argument("--mongo-collection", default=None, help="default: slug del nombre")
    ap.add_argument("--negocios-collection", default="negocios",
                    help="colección compartida de negocios (default: negocios)")
    ap.add_argument("--mongo-uri", default=DEFAULT_URI)
    ap.add_argument("--db", default=DB)
    ap.add_argument("--dry-run", action="store_true",
                    help="solo F1 con rasterize --dry-run (imprime la rejilla, no toca Google/Mongo)")
    args = ap.parse_args()

    keywords = [k.strip() for k in args.keywords.split(",") if k.strip()]
    if not keywords:
        raise SystemExit("--keywords vacío")

    if not args.dry_run and not (args.nombre or args.id):
        raise SystemExit("Se requiere --nombre (o --id para una investigación existente)")

    # --- geometría ---
    poly = None
    if args.poligono:
        poly = parse_poligono(args.poligono)
        bbox = poly_bbox(poly)
        modo = "polígono"
    elif args.lat is not None and args.lng is not None and args.radio:
        poly = circle_poly(args.lat, args.lng, args.radio)
        bbox = poly_bbox(poly)
        modo = f"círculo (centro {args.lat},{args.lng} r={args.radio}km)"
    elif args.localidad:
        gs, gw, gn, ge = geocode(args.localidad)
        if args.radio:
            poly = circle_poly((gs + gn) / 2, (gw + ge) / 2, args.radio)
            bbox = poly_bbox(poly)
            modo = f"círculo sobre '{args.localidad}' r={args.radio}km"
        else:
            bbox = (gs, gw, gn, ge)
            modo = f"bbox de '{args.localidad}'"
    else:
        raise SystemExit("Indica --localidad [--radio], --lat/--lng --radio, o --poligono")

    s, w, n, e = bbox
    print(f"Geometría: {modo}")
    print(f"bbox S={s:.6f} W={w:.6f} N={n:.6f} E={e:.6f}")

    slug = args.mongo_collection or slugify(args.nombre or args.id)
    out_dir = HERE / "raster" / slug
    out_dir.mkdir(parents=True, exist_ok=True)

    poly_file = out_dir / "poligono.geojson"
    if poly is not None and not args.dry_run:
        write_geojson(poly_file, poly)
        print(f"polígono -> {poly_file}")

    fases = set(args.fases)
    t0 = time.time()
    inv_id = None
    collection = slug
    discovery = out_dir / "discovery.json"
    config = out_dir / "config.yaml"

    # --- F0: investigación (no en dry-run) ---
    if not args.dry_run:
        inv_id, _ = resolver_investigacion(args, bbox, keywords)

    # --- F1: descubrimiento ---
    if "1" in fases:
        cmd = [sys.executable, str(HERE / "rasterize.py"),
               "--keywords", ",".join(keywords),
               "--bbox", f"{s},{w},{n},{e}",
               "--zoom", str(args.zoom), "--zoom-max", str(args.zoom_max),
               "--out-dir", str(out_dir),
               "--mongo-collection", args.negocios_collection]
        if poly is not None and not args.dry_run:
            cmd += ["--polygon", str(poly_file)]
        if inv_id:
            cmd += ["--investigacion", inv_id]
        if args.limit_searches:
            cmd += ["--limit-searches", str(args.limit_searches)]
        if args.dry_run:
            cmd += ["--dry-run"]
        run(cmd, ROOT)

    if args.dry_run:
        print("dry-run: fin.")
        return 0

    # ¿hay negocios descubiertos?
    n_places = 0
    if discovery.exists():
        try:
            n_places = len(json.loads(discovery.read_text(encoding="utf-8")))
        except Exception:
            n_places = 0

    if not discovery.exists() or n_places == 0:
        print("Sin negocios en discovery.json: se omiten F2-F6.")
        return 0

    # --- F2: config ---
    if "2" in fases:
        run([sys.executable, str(HERE / "build_config.py"),
             "--discovery", str(discovery), "--out", str(config),
             "--keyword", keywords[0], "--mongo-collection", collection], ROOT)

    # --- F3: scrape de reseñas ---
    if "3" in fases and config.exists():
        cmd = ["xvfb-run", "-a", "-s", "-screen 0 1600x1200x24",
               sys.executable, "start.py", "--config", str(config)]
        if args.max_reviews is not None:
            cmd += ["--max-reviews", str(args.max_reviews)]
        run(cmd, APP)

    # --- F4: enriquecimiento ---
    if "4" in fases:
        run([sys.executable, str(HERE / "enrich_mongo.py"),
             "--discovery", str(discovery), "--collection", collection,
             "--crawl-emails"], ROOT)

    # --- F5: sincronizar reseñas ---
    if "5" in fases:
        run([sys.executable, str(HERE / "sync_negocio_reviews.py"),
             "--collections", collection,
             "--negocios-collection", args.negocios_collection], ROOT)

    # --- F6: cierre de la investigación ---
    if "6" in fases and inv_id:
        dur = int(time.time() - t0)
        run([sys.executable, str(HERE / "investigaciones.py"),
             "--update", inv_id, "--duracion-seg", str(dur)], ROOT)
        print(f"Investigación {inv_id} cerrada (duración {dur}s).")

    print("Investigación terminada.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
