#!/usr/bin/env python3
"""
Rasterized Google Maps discovery.

Google Maps only returns a limited sample of businesses for a whole-city view.
To get near-complete coverage we tile the target area into small cells sized to
the browser viewport at a given zoom, search each cell, scroll the feed to the
end, and recursively subdivide any cell that comes back saturated (near the
per-search result cap).

Reuses the card parser / scroll logic from discover.py.

Usage:
  # 1) dry-run: geocode the area and print the grid (no Google requests)
  .venv/bin/python scripts/rasterize.py \
      --area "Santa Cruz de la Sierra, Bolivia" --zoom 16 --dry-run

  # 2) real run
  .venv/bin/python scripts/rasterize.py \
      --area "Santa Cruz de la Sierra, Bolivia" \
      --keywords "dental,odontologia,clinica dental,dentista,ortodoncia" \
      --zoom 16 --zoom-max 19 \
      --out-dir scripts/dental_scz/raster
"""

import argparse
import csv
import json
import math
import random
import sys
import time
from pathlib import Path

from seleniumbase import Driver
from selenium.webdriver.common.by import By
from pymongo import MongoClient, GEOSPHERE
from bson import ObjectId

sys.path.insert(0, str(Path(__file__).resolve().parent))
import discover as disc  # noqa: E402

M_PER_DEG_LAT = 110574.0
M_PER_DEG_LNG_EQ = 111320.0
EARTH_CIRCUMFERENCE_256 = 156543.03392


def mpp(lat: float, zoom: int) -> float:
    """Web-Mercator meters per pixel (256px tile) at a given latitude/zoom."""
    return EARTH_CIRCUMFERENCE_256 * math.cos(math.radians(lat)) / (2 ** zoom)


def cell_dims(lat: float, zoom: int, inner_w: float, inner_h: float):
    """Cell size in degrees for a viewport of inner_w x inner_h px at zoom."""
    m = mpp(lat, zoom)
    dlat = (inner_h * m) / M_PER_DEG_LAT
    dlng = (inner_w * m) / (M_PER_DEG_LNG_EQ * math.cos(math.radians(lat)))
    return dlat, dlng


def geocode(area: str):
    """Return bbox (south, west, north, east) from Nominatim/OSM."""
    import requests
    r = requests.get(
        "https://nominatim.openstreetmap.org/search",
        params={"q": area, "format": "json", "limit": 1},
        headers={"User-Agent": "statetty-raster/1.0"},
        timeout=20,
    )
    r.raise_for_status()
    js = r.json()
    if not js:
        raise SystemExit(f"No geocoding result for: {area}")
    bb = js[0]["boundingbox"]  # [south, north, west, east]
    return float(bb[0]), float(bb[2]), float(bb[1]), float(bb[3])


def build_grid(s, w, n, e, zoom, overlap, inner_w, inner_h):
    lat = (s + n) / 2
    dlat, dlng = cell_dims(lat, zoom, inner_w, inner_h)
    slat, slng = dlat * (1 - overlap), dlng * (1 - overlap)
    cells, clat, y = [], s + dlat / 2, 0
    while clat - dlat / 2 <= n + 1e-12:
        clng, x = w + dlng / 2, 0
        while clng - dlng / 2 <= e + 1e-12:
            cells.append({"id": f"z{zoom}_{y}_{x}", "lat": round(clat, 6),
                          "lng": round(clng, 6), "zoom": zoom, "parent": None})
            clng += slng
            x += 1
        clat += slat
        y += 1
    return cells


def subdivide(cell, inner_w, inner_h):
    z = cell["zoom"] + 1
    dlat, dlng = cell_dims(cell["lat"], z, inner_w, inner_h)
    kids = []
    for sy in (-1, 1):
        for sx in (-1, 1):
            clat = round(cell["lat"] + sy * dlat / 2, 6)
            clng = round(cell["lng"] + sx * dlng / 2, 6)
            kids.append({"id": f"z{z}_{clat}_{clng}", "lat": clat, "lng": clng,
                         "zoom": z, "parent": cell["id"]})
    return kids


