"""Exercise public image assets against real PNG, ICO and filesystem boundaries."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ImageAssetTests(unittest.TestCase):
    """Each process writes an inspectable report beside its generated assets."""

    def setUp(self):
        evidence = os.environ.get("BUILDFLOW_TEST_ARTIFACTS")
        if evidence:
            Path(evidence).mkdir(parents=True, exist_ok=True)
            self.root = Path(tempfile.mkdtemp(prefix=self._testMethodName + "-", dir=evidence))
        else:
            temporary = tempfile.TemporaryDirectory(prefix="buildflow-images-")
            self.addCleanup(temporary.cleanup)
            self.root = Path(temporary.name)
        self.env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1"}

    def run_case(self, body):
        script = '''import hashlib, json, struct, zlib
from pathlib import Path
from PIL import Image, ImageCms, PngImagePlugin
from buildflow.images import inspect_png, prepare_icons
from buildflow.contracts import BuildError
root=Path.cwd()
def rejects(action):
    try: action()
    except BuildError as exc: return str(exc)
    raise AssertionError("invalid input was accepted")
def save_report(data):
    Path("report.json").write_text(json.dumps(data, indent=2, ensure_ascii=False))
''' + body
        result = subprocess.run([sys.executable, "-B", "-c", script], cwd=self.root,
                                env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads((self.root / "report.json").read_text())

    def test_public_assets_preserve_variants_and_proportions(self):
        report = self.run_case('''
source=root/"source.png"
Image.new("RGBA", (80,40), (240,30,60,255)).save(source)
variant=root/"variant.png"
info=PngImagePlugin.PngInfo(); info.add_text("artist", "public fixture")
Image.new("RGB", (64,64), (1,2,3)).save(variant, pnginfo=info)
before={str(p):p.read_bytes() for p in [source,variant]}
result=prepare_icons(source, root/"work", variants={64:variant}, ico_sizes=(16,32,64))
assert result["png"][64].read_bytes()==variant.read_bytes()
with Image.open(result["png"][256]) as im:
    assert im.mode=="RGBA" and im.size==(256,256)
    assert im.getbbox()==(0,64,256,192), im.getbbox()
    assert im.getpixel((128,128))==(240,30,60,255)
with Image.open(result["ico"]) as im:
    assert im.ico.sizes()=={(16,16),(32,32),(64,64)}
for path,contents in before.items(): assert Path(path).read_bytes()==contents
for path in [*result["png"].values(),result["ico"]]:
    assert path.resolve().is_relative_to((root/"work/assets").resolve())
assert inspect_png(variant)["color_space"]=="srgb"
assert inspect_png(variant)["color_basis"]=="assumed-untagged"
metadata=result["metadata"]
assert hashlib.sha256(variant.read_bytes()).hexdigest() in json.dumps(metadata)
assert hashlib.sha256(result["png"][256].read_bytes()).hexdigest() in json.dumps(metadata)
save_report(metadata)
''')
        self.assertTrue(report)

    def test_bad_png_and_explicit_variant_fail_without_output(self):
        report = self.run_case('''
source=root/"source.png"; Image.new("RGBA",(32,16),(20,30,40,255)).save(source)
original=source.read_bytes()
errors={}
cases={"truncated":original[:-10],"trailing":original+b"not png","not-png":b"PNG"}
bad=bytearray(original); bad[29]^=1; cases["crc"]=bytes(bad)
for name,data in cases.items():
    p=root/(name+".png");p.write_bytes(data)
    errors[name]=rejects(lambda: inspect_png(p))
errors["bytes"]=rejects(lambda: inspect_png(source,max_bytes=10))
errors["pixels"]=rejects(lambda: inspect_png(source,max_pixels=100))
errors["dimensions"]=rejects(lambda: prepare_icons(source,root/"work",variants={64:source}))
errors["invalid-size"]=rejects(lambda: prepare_icons(source,root/"work",png_sizes=(True,)))
errors["output-budget"]=rejects(lambda: prepare_icons(source,root/"work",png_sizes=(4096,4095)))
animated=root/"animated.png"
Image.new("RGBA",(16,16),"red").save(animated,save_all=True,append_images=[Image.new("RGBA",(16,16),"blue")],duration=100)
errors["animation"]=rejects(lambda: inspect_png(animated))
assert source.read_bytes()==original
assert not list((root/"work").rglob("*.png"))
save_report(errors)
''')
        self.assertEqual(len(report), 10)

    def test_color_metadata_and_profile_conversion(self):
        self.run_case('''
source=root/"source.png";Image.new("RGBA",(32,32),(60,100,140,200)).save(source)
standard=PngImagePlugin.PngInfo()
standard.add(b"gAMA",struct.pack(">I",45455))
standard.add(b"cHRM",struct.pack(">8I",31270,32900,64000,33000,30000,60000,15000,6000))
tagged=root/"tagged.png";Image.new("RGB",(32,32),"blue").save(tagged,pnginfo=standard)
assert inspect_png(tagged)["color_space"]=="srgb"
conflict=PngImagePlugin.PngInfo();conflict.add(b"sRGB",b"\\x00");conflict.add(b"gAMA",struct.pack(">I",100000))
bad=root/"conflict.png";Image.new("RGB",(32,32)).save(bad,pnginfo=conflict)
errors={"conflict":rejects(lambda:inspect_png(bad))}
icc=root/"profile.png"
with Image.open(source) as im:im.save(icc,icc_profile=ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes())
assert inspect_png(icc)["color_space"]=="srgb"
preserved=prepare_icons(source,root/"preserved",png_sizes=(32,),variants={32:icc})
assert preserved["png"][32].read_bytes()==icc.read_bytes()
profile=bytearray(ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes())
for offset in range(132,132+12*struct.unpack_from(">I",profile,128)[0],12):
    if profile[offset:offset+4]==b"rXYZ":
        position=struct.unpack_from(">I",profile,offset+4)[0]
        struct.pack_into(">i",profile,position+8,struct.unpack_from(">i",profile,position+8)[0]+1000)
shifted=root/"shifted-profile.png"
with Image.open(source) as im:im.save(shifted,icc_profile=bytes(profile))
assert inspect_png(shifted)["color_space"]=="icc"
result=prepare_icons(shifted,root/"work",png_sizes=(64,))
assert inspect_png(result["png"][64])["color_space"]=="srgb"
with Image.open(result["png"][64]) as im:assert im.getpixel((32,32))[3]==200
errors["explicit-icc"]=rejects(lambda:prepare_icons(source,root/"other",png_sizes=(32,),variants={32:shifted}))
invalid=root/"invalid-profile.png";Image.new("RGB",(16,16)).save(invalid,icc_profile=b"bad profile")
errors["invalid-icc"]=rejects(lambda:inspect_png(invalid))
save_report({"errors":errors,"metadata":result["metadata"]})
''')

    @unittest.skipIf(os.name == "nt", "POSIX symbolic-link fixture")
    def test_links_cannot_redirect_inputs_or_outputs(self):
        self.run_case('''
source=root/"source.png";Image.new("RGB",(16,16),"red").save(source)
alias=root/"alias.png";alias.symlink_to(source)
work=root/"work";work.mkdir()
outside=root/"outside";outside.mkdir()
(work/"assets").symlink_to(outside,target_is_directory=True)
errors={"input-link":rejects(lambda:inspect_png(alias)),
        "output-link":rejects(lambda:prepare_icons(source,work))}
assert list(outside.iterdir())==[]
save_report(errors)
''')

    def test_plain_import_does_not_require_pillow(self):
        result = subprocess.run([sys.executable, "-S", "-B", "-c",
                                 "import buildflow.api, buildflow.images; print('ordinary import ready')"],
                                cwd=self.root, env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
