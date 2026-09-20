"""Platform-neutral checks for Opmize (Windows or macOS): pipeline geometry, colour handling, big-file streaming,
and the window with a simulated drop. Run:  python smoke_test.py   (exit code 1 on any failure)."""
import importlib.machinery, importlib.util, os, io, sys, time
import numpy as np
from PIL import Image, ImageCms, JpegImagePlugin
import tifffile

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "smoke_out")
os.makedirs(OUT, exist_ok=True)
# ".pyw" is only a recognised source suffix on Windows, so spec_from_file_location returns None
# on macOS unless the loader is named explicitly. Without this the Mac build fails before it starts.
_APP = os.path.join(HERE, "opmize.pyw")
spec = importlib.util.spec_from_file_location(
    "igo", _APP, loader=importlib.machinery.SourceFileLoader("igo", _APP))
igo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(igo)
print("platform:", sys.platform, "| Tk:", igo.tk.TkVersion, "| drag-and-drop lib:", igo.HAVE_DND, "| tifffile:", bool(igo.tifffile))
fails = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("  " + detail if detail else ""))
    if not cond:
        fails.append(name)


def props(path):
    im = Image.open(path)
    icc = im.info.get("icc_profile")
    desc = ImageCms.getProfileDescription(ImageCms.ImageCmsProfile(io.BytesIO(icc))).strip() if icc else None
    return im.size, desc, JpegImagePlugin.get_sampling(im), bool(im.info.get("exif"))


S = dict(output_mode="folder", output_dir=OUT, overwrite=True)

# 1. portrait 4:5 16-bit TIFF (like a film scan)
h, w = 3000, 2400
yy, xx = np.mgrid[0:h, 0:w]
a = np.zeros((h, w, 3), np.float32)
a[:, :, 0] = xx / (w - 1); a[:, :, 1] = yy / (h - 1); a[:, :, 2] = 0.5 + 0.4 * np.sin(xx / 31.0)
a = np.clip(a + np.random.default_rng(1).normal(0, 0.02, a.shape), 0, 1)
p1 = os.path.join(OUT, "portrait_4x5_16bit.tif")
tifffile.imwrite(p1, (a * 65535 + 0.5).astype(np.uint16), photometric="rgb")
r = igo.process_file(p1, S, igo.SRGB_ICC)
size, desc, samp, exif = props(r["output"])
check("4:5 16-bit TIFF -> 2160x2700 (not enlarged: source 2400 wide -> 2160)", size == (2160, 2700), str(size))
check("sRGB profile embedded, 4:4:4, no EXIF", desc is not None and "sRGB" in desc and samp == 0 and not exif, "%s %s %s" % (desc, samp, exif))

# 2. landscape 3:2, panorama, 2:3 with EXIF orientation 6, tiny file
def jpg(name, arr, exif=None):
    p = os.path.join(OUT, name)
    im = Image.fromarray(arr)
    im.save(p, "JPEG", quality=95, **({"exif": exif} if exif else {}))
    return p
flat = lambda hh, ww, v: np.full((hh, ww, 3), v, np.uint8)
check("3:2 landscape -> 2160x1440", igo.process_file(jpg("land.jpg", flat(3000, 4500, 120)), S, igo.SRGB_ICC)["out_size"] == (2160, 1440))
r = igo.process_file(jpg("pano.jpg", flat(2000, 4800, 200)), S, igo.SRGB_ICC)
check("2.4:1 pano -> 2160x900 whole, 'wider than' note", r["out_size"] == (2160, 900) and any("wider" in n for n in r["notes"]), str(r["notes"]))
land = np.zeros((3000, 4500, 3), np.uint8); land[:, :, 0] = np.linspace(0, 255, 4500)[None, :].astype(np.uint8); land[:1500, :, 2] = 255
ex = Image.Exif(); ex[274] = 6
r = igo.process_file(jpg("orient6.jpg", land, ex.tobytes()), S, igo.SRGB_ICC)
out = np.asarray(Image.open(r["output"]))
check("EXIF orientation 6 -> 2:3 portrait 2160x3240, rotated, no crop", r["out_size"] == (2160, 3240) and int(out[100, 1000, 0]) < int(out[3100, 1000, 0]) and int(out[1620, 1900, 2]) > 200, str(r["out_size"]))
r = igo.process_file(jpg("small.jpg", flat(1500, 1200, 90)), S, igo.SRGB_ICC)
check("small source not enlarged", r["out_size"] == (1200, 1500) and any("not enlarged" in n for n in r["notes"]))

