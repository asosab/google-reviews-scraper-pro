#!/usr/bin/env python3
"""equipamientos_sync.py — vuelca negocios de una investigación a la colección
`equipamientos` (Contabo) para servirlos en el mapa de statetty.com.

Por cada negocio de la investigación guarda:
  nombre, categoria (slug del equipamiento), categoriaGoogle (la que da Maps),
  score (media ponderada bayesiana de estrellas: WR=(v·R+m·C)/(v+m), m=25,
  C=media del lote), rating/reviews crudos, location GeoJSON, h3 res 8,
  direccion, telefono, website, urlGoogle, ciudad, investigacion_ID.

Dedup por (placeId, categoria): un mismo local puede estar en varias
categorías sin pisarse.

Uso:
  .venv/bin/python scripts/equipamientos_sync.py \
      --investigacion "Aparcamientos SCZ" --categoria aparcamiento \
      --ciudad "Santa Cruz de la Sierra"

El URI de destino por defecto se lee de CONTABO_URI en ../../nodeLab/.env
(se puede sobreescribir con --dst-mongo-uri).
"""
import argparse
import os
import sys
from datetime import datetime, timezone

import h3
from bson import ObjectId
from pymongo import MongoClient, UpdateOne, GEOSPHERE

DEFAULT_SRC_URI = "mongodb://admin:admin@127.0.0.1:27017/?authSource=admin"
M_PRIOR = 25          # peso del prior en la media ponderada
C_FALLBACK = 3.8      # media por defecto si el lote no tiene ratings
H3_RES = 8            # misma resolución que gridCell (permite cruzar ruido/clima)


def _contabo_uri_desde_env():
    """Lee CONTABO_URI de nodeLab/.env (última aparición no comentada)."""
    env_path = os.path.join(os.path.dirname(__file__), "..", "..", "nodeLab", ".env")
    env_path = os.path.abspath(env_path)
    uri = None
    try:
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("CONTABO_URI="):
                    uri = line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return uri


def _resolver_investigacion(col, ref):
    """Acepta ObjectId o nombre exacto."""
    try:
        return col.find_one({"_id": ObjectId(ref)})
    except Exception:
        pass
    return col.find_one({"nombre": ref}) or col.find_one({"nombre": {"$regex": f"^{ref}$", "$options": "i"}})


def main():
    ap = argparse.ArgumentParser(description="Sync negocios → equipamientos (Contabo)")
    ap.add_argument("--investigacion", required=True, help="ObjectId o nombre de la investigación")
    ap.add_argument("--categoria", required=True, help="slug del equipamiento: aparcamiento, escuela, farmacia…")
    ap.add_argument("--ciudad", default="Santa Cruz de la Sierra")
    ap.add_argument("--src-mongo-uri", default=DEFAULT_SRC_URI)
    ap.add_argument("--src-db", default="google_reviews")
    ap.add_argument("--src-collection", default="negocios")
    ap.add_argument("--dst-mongo-uri", default=None, help="default: CONTABO_URI de nodeLab/.env")
    ap.add_argument("--dst-db", default="test", help="BD destino (producción Contabo = 'test')")
    ap.add_argument("--dst-collection", default="equipamientos")
    ap.add_argument("--dry-run", action="store_true", help="solo muestra qué escribiría")
    args = ap.parse_args()

    src = MongoClient(args.src_mongo_uri)[args.src_db]
    inv = _resolver_investigacion(src["negocios_investigacion"], args.investigacion)
    if not inv:
        print(f"ERROR: no se encontró la investigación '{args.investigacion}'", file=sys.stderr)
        sys.exit(1)
    print(f"investigación: {inv.get('nombre')} ({inv['_id']})")

    cursor = src[args.src_collection].find(
        {"investigacion_ID": inv["_id"], "lat": {"$ne": None}, "lng": {"$ne": None}}
    )
    negocios = list(cursor)
    if not negocios:
        print("ERROR: la investigación no tiene negocios con coordenadas", file=sys.stderr)
        sys.exit(1)

    # prior C = media de ratings del lote (solo los que tienen rating)
    ratings = [float(n["rating"]) for n in negocios
               if n.get("rating") is not None and float(n.get("rating") or 0) > 0]
    C = sum(ratings) / len(ratings) if ratings else C_FALLBACK

    dst_uri = args.dst_mongo_uri or _contabo_uri_desde_env()
    if not dst_uri:
        print("ERROR: sin --dst-mongo-uri ni CONTABO_URI en nodeLab/.env", file=sys.stderr)
        sys.exit(1)

    cat = args.categoria.strip().lower()
    now = datetime.now(timezone.utc)
    ops, sin_pid, previews = [], 0, []
    for n in negocios:
        pid = n.get("place_id")
        if not pid:
            sin_pid += 1
            continue
        try:
            lat, lng = float(n["lat"]), float(n["lng"])
        except (TypeError, ValueError):
            continue
        rating = n.get("rating")
        rating = float(rating) if rating is not None else None
        v = int(n.get("review_count") or 0)
        score = round((v * rating + M_PRIOR * C) / (v + M_PRIOR), 3) if rating else None
        doc = {
            "placeId": pid,
            "nombre": n.get("name"),
            "categoria": cat,
            "categoriaGoogle": n.get("category"),
            "score": score,
            "rating": rating,
            "reviews": v,
            "location": {"type": "Point", "coordinates": [lng, lat]},
            "h3": h3.latlng_to_cell(lat, lng, H3_RES),
            "direccion": n.get("address"),
            "telefono": n.get("phone"),
            "website": n.get("website"),
            "tieneWebsite": bool(n.get("has_website") or n.get("website")),
            "urlGoogle": n.get("url"),
            "ciudad": args.ciudad,
            "investigacion_ID": str(inv["_id"]),
            "fuente": "google_maps",
            "activo": True,
            "updatedAt": now,
        }
        previews.append(doc)
        ops.append(UpdateOne(
            {"placeId": pid, "categoria": cat},
            {"$set": doc, "$setOnInsert": {"createdAt": now}},
            upsert=True,
        ))

    print(f"negocios leídos: {len(negocios)} | con place_id: {len(ops)} | sin place_id: {sin_pid}")
    print(f"prior C={C:.3f} (de {len(ratings)} ratings) | m={M_PRIOR} | h3 res {H3_RES}")
    for d in previews[:5]:
        print(f"  · {d['nombre']} | {d['categoriaGoogle']} | rating={d['rating']} n={d['reviews']} score={d['score']} h3={d['h3']}")
    if args.dry_run:
        print("dry-run: no se escribió nada.")
        return

    dst = MongoClient(dst_uri)[args.dst_db][args.dst_collection]
    dst.create_index([("placeId", 1), ("categoria", 1)], unique=True, sparse=True)
    dst.create_index([("categoria", 1)])
    dst.create_index([("h3", 1)])
    dst.create_index([("ciudad", 1)])
    dst.create_index([("location", GEOSPHERE)])
    r = dst.bulk_write(ops, ordered=False)
    print(f"equipamientos.{args.dst_db}: upserted={r.upserted_count} modified={r.modified_count} matched={r.matched_count}")
    total = dst.count_documents({"categoria": cat})
    print(f"total '{cat}' en colección: {total}")


if __name__ == "__main__":
    main()
