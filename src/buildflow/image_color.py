"""Interpret PNG color declarations and identify supported sRGB ICC profiles."""
import io
import struct
from .contracts import BuildError


_SRGB_CHROMATICITIES = (31270, 32900, 64000, 33000, 30000, 60000, 15000, 6000)


def _icc_tags(data):
    if len(data) < 132 or struct.unpack_from(">I", data)[0] != len(data) or data[36:40] != b"acsp":
        raise BuildError("invalid ICC profile header")
    count = struct.unpack_from(">I", data, 128)[0]
    if count > 128 or 132 + count * 12 > len(data):
        raise BuildError("invalid ICC profile tag table")
    tags = {}
    for offset in range(132, 132 + count * 12, 12):
        name, position, size = struct.unpack_from(">4sII", data, offset)
        if name in tags or position < 132 + count * 12 or size < 8 or position + size > len(data):
            raise BuildError("invalid ICC profile tag")
        tags[name] = data[position:position + size]
    return tags


def _icc_space(data):
    from PIL import ImageCms
    tags = _icc_tags(data)
    profile = ImageCms.ImageCmsProfile(io.BytesIO(data))
    if profile.profile.xcolor_space.strip() not in ("RGB", "GRAY"):
        raise BuildError("PNG ICC profile does not describe RGB or grayscale")
    reference = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    standard = _icc_tags(reference)
    # Compare encoded color transforms, not names or the presence of a profile.
    # Other valid profiles remain convertible sources, not byte-preserved variants.
    transforms = {b"rXYZ", b"gXYZ", b"bXYZ", b"rTRC", b"gTRC", b"bTRC", b"wtpt", b"chad", b"chrm"}
    descriptive = {b"desc", b"cprt", b"dmnd", b"dmdd"}
    same_header = data[12:24] == reference[12:24] and data[64:80] == reference[64:80]
    if (same_header and not set(tags) - transforms - descriptive
            and all(tags.get(tag) == standard.get(tag) for tag in transforms)):
        return "srgb", "icc-srgb-transform"
    return "icc", "icc-profile"


def color_metadata(colors, info):
    """Return color space and its basis; contradictory declarations are errors."""
    srgb = colors.get(b"sRGB")
    gamma = colors.get(b"gAMA")
    chromaticities = colors.get(b"cHRM")
    if srgb is not None and (len(srgb) != 1 or srgb[0] > 3):
        raise BuildError("invalid PNG sRGB rendering intent")
    if gamma is not None and (len(gamma) != 4 or struct.unpack(">I", gamma)[0] == 0):
        raise BuildError("invalid PNG gamma metadata")
    if chromaticities is not None and len(chromaticities) != 32:
        raise BuildError("invalid PNG chromaticity metadata")
    standard_gamma = gamma is None or abs(struct.unpack(">I", gamma)[0] - 45455) <= 5
    standard_chroma = chromaticities is None or all(
        abs(actual - expected) <= 5 for actual, expected in
        zip(struct.unpack(">8I", chromaticities), _SRGB_CHROMATICITIES))
    if srgb is not None and (b"iCCP" in colors or not standard_gamma or not standard_chroma):
        raise BuildError("PNG contains conflicting sRGB color metadata")
    if any(kind in colors for kind in (b"cICP", b"mDCv", b"cLLi")):
        raise BuildError("PNG HDR color metadata is not supported")
    if b"iCCP" in colors:
        profile = info.get("icc_profile")
        if not profile:
            raise BuildError("PNG ICC profile is missing or invalid")
        return _icc_space(profile)
    if not standard_gamma or not standard_chroma:
        return "unsupported", "non-srgb-gamma-chromaticities"
    if srgb is not None:
        return "srgb", "png-srgb"
    if gamma is not None or chromaticities is not None:
        return "srgb", "png-gamma-chromaticities"
    return "srgb", "assumed-untagged"
