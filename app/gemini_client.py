"""
Thin wrapper around Google's Gemini API (generateContent) for writing
Instagram captions for a picked product. Text-only prompt for now (no image
input) - keeps this simple and avoids extra download+base64 steps; the
product's own name/category/price carry enough context for a decent caption.
"""
import requests

API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


class GeminiAPIError(Exception):
    pass


def generate_instagram_caption(
    api_key: str,
    product_name: str,
    category: str,
    retail_price: float,
    brand_name: str = "Mirthovia",
    model: str = "gemini-2.0-flash",
) -> str:
    if not api_key:
        raise ValueError("GEMINI_API_KEY is missing - set it in your .env file")

    prompt = (
        f"Write an Instagram caption for a small cute-lifestyle shop called \"{brand_name}\" "
        f"(bright, playful, coral/yellow/mint aesthetic, tagline \"your little realm of joy\"). "
        f"The product: \"{product_name}\" (category: {category}), priced at ${retail_price:.2f}. "
        "Write 2-3 short, warm, playful sentences (no corporate tone), then a blank line, then "
        "10-15 relevant Instagram hashtags on one line (mix of niche + broad reach tags, no numbering). "
        "Output just the caption and hashtags, nothing else."
    )

    url = API_URL.format(model=model)
    resp = requests.post(
        url,
        params={"key": api_key},
        json={"contents": [{"parts": [{"text": prompt}]}]},
        timeout=30,
    )
    data = resp.json()
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except (KeyError, IndexError):
        raise GeminiAPIError(f"Unexpected Gemini response: {data}")
