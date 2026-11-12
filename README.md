# Mirthovia — CJ Product Scanner

A local tool that scans CJdropshipping for cute, on-brand, profitable products
across Mirthovia's categories and shows them in a dashboard ranked by margin,
with Instagram/Pinterest-ready photo downloads and Gemini-generated captions.

**Checkout, payment, and order fulfillment happen on the actual Mirthovia
store at [mirthovia.wed2c.com](https://mirthovia.wed2c.com)** (CJ's own
WED2C storefront) — this tool's job is purely *finding what to sell and
preparing it to post*, not placing orders or managing the storefront itself.

## What it does

- **Scans CJ's catalog** across 13 categories (hair accessories, phone
  accessories, stationery & desk, keychains & charms, jewelry, dresses, tops
  & blouses, skirts, Christmas & festive, bags & purses, sweaters &
  cardigans, sunglasses, socks & legwear)
- **Filters out** off-brand items (e.g. "sexy"-styled clothing) and anything
  that doesn't match your cuteness keywords, with per-category overrides
  where that filter doesn't fit (clothing titles rarely say "cute" even when
  the item is) — see `app/picker.py`'s `CATEGORIES` list
- **Picks a representative variant** per product and gets a **real shipping
  quote** to every destination country you sell to
- **Suggests a price** per product — either a realistic fixed category price
  or cost-based with a 60%-margin cap — and shows cost, margin %, and
  break-even (shipping is NOT subtracted, since WED2C bills it to the buyer
  separately by default)
- **Fetch any single product by link** — paste a CJ product URL (or just its
  ID) to pull it in directly, without waiting on a full category scan; every
  attempt is logged with retry support at `/fetched`
- **Instagram + Pinterest ready downloads** — crops/resizes any photo to each
  platform's exact canvas size (not just a format check), with a single
  "select photos → pick a size → download" flow per product
- **Tracks posting status per platform** (Instagram / Pinterest separately)
- **Keeps a full history per product** — the first time you ever see a
  product is preserved forever; every later re-scan/re-fetch appends to its
  history instead of overwriting, so nothing gets silently lost
- **Can scan automatically every day** via a standalone script + cron/Task
  Scheduler entry — no need to keep the app running

## One-time setup

### 1. Get your CJ API key
1. In the CJdropshipping app/site: **App Store → Others → "API"** → install
2. Go to `cjdropshipping.com/my.html#/authorize/API` → **API** tab → **Add API**
   → Type = "API Key" → Confirm → copy the key (looks like `CJ123456@api@xxxxxxxx...`)

### 2. Get a Gemini API key (for Instagram captions)
Free at [aistudio.google.com/app/apikey](https://aistudio.google.com/app/apikey)

### 3. Set up MongoDB Atlas (required — this is where all app data lives)
1. Create a free cluster at [mongodb.com/cloud/atlas](https://mongodb.com/cloud/atlas)
2. **Connect → Drivers** → copy the connection string
   (`mongodb+srv://user:password@cluster0.xxxxx.mongodb.net/?retryWrites=true&w=majority`)
   — you don't need to add a database name yourself, the app always uses `mirthovia`

### 4. Configure

**Linux / macOS:**
```bash
cd /path/to/testsh
cp .env.example .env
```

**Windows (PowerShell):**
```powershell
cd C:\path\to\testsh
copy .env.example .env
```

Open `.env` and fill in:
- `CJ_API_KEY` — from step 1
- `GEMINI_API_KEY` — from step 2
- `MONGODB_URI` — from step 3
- `DESTINATION_COUNTRIES` — comma-separated ISO country codes you sell to (default: `IN,US,GB,AU,CA,DE`)
- `CUTENESS_KEYWORDS` — leave as-is, or adjust to tighten/loosen the aesthetic filter
- `MARKUP_MULTIPLIER` — fallback markup for products with no fixed category price (see `app/picker.py`)
- `USD_TO_INR` — approximate rate for showing ₹ prices (not live, update occasionally)

**Never commit `.env`** — it's already in `.gitignore`.

### 5. Install dependencies

**Linux / macOS:**
```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

**Windows (PowerShell):**
```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

## Running it

**Linux / macOS:**
```bash
.venv/bin/python -m uvicorn app.main:app --reload
```

**Windows (PowerShell):**
```powershell
.venv\Scripts\python -m uvicorn app.main:app --reload
```

Open **http://127.0.0.1:8000**

1. Click **"Run new scan"** — takes a while (CJ rate-limits to ~1 call/sec,
   and each candidate needs a variant lookup + a shipping quote per
   destination country; with 13 categories this can run 20-40+ minutes).
   Watch the live log under the button, or just leave it and check back.
2. Or paste a product link/ID into the **"Fetch product"** box to pull in
   one specific item immediately instead of waiting on a full scan.
3. Each product page shows cost/margin/break-even, lets you select which
   photos to download and in what size (original, Instagram square/portrait,
   Pinterest standard/square), generate an Instagram caption via Gemini, and
   mark it posted per platform.
4. Add the product to your **Mirthovia WED2C store** yourself (via CJ's own
   "add to store" flow) — this tool doesn't push listings for you, it just
   tells you what's worth adding and gets the content ready.

## Running the scan automatically every day (no server needed)

The scan doesn't require the web app to be running — `scripts/run_daily_scan.py`
talks to CJ and MongoDB directly.

**Linux / macOS** — add to `crontab -e`:
```
0 6 * * * cd /path/to/testsh && .venv/bin/python scripts/run_daily_scan.py >> data/daily_scan.log 2>&1
```

**Windows** — create a Task Scheduler task:
- Program: `C:\path\to\testsh\.venv\Scripts\python.exe`
- Arguments: `scripts\run_daily_scan.py`
- Start in: `C:\path\to\testsh`
- Trigger: Daily, at whatever time you want

Either way, whenever you next open the app, that day's scan results are
already sitting in MongoDB waiting for you.

## Important things to know

- **Cost/shipping figures are CJ's reference prices**, not locked-in quotes —
  they can shift slightly. Double check on CJ before high-volume commitments.
- **Suggested price is a starting point, not market research** — always
  check what the item actually sells for elsewhere before trusting it.
- **A category with few/no results is usually a real supply limit**, not a
  bug — some keywords (e.g. "dress") just don't have many non-oversaturated,
  on-brand matches on CJ right now. Categories with a large pool rotate in
  new products on repeat scans; thin ones will keep showing the same items
  until CJ lists something new that passes the filters.

## Project structure

```
app/
  main.py               FastAPI routes (dashboard, scan job, fetch-by-link,
                         product page, platform exports, posted-tracking)
  picker.py              Scan logic: category search → filter → score → rank
  cj_client.py            CJ API wrapper (auth, search, variants, freight, product detail)
  db.py                   MongoDB Atlas storage for all app data
  gemini_client.py        Gemini caption generation
  image_export.py         Crops/resizes a photo to a platform's exact canvas size
  instagram_specs.py      Instagram photo format/resolution checks
  pinterest_specs.py       Pinterest photo format/resolution checks
  templates/              HTML pages (dashboard, product detail, fetch history)
scripts/
  run_daily_scan.py       Standalone scan runner for cron/Task Scheduler (no server needed)
  migrate_to_mongo.py      One-time migration from the old local-JSON storage
data/                     Local cache only (CJ auth token, cron log) — gitignored
.env                      Your secrets (gitignored) — copy from .env.example
```
