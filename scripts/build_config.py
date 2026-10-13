#!/usr/bin/env python3
"""
Build a googleReviews `config.yaml` from a discover.py output.

Merges a discovery.json (list of places) into a full config: the scraper
settings + a `businesses:` block + local MongoDB sync. Repeatable for any
keyword/location.

Usage:
  .venv/bin/python scripts/build_config.py \
      --discovery scripts/dental_scz/discovery.json \
      --out       scripts/dental_scz/config.yaml \
      --keyword   dental
"""

import argparse
import json
from pathlib import Path

import yaml


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--discovery", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--keyword", default="")
    ap.add_argument("--base-dir", default=None,
                    help="base dir for outputs (default: folder of --out)")
    ap.add_argument("--mongo-uri",
                    default="mongodb://admin:admin@127.0.0.1:27017/?authSource=admin")
    ap.add_argument("--mongo-db", default="google_reviews")
    ap.add_argument("--mongo-collection", default=None)
    ap.add_argument("--download-images", action="store_true", default=False)
    args = ap.parse_args()

    places = json.loads(Path(args.discovery).read_text(encoding="utf-8"))
    out = Path(args.out)
    base = (Path(args.base_dir) if args.base_dir else out.parent).resolve()
    collection = args.mongo_collection or (args.keyword or "reviews")

    config = {
        "headless": True,
        # relevance avoids the (broken in this UI) sort menu clicks; we want
        # all reviews in one pass anyway, so early-stop is not needed.
        "sort_by": "relevance",
        "scrape_mode": "update",
        "stop_threshold": 3,
        "max_reviews": 0,
        "max_scroll_attempts": 200,
        "scroll_idle_limit": 5,
        "db_path": str(base / "reviews.db"),
        "convert_dates": True,
        "download_images": args.download_images,
        "image_dir": str(base / "review_images"),
        "download_threads": 4,
        "use_mongodb": True,
        "mongodb": {
            "uri": args.mongo_uri,
            "database": args.mongo_db,
            "collection": collection,
            "sync_mode": "update",
        },
        "backup_to_json": True,
        "json_path": str(base / "google_reviews.json"),
        "log_dir": str(base / "logs"),
        "log_file": "scraper.log",
        "log_level": "INFO",
        "businesses": [],
    }

    for p in places:
        name = (p.get("name") or "").strip()
        cp = {
            "company": name,
            "category": p.get("category") or "",
            "address": p.get("address") or "",
            "phone": p.get("phone") or "",
            "google_rating": p.get("rating") or "",
            "google_review_count": p.get("review_count") or "",
            "search_keyword": args.keyword,
            "source": "Google Maps",
        }
        lat, lng = p.get("lat"), p.get("lng")
        if lat and lng:
            cp["lat"] = float(lat)
            cp["lng"] = float(lng)
            cp["location"] = {"type": "Point", "coordinates": [float(lng), float(lat)]}
        if p.get("website"):
            cp["website"] = p["website"]
        if p.get("email"):
            cp["email"] = p["email"]
        config["businesses"].append({"url": p["url"], "custom_params": cp})

    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        yaml.safe_dump(config, f, allow_unicode=True, sort_keys=False)
    print(f"Wrote {out} with {len(config['businesses'])} businesses")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())