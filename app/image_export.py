"""Resizes a photo to a platform's exact canvas size, fit (not cropped), on a blurred letterbox background."""
import io
import os
import subprocess
import tempfile

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


class ConversionError(RuntimeError):
    pass


def export_media(media_bytes: bytes, input_ext: str, platform: str, canvas: str) -> tuple[bytes, str]:
    """For GIFs and videos (export_image only handles static images, and
    only keeps a GIF's first frame) - scales and center-crops to the
    platform's exact canvas size with ffmpeg instead, preserving animation/
    motion. Output stays a GIF for a GIF input, otherwise becomes an MP4
    (broadest compatibility for IG/Pinterest uploads) regardless of the
    input video container."""
    sizes = CANVAS_SIZES.get(platform)
    if not sizes or canvas not in sizes:
        raise UnknownCanvasError(f"Unknown platform/canvas: {platform}/{canvas}")
    target_w, target_h = sizes[canvas]

    is_gif = input_ext.lower() == ".gif"
    out_ext = ".gif" if is_gif else ".mp4"
    vf = f"scale={target_w}:{target_h}:force_original_aspect_ratio=increase,crop={target_w}:{target_h}"

    with tempfile.TemporaryDirectory() as td:
        in_path = os.path.join(td, f"input{input_ext}")
        out_path = os.path.join(td, f"output{out_ext}")
        with open(in_path, "wb") as f:
            f.write(media_bytes)

        cmd = ["ffmpeg", "-y", "-i", in_path, "-vf", vf]
        if not is_gif:
            cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart"]
        cmd += [out_path]

        result = subprocess.run(cmd, capture_output=True, timeout=120)
        if result.returncode != 0 or not os.path.exists(out_path):
            raise ConversionError(result.stderr.decode(errors="ignore")[-800:] or "ffmpeg failed with no output")

        with open(out_path, "rb") as f:
            return f.read(), out_ext
