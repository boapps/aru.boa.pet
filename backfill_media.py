#!/usr/bin/env python
"""One-shot backfill: add main picture + lidl.hu link to existing data.

booklets.py now records `image` and `url` per product, but products.json and
data/latest.json were written before that. This re-runs the site search for
every product still missing either field, updates products.json in place, and
merges the new fields into data/latest.json so the website shows them without
waiting for a fresh stock scrape. Safe to re-run; it only touches gaps.

    python3 backfill_media.py            # all products in products.json
    python3 backfill_media.py --latest-only   # only ids present in latest.json
"""

import argparse
import json
import sys
import time

import booklets

PRODUCTS = "products.json"
LATEST = "data/latest.json"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--delay", type=float, default=0.3)
    ap.add_argument("--latest-only", action="store_true",
                    help="only resolve ids already present in latest.json")
    args = ap.parse_args()

    with open(PRODUCTS, encoding="utf-8") as f:
        products = json.load(f)

    latest = None
    try:
        with open(LATEST, encoding="utf-8") as f:
            latest = json.load(f)
    except (OSError, ValueError):
        pass

    wanted = None
    if args.latest_only and latest:
        wanted = {p["id"] for p in latest["products"]}

    s = booklets.session()
    media = {}  # id -> {"image", "url"}
    todo = [p for p in products
            if (wanted is None or p["id"] in wanted)
            and not (p.get("image") and p.get("url"))]
    print(f"[backfill] resolving {len(todo)} product(s)", file=sys.stderr)

    for i, p in enumerate(todo, 1):
        info = booklets.lookup_validity(s, p["id"])
        if info:
            if info.get("image"):
                p["image"] = info["image"]
            if info.get("url"):
                p["url"] = info["url"]
            media[p["id"]] = {"image": p.get("image"), "url": p.get("url")}
        print(f"[backfill] {i}/{len(todo)} {p['id']} "
              f"img={'Y' if p.get('image') else '-'} "
              f"url={'Y' if p.get('url') else '-'}", file=sys.stderr)
        if args.delay:
            time.sleep(args.delay)

    with open(PRODUCTS, "w", encoding="utf-8") as f:
        json.dump(products, f, ensure_ascii=False, indent=2)
    print(f"[backfill] updated {PRODUCTS}", file=sys.stderr)

    # Merge into latest.json from the full products.json (covers ids resolved
    # in earlier runs too), so the site reflects everything we know.
    if latest:
        by_id = {p["id"]: p for p in products}
        n = 0
        for lp in latest["products"]:
            src = by_id.get(lp["id"])
            if not src:
                continue
            if src.get("image"):
                lp["image"] = src["image"]
            if src.get("url"):
                lp["url"] = src["url"]
            if lp.get("image") or lp.get("url"):
                n += 1
        with open(LATEST, "w", encoding="utf-8") as f:
            json.dump(latest, f, ensure_ascii=False, indent=2)
        print(f"[backfill] updated {LATEST} ({n} products with media)",
              file=sys.stderr)


if __name__ == "__main__":
    main()
