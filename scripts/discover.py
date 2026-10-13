#!/usr/bin/env python3
"""
Discover Google Maps places for a keyword + location.

Scrapes the Google Maps search results feed (scroll-to-load), extracts every
business card (name, place URL, place_id, rating, review count, category,
address), dedupes by place_id, and writes:
  - a JSON file with the full discovery
  - a CSV for quick inspection
  - a googleReviews `businesses:` YAML fragment ready to paste into config.yaml

Usage:
  .venv/bin/python scripts/discover.py \
      --keyword dental --location "Santa Cruz de la Sierra" \
      --out-dir scripts/dental_scz
"""

import argparse
import csv
import json
import re
import sys
import time
from pathlib import Path

from seleniumbase import Driver
from selenium.webdriver.common.by import By

RE_PLACE_ID = re.compile(r"!1s(0x[0-9a-fA-F]+:0x[0-9a-fA-F]+)")
RE_CID = re.compile(r"cid=(\d+)")
RE_COORDS_AT = re.compile(r"@(-?[\d.]+),(-?[\d.]+)")
RE_COORDS_BANG = re.compile(r"!3d(-?[\d.]+)!4d(-?[\d.]+)")
RE_WEBSITE_WORDS = ("sitio web", "website", "sitio web del negocio")


def extract_place_id(href: str) -> str:
    m = RE_PLACE_ID.search(href)
    if m:
        return m.group(1)
    m = RE_CID.search(href)
    return f"cid:{m.group(1)}" if m else ""


def extract_coords(href: str):
    for rx in (RE_COORDS_BANG, RE_COORDS_AT):
        m = rx.search(href)
        if m:
            return (m.group(1), m.group(2))
    return (None, None)


def extract_website(card_el) -> str:
    """Return the external website URL linked from a result card, if any."""
    try:
        for a in card_el.find_elements(By.CSS_SELECTOR, "a[data-value]"):
            dv = (a.get_attribute("data-value") or "").strip().lower()
            if dv in RE_WEBSITE_WORDS:
                href = a.get_attribute("href") or ""
                if href.startswith("http"):
                    return href
    except Exception:
        pass
    try:
        for a in card_el.find_elements(By.CSS_SELECTOR, 'a[href^="http"]'):
            href = a.get_attribute("href") or ""
            if "google.com" not in href and "/maps/" not in href:
                return href
    except Exception:
        pass
    return None


def _card_count(driver) -> int:
    return len(driver.find_elements(By.CSS_SELECTOR, "div.Nv2PK"))


def dismiss(driver) -> None:
    try:
        for btn in driver.find_elements(By.CSS_SELECTOR, "button"):
            t = (btn.text or "").strip().lower()
            if t in ("reject all", "accept all", "rechazar todo", "aceptar todo"):
                btn.click()
                time.sleep(1)
                break
    except Exception:
        pass


def _at_end(driver) -> bool:
    try:
        txt = driver.execute_script("""
            var f = document.querySelector('div[role="feed"]');
            return f ? f.innerText.slice(-400).toLowerCase() : '';
        """)
        return any(s in txt for s in (
            "final de la lista", "end of the list", "no se encontraron",
            "has llegado al final",
        ))
    except Exception:
        return False


def scroll_feed(driver, url: str, max_iters: int = 150, idle_limit: int = 8) -> None:
    driver.get(url)
    time.sleep(7)
    dismiss(driver)
    time.sleep(2)

    stale = 0
    last_count = _card_count(driver)
    for i in range(max_iters):
        driver.execute_script(
            """
            var f = document.querySelector('div[role="feed"]');
            if (f) { f.scrollTop = f.scrollHeight; f.scrollBy(0, 4000); }
            """
        )
        time.sleep(2.4)
        new_count = _card_count(driver)
        if new_count > last_count:
            stale = 0
            last_count = new_count
        else:
            stale += 1
            if _at_end(driver):
                print(f"  reached end of list ({last_count} cards)")
                break
            if stale >= idle_limit:
                print(f"  feed stable ({last_count} cards), stopping scrolls")
                break
        if i % 5 == 0:
            print(f"  scroll {i}: {last_count} cards")


DAYS = [
    ("lunes", "lun"), ("monday", "lun"), ("martes", "mar"), ("tuesday", "mar"),
    ("mi[eé]rcoles", "mié"), ("wednesday", "mié"), ("jueves", "jue"), ("thursday", "jue"),
    ("viernes", "vie"), ("friday", "vie"), ("s[aá]bado", "sáb"), ("saturday", "sáb"),
    ("domingo", "dom"), ("sunday", "dom"),
    ("lun", "lun"), ("mar", "mar"), ("mi[eé]", "mié"), ("jue", "jue"), ("vie", "vie"),
    ("s[aá]b", "sáb"), ("dom", "dom"), ("mon", "lun"), ("tue", "mar"), ("wed", "mié"),
    ("thu", "jue"), ("fri", "vie"), ("sat", "sáb"), ("sun", "dom"),
]


def normalize_hours(text: str):
    """Convierte el chip 'open now' de Google en tokens limpios '8am lun'."""
    if not text:
        return None
    t = text.lower().replace(".", "").replace("\u202f", " ").replace("\xa0", " ")
    tokens = []
    for m in re.finditer(r"(\d{1,2})\s*([ap])\s*m", t):
        token = m.group(1) + m.group(2) + "m"
        if token not in tokens:
            tokens.append(token)
    if not tokens:
        return None
    day = None
    for pattern, abbr in DAYS:
        if re.search(rf"\b{pattern}\b", t):
            day = abbr
            break
    day_suf = f" {day}" if day else ""
    return [tk + day_suf for tk in tokens]


