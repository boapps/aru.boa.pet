#!/usr/bin/env python
"""Query Lidl non-food product stock availability via the Schwarz chatbot websocket.

The endpoint speaks Socket.IO v2 / Engine.IO v3 over a raw websocket. The flow
recorded in websocket_info.txt is a scripted chatbot conversation:

    1. STORE_NONFOOD_STOCK_CHECK           -> start the "check stock" intent
    2. <product id>                         -> e.g. 478580
    3. cIntent:check_availability| 0<id>    -> confirm the matched product
    4. <store>                              -> e.g. "Budapest 08"

The bot answers each step with a `42[...]` event; the final answer carries the
stock info.

NOTE: userId / sessionId / urlToken below were captured from a live browser
session and WILL expire. Re-record websocket_info.txt and update the constants
(or pass --user/--session/--token) when the bot stops responding.
"""

import argparse
import asyncio
import html
import json
import re
import secrets
import sys

from websockets.asyncio.client import connect

# --- Captured session credentials (refresh when expired) ---------------------
USER_ID = "USER_21a8685daf2e830a"
SESSION_ID = "SESSION_21a8685daf2e830a"
URL_TOKEN = "2cc23d08d3b54469b9c831c79494094383f19b16506d09bb111452e058a9cb10"

WS_URL = (
    "wss://endpoint-prod.scon.schwarz/socket.io/"
    "?userId={user}&sessionId={session}&urlToken={token}"
    "&testMode=false&EIO=3&transport=websocket"
)


def build_event(message, *, user, session, token, reset_flow=False):
    """Build a Socket.IO `42` event frame carrying a processInput request."""
    payload = {
        "URLToken": token,
        "userId": user,
        "sessionId": session,
        "channel": "socket-client",
        "source": "device",
        "resetFlow": reset_flow,
        "text": "",
        "data": {
            "version": "2.0",
            "user": {
                "userId": user,
                "sessionId": session,
                "timezone": "Europe/Budapest",
            },
            "locale": "hu-HU",
            "request": {
                "type": "TEXT",
                "message": message,
                "data": {
                    "topic": "product2",
                    "title": False,
                    "productId": False,
                    "customerData_en": {},
                    "ecommerceData_en": {},
                },
            },
        },
    }
    return "42" + json.dumps(["processInput", payload], ensure_ascii=False)


class LidlChat:
    """Drives the Engine.IO v3 chatbot conversation over a single websocket."""

    def __init__(self, ws, *, user, session, token, verbose=False):
        self.ws = ws
        self.user = user
        self.session = session
        self.token = token
        self.verbose = verbose
        self.inbox = asyncio.Queue()  # decoded Socket.IO event payloads (lists)
        self.ping_interval = 25.0
        self._connected = asyncio.Event()
        self._tasks = []

    async def __aenter__(self):
        self._tasks.append(asyncio.create_task(self._reader()))
        # Wait for the Socket.IO namespace connect (`40`) before sending events.
        await asyncio.wait_for(self._connected.wait(), timeout=15)
        self._tasks.append(asyncio.create_task(self._heartbeat()))
        return self

    async def __aexit__(self, *exc):
        for t in self._tasks:
            t.cancel()
        return False

    async def _reader(self):
        """Single consumer: handle Engine.IO frames, queue Socket.IO events."""
        async for raw in self.ws:
            msg = raw if isinstance(raw, str) else raw.decode("utf-8", "replace")
            if self.verbose:
                print(f"<< {msg}", file=sys.stderr)
            if not msg:
                continue
            # Engine.IO packet types.
            if msg[0] == "0":  # open
                try:
                    info = json.loads(msg[1:])
                    self.ping_interval = info.get("pingInterval", 25000) / 1000
                except (ValueError, AttributeError):
                    pass
            elif msg[0] == "2":  # server ping -> pong (EIO4-style, harmless on v3)
                await self.ws.send("3")
            elif msg[0] == "3":  # pong, ignore
                pass
            elif msg.startswith("40"):  # namespace connect
                self._connected.set()
            elif msg.startswith("42"):  # event (incl. the end-of-turn finalPing)
                try:
                    self.inbox.put_nowait(json.loads(msg[2:]))
                except ValueError:
                    pass
            elif msg.startswith("41"):  # namespace disconnect
                break

    async def _heartbeat(self):
        """Engine.IO v3 keep-alive: client sends ping (`2`) every interval."""
        while True:
            await asyncio.sleep(self.ping_interval)
            try:
                await self.ws.send("2")
            except Exception:
                return

    async def send(self, message, *, reset_flow=False):
        event = build_event(
            message,
            user=self.user,
            session=self.session,
            token=self.token,
            reset_flow=reset_flow,
        )
        if self.verbose:
            print(f">> {message}", file=sys.stderr)
        await self.ws.send(event)

    async def turn(self, message, *, reset_flow=False, idle=4.0, overall=25.0):
        """Send one message and collect the bot's reply up to its `finalPing`."""
        await self.send(message, reset_flow=reset_flow)
        events = []
        deadline = asyncio.get_event_loop().time() + overall
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                break
            try:
                event = await asyncio.wait_for(
                    self.inbox.get(), timeout=min(idle, remaining)
                )
            except asyncio.TimeoutError:
                break
            events.append(event)
            # `42["finalPing", ...]` signals the bot finished this turn.
            if isinstance(event, list) and event and event[0] == "finalPing":
                break
        return events


