import argparse
import json
from datetime import datetime, timezone

from pymongo import MongoClient

DEFAULT_URI = "mongodb://admin:admin@127.0.0.1:27017/?authSource=admin"
DB = "google_reviews"
COL = "negocios_investigacion"


def _now():
    return datetime.now(timezone.utc)


def main():
    ap = argparse.ArgumentParser(description="Gestiona investigaciones de negocios")
    ap.add_argument("--mongo-uri", default=DEFAULT_URI)
    ap.add_argument("--db", default=DB)
    ap.add_argument("--new", action="store_true", help="crea una nueva investigación")
    ap.add_argument("--update", metavar="ID", help="agrega una actualización a la investigación")
    ap.add_argument("--list", action="store_true", help="lista investigaciones")
    ap.add_argument("--nombre")
    ap.add_argument("--descripcion")
    ap.add_argument("--keywords", help="comas separadas")
    ap.add_argument("--bbox", help='"s,w,n,e"')
    ap.add_argument("--duracion-seg", type=int, help="duración en segundos (primera vez o actualización)")
    ap.add_argument("--tag", choices=["all", "keywords", "none"], default="keywords",
                    help="qué negocios marcar al crear (primera ejecución recomienda --tag all)")
    ap.add_argument("--inactiva", action="store_true", help="crea la investigación inactiva")
    ap.add_argument("--set-activa", nargs=2, metavar=("ID", "true|false"),
                    help="activa/desactiva una investigación")
    args = ap.parse_args()

    mongo = MongoClient(args.mongo_uri)
    dbx = mongo[args.db]
    col = dbx[COL]
    negocios = dbx["negocios"]

    if args.set_activa:
        from bson import ObjectId
        oid = ObjectId(args.set_activa[0])
        val = args.set_activa[1].lower() == "true"
        col.update_one({"_id": oid}, {"$set": {"activa": val}})
        print(f"investigación {oid}: activa = {val}")
        mongo.close()
        return

    if args.list:
        for d in col.find().sort("fecha_pedida", 1):
            print(d["_id"], "|", d["nombre"], "|", "activa:",
                  d.get("activa", False), "|", d["fecha_pedida"].date(),
                  "| negocios:", d["registros_negocios"],
                  "| reviews:", d["registros_negocio_reviews"],
                  "| actualizaciones:", len(d.get("actualizaciones", [])))
        mongo.close()
        return

    if args.new:
        if not args.nombre:
            ap.error("--new requiere --nombre")
        filtro_tag = {}
        if args.tag == "keywords":
            kws = [k.strip() for k in (args.keywords or "").split(",") if k.strip()]
            filtro_tag = {"found_keyword": {"$in": kws}}
        elif args.tag == "none":
            filtro_tag = {"_id": None}
        doc = {
            "nombre": args.nombre,
            "activa": not args.inactiva,
            "descripcion": args.descripcion,
            "criterio": {
                "keywords": [k.strip() for k in (args.keywords or "").split(",") if k.strip()],
                "bbox": [float(x) for x in args.bbox.split(",")] if args.bbox else None,
                "tag": args.tag,
            },
            "fecha_pedida": _now(),
            "duracion_primera_vez_seg": args.duracion_seg,
            "registros_negocios": 0,
            "registros_negocio_reviews": 0,
            "actualizaciones": [],
        }
        rid = col.insert_one(doc).inserted_id
        if args.tag != "none":
            negocios.update_many(filtro_tag, {"$set": {"investigacion_ID": rid}})
        _refresh_counts(col, negocios, rid, dbx)
        print("INVESTIGACIÓN CREADA:", rid)
        _log_counts(col, rid)
        mongo.close()
        return

    if args.update:
        from bson import ObjectId
        oid = ObjectId(args.update)
        inv = col.find_one({"_id": oid})
        if inv is None:
            ap.error(f"no existe investigación {args.update}")
        kws = (inv.get("criterio") or {}).get("keywords") or []
        if kws:
            negocios.update_many(
                {"found_keyword": {"$in": kws},
                 "$or": [{"investigacion_ID": {"$exists": False}},
                         {"investigacion_ID": None},
                         {"investigacion_ID": oid}]},
                {"$set": {"investigacion_ID": oid}})
        entry = {"fecha": _now(), "registros_revisados": 0, "duracion_seg": args.duracion_seg}
        entry["registros_revisados"] = _refresh_counts(col, negocios, oid, dbx)
        col.update_one({"_id": oid}, {"$push": {"actualizaciones": entry}})
        print("ACTUALIZACIÓN REGISTRADA:", oid)
        _log_counts(col, oid)
        mongo.close()
        return

    ap.print_help()


def _refresh_counts(col, negocios, rid, dbx):
    n_neg = negocios.count_documents({"investigacion_ID": rid})
    like_ids = negocios.find({"investigacion_ID": rid}, {"_id": 1})
    ids = [d["_id"] for d in like_ids]
    n_rev = dbx["negocio_reviews"].count_documents({"negocio_id": {"$in": ids}}) if ids else 0
    col.update_one({"_id": rid},
                   {"$set": {"registros_negocios": n_neg,
                             "registros_negocio_reviews": n_rev}})
    return n_neg


def _log_counts(col, rid):
    d = col.find_one({"_id": rid})
    print(f"  registros_negocios: {d['registros_negocios']}  "
          f"| registros_negocio_reviews: {d['registros_negocio_reviews']}")


if __name__ == "__main__":
    main()