import argparse
import json
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yaml
from playwright.sync_api import sync_playwright

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)


def require_env(name):
    value = os.getenv(name, "").strip()
    if not value:
        logging.error(
            "Missing required environment variable %s. Set it in your "
            "orchestrator (e.g. Dockhand's env section).",
            name,
        )
        raise SystemExit(1)
    return value


PRODUCTS_PATH = Path(os.getenv("PRODUCTS_PATH", "/config/products.yml"))
STATE_PATH = Path(os.getenv("STATE_PATH", "/data/state.json"))

NTFY_URL = require_env("NTFY_URL").rstrip("/")
NTFY_TOPIC = require_env("NTFY_TOPIC")
NTFY_TOKEN = os.getenv("NTFY_TOKEN", "")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL_SECONDS", "21600"))
TIMEOUT = int(os.getenv("REQUEST_TIMEOUT_SECONDS", "30"))
REQUEST_DELAY = float(os.getenv("REQUEST_DELAY_SECONDS", "3"))
TOP_DEALS = int(os.getenv("TOP_DEALS", "3"))
TIMEZONE = os.getenv("TIMEZONE", "Europe/Istanbul")
PAGE_LOAD_TIMEOUT = int(os.getenv("PAGE_LOAD_TIMEOUT_SECONDS", "60000"))
USER_AGENT = os.getenv(
    "USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
)

CHALLENGE_MARKERS = (
    "just a moment",
    "attention required",
    "cf-mitigated",
    "captcha",
    "güvenlik",
)


def page_has_challenge(html):
    low = html.lower()
    return any(marker in low for marker in CHALLENGE_MARKERS)


def load_products():
    with PRODUCTS_PATH.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    products = data.get("products") or []
    if not isinstance(products, list):
        raise ValueError("products must be a YAML list")

    seen = set()
    for product in products:
        for key in ("id", "name", "url"):
            if not product.get(key):
                raise ValueError(f"Product missing required field: {key}")
        if product["id"] in seen:
            raise ValueError(f"Duplicate product id: {product['id']}")
        seen.add(product["id"])

    return products


def load_state():
    if not STATE_PATH.exists():
        return {}
    try:
        with STATE_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        logging.exception("Could not read state file, starting fresh")
        return {}