def iter_responses(events):
    """Yield each response object from `42["output", {...}]` bot events."""
    for ev in events:
        if not (isinstance(ev, list) and len(ev) >= 2 and ev[0] == "output"):
            continue
        responses = ev[1].get("data", {}).get("response", [])
        if isinstance(responses, list):
            yield from responses


def parse_turn(events):
    """Summarize a bot turn: free text, quick-reply values, and card items."""
    texts, quick_replies, cards = [], [], []
    for resp in iter_responses(events):
        rtype = resp.get("type")
        if rtype == "TEXT":
            msg = resp.get("message")
            if isinstance(msg, str) and msg.strip():
                texts.append(msg.strip())
            for qr in resp.get("quickReplies", []) or []:
                if isinstance(qr, dict) and qr.get("value"):
                    quick_replies.append((qr.get("label", ""), qr["value"]))
        elif rtype == "CARD":
            for item in resp.get("data", {}).get("items", []) or []:
                buttons = [
                    b.get("value")
                    for b in item.get("buttons", []) or []
                    if isinstance(b, dict) and b.get("value")
                ]
                cards.append(
                    {
                        "title": item.get("title", ""),
                        "description": item.get("description", ""),
                        "buttons": buttons,
                        "figure": item.get("figure", ""),  # e.g. distance "1.28 km"
                    }
                )
    return {"texts": texts, "quick_replies": quick_replies, "cards": cards}


def find_product_button(cards, product_id):
    """Return the check-availability postback for the card matching product_id."""
    pid = str(product_id).lstrip("0")
    for card in cards:
        desc = card["description"]
        # Match on the article number printed in the card ("Cikkszám: 478580").
        if f"ikkszám: {pid}" in desc or f"ikkszám: {product_id}" in desc:
            if card["buttons"]:
                return card["buttons"][0]
    # Fallback: a button whose value embeds the (zero-padded) article number.
    for card in cards:
        for value in card["buttons"]:
            if pid in value:
                return value
    return None


