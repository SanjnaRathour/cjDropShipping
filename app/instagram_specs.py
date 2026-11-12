"""
Checks a product's images/video against Instagram's actual posting specs, so
the detail page can tell you which media is safe to post as-is vs. needs a
crop/resize first. Reference specs (feed + Reels, as of Instagram's current
guidance): images - square 1:1, portrait 4:5, or landscape 1.91:1, minimum
1080px on the shorter side recommended; Reels/video - vertical 9:16
(1080x1920) performs best, MP4/MOV container.
"""
import io

import requests
from PIL import Image

IDEAL_RATIOS = {
    "Square (1:1)": 1.0,
    "Portrait (4:5)": 0.8,
    "Landscape (1.91:1)": 1.91,
}
RATIO_TOLERANCE = 0.06
MIN_GOOD_RESOLUTION = 1080
MIN_USABLE_RESOLUTION = 320


def check_image_for_instagram(url: str) -> dict:
    """Downloads the image and returns {width, height, ratio_label, verdict,
    note} - verdict is one of 'good', 'warn', 'error'."""
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
        notes.append(f"Unusual aspect ratio ({ratio:.2f}:1) - Instagram will auto-crop this, recommend cropping to square or 4:5 yourself first")

    if shorter_side < MIN_USABLE_RESOLUTION:
        verdict = "error"
        notes.append(f"Very low resolution ({width}×{height}) - will look blurry/pixelated on Instagram")
    elif shorter_side < MIN_GOOD_RESOLUTION:
        if verdict == "good":
            verdict = "warn"
        notes.append(f"Below Instagram's recommended {MIN_GOOD_RESOLUTION}px - usable but may look slightly soft")

    if not notes:
        notes.append(f"{ratio_label}, {width}×{height} - good to post as-is")

    return {"width": width, "height": height, "ratio_label": ratio_label, "verdict": verdict, "note": " · ".join(notes)}


def check_video_for_instagram(url: str) -> dict:
    """Basic container-format check via Content-Type/extension - can't get
    actual resolution/duration without ffprobe, which isn't installed, so
    this only catches the most obvious "wrong file type" problem and gives
    textual guidance for the rest."""
    verdict = "good"
    notes = []
    lower = url.lower()
    if lower.endswith((".mp4", ".mov")):
        notes.append("MP4/MOV container looks fine for Instagram")
    else:
        try:
            head = requests.head(url, timeout=10, allow_redirects=True)
            ctype = head.headers.get("Content-Type", "")
        except Exception:
            ctype = ""
        if "mp4" in ctype or "quicktime" in ctype:
            notes.append("MP4/MOV container looks fine for Instagram")
        else:
            verdict = "warn"
            notes.append(f"Unclear/unusual format ({ctype or 'unknown'}) - convert to MP4 before posting if Instagram rejects it")

    notes.append("For Reels, vertical 9:16 (1080×1920) performs best - trim/crop in CapCut if this isn't already vertical")
    return {"verdict": verdict, "note": " · ".join(notes)}
