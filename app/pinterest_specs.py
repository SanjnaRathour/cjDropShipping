"""Checks a product's images against Pinterest's Pin specs, mirroring instagram_specs.py."""
import io

import requests
from PIL import Image

IDEAL_RATIOS = {
    "Standard Pin (2:3)": 2 / 3,
    "Square (1:1)": 1.0,
}
RATIO_TOLERANCE = 0.06
MIN_GOOD_RESOLUTION = 1000
MIN_USABLE_RESOLUTION = 300


def check_image_for_pinterest(url: str) -> dict:
    try:
        resp = requests.get(url, timeout=20)
        resp.raise_for_status()
        img = Image.open(io.BytesIO(resp.content))
        width, height = img.size
    except Exception as e:
        return {"width": None, "height": None, "ratio_label": None, "verdict": "error", "note": f"Couldn't check: {e}"}

    ratio = width / height
    ratio_label = None
    for label, target in IDEAL_RATIOS.items():
        if abs(ratio - target) <= RATIO_TOLERANCE:
            ratio_label = label
            break

    shorter_side = min(width, height)
    notes = []
    verdict = "good"

    if ratio_label is None:
        verdict = "warn"
        notes.append(f"Unusual aspect ratio ({ratio:.2f}:1) - Pinterest will crop this in the feed, recommend the Standard Pin export below instead of posting as-is")

    if shorter_side < MIN_USABLE_RESOLUTION:
        verdict = "error"
        notes.append(f"Very low resolution ({width}×{height}) - will look blurry/pixelated on Pinterest")
    elif shorter_side < MIN_GOOD_RESOLUTION:
        if verdict == "good":
            verdict = "warn"
        notes.append(f"Below Pinterest's recommended {MIN_GOOD_RESOLUTION}px width - usable but may look slightly soft")

    if not notes:
        notes.append(f"{ratio_label}, {width}×{height} - good to post as-is")

    return {"width": width, "height": height, "ratio_label": ratio_label, "verdict": verdict, "note": " · ".join(notes)}