def parse_stock(events):
    """Parse the per-store availability carousel into structured records.

    Returns (product_name, [ {status, emoji, address, open, distance}, ... ]).
    """
    product = None
    stores = []
    for resp in iter_responses(events):
        if resp.get("type") != "CARD":
            continue
        for item in resp.get("data", {}).get("items", []) or []:
            desc = item.get("description", "")
            if "üzletben" not in desc:  # not a store-availability card
                continue
            text = html.unescape(desc)

            m = re.search(r"a\(az\)\*\*(.+?)\*\*\s*az általad", text)
            if m and not product:
                product = m.group(1).strip()

            m = re.search(r"üzletben:\s*(\S+)?\s*\*\*(.+?)\*\*", text)
            emoji = (m.group(1) or "").strip() if m else ""
            status = m.group(2).strip() if m else ""

            if "nyitva van" in text:
                is_open = "open"
            elif "zárva van" in text:
                is_open = "closed"
            else:
                is_open = ""

            # Address sits between the status bold block and the open/closed
            # bold block, e.g. "...**Magas...**\n Ferenciek tere 2 \n 1053 Budapest\n**Az üzlet...".
            address = ""
            a = re.search(r"üzletben:.*?\*\*.*?\*\*(.*?)\*\*Az üzlet", text, re.S)
            if a:
                address = " ".join(
                    ln.strip() for ln in a.group(1).splitlines() if ln.strip()
                )

            # Split into street / postcode / city. A Hungarian postcode is the
            # 4-digit group immediately followed by the city name at the END of
            # the string — anchoring there avoids matching street or land-registry
            # (HRSZ) numbers like "Dózsa György út 3152/1 HRSZ 2096 Üröm".
            pm = re.search(r"\b(\d{4})\s+([^\d]+)$", address)
            postcode = pm.group(1) if pm else ""
            city = pm.group(2).strip() if pm else ""
            street = address[: pm.start()].strip() if pm else address

            # The card's static-map URL embeds the store's coordinates:
            # ".../staticmap?center=47.50237,19.05319&zoom=...".
            cm = re.search(r"center=(-?\d+\.\d+),(-?\d+\.\d+)", item.get("image", ""))
            lat = float(cm.group(1)) if cm else None
            lon = float(cm.group(2)) if cm else None

            stores.append(
                {
                    "emoji": emoji,
                    "status": status,
                    "address": address,
                    "street": street,
                    "postcode": postcode,
                    "city": city,
                    "lat": lat,
                    "lon": lon,
                    "open": is_open,
                    "distance": item.get("figure", ""),
                }
            )
    return product, stores


def format_stock(product, stores):
    """Render parsed stock records as readable lines."""
    lines = []
    if product:
        lines.append(f"Termék: {product}")
        lines.append("")
    for s in stores:
        addr = s["address"] or "(ismeretlen cím)"
        bits = [f"{s['emoji']} {s['status']}".strip(), addr]
        meta = " · ".join(x for x in (s["distance"], s["open"]) if x)
        if meta:
            bits.append(f"({meta})")
        lines.append("  ".join(b for b in bits if b))
    return "\n".join(lines)


def extract_text(events):
    """Pull human-readable text out of the bot's processInput responses."""
    texts = []

    def walk(obj):
        if isinstance(obj, dict):
            for key in ("text", "message", "title", "value"):
                v = obj.get(key)
                if isinstance(v, str) and v.strip():
                    texts.append(v.strip())
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)

    for ev in events:
        walk(ev)
    # Dedupe while preserving order.
    seen, out = set(), []
    for t in texts:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


async def check_stock(product_id, store, *, user, session, token, verbose=False, raw=False):
    """Drive the chatbot conversation and return the per-store stock answer.

    On a fresh session the flow is deterministic:
        1. STORE_NONFOOD_STOCK_CHECK   start the stock-check intent
        2. Middle of Lidl              select the non-food catalog
        3. <article number>            search -> bot shows the product card(s)
        4. <card's check button>       request availability -> bot asks the city
        5. <store>                     -> bot returns the per-store stock carousel

    Card matching (step 4) is done by article number so a fuzzy multi-result
    search still picks the right product.
    """
    url = WS_URL.format(user=user, session=session, token=token)
    all_events = []

    async def run(chat, message, **kw):
        events = await chat.turn(message, **kw)
        all_events.extend(events)
        return events

    async with connect(url) as ws:
        async with LidlChat(
            ws, user=user, session=session, token=token, verbose=verbose
        ) as chat:
            await run(chat, "STORE_NONFOOD_STOCK_CHECK", reset_flow=True)
            await run(chat, "Middle of Lidl")
            search = parse_turn(await run(chat, str(product_id)))

            button = find_product_button(search["cards"], product_id)
            if button is None:
                # No matching product card — return whatever the bot said.
                if raw:
                    return all_events
                return {
                    "product": None,
                    "stores": [],
                    "messages": extract_text(all_events),
                }

            await run(chat, button)   # request availability -> "which city?"
            await run(chat, store)    # -> per-store stock carousel

    if raw:
        return all_events
    product, stores = parse_stock(all_events)
    return {
        "product": product,
        "stores": stores,
        "messages": extract_text(all_events) if not stores else [],
    }


# --- Bulk / national coverage -----------------------------------------------

