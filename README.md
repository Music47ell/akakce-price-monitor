# Akakçe Price Monitor

[![Build and publish image](https://github.com/Music47ell/akakce-price-monitor/actions/workflows/docker.yml/badge.svg)](https://github.com/Music47ell/akakce-price-monitor/actions/workflows/docker.yml)

A small Docker service that periodically renders Akakçe product pages in a
headless browser, collects every seller offer, and **notifies you on ntfy every
cycle** with a single message listing each product's top 3 deals and whether its
price went up, down, or stayed the same.

Because Akakçe loads its full seller list client-side (via a Cloudflare-protected
API), the page is rendered with a real browser (Playwright/Chromium). This is what
makes marketplace sellers (e.g. `hepsiburada/<seller>`) visible — a plain HTTP
request only sees a partial, cached subset of offers.

No external scraping API or API keys are required: the browser runs inside the
container.

## Features

- Multiple products
- Full seller list per product, including marketplace sellers
- Top 3 deals per product in one notification
- **Per-product direction emoji**: 🟩 down · 🟥 up · ⬜ unchanged
- **A notification every cycle** (and one on every container restart)
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

Add your products to `config/products.yml` **before** the first launch. An empty
product list is skipped entirely — it does not launch the browser, send a
notification, or write `state.json`.

## Products

`config/products.yml` in the repo is a template. At runtime it is mounted from
`/opt/docker/data/akakce-price-monitor/config/products.yml` on the host. Add the
Akakçe product pages you want to monitor:

```yaml
products:
  - id: unique-id
    name: Product display name
    url: https://www.akakce.com/some-product-fiyati,123456.html

  - id: another-product
    name: Another Product
    url: https://www.akakce.com/...
```

Each product needs a unique `id`, a `name` (used in the notification) and an
Akakçe product `url`.

## How directions are detected

- Each cycle, the service records the **cheapest offer** for every product in
  `STATE_PATH` (`/data/state.json`, persisted at
  `/opt/docker/data/akakce-price-monitor` on the host).
- It compares the current cheapest to the last recorded one and marks each product:
  - 🟩 **down** — cheaper than the last check
  - 🟥 **up** — more expensive than the last check
  - ⬜ **same** — unchanged (within 0.005 TL)
  - 🆕 **baseline** — first time seen (no previous price)
  - ⚠️ **failed** — no prices fetched (e.g. a Cloudflare challenge)
- A notification is sent **every cycle**, whether or not anything changed.
- This cycle runs immediately on startup, so **restarting the container sends a
  notification** with the current prices.

No database is required — the state is a tiny JSON file. Delete it to reset all
baselines.

## Notifications

One message is sent every cycle. The title summarizes the directions (counts for
the directions present):

```text
Prices 27.09.2026 - 🟩1 🟥1 ⬜1 🆕1 ⚠️1
```

The body lists each product with its direction, current cheapest price, change vs.
the previous check, and its top 3 deals:

```text
🟩 Bialetti Express 2 Cup - 1.646,10 TL (was 1.737,56 TL, -5.3%)
   1. Hepsiburada/Venti Gıda - 1.646,10 TL
   2. n11/ventigida - 1.682,68 TL
   3. Pazarama/Venti Gıda - 1.737,56 TL
   https://www.akakce.com/moka-pot/...-936632614.html

🟥 Another Product - 920,00 TL (was 900,00 TL, +2.2%)
   1. Trendyol/<seller> - 920,00 TL
   2. Hepsiburada/<seller> - 940,00 TL
   3. <seller> - 950,00 TL
   https://www.akakce.com/some-product-fiyati,123456.html

⬜ Third Product - 500,00 TL (unchanged)
   1. <seller> - 500,00 TL
   2. <seller> - 510,00 TL
   3. <seller> - 520,00 TL
   https://www.akakce.com/...

🆕 New Product - 300,00 TL (baseline)
   1. <seller> - 300,00 TL
   https://www.akakce.com/...

⚠️ Broken Product - no prices found
   https://www.akakce.com/...
```

If a product cannot be fetched, its previous price is kept and the cycle continues.

## Running

The stack is defined in `compose.yaml`. It attaches to the external `tunnel-net`
network and stores its state and config under
`/opt/docker/data/akakce-price-monitor` on the host.

Create the external network and the config directory once, and seed it with the
template:

```bash
docker network create tunnel-net

mkdir -p /opt/docker/data/akakce-price-monitor/config
cp config/products.yml /opt/docker/data/akakce-price-monitor/config/products.yml
```

Edit the product list (the environment variables are set separately, e.g. in
Dockhand's env section):

```bash
nano /opt/docker/data/akakce-price-monitor/config/products.yml
```

The Compose file pulls the prebuilt image from GHCR, so no local build is needed:

```bash
docker compose pull
docker compose up -d
```

Logs:

```bash
docker compose logs -f
```

Stop:

```bash
docker compose down
```

A notification is sent immediately on startup and every `CHECK_INTERVAL_SECONDS`
after that, so a reminder of the current prices arrives on every restart.

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

The `config` directory is intentionally separate from application code so product changes can be committed without modifying the scraper.

## Development

Changes pushed to `main` trigger the GitHub Actions workflow, which builds the
image and pushes it to GHCR. Tagging a release (`git tag v1.2.3 && git push --tags`)
publishes versioned tags as well.

## How it works / scraping notes

- Each product page is rendered with headless Chromium (Playwright). Akakçe's
  offers hydrate client-side and end up in the page's `application/ld+json`
  `offers` array, which is parsed for `seller.name` and `price`.
- Offers are sorted by price; the cheapest is stored for direction detection and the
  cheapest `TOP_DEALS` are included in the notification.
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