def save_state(state):
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_PATH.with_name(STATE_PATH.name + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        tmp.replace(STATE_PATH)
    except Exception:
        logging.exception("Could not write state file")


def parse_price(value):
    if value is None:
        return None

    text = str(value).strip()
    text = text.replace("\xa0", " ")

    matches = re.findall(r"\d[\d.,]*", text)
    if not matches:
        return None

    candidate = max(matches, key=len)

    if "," in candidate and "." in candidate:
        if candidate.rfind(",") > candidate.rfind("."):
            candidate = candidate.replace(".", "").replace(",", ".")
        else:
            candidate = candidate.replace(",", "")
    elif "," in candidate:
        parts = candidate.split(",")
        if len(parts[-1]) in (1, 2):
            candidate = candidate.replace(".", "").replace(",", ".")
        else:
            candidate = candidate.replace(",", "")
    elif "." in candidate:
        parts = candidate.split(".")
        if len(parts[-1]) in (1, 2):
            candidate = candidate.replace(",", "")
        else:
            candidate = candidate.replace(".", "")

    try:
        return float(candidate)
    except ValueError:
        return None


def walk_offers(node):
    if isinstance(node, dict):
        offers = node.get("offers")
        if isinstance(offers, dict):
            yield offers
        elif isinstance(offers, list):
            for offer in offers:
                if isinstance(offer, dict):
                    yield offer

        for value in node.values():
            if isinstance(value, (dict, list)):
                yield from walk_offers(value)
    elif isinstance(node, list):
        for item in node:
            yield from walk_offers(item)


def extract_offers(html):
    offers = []
    seen = set()

    for raw in re.findall(r'application/ld\+json[^>]*>(.*?)</script>', html, re.S):
        try:
            data = json.loads(raw.strip())
        except Exception:
            continue

        for offer in walk_offers(data):
            seller = (offer.get("seller") or {}).get("name")
            price = parse_price(offer.get("price") or offer.get("lowPrice"))
            if not seller or price is None:
                continue
            key = (seller, round(price, 2))
            if key in seen:
                continue
            seen.add(key)
            offers.append({"seller": seller, "price": price})

    offers.sort(key=lambda item: item["price"])
    return offers


def fetch_offers(page, url):
    page.goto(url, timeout=PAGE_LOAD_TIMEOUT, wait_until="domcontentloaded")

    offers = []
    html = ""
    previous_count = -1
    stable = 0

    for _ in range(30):
        page.wait_for_timeout(1000)
        html = page.content()
        offers = extract_offers(html)
        count = len(offers)

        if count > 0 and count == previous_count:
            stable += 1
            if stable >= 2:
                break
        else:
            stable = 0

        previous_count = count

    challenge = not offers and page_has_challenge(html)
    return offers, challenge


def format_tl(value):
    return f"{value:,.2f} TL".replace(",", "X").replace(".", ",").replace("X", ".")


DIRECTION_EMOJI = {
    "down": "\U0001F7E9",  # 🟩
    "up": "\U0001F7E5",  # 🟥
    "same": "\u2B1C",  # ⬜
    "baseline": "\U0001F195",  # 🆕
    "failed": "\u26A0\uFE0F",  # ⚠️
}

DIRECTION_ORDER = ("down", "up", "same", "baseline", "failed")


def build_update(results):
    now = datetime.now(ZoneInfo(TIMEZONE))

    counts = {}
    for result in results:
        counts[result["direction"]] = counts.get(result["direction"], 0) + 1

    summary = " ".join(
        f"{DIRECTION_EMOJI[d]}{counts[d]}"
        for d in DIRECTION_ORDER
        if counts.get(d)
    )
    title = f"Prices {now.strftime('%d.%m.%Y')} - {summary}"

    lines = []
    for result in results:
        emoji = DIRECTION_EMOJI[result["direction"]]
        direction = result["direction"]
        current = result["current"]
        previous = result["previous"]

        if direction == "failed":
            lines.append(f"{emoji} {result['name']} - no prices found")
        elif direction == "baseline":
            lines.append(
                f"{emoji} {result['name']} - {format_tl(current)} (baseline)"
            )
        elif direction == "same":
            lines.append(
                f"{emoji} {result['name']} - {format_tl(current)} (unchanged)"
            )
        else:
            percent = (current - previous) / previous * 100
            lines.append(
                f"{emoji} {result['name']} - {format_tl(current)} "
                f"(was {format_tl(previous)}, {percent:+.1f}%)"
            )

        for index, offer in enumerate(result["offers"][:TOP_DEALS], 1):
            lines.append(
                f"   {index}. {offer['seller']} - {format_tl(offer['price'])}"
            )

        lines.append(f"   {result['url']}")
        lines.append("")

    return title, "\n".join(lines).strip()


def build_report(results, title_prefix="Test run"):
    now = datetime.now(ZoneInfo(TIMEZONE))
    title = f"{title_prefix} - {now.strftime('%d.%m.%Y')}"

    lines = ["Startup check", ""]
    ok = 0
    for result in results:
        if result["offers"]:
            ok += 1
            cheapest = result["offers"][0]
            lines.append(
                f"OK   - {result['name']}: {len(result['offers'])} deals, "
                f"cheapest {format_tl(cheapest['price'])} ({cheapest['seller']})"
            )
        elif result.get("challenge"):
            lines.append(f"FAIL - {result['name']}: Cloudflare challenge, no prices")
        else:
            lines.append(f"FAIL - {result['name']}: no prices found")

    lines.append("")
    lines.append(f"{ok}/{len(results)} product(s) fetched successfully")
    return title, "\n".join(lines)


def send_ntfy(title, body):
    headers = {}
    if NTFY_TOKEN:
        headers["Authorization"] = f"Bearer {NTFY_TOKEN}"

    payload = {
        "topic": NTFY_TOPIC,
        "title": title,
        "message": body,
        "tags": ["money_with_wings"],
        "priority": 3,
    }

    response = requests.post(
        NTFY_URL,
        json=payload,
        headers=headers,
        timeout=TIMEOUT,
    )
    response.raise_for_status()


def collect(products):
    results = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        context = browser.new_context(
            user_agent=USER_AGENT,
            locale="tr-TR",
            timezone_id=TIMEZONE,
            viewport={"width": 1366, "height": 900},
            extra_http_headers={"Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8"},
        )
        context.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
        )
        page = context.new_page()

        try:
            for index, product in enumerate(products):
                try:
                    offers, challenge = fetch_offers(page, product["url"])
                    logging.info(
                        "%s: %d offer(s)%s",
                        product["name"],
                        len(offers),
                        " (Cloudflare challenge)" if challenge else "",
                    )
                    results.append(
                        {
                            "id": product["id"],
                            "name": product["name"],
                            "url": product["url"],
                            "offers": offers,
                            "challenge": challenge,
                        }
                    )
                except Exception:
                    logging.exception("%s: fetch failed", product["name"])
                    results.append(
                        {
                            "id": product["id"],
                            "name": product["name"],
                            "url": product["url"],
                            "offers": [],
                            "challenge": False,
                        }
                    )

                if index < len(products) - 1:
                    time.sleep(REQUEST_DELAY)
        finally:
            browser.close()

    return results


