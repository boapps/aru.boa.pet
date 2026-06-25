#!/usr/bin/env python
"""Download Lidl HU "Nonfood" ad booklets and harvest their product validity.

Lidl HU publishes ad booklets ("akciós újság") through the Schwarz leaflet
platform. This script:

  1. Reads the booklet overview at lidl.hu and keeps the ones whose title
     contains "Nonfood".
  2. For each Nonfood booklet, fetches its leaflet metadata (PDF url + dates)
     and downloads the PDF -- but only if we don't already have it, so reruns
     just pick up newly published booklets.
  3. Extracts candidate product article numbers from the PDF text with the
     regex \\d{6}.
  4. Confirms each candidate against the site search
     (https://www.lidl.hu/q/search?q=<id>) and reads its offer validity, e.g.
     "Ajánlat érvényes: 06.25." -- the érvényesség. Six-digit numbers that are
     not real articles (prices, sizes, ...) don't return an exact match and are
     dropped.
  5. Writes one JSON file per booklet under data/booklets/ with the products
     and their érvényesség, and regenerates products.json (the tracker input)
     so track.py can skip offers whose date has already passed.

Usage:
    python3 booklets.py                  # download + parse all Nonfood booklets
    python3 booklets.py -v               # verbose progress
    python3 booklets.py --limit 5        # only resolve the first 5 ids (debug)
    python3 booklets.py --keep-pdf       # also keep PDFs we just downloaded

Prereqs: `requests`, and either the `pdftotext` binary (poppler) or `pymupdf`.
"""

import argparse
import datetime
import html
import json
import os
import re
import subprocess
import sys
import time

import requests

OVERVIEW_URL = "https://www.lidl.hu/c/szorolap/s10013623"
FLYER_API = "https://endpoints.leaflets.schwarz/v4/flyer"
SEARCH_URL = "https://www.lidl.hu/q/search"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"
)

DATA_DIR = "data"
BOOKLET_DIR = os.path.join(DATA_DIR, "booklets")
PDF_DIR = os.path.join(BOOKLET_DIR, "pdfs")
PRODUCTS_FILE = "products.json"

# Exactly six digits, not embedded in a longer run of digits.
ARTICLE_RE = re.compile(r"(?<!\d)\d{6}(?!\d)")
# "Ajánlat érvényes: 06.25." and similar.
VALID_TEXT_RE = re.compile(r"(?:rvényes|rvényes)[^0-9]*(\d{2})\.(\d{2})\.")
# Main product image and the product's page path, both carried by the same
# product object the search result returns for an exact article match.
IMAGE_RE = re.compile(r'"image":"(https://[^"\\]+)"')
CANONICAL_RE = re.compile(r'"canonicalUrl":"(/[^"\\]+)"')
SITE_BASE = "https://www.lidl.hu"


def session():
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    s.headers["Accept-Language"] = "hu-HU,hu;q=0.9"
    return s


# --------------------------------------------------------------------------
# Booklet discovery
# --------------------------------------------------------------------------

FLYER_BLOCK_RE = re.compile(
    r'<a href="(?P<href>[^"]+)"\s+target="_self"\s+class="flyer"\s+'
    r'id="flyer-(?P<uuid>[0-9a-f-]+)"(?P<body>.*?)</a>',
    re.S,
)
NAME_RE = re.compile(r'flyer__name">\s*(.+?)\s*<', re.S)
TITLE_RE = re.compile(r'flyer__title">\s*(.+?)\s*<', re.S)


def discover_nonfood_booklets(s, verbose=False):
    """Return [{uuid, title, name, href}] for booklets whose title is Nonfood."""
    r = s.get(OVERVIEW_URL, timeout=30)
    r.raise_for_status()
    page = r.text
    booklets = []
    for m in FLYER_BLOCK_RE.finditer(page):
        body = m.group("body")
        title = TITLE_RE.search(body)
        name = NAME_RE.search(body)
        title = html.unescape(title.group(1)).strip() if title else ""
        name = html.unescape(name.group(1)).strip() if name else ""
        if "nonfood" not in title.lower():
            continue
        booklets.append(
            {
                "uuid": m.group("uuid"),
                "title": title,
                "name": name,
                "href": html.unescape(m.group("href")),
            }
        )
    if verbose:
        print(f"[booklets] {len(booklets)} Nonfood booklet(s) on the overview",
              file=sys.stderr)
    return booklets


def fetch_flyer_meta(s, uuid):
    """Fetch leaflet metadata for a booklet: pdf url, title, dates."""
    r = s.get(FLYER_API, params={"flyer_identifier": uuid}, timeout=30)
    r.raise_for_status()
    flyer = r.json().get("flyer", {})
    return {
        "uuid": uuid,
        "title": flyer.get("title"),
        "name": flyer.get("name"),
        "pdf_url": flyer.get("pdfUrl") or flyer.get("hiResPdfUrl"),
        "start_date": flyer.get("startDate"),
        "end_date": flyer.get("endDate"),
        "offer_start_date": flyer.get("offerStartDate"),
        "offer_end_date": flyer.get("offerEndDate"),
    }


