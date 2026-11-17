"""
Thin wrapper around Google's Gemini API (generateContent) for writing
Instagram-ready TEXT content for a picked product - description, caption,
and hashtags - plus a free, no-billing price *estimate* based on Gemini's
training knowledge.

That estimate is NOT live web research - Gemini's real search-grounding
(the Interactions API with a google_search tool) was tested against this
account and returned a 429 quota/billing error, same blocker as Veo video
generation. So the price estimate here is a quick, free first-pass signal
only - always verify it yourself before trusting it over a real market check.
"""
import time

import requests

API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
MAX_RETRIES = 3
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


class GeminiAPIError(Exception):
    pass


def _call_gemini(api_key: str, prompt: str, model: str) -> str:
    if not api_key:
        raise ValueError("GEMINI_API_KEY is missing - set it in your .env file")

    url = API_URL.format(model=model)
    body = {"contents": [{"parts": [{"text": prompt}]}]}

    last_err = None
    data = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.post(url, params={"key": api_key}, json=body, timeout=45)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            last_err = f"network error: {e}"
            time.sleep(2 * attempt)
            continue

        data = resp.json()
        if resp.status_code == 200:
            last_err = None
            break
        if resp.status_code in RETRYABLE_STATUS_CODES:
            last_err = data.get("error", {}).get("message", str(data))
            time.sleep(2 * attempt)
            continue
        # non-retryable error (bad API key, bad request, etc.) - fail immediately
        raise GeminiAPIError(f"Gemini request failed (HTTP {resp.status_code}): {data.get('error', {}).get('message', data)}")

    if last_err:
        raise GeminiAPIError(f"Gemini is unavailable after {MAX_RETRIES} attempts: {last_err}")

    try:
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except (KeyError, IndexError):
        raise GeminiAPIError(f"Unexpected Gemini response: {data}")


def _parse_sections(text: str) -> dict:
    """Pulls out DESCRIPTION/CAPTION/HASHTAGS labeled sections.
    Falls back to dumping everything into 'caption' if the model didn't
    follow the format, so the page never ends up with nothing to show."""
    sections = {"description": "", "caption": "", "hashtags": ""}
    current = None
    labels = {
        "DESCRIPTION:": "description",
        "CAPTION:": "caption",
        "HASHTAGS:": "hashtags",
    }
    for line in text.splitlines():
        stripped = line.strip()
        matched = False
        for label, key in labels.items():
            if stripped.upper().startswith(label):
                current = key
                rest = stripped[len(label):].strip()
                sections[key] = rest
                matched = True
                break
        if not matched and current:
            sections[current] = (sections[current] + "\n" + line).strip()

    if not any(sections.values()):
        sections["caption"] = text.strip()
    return sections


def build_instagram_prompt(product_name: str, category: str, retail_price: float, brand_name: str = "Mirthovia") -> str:
    """Same prompt generate_instagram_content sends to Gemini - exposed on
    its own so it can be copy-pasted into ChatGPT or any other AI when
    Gemini itself is down, without needing a second code path."""
    return (
        f"You're writing Instagram-ready content for a small cute-lifestyle shop called "
        f"\"{brand_name}\" (bright, playful, coral/yellow/mint aesthetic, tagline "
        f"\"your little realm of joy\"). The product: \"{product_name}\" (category: {category}), "
        f"priced at ${retail_price:.2f}.\n\n"
        "Output EXACTLY these three labeled sections, each on its own line(s), nothing else:\n\n"
        "DESCRIPTION: a factual 1-2 sentence product description (what it is, what it's for)\n"
        "CAPTION: 2-3 short, warm, playful Instagram caption sentences (no corporate tone)\n"
        "HASHTAGS: exactly 5 relevant hashtags, space-separated, no numbering"
    )


def generate_instagram_content(
    api_key: str,
    product_name: str,
    category: str,
    retail_price: float,
    brand_name: str = "Mirthovia",
    model: str = "gemini-3.8-flash",
) -> dict:
    """Returns {"description", "caption", "hashtags", "raw"}."""
    prompt = build_instagram_prompt(product_name, category, retail_price, brand_name)
    raw = _call_gemini(api_key, prompt, model)
    parsed = _parse_sections(raw)
    parsed["raw"] = raw
    return parsed


def estimate_market_price(
    api_key: str,
    product_name: str,
    category: str,
    cost: float,
    model: str = "gemini-3.8-flash",
) -> dict:
    """A free, no-billing price *estimate* from Gemini's training knowledge -
    NOT live search (that's billing-gated, see module docstring). Returns
    {"estimate", "raw"}; always show this labeled as an estimate to verify,
    never as confirmed market research."""
    prompt = (
        f"Based on your general knowledge of online retail pricing (not live search), "
        f"estimate a typical retail price range in USD for this kind of product: "
        f"\"{product_name}\" (category: {category}). For context, it costs about ${cost:.2f} "
        f"wholesale from a dropshipping supplier.\n\n"
        "Respond in 2-3 sentences: give a plausible price range, name 1-2 reference "
        "platforms where this type of item commonly sells (e.g. Amazon, Shein, Etsy, Meesho), "
        "and explicitly note that this is an estimate from general knowledge, not a live "
        "price check, so it should be verified before relying on it."
    )
    raw = _call_gemini(api_key, prompt, model)
    return {"estimate": raw.strip(), "raw": raw}


def build_collection_prompt(product_names: list[str], brand_name: str = "Mirthovia") -> str:
    """Same prompt generate_collection_content sends to Gemini - exposed on
    its own so it can be copy-pasted into ChatGPT or any other AI when
    Gemini itself is down."""
    items = "\n".join(f"- {name}" for name in product_names)
    return (
        f"You're writing an Instagram carousel caption for a small cute-lifestyle shop called "
        f"\"{brand_name}\" (bright, playful aesthetic, tagline \"your little realm of joy\"). "
        f"This single post features {len(product_names)} products together, swipeable as a carousel:\n{items}\n\n"
        "Output EXACTLY these two labeled sections, each on its own line(s), nothing else:\n\n"
        "CAPTION: 2-4 short, warm, playful sentences introducing this set of products together "
        "(don't describe each one individually - treat it as one cohesive roundup/collection)\n"
        "HASHTAGS: exactly 6 relevant hashtags, space-separated, no numbering"
    )


def generate_collection_content(
    api_key: str,
    product_names: list[str],
    brand_name: str = "Mirthovia",
    model: str = "gemini-3.8-flash",
) -> dict:
    """One combined caption for posting several products together (e.g. as
    an Instagram carousel) instead of one caption per product. Returns
    {"caption", "hashtags", "raw"}."""
    prompt = build_collection_prompt(product_names, brand_name)
    raw = _call_gemini(api_key, prompt, model)
    parsed = _parse_sections(raw)
    parsed["raw"] = raw
    return parsed