# Seed locations to bootstrap discovery: a fresh session geocodes free text, so
# major cities + Budapest districts spread the initial probes across the country.
# Discovery then expands from the postcodes of whatever stores these surface.
SEED_CITIES = [
    "Debrecen", "Szeged", "Miskolc", "Pécs", "Győr", "Nyíregyháza",
    "Kecskemét", "Székesfehérvár", "Szombathely", "Szolnok", "Tatabánya",
    "Kaposvár", "Békéscsaba", "Eger", "Zalaegerszeg", "Sopron", "Veszprém",
    "Nagykanizsa", "Dunaújváros", "Salgótarján", "Hódmezővásárhely", "Cegléd",
    "Baja", "Kazincbarcika", "Ózd", "Mosonmagyaróvár", "Pápa", "Gyula",
    "Esztergom", "Kiskunfélegyháza", "Hajdúböszörmény", "Siófok",
] + [f"Budapest {d:02d}" for d in range(1, 24)]


async def fetch_location(product_id, location, *, token, sem, verbose=False, retries=2):
    """Fetch one location's stock in its own fresh session (concurrency-safe).

    Retries on error or an empty result, since a transient failure on a seed that
    uniquely covers some stores would otherwise drop them from the bulk result.
    """
    async with sem:
        for attempt in range(retries + 1):
            user = "USER_" + secrets.token_hex(8)
            session = "SESSION_" + secrets.token_hex(8)
            try:
                res = await check_stock(
                    product_id, location, user=user, session=session,
                    token=token, verbose=verbose,
                )
                stores = res.get("stores", [])
                if stores or attempt == retries:
                    return location, (stores, res.get("product"))
            except Exception as exc:  # noqa: BLE001 - one bad seed shouldn't abort
                if verbose:
                    print(f"[fetch] {location!r} attempt {attempt} failed: {exc}",
                          file=sys.stderr)
                if attempt == retries:
                    return location, ([], None)
        return location, ([], None)


async def fetch_many(product_id, locations, *, token, concurrency=6, verbose=False):
    """Fetch many locations concurrently; returns {location: (stores, product)}."""
    sem = asyncio.Semaphore(concurrency)
    results = await asyncio.gather(
        *(
            fetch_location(product_id, loc, token=token, sem=sem, verbose=verbose)
            for loc in locations
        )
    )
    return dict(results)


async def discover_stores(product_id, *, token, concurrency=6, verbose=False):
    """Self-expanding sweep: find every store and which seed covers it.

    A store always appears in the nearest-N of its own postcode (distance ~0),
    so we BFS over postcodes until no new store surfaces. Returns
    (stores_by_postcode, coverage) where coverage maps each queried seed to the
    set of store keys it returned.
    """
    stores = {}        # store_key -> store record
    coverage = {}      # seed -> set(store_key)
    queried = set()
    frontier = list(dict.fromkeys(SEED_CITIES))
    rounds = 0

    while frontier:
        rounds += 1
        batch = [s for s in frontier if s not in queried]
        if not batch:
            break
        print(f"[discover] round {rounds}: {len(batch)} seeds, "
              f"{len(stores)} stores so far", file=sys.stderr)
        results = await fetch_many(
            product_id, batch, token=token, concurrency=concurrency, verbose=verbose
        )
        for seed in batch:
            queried.add(seed)
            found = results.get(seed, ([], None))[0]
            coverage[seed] = set()
            for st in found:
                key = store_key(st)
                if not key:
                    continue
                coverage[seed].add(key)
                stores.setdefault(key, st)

        # New frontier: postcodes of discovered stores we haven't queried yet.
        frontier = [
            st["postcode"]
            for st in stores.values()
            if st["postcode"] and st["postcode"] not in queried
        ]
        frontier = list(dict.fromkeys(frontier))

    return stores, coverage


def store_key(store):
    """Stable identity for a store: postcode + street (falls back to address)."""
    if store.get("postcode") and store.get("street"):
        return f"{store['postcode']} {store['street']}"
    return store.get("address", "")


def greedy_cover(coverage, all_keys):
    """Greedy set-cover: fewest seeds whose returned stores cover all_keys."""
    uncovered = set(all_keys)
    sets = {seed: set(keys) for seed, keys in coverage.items()}
    chosen = []
    while uncovered:
        best = max(
            sets, key=lambda s: len(sets[s] & uncovered), default=None
        )
        if best is None or not (sets[best] & uncovered):
            break  # remaining stores unreachable by any seed (shouldn't happen)
        chosen.append(best)
        uncovered -= sets[best]
    return chosen, uncovered


