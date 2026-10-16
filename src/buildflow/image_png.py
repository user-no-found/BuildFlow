"""Read bounded static PNG inputs without following filesystem links."""
import hashlib
import io
import os
from pathlib import Path
import stat
import struct
import zlib
from .contracts import BuildError
from .image_color import color_metadata


MAX_BYTES = 20 * 1024 * 1024
MAX_PIXELS = 16 * 1024 * 1024
_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def checked_path(value):
    """Reject parent traversal and links before reading or creating asset paths."""
    path = Path(value)
    if ".." in path.parts:
        raise BuildError(f"image path must not contain parent traversal: {path}")
    path = path.absolute()
    if any(item.is_symlink() for item in (path, *path.parents)):
        raise BuildError(f"image path must not contain symbolic links: {path}")
    return path


def _limits(max_bytes, max_pixels):
    for name, value in (("max_bytes", max_bytes), ("max_pixels", max_pixels)):
        if type(value) is not int or value < 1:
            raise BuildError(f"{name} must be a positive integer")


def _bytes(path, limit):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as stream:
        status = os.fstat(stream.fileno())
        if not stat.S_ISREG(status.st_mode):
            raise BuildError(f"PNG input must be a regular file: {path}")
        if status.st_size > limit:
            raise BuildError(f"PNG input exceeds {limit} bytes: {path}")
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise BuildError(f"PNG input exceeds {limit} bytes: {path}")
    return data


def _structure(data, max_pixels):
    if data[:8] != _SIGNATURE:
        raise BuildError("image must be PNG")
    position, count, header, pixels = 8, 0, False, False
    colors = {}
    while position < len(data):
        count += 1
        if count > 16384 or position + 12 > len(data):
            raise BuildError("PNG chunk structure is invalid or excessive")
        length = struct.unpack_from(">I", data, position)[0]
        kind = data[position + 4:position + 8]
        end = position + length + 12
        if end > len(data):
            raise BuildError("PNG chunk is truncated")
        value = data[position + 8:end - 4]
        checksum = struct.unpack_from(">I", data, end - 4)[0]
        if zlib.crc32(kind + value) != checksum:
            raise BuildError("PNG chunk CRC mismatch")
        if not header and kind != b"IHDR":
            raise BuildError("PNG must begin with IHDR")
        if kind == b"IHDR":
            if header or length != 13:
                raise BuildError("PNG has an invalid IHDR")
            width, height = struct.unpack_from(">II", value)
            if not width or not height or width * height > max_pixels:
                raise BuildError(f"PNG exceeds {max_pixels} pixels or has empty dimensions")
            header = True
        elif kind == b"IDAT":
            pixels = True
        elif kind == b"IEND":
            if length or not pixels or end != len(data):
                raise BuildError("PNG end is invalid or has trailing data")
            return colors
        elif kind in (b"acTL", b"fcTL", b"fdAT"):
            raise BuildError("animated PNG is not supported")
        elif kind in (b"sRGB", b"gAMA", b"cHRM", b"iCCP", b"cICP", b"mDCv", b"cLLi"):
            if kind in colors or pixels:
                raise BuildError("PNG color metadata is duplicated or follows image data")
            colors[kind] = value
        elif kind not in (b"PLTE",) and kind[:1].isupper():
            raise BuildError("PNG contains an unsupported critical chunk")
        position = end
    raise BuildError("PNG is missing IEND")


def load_png(path, *, max_bytes=MAX_BYTES, max_pixels=MAX_PIXELS):
    """Return decoded pixels, metadata and the exact bytes from one bounded read."""
    _limits(max_bytes, max_pixels)
    path = checked_path(path)
    try:
        from PIL import Image
        data = _bytes(path, max_bytes)
        colors = _structure(data, max_pixels)
        with Image.open(io.BytesIO(data), formats=["PNG"]) as candidate:
            candidate.verify()
        with Image.open(io.BytesIO(data), formats=["PNG"]) as candidate:
            candidate.load()
            space, basis = color_metadata(colors, candidate.info)
            image = candidate.copy()
        metadata = {"path": str(path), "width": image.width, "height": image.height,
                    "mode": image.mode, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                    "color_space": space, "color_basis": basis}
        return image, metadata, data
    except ImportError as exc:
        raise BuildError("PNG assets require BuildFlow's images extra (Pillow)") from exc
    except BuildError:
        raise
    except (OSError, ValueError, SyntaxError, struct.error) as exc:
        raise BuildError(f"cannot decode PNG {path}: {exc}") from exc


def inspect_png(path, *, max_bytes=MAX_BYTES, max_pixels=MAX_PIXELS):
    """Inspect a static PNG; untagged pixels are explicitly interpreted as sRGB."""
    image, metadata, _ = load_png(path, max_bytes=max_bytes, max_pixels=max_pixels)
    image.close()
    return metadata
