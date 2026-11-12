"""Resizes a photo to a platform's exact canvas size, fit (not cropped), on a blurred letterbox background."""
import io

from PIL import Image, ImageFilter

CANVAS_SIZES = {
    "instagram": {
        "square": (1080, 1080),
        "portrait": (1080, 1350),
    },
    "pinterest": {
        "standard": (1000, 1500),
        "square": (1000, 1000),
    },
}


class UnknownCanvasError(ValueError):
    pass


def export_image(image_bytes: bytes, platform: str, canvas: str) -> bytes:
    sizes = CANVAS_SIZES.get(platform)
    if not sizes or canvas not in sizes:
        raise UnknownCanvasError(f"Unknown platform/canvas: {platform}/{canvas}")
    target_w, target_h = sizes[canvas]
    target_ratio = target_w / target_h

    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    src_w, src_h = img.size
    src_ratio = src_w / src_h

    if src_ratio > target_ratio:
        bg_new_w = round(src_h * target_ratio)
        left = (src_w - bg_new_w) // 2
        bg_crop = img.crop((left, 0, left + bg_new_w, src_h))
    else:
        bg_new_h = round(src_w / target_ratio)
        top = (src_h - bg_new_h) // 2
        bg_crop = img.crop((0, top, src_w, top + bg_new_h))
    background = bg_crop.resize((target_w, target_h), Image.LANCZOS)
    background = background.filter(ImageFilter.GaussianBlur(40))

    fit_scale = min(target_w / src_w, target_h / src_h, 1.0)
    fit_w = max(1, round(src_w * fit_scale))
    fit_h = max(1, round(src_h * fit_scale))
    foreground = img.resize((fit_w, fit_h), Image.LANCZOS)

    canvas_img = background
    paste_x = (target_w - fit_w) // 2
    paste_y = (target_h - fit_h) // 2
    canvas_img.paste(foreground, (paste_x, paste_y))

    out = io.BytesIO()
    canvas_img.save(out, format="JPEG", quality=92)
    return out.getvalue()