def in_zone(lat, lng, s, w, n, e, poly=None) -> bool:
    if lat is None or lng is None:
        return False
    if not (s <= lat <= n and w <= lng <= e):
        return False
    if poly is None:
        return True
    inside, j = False, len(poly) - 1
    for i in range(len(poly)):
        yi, xi = poly[i][0], poly[i][1]
        yj, xj = poly[j][0], poly[j][1]
        if ((yi > lat) != (yj > lat)) and \
                (lng < (xj - xi) * (lat - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def collect(driver, url, max_iters, idle_limit):
    driver.get(url)
    time.sleep(7)
    disc.dismiss(driver)
    time.sleep(2)
    stale, last = 0, disc._card_count(driver)
    for _ in range(max_iters):
        driver.execute_script(
            'var f=document.querySelector(\'div[role="feed"]\');'
            "if(f){f.scrollTop=f.scrollHeight;f.scrollBy(0,4000);}"
        )
        time.sleep(2.4)
        c = disc._card_count(driver)
        if c > last:
            stale, last = 0, c
        else:
            stale += 1
            if disc._at_end(driver) or stale >= idle_limit:
                break
    cards = driver.find_elements(By.CSS_SELECTOR, "div.Nv2PK")
    places = []
    for card in cards:
        try:
            a = card.find_element(By.CSS_SELECTOR, 'a[href*="/maps/place/"]')
            href = a.get_attribute("href")
            pid = disc.extract_place_id(href)
            if not pid:
                continue
            text = card.text or ""
            lat, lng = disc.extract_coords(href)
            p = disc.parse_card(text)
            p.update({"place_id": pid, "url": href, "lat": lat, "lng": lng,
                      "website": disc.extract_website(card), "raw": text})
            places.append(p)
        except Exception:
            pass
    return places, len(cards)


def norm(s: str) -> str:
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())


def dedup_key(p):
    if p.get("place_id"):
        return p["place_id"]
    return "|".join([norm(p.get("name")), norm(p.get("address")),
                     str(round(float(p.get("lat") or 0), 5)),
                     str(round(float(p.get("lng") or 0), 5))])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--area", default="Santa Cruz de la Sierra, Bolivia")
    ap.add_argument("--bbox", default=None, help="s,w,n,e (overrides --area)")
    ap.add_argument("--polygon", default=None, help="GeoJSON polygon file")
    ap.add_argument("--keywords", required=True)
    ap.add_argument("--zoom", type=int, default=16)
    ap.add_argument("--zoom-max", type=int, default=19)
    ap.add_argument("--overlap", type=float, default=0.15)
    ap.add_argument("--cap", type=int, default=120, help="per-search result cap")
    ap.add_argument("--saturate-ratio", type=float, default=0.9)
    ap.add_argument("--out-dir", default="scripts/raster")
    ap.add_argument("--max-scrolls", type=int, default=150)
    ap.add_argument("--idle-limit", type=int, default=8)
    ap.add_argument("--retries", type=int, default=2)
    ap.add_argument("--sleep-min", type=float, default=3.0)
    ap.add_argument("--sleep-max", type=float, default=8.0)
    ap.add_argument("--limit-searches", type=int, default=0, help="0 = no limit")
    ap.add_argument("--investigacion", default=None, help="ObjectId de la investigación que crea estos negocios")
    ap.add_argument("--mongo-uri",
                    default="mongodb://admin:admin@127.0.0.1:27017/?authSource=admin")
    ap.add_argument("--mongo-db", default="google_reviews")
    ap.add_argument("--mongo-collection", default=None,
                    help="si se indica, hace upsert de los negocios a esta colección")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if args.bbox:
        s, w, n, e = (float(x) for x in args.bbox.split(","))
    else:
        s, w, n, e = geocode(args.area)
    print(f"Area bbox S={s} W={w} N={n} E={e}")

    poly = None
    if args.polygon:
        gj = json.loads(Path(args.polygon).read_text(encoding="utf-8"))
        ring = gj["coordinates"][0] if gj.get("type") == "Polygon" else gj["features"][0]["geometry"]["coordinates"][0]
        poly = [(pt[1], pt[0]) for pt in ring]  # (lat, lng)

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    keywords = [k.strip() for k in args.keywords.split(",") if k.strip()]

    col = None
    dbx = None
    inv_oid = None
    if args.investigacion:
        inv_oid = ObjectId(args.investigacion)
    if args.mongo_collection:
        dbx = MongoClient(args.mongo_uri)[args.mongo_db]
        col = dbx[args.mongo_collection]
        print(f"mongo: {args.mongo_db}.{args.mongo_collection}")

    def to_doc(p):
        doc = {k: v for k, v in p.items() if k != "raw"}
        try:
            lat, lng = float(p["lat"]), float(p["lng"])
            doc["lat"], doc["lng"] = lat, lng
            doc["location"] = {"type": "Point", "coordinates": [lng, lat]}
        except (TypeError, ValueError, KeyError):
            pass
        doc["_updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        return doc

    driver = Driver(uc=True, headless=True, browser="chrome",
                    no_sandbox=True, disable_csp=True)
    driver.set_window_size(1400, 1000)
    inner_w, inner_h = driver.execute_script(
        "return [window.innerWidth, window.innerHeight]")
    print(f"viewport: {inner_w}x{inner_h}")

    base = build_grid(s, w, n, e, args.zoom, args.overlap, inner_w, inner_h)
    print(f"zoom {args.zoom} | celdas base: {len(base)} | "
          f"keywords: {len(keywords)} | búsquedas base: {len(base)*len(keywords)}")

    if args.dry_run:
        for c in base:
            print(f"  {c['id']}  {c['lat']},{c['lng']}  z{c['zoom']}")
        driver.quit()
        return 0

    cells_jsonl = out / "cells.jsonl"
    done = {}
    if cells_jsonl.exists():
        for ln in cells_jsonl.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(ln)
                done[f"{rec['keyword']}|{rec['cell_id']}"] = rec
            except Exception:
                pass
    print(f"checkpoint: {len(done)} celdas ya procesadas")

    seen, searches, subdivided, out_of_zone = {}, 0, 0, 0
    cells_log = open(cells_jsonl, "a", encoding="utf-8")

    def log_cell(rec):
        cells_log.write(json.dumps(rec, ensure_ascii=False) + "\n")
        cells_log.flush()

    try:
        for kw in keywords:
            queue = [dict(c) for c in base]
            while queue:
                cell = queue.pop(0)
                key = f"{kw}|{cell['id']}"
                if key in done:
                    rec = done[key]
                    if rec.get("subdivided") and cell["zoom"] < args.zoom_max:
                        queue = subdivide(cell, inner_w, inner_h) + queue
                    continue
                if args.limit_searches and searches >= args.limit_searches:
                    print(f"límite de búsquedas alcanzado ({searches})")
                    raise SystemExit
                url = (f"https://www.google.com/maps/search/"
                       f"{kw.replace(' ', '+')}/@{cell['lat']},{cell['lng']},"
                       f"{cell['zoom']}z?hl=es")
                places, raw = [], 0
                for attempt in range(args.retries + 1):
                    try:
                        places, raw = collect(driver, url, args.max_scrolls,
                                              args.idle_limit)
                        break
                    except Exception as ex:
                        print(f"  retry {attempt+1} {cell['id']}: {ex}")
                        time.sleep(5)
                searches += 1
                inbox = [p for p in places
                         if in_zone(float(p.get("lat") or 0) if p.get("lat") else None,
                                    float(p.get("lng") or 0) if p.get("lng") else None,
                                    s, w, n, e, poly)]
                out_of_zone += len(places) - len(inbox)
                saturated = raw >= args.cap * args.saturate_ratio
                for p in inbox:
                    p["found_keyword"] = kw
                    p["found_cell"] = cell["id"]
                    k = dedup_key(p)
                    if k not in seen or len(p.get("raw", "")) > len(seen[k].get("raw", "")):
                        seen[k] = p
                if col is not None:
                    for p in inbox:
                        hexp = (p["place_id"] or "").split(":")[0]
                        update = {"$set": to_doc(p)}
                        if inv_oid is not None:
                            update["$setOnInsert"] = {"investigacion_ID": inv_oid}
                        col.update_one(
                            {"place_id": {"$regex": f"^{hexp}:"}} if hexp else {"place_id": p["place_id"]},
                            update,
                            upsert=True)
                if saturated and cell["zoom"] < args.zoom_max:
                    kids = subdivide(cell, inner_w, inner_h)
                    queue = kids + queue
                    subdivided += 1
                log_cell({"keyword": kw, "cell_id": cell["id"],
                           "lat": cell["lat"], "lng": cell["lng"],
                           "zoom": cell["zoom"], "raw": raw,
                           "in_zone": len(inbox), "saturated": saturated,
                           "subdivided": saturated and cell["zoom"] < args.zoom_max,
                           "searches": searches, "queue": len(queue),
                           "ts": time.strftime("%Y-%m-%dT%H:%M:%S")})
                if inv_oid is not None and dbx is not None:
                    try:
                        n_neg = dbx["negocios"].count_documents({"investigacion_ID": inv_oid})
                        revs = 0
                        try:
                            rev_cur = dbx["negocio_reviews"].aggregate([
                                {"$lookup": {"from": "negocios", "localField": "negocio_id", "foreignField": "_id", "as": "n"}},
                                {"$match": {"n.investigacion_ID": inv_oid}},
                                {"$count": "c"},
                            ])
                            revs = next(rev_cur)["c"] if rev_cur else 0
                        except Exception:
                            revs = 0
                        dbx["negocios_investigacion"].update_one(
                            {"_id": inv_oid},
                            {"$set": {
                                "registros_negocios": n_neg,
                                "registros_negocio_reviews": revs,
                                "celdas_procesadas": len(done),
                                "busquedas_realizadas": searches,
                                "celdas_en_cola": len(queue),
                                "fecha_actualizada": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                "activa": True,
                            }})
                    except Exception as ex:
                        print(f"[inv] {ex}")
                flag = " SATURADA->subdividida" if (saturated and cell["zoom"] < args.zoom_max) else ""
                print(f"  [{kw}] {cell['id']} raw={raw} in={len(inbox)}{flag}")
                time.sleep(random.uniform(args.sleep_min, args.sleep_max))
    finally:
        cells_log.close()
        try:
            driver.quit()
        except Exception:
            pass

    places = sorted(seen.values(),
                    key=lambda p: int(p.get("review_count") or 0), reverse=True)
    (out / "discovery.json").write_text(
        json.dumps(places, ensure_ascii=False, indent=2), encoding="utf-8")
    with open(out / "discovery.csv", "w", encoding="utf-8", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(["place_id", "name", "rating", "review_count", "category",
                     "address", "phone", "website", "lat", "lng", "found_keyword", "url"])
        for p in places:
            wr.writerow([p.get("place_id"), p.get("name"), p.get("rating"),
                         p.get("review_count"), p.get("category"), p.get("address"),
                         p.get("phone"), p.get("website"), p.get("lat"), p.get("lng"),
                         p.get("found_keyword"), p.get("url")])
    report = {"bbox": [s, w, n, e], "zoom": args.zoom, "zoom_max": args.zoom_max,
              "overlap": args.overlap, "cap": args.cap,
              "keywords": keywords, "base_cells": len(base),
              "searches": searches, "subdivided": subdivided,
              "out_of_zone_discarded": out_of_zone,
              "unique_places": len(places)}
    (out / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("REPORTE:", json.dumps(report, ensure_ascii=False))
    if col is not None:
        col.create_index([("location", GEOSPHERE)], name="location_2dsphere",
                         sparse=True)
        print(f"mongo {args.mongo_db}.{args.mongo_collection}: "
              f"{col.count_documents({})} negocios")
    return 0


if __name__ == "__main__":
    import os
    import shutil
    if (sys.platform.startswith("linux")
            and not os.environ.get("_GR_XVFB")
            and shutil.which("xvfb-run")):
        os.environ["_GR_XVFB"] = "1"
        os.execvp("xvfb-run", ["xvfb-run", "-a", "-s",
                  "-screen 0 1600x1200x24", sys.executable, *sys.argv])
    sys.exit(main())
