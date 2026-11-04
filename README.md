# Mirthovia — CJ Product Scanner

A local tool that scans CJdropshipping for cute, on-brand, profitable products
across Mirthovia's categories and shows them in a dashboard ranked by margin,
with photo downloads and Gemini-generated Instagram captions ready to post.

**Checkout, payment, and order fulfillment happen on the actual Mirthovia
store at [mirthovia.wed2c.com](https://mirthovia.wed2c.com)** (CJ's own
WED2C storefront, built-in PayPal/Stripe checkout, automatic CJ fulfillment)
— this tool's job is purely *finding what to sell*, not placing orders.

## What it does

- **Scans CJ's catalog** across Mirthovia's categories (hair accessories,
  phone accessories, stationery & desk, keychains & charms, jewelry,
  dresses, tops & blouses, skirts)
- **Filters out** seasonal/holiday items, off-brand items (e.g. "sexy"-styled
  clothing that slipped through on an unrelated keyword match), and anything
  that doesn't match your cuteness keywords — so results stay on-brand even
  though the category search itself is broad
- **Picks a representative variant** per product (median-priced single-unit
  variant, not a multi-pack bundle, not just the cheapest/oldest SKU)
- **Gets real shipping cost** to every destination country you sell to, and
  ranks products by **worst-case margin %** — a product only ranks well if
  it's still profitable in *every* country you sell to, not just one
- **Shows a break-even price** (in red) per product, plus an interactive
  "check a price" box — type in a real price you found elsewhere and
  instantly see if the product is still worth selling or should be dropped
- **Shows a product grid** with cost/retail/margin (in $ and ₹), a verified
  link to the real CJ product page, a photo/video download, and a
  Gemini-generated Instagram caption
- **Tracks what you've already posted** ("📌 Mark as posted") so the daily
  picks never repeat themselves

## One-time setup

### 1. Get your CJ API key
1. In the CJdropshipping app/site: **App Store → Others → "API"** → install
2. Go to `cjdropshipping.com/my.html#/authorize/API` → **API** tab → **Add API**
   → Type = "API Key" → Confirm → copy the key (looks like `CJ123456@api@xxxxxxxx...`)

### 2. Get a Gemini API key (for Instagram captions)
Free at [aistudio.google.com/app/apikey](https://aistudio.google.com/app/apikey)

### 3. Configure
```bash
cd /home/vqcodes/Documents/Sanjna/testsh
cp .env.example .env
```
Open `.env` and fill in:
- `CJ_API_KEY` — from step 1
- `GEMINI_API_KEY` — from step 2
- `DESTINATION_COUNTRIES` — comma-separated ISO country codes you sell to (default: `IN,US,GB,AU,CA,DE`)
- `CUTENESS_KEYWORDS` — leave as-is, or adjust to tighten/loosen the aesthetic filter
- `MARKUP_MULTIPLIER` — fallback markup, only used if a category's target retail price (set in `app/picker.py`) is already below landed cost
- `USD_TO_INR` — approximate rate for showing ₹ prices (not live, update occasionally)

**Never commit `.env`** — it's already in `.gitignore`.

### 4. Install dependencies
```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Running it

```bash
.venv/bin/uvicorn app.main:app --reload
```
Open **http://127.0.0.1:8000**

1. Click **"🔍 Run new scan"** — takes a few minutes (CJ rate-limits to ~1
   call/sec, and each candidate product needs a variant lookup + a shipping
   quote per destination country). Watch the live log under the button.
2. Once done, check the **"📅 Today's picks"** banner at the top — these are
   the best unposted candidates. Each card has:
   - **View on CJ ↗** — opens the real product page (use this to find/import
     the product into your WED2C store)
   - **⬇ Photo** / **🖼 All media** — download images/video for the Instagram post
   - **✨ IG caption** — generates a caption + hashtags via Gemini, with a copy button
   - **Check a price** box — type in a real market price you found and see
     instantly whether it's still above break-even
   - **📌 Mark as posted** — once you've posted it, so it won't show up again
3. Import the product into your **Mirthovia WED2C store** (via its
   CJdropshipping app) to actually make it sellable — this tool doesn't do
   that part, it just tells you what's worth importing.

## The actual selling workflow

1. Run a scan → review today's picks → check real market price against the break-even line
2. Import worthwhile products into `mirthovia.wed2c.com` via its CJdropshipping app
3. Download the photo + generate the caption → post on Instagram with the store link in bio
4. Customer clicks through, browses, and checks out directly on the WED2C
   store (PayPal/Stripe) — CJ fulfills and ships automatically
5. Mark the product as posted here so tomorrow's picks are fresh

## Important things to know

- **Cost/shipping figures are CJ's reference prices**, not locked-in quotes —
  they can shift slightly based on exact warehouse/postcode and real-time
  factors (tariffs, logistics). Double check on CJ before high-volume commitments.
- **"Sell at" price is a rough suggestion, not market research** — it's
  either a guessed category target price or cost × a fallback markup,
  neither of which knows what this exact item sells for elsewhere. Always
  search the product online before trusting it (see the real example: a
  skirt set the tool suggested at $90 was actually selling for $25 — always verify).
- **Product search, variant lookup, and freight calc are confirmed working**
  against a real account (see `/home/vqcodes/Documents/Sanjna/check-products/cj_tool.py`,
  a previously tested standalone version of the scanning logic).

## Project structure

```
app/
  cj_client.py        CJ API wrapper (auth, search, variants, freight, product detail)
  gemini_client.py     Gemini caption generation
  picker.py             Scan logic: category search → filter → score → rank
  main.py               FastAPI routes (dashboard, scan job, media, caption, posted-tracking)
  templates/            HTML pages (dashboard, media gallery, caption)
data/                   Cached token + last scan results + posted-products log (gitignored)
.env                    Your secrets (gitignored) — copy from .env.example
```