# 3. colour: Display P3 (macOS ships it) or Adobe RGB (Adobe apps ship it) -> sRGB matches LittleCMS
cands = ["/System/Library/ColorSync/Profiles/Display P3.icc",
         "/Library/Application Support/Adobe/Color/Profiles/Recommended/AdobeRGB1998.icc",
         r"C:\Program Files\Common Files\Adobe\Color\Profiles\Recommended\AdobeRGB1998.icc"]
prof = next((c for c in cands if os.path.exists(c)), None)
if prof:
    pb = open(prof, "rb").read()
    base = np.zeros((400, 600, 3), np.float32); gy, gx = np.mgrid[0:400, 0:600]
    base[:, :, 0] = gx / 599; base[:, :, 1] = gy / 399; base[:, :, 2] = 0.5
    src8 = (base * 255 + 0.5).astype(np.uint8)
    sp = ImageCms.ImageCmsProfile(io.BytesIO(igo.SRGB_ICC)); wp = ImageCms.ImageCmsProfile(io.BytesIO(pb))
    wide = ImageCms.profileToProfile(Image.fromarray(src8), sp, wp, renderingIntent=ImageCms.Intent.RELATIVE_COLORIMETRIC, outputMode="RGB")
    pw = os.path.join(OUT, "wide.jpg"); wide.save(pw, "JPEG", quality=100, subsampling=0, icc_profile=pb)
    r = igo.process_file(pw, dict(S, width=600, sharpen=False), igo.SRGB_ICC)
    got = np.asarray(Image.open(r["output"])).astype(np.float32)
    ref = np.asarray(ImageCms.profileToProfile(Image.open(pw), wp, sp, renderingIntent=ImageCms.Intent.RELATIVE_COLORIMETRIC, outputMode="RGB")).astype(np.float32)
    d = np.abs(got - ref)
    check("%s -> sRGB agrees with LittleCMS" % os.path.basename(prof), float(d.mean()) < 1.5 and float(np.percentile(d, 99)) < 6, "mean %.2f p99 %.1f" % (d.mean(), np.percentile(d, 99)))
else:
    print("SKIP colour conversion (no wide-gamut profile found on this machine)")

# 4. big-file streaming: 4000x6000 16-bit TIFF at width 1000 goes through the 2x box pre-reduction
big = (np.clip(a[:, :, :], 0, 1) * 65535).astype(np.uint16)
big = np.tile(big, (2, 2, 1))[:6000, :4000]
pb2 = os.path.join(OUT, "big16.tif"); tifffile.imwrite(pb2, big, photometric="rgb")
r = igo.process_file(pb2, dict(S, width=1000, sharpen=False), igo.SRGB_ICC)
ref = igo.resize_lanczos(igo._float_rgb(big), 1000, 1500)
got = np.asarray(Image.open(r["output"])).astype(np.float32) / 255.0
d = np.abs(got - ref) * 255
check("streamed 2x-reduced path matches direct Lanczos", r["out_size"] == (1000, 1500) and float(d.mean()) < 1.2, "mean %.2f/255" % d.mean())

# 5. the window: fonts, a simulated drop, the size box (window stays hidden)
igo.register_fonts()
root = igo.TkinterDnD.Tk() if igo.HAVE_DND else igo.tk.Tk()
root.withdraw()
app = igo.App(root)
app.v_outmode.set("folder"); app.v_outdir.set(OUT); app.v_overwrite.set(True)
print("   fonts picked:", app.fonts.sans, "|", app.fonts.body_med, "|", app.fonts.mono)
check("Geist fonts registered for this process", app.fonts.sans == "Geist" and app.fonts.mono == "Geist Mono", "%s / %s" % (app.fonts.sans, app.fonts.mono))
class E:
    data = root.tk.eval("list {%s} {%s}" % (os.path.join(OUT, "land.jpg"), p1)); action = "copy"
app._on_drop(E())
t0 = time.time()
while app.done < app.total and time.time() - t0 < 120:
    root.update(); time.sleep(0.03)
rows = [(r.file, r.dims_text, r.state) for r in app.rows]
check("simulated drop processed 2 files in the window", app.total == 2 and app.done == 2 and all(r[2] == "done" for r in rows), str(rows))
app.set_ui_scale(1.2); root.update()
check("app size box rebuilds and keeps the list", app.fonts.body[1] == 12 and [(r.file, r.dims_text, r.state) for r in app.rows] == rows)
app.set_ui_scale(1.0); root.update()
app._close()
igo.save_settings(dict(igo.DEFAULTS))
print("\nFAILED:", fails if fails else "none")
sys.exit(1 if fails else 0)
