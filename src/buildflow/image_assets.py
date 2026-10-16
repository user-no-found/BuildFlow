"""Generate isolated PNG and ICO assets with explicit provenance."""
import hashlib
import io
from pathlib import Path
import shutil
import tempfile
from .contracts import BuildError
from .image_png import MAX_BYTES, MAX_PIXELS, checked_path, inspect_png, load_png


def _sizes(values, maximum):
    try:
        sizes = tuple(values)
    except TypeError as exc:
        raise BuildError("image sizes must be an iterable of integers") from exc
    if len(sizes) > 32 or any(type(size) is not int or not 1 <= size <= maximum for size in sizes):
        raise BuildError(f"request at most 32 image sizes, each within 1..{maximum}")
    if len(set(sizes)) != len(sizes):
        raise BuildError("image sizes must be unique")
    return sizes


def _rgba(image, metadata):
    from PIL import ImageCms
    if metadata["color_space"] == "unsupported":
        raise BuildError("PNG color space cannot be converted without an ICC profile")
    rgba = image.convert("RGBA")
    if metadata["color_space"] == "icc":
        try:
            profile = ImageCms.ImageCmsProfile(io.BytesIO(image.info["icc_profile"]))
            source = image.convert("L" if profile.profile.xcolor_space.strip() == "GRAY" else "RGB")
            converted = ImageCms.profileToProfile(source, profile, ImageCms.createProfile("sRGB"), outputMode="RGB")
            converted.putalpha(rgba.getchannel("A"))
            rgba.close()
            rgba = converted
            source.close()
        except (OSError, ValueError, ImageCms.PyCMSError) as exc:
            rgba.close()
            raise BuildError(f"cannot convert PNG ICC profile to sRGB: {exc}") from exc
    rgba.info.clear()
    return rgba


def _square(image, size):
    from PIL import Image
    ratio = size / max(image.size)
    dimensions = tuple(max(1, round(value * ratio)) for value in image.size)
    scaled = image.resize(dimensions, Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    canvas.paste(scaled, ((size - scaled.width) // 2, (size - scaled.height) // 2))
    scaled.close()
    return canvas


def prepare_icons(source, work, *, png_sizes=(64, 256), variants=None, ico_sizes=()):
    """Create assets beneath work/assets, preserving qualified PNG variant bytes.

    Explicit variants must match a requested square size and have unambiguous
    supported sRGB metadata (untagged PNG is interpreted as sRGB). Other ICC
    sources are converted; unconfirmed variants are rejected, never rewritten.
    """
    png_sizes = _sizes(png_sizes, 4096)
    ico_sizes = _sizes(ico_sizes, 256)
    if not png_sizes and not ico_sizes:
        raise BuildError("request at least one PNG or ICO size")
    if sum(size * size for size in (*png_sizes, *ico_sizes)) > MAX_PIXELS:
        raise BuildError(f"generated images exceed the total budget of {MAX_PIXELS} pixels")
    if variants is not None and not isinstance(variants, dict):
        raise BuildError("image variants must map integer sizes to PNG paths")
    variants = dict(variants or {})
    if any(type(size) is not int or size not in png_sizes for size in variants):
        raise BuildError("explicit variants must match requested PNG sizes")
    loaded = []
    directory = None
    rgba = None
    try:
        original, source_info, source_data = load_png(source)
        loaded.append(original)
        rgba = _rgba(original, source_info)
        input_bytes = len(source_data)
        validated = {}
        for size, path in variants.items():
            image, metadata, data = load_png(path)
            dimensions = image.size
            image.close()
            input_bytes += len(data)
            if input_bytes > 2 * MAX_BYTES:
                raise BuildError(f"combined PNG inputs exceed {2 * MAX_BYTES} bytes")
            if dimensions != (size, size) or metadata["color_space"] != "srgb":
                raise BuildError(f"explicit PNG variant {size} must be {size}x{size} with supported sRGB color metadata")
            validated[size] = (metadata, data)
        assets = checked_path(work) / "assets"
        checked_path(assets)
        assets.mkdir(parents=True, exist_ok=True)
        checked_path(assets)
        directory = Path(tempfile.mkdtemp(prefix="icons-", dir=assets))
        png, outputs = {}, []
        from PIL import PngImagePlugin
        srgb = PngImagePlugin.PngInfo()
        srgb.add(b"sRGB", b"\x00")
        for size in png_sizes:
            path = directory / f"icon-{size}.png"
            if size in validated:
                with path.open("xb") as stream:
                    stream.write(validated[size][1])
                method = "copy-variant"
            else:
                image = _square(rgba, size)
                with path.open("xb") as stream:
                    image.save(stream, format="PNG", pnginfo=srgb)
                image.close()
                method = "resize-contain"
            png[size] = path
            outputs.append({**inspect_png(path), "format": "png", "method": method})
        ico = None
        if ico_sizes:
            ico = directory / "icon.ico"
            images = [_square(rgba, size) for size in sorted(ico_sizes, reverse=True)]
            try:
                with ico.open("xb") as stream:
                    images[0].save(stream, format="ICO", sizes=[(size, size) for size in ico_sizes], append_images=images[1:])
            finally:
                for image in images:
                    image.close()
            data = ico.read_bytes()
            outputs.append({"path": str(ico), "format": "ico", "sizes": list(ico_sizes),
                            "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                            "method": "resize-contain", "color_space": "srgb"})
        metadata = {"schema": 1, "source": source_info,
                    "variants": {str(size): info for size, (info, _) in validated.items()},
                    "outputs": outputs, "resampling": "lanczos", "padding": "transparent",
                    "generated_mode": "RGBA"}
        return {"png": png, "ico": ico, "metadata": metadata}
    except BuildError:
        if directory:
            shutil.rmtree(directory)
        raise
    except (OSError, ValueError) as exc:
        if directory:
            shutil.rmtree(directory)
        raise BuildError(f"cannot prepare image assets: {exc}") from exc
    except BaseException:
        if directory:
            shutil.rmtree(directory)
        raise
    finally:
        if rgba is not None:
            rgba.close()
        for image in loaded:
            image.close()
