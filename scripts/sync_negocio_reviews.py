import argparse

from pymongo import MongoClient, GEOSPHERE

DEFAULT_URI = "mongodb://admin:admin@127.0.0.1:27017/?authSource=admin"


def main():
    ap = argparse.ArgumentParser(description="Registra reseñas en negocio_reviews con el _id del negocio (~place_id)")
    ap.add_argument("--mongo-uri", default=DEFAULT_URI)
    ap.add_argument("--db", default="google_reviews")
    ap.add_argument("--collections", default="dental,smoke", help="fuentes de reseñas, separadas por coma")
    ap.add_argument("--negocios-collection", default="negocios")
    ap.add_argument("--reviews-collection", default="negocio_reviews")
    args = ap.parse_args()

    mongo = MongoClient(args.mongo_uri)
    db_ = mongo[args.db]
    src_coll = [c.strip() for c in args.collections.split(",") if c.strip()]

    by_hex = {}
    for n in db_[args.negocios_collection].find({}, {"place_id": 1}):
        hexp = (n.get("place_id") or "").split(":")[0]
        if hexp:
            by_hex.setdefault(hexp, n["_id"])

    created, linked, synced, unlinked = 0, 0, 0, 0
    for coll_name in src_coll:
        for rev in db_[coll_name].find():
            pid = rev.get("place_id")
            hexp = (pid or "").split(":")[0] if pid else ""
            neg_id = by_hex.get(hexp)
            if neg_id is None:
                lat, lng = rev.get("lat"), rev.get("lng")
                neg_doc = {
                    "name": rev.get("company") or rev.get("name"),
                    "category": rev.get("category"),
                    "address": rev.get("address"),
                    "phone": rev.get("phone"),
                    "rating": 0.0,
                    "review_count": 0,
                    "source": "from_reviews",
                    "place_id": pid,
                }
                if lat is not None and lng is not None:
                    try:
                        lat, lng = float(lat), float(lng)
                        neg_doc["lat"] = lat
                        neg_doc["lng"] = lng
                        neg_doc["location"] = {"type": "Point", "coordinates": [lng, lat]}
                    except (TypeError, ValueError):
                        pass
                db_[args.negocios_collection].update_one(
                    {"place_id": pid}, {"$setOnInsert": neg_doc}, upsert=True
                )
                neg_id = db_[args.negocios_collection].find_one(
                    {"place_id": pid}, {"_id": 1}
                )["_id"]
                by_hex[hexp] = neg_id
                created += 1
            else:
                linked += 1

            doc = {k: v for k, v in rev.items() if k != "_id"}
            doc["negocio_id"] = neg_id
            db_[args.reviews_collection].update_one(
                {"review_id": rev["review_id"]}, {"$set": doc}, upsert=True
            )
            synced += 1

    try:
        db_[args.reviews_collection].create_index([("negocio_id", 1)])
        db_[args.reviews_collection].create_index([("review_id", 1)], unique=True)
        db_[args.negocios_collection].create_index([("location", GEOSPHERE)])
    except Exception as e:  # noqa: BLE001
        print("aviso índices:", e)

    mongo.close()
    print(f"reseñas sincronizadas: {synced} | negocio ya existía: {linked} | negocios creados: {created}")


if __name__ == "__main__":
    main()