def download_pdf(s, meta, verbose=False):
    """Download the booklet PDF if not already present. Returns the path."""
    os.makedirs(PDF_DIR, exist_ok=True)
    path = os.path.join(PDF_DIR, f"{meta['uuid']}.pdf")
    if os.path.exists(path) and os.path.getsize(path) > 0:
        if verbose:
            print(f"[booklets] have {meta['uuid']}.pdf already", file=sys.stderr)
        return path, False
    if not meta.get("pdf_url"):
        return None, False
    if verbose:
        print(f"[booklets] downloading {meta['title']!r}", file=sys.stderr)
    with s.get(meta["pdf_url"], timeout=120, stream=True) as r:
        r.raise_for_status()
        tmp = path + ".part"
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(65536):
                f.write(chunk)
        os.replace(tmp, path)
    return path, True


# --------------------------------------------------------------------------
# PDF -> article numbers
# --------------------------------------------------------------------------

def pdf_text(path):
    """Extract all text from a PDF, preferring poppler's pdftotext."""
    try:
        out = subprocess.run(
            ["pdftotext", "-q", path, "-"],
            capture_output=True, check=True,
        )
        return out.stdout.decode("utf-8", "replace")
    except (FileNotFoundError, subprocess.CalledProcessError):
        pass
    try:
        import fitz  # pymupdf
    except ImportError:
        raise RuntimeError(
            "need the `pdftotext` binary or the `pymupdf` package to read PDFs")
    doc = fitz.open(path)
    return "".join(page.get_text() for page in doc)


def article_ids(path):
    """Return the set of distinct six-digit candidate article numbers."""
    return set(ARTICLE_RE.findall(pdf_text(path)))


# --------------------------------------------------------------------------
# Site search -> érvényesség
# --------------------------------------------------------------------------

def _match_object(text, brace_pos):
    """Return the balanced {...} substring starting at brace_pos (string-aware)."""
    depth = 0
    in_str = False
    esc = False
    for k in range(brace_pos, len(text)):
        c = text[k]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return text[brace_pos:k + 1]
    return None


def _resolve_year(month, day, context_dates):
    """Pick the calendar year for a bare MM.DD. using nearby booklet dates."""
    for d in context_dates:
        if d:
            try:
                y = int(d[:4])
                # A December offer listed in a January booklet etc.
                if month == 12 and d[5:7] == "01":
                    y -= 1
                return y
            except (ValueError, IndexError):
                pass
    today = datetime.date.today()
    return today.year


def lookup_validity(s, article_id, context_dates=(), verbose=False):
    """Search the site for an article number; return its érvényesség or None.

    Returns {id, name, valid_from_text, valid_from} when `article_id` is a real
    article (the search returns it as an exact `ians` match), else None.
    """
    r = None
    for attempt in range(3):
        try:
            r = s.get(SEARCH_URL, params={"q": article_id}, timeout=30)
            break
        except requests.RequestException as e:
            if attempt == 2:
                if verbose:
                    print(f"[booklets]   {article_id}: request failed ({e})",
                          file=sys.stderr)
                return None
            time.sleep(1 + attempt)
    if r is None or r.status_code != 200:
        return None
    page = html.unescape(r.text)

    marker = f'"ians":["{article_id}"]'
    pos = page.find(marker)
    if pos == -1:
        # Not a real article (prices, sizes, etc.) -> the site returns generic
        # recommendations rather than this exact id.
        return None

    name = None
    m = re.search(r'"fullTitle":"(.*?)"', page[pos:pos + 4000])
    if m:
        name = m.group(1)

    # Main picture: the product object's "image" url sits right after the ians
    # marker. The product page url ("canonicalUrl", a /p/... path) sits just
    # before it, so take the closest match preceding the marker.
    image = None
    m = IMAGE_RE.search(page, pos, pos + 4000)
    if m:
        image = m.group(1)

    url = None
    last = None
    for last in CANONICAL_RE.finditer(page, max(0, pos - 2000), pos):
        pass
    if last:
        url = SITE_BASE + last.group(1)

    valid_text = valid_from = None
    sa = page.find('"stockAvailability":', pos)
    nxt = page.find('"ians":[', pos + len(marker))
    if sa != -1 and (nxt == -1 or sa < nxt):
        brace = page.index("{", sa)
        try:
            stock = json.loads(_match_object(page, brace))
        except (ValueError, TypeError):
            stock = {}
        # Prefer the exact epoch from badgeInfoV2; fall back to badge text.
        for grp in stock.get("badgeInfoV2") or []:
            for badge in grp.get("badges") or []:
                if "rvényes" in badge.get("text", ""):
                    valid_text = badge["text"]
            if grp.get("validFrom"):
                valid_from = (
                    datetime.datetime.fromtimestamp(grp["validFrom"]).date().isoformat()
                )
        if not valid_text:
            for badge in (stock.get("badgeInfo") or {}).get("badges") or []:
                if "rvényes" in badge.get("text", ""):
                    valid_text = badge["text"]

    if valid_text and not valid_from:
        m = VALID_TEXT_RE.search(valid_text)
        if m:
            month, day = int(m.group(1)), int(m.group(2))
            year = _resolve_year(month, day, context_dates)
            try:
                valid_from = datetime.date(year, month, day).isoformat()
            except ValueError:
                pass

    return {
        "id": article_id,
        "name": name,
        "image": image,
        "url": url,
        "valid_from_text": valid_text,
        "valid_from": valid_from,
    }


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

