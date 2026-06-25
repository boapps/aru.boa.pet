#!/usr/bin/env python
"""Periodically snapshot Lidl HU stock availability for a list of products.

Reads a products file (default products.json), fetches nationwide availability
for each product via lidl.py's covering-seed + gap-fill logic, and writes a
timestamped JSON snapshot (plus a stable latest.json) into the output directory.

Intended to be run on a schedule, e.g. hourly via cron:

    0 * * * * cd /home/boa/termekek && /usr/bin/python3 track.py >> data/track.log 2>&1

Prerequisites (run once): `python3 lidl.py discover` to create seeds.json /
stores.json. The urlToken in lidl.py must be valid (re-record when it expires).

Usage:
    python3 track.py                          # products.json -> data/ (sequential)
    python3 track.py --products my.json --out-dir snapshots
"""

import argparse
import asyncio
import json
import os
import sys
from datetime import date, datetime, timezone

import lidl


def load_products(path):
    """Load the product list: [{"id": "478580", "name": "..."}, ...].

    Also accepts a bare list of id strings, or {"products": [...]}. Booklet
    entries (written by booklets.py) additionally carry `valid_from` /
    `valid_until` érvényesség dates, which are preserved here for date filtering.
    """
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        data = data.get("products", [])
    products = []
    for entry in data:
        if isinstance(entry, str):
            products.append({"id": entry, "name": None})
        elif isinstance(entry, dict) and entry.get("id"):
            products.append(
                {
                    "id": str(entry["id"]),
                    "name": entry.get("name"),
                    "image": entry.get("image"),
                    "url": entry.get("url"),
                    "valid_from": entry.get("valid_from"),
                    "valid_until": entry.get("valid_until"),
                }
            )
    return products


def filter_current(products, today=None):
    """Keep only offers that have already started.

    Keeps a product when its offer date (`valid_from`, the "Ajánlat érvényes:"
    date set by booklets.py) is today or earlier -- i.e. the offer is live --
    and drops offers that only start in the future. Products with no date --
    e.g. manually added ids -- are always kept, so this never silently drops
    entries that carry no validity information.
    """
    if today is None:
        today = date.today()
    kept = []
    for p in products:
        vf = p.get("valid_from")
        if not vf:
            kept.append(p)
            continue
        try:
            if date.fromisoformat(vf) <= today:
                kept.append(p)
        except ValueError:
            kept.append(p)
    return kept


IN_STOCK_EMOJI = ("🟢", "🟡")


def _store_key(s):
    """Stable identity for a store, shared by every product snapshot."""
    return (s.get("postcode", ""), s.get("street", ""))


def encode_snapshot(fetched_at, products):
    """Build a compact, denormalized snapshot.

    Store metadata (address, coordinates) is identical across every product, so
    we emit it once in a shared `stores` list and give each product only a flat
    `avail` array of status codes parallel to that list (null = the store was
    not returned for this product). Statuses are interned into a small legend.
    This shrinks the payload roughly N-fold for N products and lets the website
    render any product without re-parsing per-store address objects.

    `products` is a list of {"id", "name", "stores": [<raw store dicts>]} where
    each raw store dict carries postcode/city/street/status/emoji/open/lat/lon.
    """
    statuses, status_index = [], {}

    def status_code(emoji, label):
        key = (emoji, label)
        if key not in status_index:
            status_index[key] = len(statuses)
            statuses.append({"emoji": emoji, "label": label})
        return status_index[key]

    stores, store_index = [], {}

    def store_code(s):
        key = _store_key(s)
        if key not in store_index:
            store_index[key] = len(stores)
            stores.append(
                {
                    "postcode": s["postcode"],
                    "city": s["city"],
                    "street": s["street"],
                    "lat": s["lat"],
                    "lon": s["lon"],
                    "open": s["open"] == "open",
                }
            )
        return store_index[key]

    # Pass 1: intern every store and status across all products.
    product_maps = []
    for p in products:
        amap = {}
        for s in p["stores"]:
            amap[store_code(s)] = status_code(s["emoji"], s["status"])
        product_maps.append(amap)

    # Pass 2: build dense per-product arrays over the final store list.
    n = len(stores)
    out_products = []
    for p, amap in zip(products, product_maps):
        avail = [amap.get(i) for i in range(n)]
        in_stock = sum(
            1 for c in amap.values() if statuses[c]["emoji"] in IN_STOCK_EMOJI
        )
        out_products.append(
            {
                "id": p["id"],
                "name": p["name"],
                "image": p.get("image"),
                "url": p.get("url"),
                "store_count": len(amap),
                "in_stock_count": in_stock,
                "avail": avail,
            }
        )

    return {
        "fetched_at": fetched_at,
        "statuses": statuses,
        "stores": stores,
        "products": out_products,
    }


def _load_checkpoint(path, products):
    """Return raw entries already fetched for a compatible interrupted run.

    A checkpoint is only reused when it covers exactly the same set of product
    ids, so editing products.json (or crossing a date boundary that changes the
    filtered set) starts fresh instead of mixing results from different runs.
    Returns a list of raw `{"id", "name", "stores"}` entries in fetch order.
    """
    if not path or not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            ck = json.load(f)
    except (json.JSONDecodeError, OSError):
        return []
    want = sorted(p["id"] for p in products)
    if sorted(ck.get("product_ids", [])) != want:
        print("[track] ignoring stale checkpoint (product set changed)",
              file=sys.stderr)
        return []
    return ck.get("raw", [])


