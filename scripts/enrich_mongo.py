#!/usr/bin/env python3
"""
Enrich every review document in MongoDB with place-level data pulled from a
discovery.json: GeoJSON coordinates, website URL and (best-effort) email.

The review docs written by the scraper only carry text-level custom_params
(company/phone/address/...). Coordinates live in discovery, websites only on
the Google Maps card, and emails are not exposed by Google Maps at all, so we
fetch each business website to look for a mailto/contact address.

Updates are applied per place_id with updateMany, so every review of a place
gets the same location/website/email.

Usage:
  .venv/bin/python scripts/enrich_mongo.py \
      --discovery scripts/dental_scz/discovery.json \
      --db google_reviews --collection dental --crawl-emails
"""

import argparse
import json
import re
import sys
import time
from pathlib import Path

from pymongo import MongoClient, GEOSPHERE

RE_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
RE_MAILTO = re.compile(r"mailto:([^\"'>?\s]+)", re.I)
RE_BAD_EMAIL = re.compile(r"\.(png|jpe?g|gif|webp|svg|css|js)$", re.I)
CONTACT_HINTS = ("contacto", "contact", "about", "nosotros", "quienes", "empresa")
PLATFORM_DOMAINS = ("facebook.com", "fb.com", "linktr.ee", "wa.me", "whatsapp.com",
                    "instagram.com", "youtube.com", "twitter.com", "tiktok.com",
                    "google.com", "goo.gl")


def _is_platform_email(email: str) -> bool:
    dom = email.rsplit("@", 1)[-1]
    return any(dom == d or dom.endswith("." + d) for d in PLATFORM_DOMAINS)


def _clean_email(email: str) -> str:
    return email.strip().strip("\\/.,;:'\"()<>").lower()


def extract_coords_from_url(url: str):
    for rx in (re.compile(r"!3d(-?[\d.]+)!4d(-?[\d.]+)"),
               re.compile(r"@(-?[\d.]+),(-?[\d.]+)")):
        m = rx.search(url or "")
        if m:
            return float(m.group(1)), float(m.group(2))
    return None, None


def find_emails(website: str, timeout: float = 12.0) -> list:
    """Fetch a website (home + contact-ish pages) and return unique emails."""
    import html as htmllib
    import requests
    from urllib.parse import urljoin

    headers = {"User-Agent": "Mozilla/5.0 (compatible; StatettyBot/1.0)"}
    try:
        r = requests.get(website, headers=headers, timeout=timeout,
                         allow_redirects=True)
        html = r.text
    except Exception:
        return []
    pages = [html]
    links = [urljoin(r.url, h) for h in
             re.findall(r'href=["\']([^"\']+)["\']', html, re.I)]
    contact = [u for u in links
               if any(k in u.lower() for k in CONTACT_HINTS)][:3]
    for u in contact:
        try:
            pages.append(requests.get(u, headers=headers,
                                      timeout=timeout).text)
        except Exception:
            pass
    emails = []
    for page in pages:
        text = htmllib.unescape(page)
        for e in RE_MAILTO.findall(text) + RE_EMAIL.findall(text):
            e = _clean_email(e)
            if ("@" in e and not RE_BAD_EMAIL.search(e)
                    and not _is_platform_email(e) and e not in emails):
                emails.append(e)
    return emails


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--discovery", required=True)
    ap.add_argument("--mongo-uri",
                    default="mongodb://admin:admin@127.0.0.1:27017/?authSource=admin")
    ap.add_argument("--db", default="google_reviews")
    ap.add_argument("--collection", default="dental")
    ap.add_argument("--crawl-emails", action="store_true",
                    help="visit each business website to look for an email")
    ap.add_argument("--email-delay", type=float, default=0.5)
    args = ap.parse_args()

    places = json.loads(Path(args.discovery).read_text(encoding="utf-8"))
    print(f"discovery places: {len(places)}")

    client = MongoClient(args.mongo_uri)
    col = client[args.db][args.collection]

    # The scraper stores place_id as "<hex-feature>:0" while discovery keeps the
    # full "!1s<hex>:0x<cid>" form, so we join on the hex prefix.
    by_prefix = {p["place_id"].split(":")[0]: p for p in places if p.get("place_id")}
    mongo_ids = col.distinct("place_id")
    print(f"mongo place_ids: {len(mongo_ids)} | con match: "
          f"{sum(1 for m in mongo_ids if m.split(':')[0] in by_prefix)}")

    emails_done = {}
    if args.crawl_emails:
        sites = sorted({by_prefix[m.split(":")[0]].get("website")
                        for m in mongo_ids
                        if m.split(":")[0] in by_prefix
                        and by_prefix[m.split(":")[0]].get("website")})
        print(f"crawling {len(sites)} websites for emails...")
        for s in sites:
            found = find_emails(s)
            emails_done[s] = found
            print(f"  {s} -> {found or 'no email'}")
            time.sleep(args.email_delay)

    n_loc = n_site = n_mail = 0
    for mongo_id in mongo_ids:
        p = by_prefix.get(mongo_id.split(":")[0])
        if not p:
            continue
        lat, lng = p.get("lat"), p.get("lng")
        if lat is None or lng is None:
            lat, lng = extract_coords_from_url(p.get("url"))
        sets = {}
        if lat is not None and lng is not None:
            sets["lat"] = float(lat)
            sets["lng"] = float(lng)
            sets["location"] = {"type": "Point",
                                "coordinates": [float(lng), float(lat)]}
        if p.get("website"):
            sets["website"] = p["website"]
        mail = emails_done.get(p.get("website")) if args.crawl_emails else None
        if mail:
            sets["email"] = mail[0]
            sets["emails"] = mail
        if not sets:
            continue
        res = col.update_many({"place_id": mongo_id}, {"$set": sets})
        if "location" in sets:
            n_loc += res.modified_count
        if "website" in sets:
            n_site += res.modified_count
        if "email" in sets:
            n_mail += res.modified_count

    col.create_index([("location", GEOSPHERE)], name="location_2dsphere",
                     sparse=True)
    print(f"docs con location actualizados: {n_loc}")
    print(f"docs con website actualizados:  {n_site}")
    print(f"docs con email actualizados:    {n_mail}")
    print(f"total docs: {col.count_documents({})} | "
          f"con location: {col.count_documents({'location': {'$exists': True}})} | "
          f"con website: {col.count_documents({'website': {'$exists': True}})} | "
          f"con email: {col.count_documents({'email': {'$exists': True}})}")
    client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