async def run_discover(token, *, out_dir, concurrency, verbose):
    stores, coverage = await discover_stores(
        "478580", token=token, concurrency=concurrency, verbose=verbose
    )
    all_keys = set(stores)
    seeds, uncovered = greedy_cover(coverage, all_keys)

    store_list = sorted(stores.values(), key=lambda s: (s["postcode"], s["street"]))
    stores_path = f"{out_dir}/stores.json"
    seeds_path = f"{out_dir}/seeds.json"
    with open(stores_path, "w", encoding="utf-8") as f:
        json.dump(store_list, f, ensure_ascii=False, indent=2)
    with open(seeds_path, "w", encoding="utf-8") as f:
        json.dump(seeds, f, ensure_ascii=False, indent=2)

    print(f"\nDiscovered {len(stores)} stores.")
    print(f"Minimal covering seed set: {len(seeds)} seeds "
          f"(from {len(coverage)} queried).")
    if uncovered:
        print(f"WARNING: {len(uncovered)} stores not covered by any single seed; "
              f"include their postcodes directly.", file=sys.stderr)
    print(f"Wrote {stores_path} and {seeds_path}.")


def _merge_into(merged, results):
    """Merge fetched stores into `merged`; return the product title if seen."""
    product = None
    for stores, prod in results.values():
        for st in stores:
            key = store_key(st)
            if key and key not in merged:
                merged[key] = st
        if prod and not product:
            product = prod
    return product


async def run_all(product_id, token, *, seeds_path, concurrency, verbose):
    with open(seeds_path, encoding="utf-8") as f:
        seeds = json.load(f)

    merged = {}
    results = await fetch_many(
        product_id, seeds, token=token, concurrency=concurrency, verbose=verbose
    )
    product = _merge_into(merged, results)

    # Gap-fill: any store from the known list (stores.json) we didn't see gets
    # queried by its OWN postcode, which always returns it (distance ~0). This
    # makes the bulk fetch complete even if a covering seed came back short.
    # Skip it entirely when nothing was found at all: a valid product always
    # comes back from every seed (even as "no non-food sales"), so an empty
    # result means the article id is unknown/unavailable — gap-filling would
    # then pointlessly brute-force every postcode.
    seeds_dir = seeds_path.rsplit("/", 1)[0] if "/" in seeds_path else "."
    try:
        with open(f"{seeds_dir}/stores.json", encoding="utf-8") as f:
            expected = json.load(f)
    except FileNotFoundError:
        expected = []

    for _ in range(2 if merged else 0):
        missing = [s for s in expected if store_key(s) not in merged]
        if not missing:
            break
        postcodes = list(dict.fromkeys(
            s["postcode"] for s in missing if s["postcode"]
        ))
        if verbose:
            print(f"[all] gap-filling {len(missing)} stores via "
                  f"{len(postcodes)} postcodes", file=sys.stderr)
        results = await fetch_many(
            product_id, postcodes, token=token,
            concurrency=concurrency, verbose=verbose,
        )
        product = product or _merge_into(merged, results)

    still_missing = [s for s in expected if store_key(s) not in merged]
    if still_missing and verbose:
        print(f"[all] {len(still_missing)} known stores still missing",
              file=sys.stderr)

    out = sorted(merged.values(), key=lambda s: (s["city"], s["street"]))
    return product, out


async def run_geo(token, *, seeds_path, out_dir, concurrency, verbose):
    """One nationwide pass to capture each store's coordinates + address.

    Coordinates come embedded in every availability card, so they are
    product-independent; we just use a known article number to enumerate stores.
    Writes store_coords.json and back-fills lat/lon into stores.json.
    """
    _, stores = await run_all(
        "478580", token, seeds_path=seeds_path,
        concurrency=concurrency, verbose=verbose,
    )
    coords = [
        {
            "address": s["address"],
            "street": s["street"],
            "postcode": s["postcode"],
            "city": s["city"],
            "lat": s["lat"],
            "lon": s["lon"],
        }
        for s in sorted(stores, key=lambda s: (s["postcode"], s["street"]))
    ]
    coords_path = f"{out_dir}/store_coords.json"
    with open(coords_path, "w", encoding="utf-8") as f:
        json.dump(coords, f, ensure_ascii=False, indent=2)

    # Back-fill coordinates into the existing store catalogue, if present.
    catalog_path = f"{out_dir}/stores.json"
    try:
        with open(catalog_path, encoding="utf-8") as f:
            catalog = json.load(f)
        by_key = {store_key(s): s for s in stores}
        for entry in catalog:
            match = by_key.get(store_key(entry))
            if match:
                entry["lat"], entry["lon"] = match["lat"], match["lon"]
        with open(catalog_path, "w", encoding="utf-8") as f:
            json.dump(catalog, f, ensure_ascii=False, indent=2)
    except FileNotFoundError:
        pass

    missing = sum(1 for c in coords if c["lat"] is None)
    print(f"Wrote {coords_path} with {len(coords)} stores"
          + (f" ({missing} without coordinates)." if missing else "."))


