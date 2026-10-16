from __future__ import annotations

from io import BytesIO

from PIL import Image, ImageOps, UnidentifiedImageError


FOUR_K_SIZES = {(3840, 2160), (2160, 3840)}
MAX_INPUT_BYTES = 50 * 1024 * 1024
MAX_INPUT_PIXELS = 50_000_000


def upscale_to_4k(image_data: bytes, size: str | None) -> bytes:
    """Resize a generated image for an explicit 4K request without AI processing."""
    target = parse_4k_size(size)
    if target is None:
        return image_data
    if not image_data or len(image_data) > MAX_INPUT_BYTES:
        raise ValueError("image must be between 1 byte and 50 MB")
    try:
        with Image.open(BytesIO(image_data)) as source:
            width, height = source.size
            if width <= 0 or height <= 0 or width * height > MAX_INPUT_PIXELS:
                raise ValueError("image dimensions exceed the 50 MP limit")
            if source.size == target and source.format == "PNG" and source.getexif().get(274, 1) == 1:
                return image_data
            image = ImageOps.exif_transpose(source)
            image.load()
            has_alpha = "A" in image.getbands() or "transparency" in image.info
            image = image.convert("RGBA" if has_alpha else "RGB")
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ValueError("invalid image data") from exc

    if image.size != target:
        # The requested aspect ratio is normally already respected upstream.
        # Fit preserves geometry if an upstream model returns a different ratio.
        image = ImageOps.fit(image, target, method=Image.Resampling.LANCZOS)

    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def parse_4k_size(size: str | None) -> tuple[int, int] | None:
    try:
        width, height = (int(part) for part in str(size or "").lower().split("x", 1))
    except (TypeError, ValueError):
        return None
    return (width, height) if (width, height) in FOUR_K_SIZES else None