def parse_card(link_text: str) -> dict:
    lines = [ln.strip() for ln in link_text.split("\n") if ln.strip()]
    out = {"name": lines[0] if lines else ""}
    rating = 0
    count = None
    # rating + review count: "5,0(3)"  |  "No hay reseñas"
    m = re.search(r"(\d+[.,]\d+)\((\d[\d.,]*)\)", link_text)
    if m:
        rating = float(m.group(1).replace(",", "."))
        count = int(m.group(2).replace(".", "").replace(",", ""))
    elif "no hay reseñas" in link_text.lower() or "no reviews" in link_text.lower():
        count = 0

    category = None
    address = None
    hours = None
    phone = None
    for ln in lines[1:]:
        if "·" not in ln:
            continue
        parts = [p.strip() for p in ln.split("·")]
        if re.match(r"^(abierto|cerrado|cierra|abre|open|closed|temporarily)", ln, re.I):
            hours = normalize_hours(ln)
            tail = parts[-1]
            if re.fullmatch(r"\+?[\d][\d\s\-]{4,}", tail):
                phone = tail
        elif category is None:
            category = parts[0]
            address = " · ".join(parts[1:]).strip() or None

    out.update({
        "name": out["name"],
        "rating": rating,
        "review_count": count,
        "category": category,
        "address": address,
        "hours": hours,
        "phone": phone,
        "has_website": "sitio web" in link_text.lower() or "website" in link_text.lower(),
    })
    return out


def discover(driver, url: str) -> list:
    seen = {}
    scroll_feed(driver, url)
    cards = driver.find_elements(By.CSS_SELECTOR, "div.Nv2PK")
    print(f"  found {len(cards)} result cards")
    for card_el in cards:
        try:
            a = card_el.find_element(By.CSS_SELECTOR, 'a[href*="/maps/place/"]')
            href = a.get_attribute("href")
            pid = extract_place_id(href)
            if not pid:
                continue
            text = card_el.text or ""
            lat, lng = extract_coords(href)
            website = extract_website(card_el)
            parsed = parse_card(text)
            parsed.update({"place_id": pid, "url": href, "lat": lat, "lng": lng,
                           "website": website, "raw": text})
            existing = seen.get(pid)
            # keep the richer variant
            if not existing or len(text) > len(existing.get("raw", "")):
                seen[pid] = parsed
        except Exception as e:
            print(f"  skip card: {e}")
    return list(seen.values())


def write_outputs(places: list, keyword: str, location: str, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "discovery.json"
    csv_path = out_dir / "discovery.csv"
    yaml_path = out_dir / "businesses.yaml"

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(places, f, ensure_ascii=False, indent=2)

    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["place_id", "name", "rating", "review_count", "category",
                    "address", "phone", "hours", "has_website", "website", "lat", "lng", "url"])
        for p in places:
            w.writerow([p.get("place_id"), p.get("name"), p.get("rating"),
                        p.get("review_count"), p.get("category"), p.get("address"),
                        p.get("phone"), p.get("hours"), p.get("has_website"),
                        p.get("website"), p.get("lat"), p.get("lng"), p.get("url")])

    with open(yaml_path, "w", encoding="utf-8") as f:
        f.write("# Automatizado por scripts/discover.py\n")
        f.write(f"# Palabra: {keyword} | Lugar: {location}\n")
        f.write(f"# {len(places)} negocios descubiertos\n")
        f.write("businesses:\n")
        for p in places:
            name = (p.get("name") or "").replace(":", "-")
            f.write(f"  - url: \"{p['url']}\"\n")
            f.write(f"    custom_params:\n")
            f.write(f"      company: \"{name}\"\n")
            f.write(f"      category: dental\n")
            f.write(f"      source: \"Google Maps\"\n")

    print(f"  JSON -> {json_path}")
    print(f"  CSV  -> {csv_path}")
    print(f"  YAML -> {yaml_path}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--keyword", default="dental", help="Palabra de búsqueda (default: dental)")
    ap.add_argument("--location", default="Santa Cruz de la Sierra",
                    help="Lugar (default: Santa Cruz de la Sierra)")
    ap.add_argument("--out-dir", default="scripts/dental_scz")
    ap.add_argument("--max-scrolls", type=int, default=80)
    ap.add_argument("--idle-limit", type=int, default=12)
    args = ap.parse_args()

    query = f"{args.keyword} {args.location}".strip().replace(" ", "+")
    url = f"https://www.google.com/maps/search/{query}?hl=es"

    print(f"Search: {url}")
    driver = Driver(uc=True, headless=True, browser="chrome",
                    no_sandbox=True, disable_csp=True)
    driver.set_window_size(1400, 1000)
    try:
        places = discover(driver, url)
    finally:
        try:
            driver.quit()
        except Exception:
            pass

    places.sort(key=lambda p: int(p.get("review_count") or 0), reverse=True)
    print(f"TOTAL: {len(places)} negocios únicos")
    write_outputs(places, args.keyword, args.location, Path(args.out_dir))
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