def main():
    ap = argparse.ArgumentParser(description="Check Lidl non-food product stock.")
    ap.add_argument("product_id", nargs="?",
                    help="article number (e.g. 478580), or 'discover'")
    ap.add_argument("store", nargs="?",
                    help='store query, e.g. "Budapest 08" (omit with --all)')
    ap.add_argument("--all", action="store_true",
                    help="check every store nationwide (uses seeds.json)")
    ap.add_argument("--seeds", default="seeds.json",
                    help="covering seed set for --all (default: seeds.json)")
    ap.add_argument("--out-dir", default=".",
                    help="output dir for 'discover' (default: .)")
    ap.add_argument("--concurrency", type=int, default=6,
                    help="parallel websocket sessions for --all/discover")
    ap.add_argument("--user", help="override userId (default: fresh random)")
    ap.add_argument("--session", help="override sessionId (default: fresh random)")
    ap.add_argument("--token", default=URL_TOKEN, help="urlToken (auth credential)")
    ap.add_argument("-v", "--verbose", action="store_true", help="log raw frames")
    ap.add_argument("--raw", action="store_true", help="print raw JSON events")
    ap.add_argument("--json", action="store_true", help="print parsed result as JSON")
    args = ap.parse_args()

    # Mode: discover ---------------------------------------------------------
    if args.product_id == "discover":
        asyncio.run(run_discover(
            args.token, out_dir=args.out_dir,
            concurrency=args.concurrency, verbose=args.verbose,
        ))
        return

    # Mode: build store coordinates -----------------------------------------
    if args.product_id == "geo":
        asyncio.run(run_geo(
            args.token, seeds_path=args.seeds, out_dir=args.out_dir,
            concurrency=args.concurrency, verbose=args.verbose,
        ))
        return

    if not args.product_id:
        ap.error("product_id is required (or use 'discover')")

    # Mode: nationwide -------------------------------------------------------
    if args.all:
        _, stores = asyncio.run(run_all(
            args.product_id, args.token, seeds_path=args.seeds,
            concurrency=args.concurrency, verbose=args.verbose,
        ))
        if args.json:
            print(json.dumps(stores, ensure_ascii=False, indent=2))
            return
        in_stock = [s for s in stores if s["emoji"] in ("🟢", "🟡")]
        print(f"{len(stores)} stores total · {len(in_stock)} with stock\n")
        for s in stores:
            loc = f"{s['street']}, {s['postcode']} {s['city']}".strip(", ")
            print(f"{s['emoji']} {s['status']:<32} {loc}")
        return

    # Mode: single store -----------------------------------------------------
    if not args.store:
        ap.error("store is required (or use --all)")

    # A fresh user/session per run isolates server-side conversation state, which
    # the bot keys to sessionId and otherwise leaks between invocations.
    user = args.user or "USER_" + secrets.token_hex(8)
    session = args.session or "SESSION_" + secrets.token_hex(8)

    result = asyncio.run(
        check_stock(
            args.product_id,
            args.store,
            user=user,
            session=session,
            token=args.token,
            verbose=args.verbose,
            raw=args.raw,
        )
    )

    if args.raw or args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    if result["stores"]:
        print(format_stock(result["product"], result["stores"]))
    elif result["messages"]:
        # No stock card — surface whatever the bot said (likely an error/expiry).
        for line in result["messages"]:
            print(line)
        print("\n(No stock data — token may have expired, or no match.)",
              file=sys.stderr)
        sys.exit(1)
    else:
        print("No response (session token may have expired — re-record).",
              file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