def run_once():
    try:
        products = load_products()
    except Exception:
        logging.exception("Could not load product configuration")
        return

    if not products:
        logging.warning(
            "No products configured in %s; nothing to check.", PRODUCTS_PATH
        )
        return

    logging.info("Checking %d product(s)", len(products))

    state = load_state()
    results = collect(products)
    now = datetime.now(ZoneInfo(TIMEZONE)).isoformat()

    items = []
    for result in results:
        offers = result["offers"]
        previous = state.get(result["id"], {}).get("last_price")

        if not offers:
            direction = "failed"
            current = None
        else:
            current = offers[0]["price"]

            if previous is None:
                direction = "baseline"
            elif current < previous - 0.005:
                direction = "down"
            elif current > previous + 0.005:
                direction = "up"
            else:
                direction = "same"

            state[result["id"]] = {"last_price": current, "updated": now}

        items.append(
            {
                "name": result["name"],
                "url": result["url"],
                "offers": offers,
                "direction": direction,
                "previous": previous,
                "current": current,
            }
        )

        logging.info("%s: %s", result["name"], direction)

    save_state(state)

    title, body = build_update(items)

    try:
        send_ntfy(title, body)
        logging.info("Notification sent: %s", title)
    except Exception:
        logging.exception("Could not send notification")

    logging.debug("Update:\n%s", body)


def run_test():
    try:
        products = load_products()
    except Exception:
        logging.exception("Could not load product configuration")
        raise SystemExit(1)

    if not products:
        logging.error("No products configured in %s; nothing to test.", PRODUCTS_PATH)
        raise SystemExit(1)

    logging.info("Test run: checking %d product(s)", len(products))
    results = collect(products)
    title, body = build_report(results)

    try:
        send_ntfy(title, body)
        logging.info("Test notification sent: %s", title)
    except Exception:
        logging.exception("Could not send test notification")

    logging.info("Test result:\n%s", body)

    if not any(result["offers"] for result in results):
        logging.error("Test run failed: no prices fetched")
        raise SystemExit(1)


def main():
    parser = argparse.ArgumentParser(description="Akakçe price monitor")
    parser.add_argument(
        "--test",
        "--check",
        dest="test",
        action="store_true",
        help="Run a one-off price check, send the result, then exit. "
        "Does not read or write saved state.",
    )
    args = parser.parse_args()

    if args.test:
        run_test()
        return

    while True:
        run_once()
        logging.info("Next check in %d seconds", CHECK_INTERVAL)
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