def write_booklet(meta, products):
    os.makedirs(BOOKLET_DIR, exist_ok=True)
    fetched_at = datetime.datetime.now().astimezone().isoformat()
    record = {
        "uuid": meta["uuid"],
        "title": meta["title"],
        "name": meta["name"],
        "pdf_url": meta.get("pdf_url"),
        "start_date": meta.get("start_date"),
        "end_date": meta.get("end_date"),
        "fetched_at": fetched_at,
        "product_count": len(products),
        "products": products,
    }
    path = os.path.join(BOOKLET_DIR, f"{meta['uuid']}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False, indent=2)
    return path


def rebuild_products_file(all_booklets):
    """Regenerate products.json: existing manual entries + all booklet products.

    Each product carries `valid_from` (the érvényesség date) and `valid_until`
    (the booklet end date) so track.py can drop offers whose date has passed.
    Manual entries already in products.json are preserved (kept date-less, so
    the tracker always checks them).
    """
    merged = {}

    # Preserve whatever is already there (manual ids or earlier runs).
    if os.path.exists(PRODUCTS_FILE):
        try:
            with open(PRODUCTS_FILE, encoding="utf-8") as f:
                existing = json.load(f)
            if isinstance(existing, dict):
                existing = existing.get("products", [])
            for e in existing:
                if isinstance(e, str):
                    merged[e] = {"id": e, "name": None}
                elif isinstance(e, dict) and e.get("id"):
                    merged[str(e["id"])] = dict(e, id=str(e["id"]))
        except (ValueError, OSError):
            pass

    # Layer booklet products on top; a later (newer) booklet wins on dupes.
    for booklet in all_booklets:
        for p in booklet["products"]:
            pid = p["id"]
            prev = merged.get(pid, {})
            merged[pid] = {
                "id": pid,
                "name": p.get("name") or prev.get("name"),
                "image": p.get("image") or prev.get("image"),
                "url": p.get("url") or prev.get("url"),
                "valid_from": p.get("valid_from") or prev.get("valid_from"),
                "valid_from_text": p.get("valid_from_text")
                or prev.get("valid_from_text"),
                "valid_until": booklet["meta"].get("end_date")
                or prev.get("valid_until"),
                "booklet": booklet["meta"].get("title"),
            }

    products = sorted(merged.values(), key=lambda p: p["id"])
    with open(PRODUCTS_FILE, "w", encoding="utf-8") as f:
        json.dump(products, f, ensure_ascii=False, indent=2)
    return products


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--limit", type=int, default=0,
                    help="resolve at most N article ids per booklet (debug)")
    ap.add_argument("--delay", type=float, default=0.3,
                    help="seconds to wait between site searches (default 0.3)")
    ap.add_argument("--keep-pdf", action="store_true",
                    help="keep downloaded PDFs (default: keep them anyway)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    s = session()
    booklets = discover_nonfood_booklets(s, verbose=args.verbose)
    if not booklets:
        print("No Nonfood booklets found on the overview page.", file=sys.stderr)
        sys.exit(1)

    # Cache lookups so an id shared by two booklets is searched only once.
    validity_cache = {}
    results = []

    for b in booklets:
        meta = fetch_flyer_meta(s, b["uuid"])
        # Prefer the overview title/name (already localized) if the API lacks them.
        meta["title"] = meta.get("title") or b["title"]
        meta["name"] = meta.get("name") or b["name"]

        path, _ = download_pdf(s, meta, verbose=args.verbose)
        if not path:
            print(f"[booklets] no PDF for {meta['title']!r}, skipping",
                  file=sys.stderr)
            continue

        ids = sorted(article_ids(path))
        if args.limit:
            ids = ids[:args.limit]
        if args.verbose:
            print(f"[booklets] {meta['title']!r}: {len(ids)} candidate ids",
                  file=sys.stderr)

        context_dates = (meta.get("start_date"), meta.get("end_date"))
        products = []
        for i, aid in enumerate(ids):
            if aid in validity_cache:
                product = validity_cache[aid]
            else:
                product = lookup_validity(
                    s, aid, context_dates=context_dates, verbose=args.verbose)
                validity_cache[aid] = product
                if args.delay:
                    time.sleep(args.delay)
            if product:
                products.append(product)
                if args.verbose:
                    print(f"[booklets]   {aid}: {product.get('valid_from_text')}"
                          f" -> {product.get('valid_from')}", file=sys.stderr)

        out = write_booklet(meta, products)
        confirmed = sum(1 for p in products if p.get("valid_from"))
        print(f"{meta['title']}: {len(products)} products "
              f"({confirmed} with a date)  ->  {out}")
        results.append({"meta": meta, "products": products})

    if results:
        merged = rebuild_products_file(results)
        print(f"products.json updated: {len(merged)} products total")


if __name__ == "__main__":
    main()
