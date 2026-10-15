# Akakçe Price Monitor

[![Build and publish image](https://github.com/Music47ell/akakce-price-monitor/actions/workflows/docker.yml/badge.svg)](https://github.com/Music47ell/akakce-price-monitor/actions/workflows/docker.yml)

A small Docker service that periodically renders Akakçe product pages in a
headless browser, collects every seller offer, and **notifies you on ntfy when a
price changes** — one message listing the changed products with their top 3 deals.

Because Akakçe loads its full seller list client-side (via a Cloudflare-protected
API), the page is rendered with a real browser (Playwright/Chromium). This is what
makes marketplace sellers (e.g. `hepsiburada/<seller>`) visible — a plain HTTP
request only sees a partial, cached subset of offers.

No external scraping API or API keys are required: the browser runs inside the
container.

## Features

- Multiple products
- **Web UI** to add/remove monitored URLs (password-protected)
- Full seller list per product, including marketplace sellers
- Top 3 deals per product in one notification
- **Per-product direction emoji**: 🟩 down · 🟥 up · ⬜ unchanged
- **Unit price** (e.g. `TL/kg`) shown when Akakçe provides it
- **Notifies only when something changed** (skips cycles where every price is unchanged)
- Persistent state across container restarts (small JSON file, no database)
- ntfy with optional Bearer-token authentication
- Configurable polling interval (default: every 6 hours)
- Prebuilt image on GitHub Container Registry
- No external scraping API or API keys required
- Docker Compose / Dockhand friendly

## Image

Images are built and published automatically by GitHub Actions
(`.github/workflows/docker.yml`) on every push to `main` and on `v*` tags.

```text
ghcr.io/music47ell/akakce-price-monitor:latest
```

| Trigger | Tags |
| --- | --- |
| Push to `main` | `latest`, `sha-<short>` |
| Tag `v1.2.3` | `1.2.3`, `1.2`, `sha-<short>` |

The package is private by default. Either make it public in the repository's
package settings, or authenticate before pulling:

```bash
echo $GITHUB_TOKEN | docker login ghcr.io -u <your-github-username> --password-stdin
```

## Configuration

Set the following environment variables in your orchestrator (e.g. Dockhand's env
section). `.env.example` lists them for reference:

```env
NTFY_URL=https://ntfy.example.com
NTFY_TOPIC=price-alerts
NTFY_TOKEN=
CHECK_INTERVAL_SECONDS=21600
REQUEST_TIMEOUT_SECONDS=30
REQUEST_DELAY_SECONDS=3
TOP_DEALS=3
TIMEZONE=Europe/Istanbul
PAGE_LOAD_TIMEOUT_SECONDS=60000
STATE_PATH=/data/state.json
UI_PASSWORD_HASH=
UI_PORT=8080
```

| Variable | Default | Description |
| --- | --- | --- |
| `NTFY_URL` | – | Base URL of your ntfy server (required) |
| `NTFY_TOPIC` | – | ntfy topic to publish to (required) |
| `NTFY_TOKEN` | empty | Bearer token sent as `Authorization: Bearer <token>` |
| `CHECK_INTERVAL_SECONDS` | `21600` | Polling interval (6 hours) |
| `REQUEST_TIMEOUT_SECONDS` | `30` | HTTP timeout for the ntfy request |
| `REQUEST_DELAY_SECONDS` | `3` | Delay between products |
| `TOP_DEALS` | `3` | Number of cheapest deals to include per product |
| `TIMEZONE` | `Europe/Istanbul` | Timezone used for the notification date |
| `PAGE_LOAD_TIMEOUT_SECONDS` | `60000` | Page load / hydration timeout |
| `STATE_PATH` | `/data/state.json` | Where the last-seen prices are stored |
| `UI_PASSWORD_HASH` | empty | pbkdf2 hash for the web UI; if empty, the UI is disabled |
| `UI_PORT` | `8080` | Port the web UI listens on inside the container |

`compose.yaml` wires these into the container through an `environment:` block that
references them with `${VAR}` interpolation. This matters for tools like Dockhand,
which inject stack variables into the `docker compose` process for interpolation
only — a variable that is not referenced there never reaches the container.

## On-demand test (`--test`)

Run a price check at any time from the running container, without waiting for the
interval and without touching saved state:

```bash
docker exec akakce-price-monitor python -u app.py --test
```

or, with Compose:

```bash
docker compose exec akakce-price-monitor python -u app.py --test
```

`--test` (alias `--check`) fetches every product once, sends a `Test run - …`
notification with an `OK`/`FAIL` report (including Cloudflare challenges), prints it
to the logs, and exits — it does **not** read or write `state.json`, so it never
affects the direction shown in the regular notifications. It exits non-zero if no
prices could be fetched, which makes it scriptable.

The exec session inherits the container's environment, so `NTFY_URL`/`NTFY_TOPIC`
(set in Dockhand) are used.

## Products & web UI

Products are no longer managed with a YAML file. They are stored in
`/data/products.json` and managed through a small **password-protected web UI**.
Open the UI (via your tunnel, e.g. `https://your-host/`), log in, and you'll see the
monitored URLs with their last price and direction. There you can:

- **Add** a URL (only the URL — the name is read from the Akakçe page on each check),
- **Remove** entries,
- click **Check now** to run a cycle immediately instead of waiting for the interval.

The changes are saved immediately. New URLs are checked on the next cycle (or right
away via **Check now**).

### Password

Generate a hash and set it as `UI_PASSWORD_HASH` (e.g. in Dockhand's env section):

```bash
docker run --rm -it ghcr.io/music47ell/akakce-price-monitor python -u app.py --hash-password
```

or from a running container:

```bash
docker exec -it akakce-price-monitor python -u app.py --hash-password
```

Or generate it anywhere, without this image (plain Python):

```bash
docker run --rm -it python:3.13-slim python -c "import getpass,hashlib,base64,secrets; p=getpass.getpass('Password: ').encode(); s=secrets.token_bytes(16); print('pbkdf2_sha256$600000$'+base64.b64encode(s).decode()+'$'+base64.b64encode(hashlib.pbkdf2_hmac('sha256',p,s,600000)).decode())"
```

If `UI_PASSWORD_HASH` is empty, the web UI is disabled (the monitor still runs).

### Migrating from products.yml

On startup, if `/data/products.json` does not exist but `/config/products.yml` does,
the YAML is imported once automatically (only the URLs are kept). After that,
manage everything in the UI, and you can remove the `/config` mount.

### Reaching the UI

The container serves the UI on `UI_PORT` (`8080`) over plain HTTP. Expose it through
your tunnel (route the hostname to `akakce-price-monitor:8080`, on `tunnel-net`) —
TLS and any extra access control are handled there.

## How directions are detected

- Each cycle, the service records the **shipping-included ("kargo dahil") cheapest
  price** for every product in `STATE_PATH` (`/data/state.json`, persisted at
  `/opt/docker/data/akakce-price-monitor` on the host).
- It compares the current price to the last recorded one and marks each product:
  - 🟩 **down** — cheaper than the last check
  - 🟥 **up** — more expensive than the last check
  - ⬜ **same** — unchanged (within 0.005 TL)
  - 🆕 **baseline** — first time seen (no previous price)
  - ⚠️ **failed** — no prices fetched (e.g. a Cloudflare challenge)
- A notification is sent **only when at least one product changed** — i.e. when any
  product is 🟩 down, 🟥 up, 🆕 baseline, or ⚠️ failed. If **every** product is ⬜
  same, nothing is sent.
- The message lists **only the products that changed** (unchanged ones are omitted).

No database is required — the state is a tiny JSON file. Delete it to reset all
baselines.

## Notifications

A message is sent only when something changed, and it lists only the changed
products. The title summarizes the directions present:

```text
Prices 11.10.2026 — 🟩1 🟥1
```

The body lists each product with its direction, the current **shipping-included**
cheapest price, the change vs. the previous check, its unit price (when Akakçe
provides one), and its top 3 seller offers. Seller lines are the cheapest offers by
product price; shipping is shown where Akakçe reports it:

```text
🟩 Eti Lifalif 500 gr Yulaf Ezmesi - 74,05 TL (was 77,95 TL, -5.0%) · 148,10 TL/kg
   1. Happy Center - 73,99 TL
   2. Amazon Türkiye - 74,05 TL (ücretsiz kargo)
   3. Migros - 77,95 TL
   https://www.akakce.com/kahvaltilik-gevrek/...850418.html

🟥 Another Product - 920,00 TL (was 900,00 TL, +2.2%)
   1. Trendyol/<seller> - 920,00 TL
   2. Hepsiburada/<seller> - 940,00 TL
   3. <seller> - 950,00 TL
   https://www.akakce.com/some-product-fiyati,123456.html

🆕 New Product - 300,00 TL (baseline)
   1. <seller> - 300,00 TL
   https://www.akakce.com/...

⚠️ Broken Product - no prices found
   https://www.akakce.com/...
```

If a product cannot be fetched, its previous price is kept and the cycle continues.

## Running

The stack is defined in `compose.yaml`. It attaches to the external `tunnel-net`
network and stores its state (and the product list) under
`/opt/docker/data/akakce-price-monitor` on the host.

Create the external network once:

```bash
docker network create tunnel-net
```

Set `NTFY_URL`, `NTFY_TOPIC` (and `UI_PASSWORD_HASH`, see below) in your orchestrator,
then start the stack. The Compose file pulls the prebuilt image from GHCR, so no
local build is needed:

```bash
docker compose pull
docker compose up -d
```

If you still have a `config/products.yml` from the old version, place it at
`/opt/docker/data/akakce-price-monitor/config/products.yml` before the first run so
it is imported into `/data/products.json` automatically.

Logs:

```bash
docker compose logs -f
```

Stop:

```bash
docker compose down
```

A cycle runs immediately on startup and every `CHECK_INTERVAL_SECONDS` after that.
A notification is sent only when at least one product changed, so restarts with
unchanged prices send nothing.

### Updating

`compose.yaml` uses `pull_policy: always`, so `docker compose up -d` picks up
the newest `latest` image. To update manually:

```bash
docker compose pull
docker compose up -d
```

### Building locally

To build the image yourself instead of pulling it, uncomment `build: .` in
`compose.yaml` (and remove `image`/`pull_policy`), then:

```bash
docker compose up -d --build
```

## Dockhand

For a Git-backed Dockhand Stack, commit this directory to your repository and point Dockhand at the repository/Compose file.

Set the environment variables (see Configuration) in Dockhand's env section. The
`environment:` block in `compose.yaml` passes them through via `${VAR}`
interpolation, which is required for them to reach the container.

The Compose file references the published GHCR image, so Dockhand pulls it rather than building. If the package is private, provide GHCR credentials to Dockhand.

The product list is managed through the web UI and stored at
`/opt/docker/data/akakce-price-monitor/products.json` on the host, separate from the
image. Route your tunnel hostname to `akakce-price-monitor:8080` to reach the UI.

## Development

Changes pushed to `main` trigger the GitHub Actions workflow, which builds the
image and pushes it to GHCR. Tagging a release (`git tag v1.2.3 && git push --tags`)
publishes versioned tags as well.

## How it works / scraping notes

- Each product page is rendered with headless Chromium (Playwright). Offers are
  parsed from the **rendered DOM** (`.pb_v8` rows): price (`.pt_v8`), shipping
  (`.uk_v8`), and seller name (`.v_v8` logo `alt` + optional `/subseller` text).
  The DOM carries the **full** seller list; the JSON-LD `offers` array only holds a
  rotating subset of ~10, so it is used only as a fallback.
- The tracked price is Akakçe's **"kargo dahil en ucuz"** headline (`.bb_w`,
  shipping-included). Seller lines show the cheapest `TOP_DEALS` offers by product
  price, with shipping where Akakçe reports it.
- The product's unit price ("Birim Fiyat", e.g. `TL/kg`) is read from the rendered
  markup (`<span class="up_v8">`) when Akakçe provides it, and appended to the
  product line.
- Products live in `/data/products.json` (managed by the web UI); the monitor
  re-reads it every cycle. The product **name** is read from the Akakçe page at
  check time (JSON-LD `name`, falling back to the page title) and shown in the UI and
  notifications.
- The web UI is a stdlib `http.server` running in a background thread; the monitor
  loop stays on the main thread (Playwright sync API). Auth uses a `pbkdf2` hash
  (`UI_PASSWORD_HASH`), a session cookie, a CSRF token, and login rate-limiting.
- Chromium needs shared memory, so the Compose file sets `shm_size: "1gb"` and the
  browser runs with `--no-sandbox` / `--disable-dev-shm-usage`.
- The image bundles only Playwright's **headless shell** (`playwright install
  --with-deps --only-shell chromium`), not the full Chromium — we always run
  `headless=True`, so the extra ~430 MB build and its apt caches are skipped.
  Expect a few hundred MB of image and a few hundred MB of RAM while a page is
  rendered.
- Akakçe may serve Cloudflare challenges to some datacenter IPs. If a product logs
  `0 offer(s)`, check the logs; running from a residential connection or adding a
  proxy may be required.
- The service intentionally spaces requests between products. Do not set the
  interval unnecessarily low.