def _save_checkpoint(path, products, raw):
    """Atomically persist fetched-so-far results so a stopped run can resume."""
    if not path:
        return
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(
            {"product_ids": [p["id"] for p in products], "raw": raw},
            f, ensure_ascii=False,
        )
    os.replace(tmp, path)


async def snapshot(products, *, token, seeds_path, concurrency, verbose,
                   checkpoint_path=None):
    """Fetch availability for every product; return a compact snapshot dict.

    Results are checkpointed to `checkpoint_path` after each product, so a run
    interrupted partway through (Ctrl-C, crash, expired token) resumes from the
    next unfetched product instead of starting over.
    """
    done = {e["id"]: e for e in _load_checkpoint(checkpoint_path, products)}
    if done and not verbose:
        print(f"[track] resuming: {len(done)}/{len(products)} already fetched",
              file=sys.stderr)
    raw = []
    total = len(products)
    for i, p in enumerate(products, 1):
        cached = done.get(p["id"])
        if cached is not None:
            raw.append(cached)
            continue
        if verbose:
            print(f"[track] fetching {p['id']} ({p.get('name') or '?'})",
                  file=sys.stderr)
        else:
            # One progress line per product so a long run shows its position.
            # Overwrite in place on a TTY; emit plain lines for log files.
            label = p.get("name") or p["id"]
            end = "\r" if sys.stderr.isatty() else "\n"
            print(f"[track] {i}/{total} {label[:50]}".ljust(70),
                  end=end, file=sys.stderr, flush=True)
        product, stores = await lidl.run_all(
            p["id"], token, seeds_path=seeds_path,
            concurrency=concurrency, verbose=verbose,
        )
        # Prefer the name given in products.json; otherwise use the title the
        # website returns for this article number. The main picture and product
        # page link come from the booklet scrape (products.json) and are carried
        # through unchanged.
        raw.append({"id": p["id"], "name": p.get("name") or product,
                    "image": p.get("image"), "url": p.get("url"),
                    "stores": stores})
        _save_checkpoint(checkpoint_path, products, raw)
    if not verbose and total and sys.stderr.isatty():
        print(f"[track] {total}/{total} done".ljust(70), file=sys.stderr)
    fetched_at = datetime.now(timezone.utc).astimezone().isoformat()
    return encode_snapshot(fetched_at, raw)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--products", default="products.json",
                    help="JSON file listing products (default: products.json)")
    ap.add_argument("--out-dir", default="data",
                    help="directory for snapshots (default: data)")
    ap.add_argument("--seeds", default="seeds.json",
                    help="covering seed set from `lidl.py discover`")
    ap.add_argument("--concurrency", type=int, default=1,
                    help="parallel websocket sessions (default: 1, i.e. fully "
                         "sequential to keep load on the Lidl site low)")
    ap.add_argument("--token", default=lidl.URL_TOKEN,
                    help="urlToken (auth credential)")
    ap.add_argument("--all-dates", action="store_true",
                    help="check every product, including offers that have not "
                         "started yet (érvényesség date in the future)")
    ap.add_argument("--no-resume", action="store_true",
                    help="ignore any saved progress and fetch every product "
                         "from scratch")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    products = load_products(args.products)
    if not products:
        print(f"No products found in {args.products}", file=sys.stderr)
        sys.exit(1)

    if not args.all_dates:
        before = len(products)
        products = filter_current(products)
        skipped = before - len(products)
        if skipped:
            print(f"[track] skipping {skipped} product(s) whose offer has not "
                  f"started yet; {len(products)} remain", file=sys.stderr)
        if not products:
            print("No products with a started offer date; nothing to check.",
                  file=sys.stderr)
            sys.exit(0)

    os.makedirs(args.out_dir, exist_ok=True)
    checkpoint_path = os.path.join(args.out_dir, ".track-progress.json")
    if args.no_resume and os.path.exists(checkpoint_path):
        os.remove(checkpoint_path)

    snap = asyncio.run(snapshot(
        products, token=args.token, seeds_path=args.seeds,
        concurrency=args.concurrency, verbose=args.verbose,
        checkpoint_path=checkpoint_path,
    ))

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    snap_path = os.path.join(args.out_dir, f"availability-{stamp}.json")
    latest_path = os.path.join(args.out_dir, "latest.json")
    for path in (snap_path, latest_path):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False, indent=2)

    # The run completed and the snapshot is durable; drop the resume checkpoint
    # so the next run starts clean.
    if os.path.exists(checkpoint_path):
        os.remove(checkpoint_path)

    summary = ", ".join(
        f"{p['id']}: {p['in_stock_count']}/{p['store_count']}"
        for p in snap["products"]
    )
    print(f"{snap['fetched_at']}  ->  {snap_path}  ({summary})")


if __name__ == "__main__":
    main()
