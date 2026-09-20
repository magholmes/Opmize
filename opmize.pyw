#!/usr/bin/env python3
"""
Opmize - drop photos in, get Instagram-ready JPEGs out. One drop, done. Nothing is ever cropped.

Pipeline for every photo:
  1. Reads the full-quality source (8/16-bit TIFF, JPEG, PNG, WebP, BMP, PSD composite, HEIC if pillow-heif is present),
     streaming very large files in bands so a 300-megapixel scan does not need gigabytes of RAM
  2. Applies EXIF/TIFF orientation
  3. Downscales to 2160 px wide (portrait or landscape) with a full anti-aliased Lanczos filter in 32-bit float
  4. Converts the embedded colour profile to sRGB (matrix profiles in float; LUT profiles via LittleCMS)
  5. Light luminance-only "screen" sharpening, then dithers to 8-bit
  6. Saves a baseline JPEG (quality 93, 4:4:4 chroma) with the sRGB IEC61966-2.1 profile embedded and no EXIF/GPS
The photo's own aspect ratio is always kept: no crop, no bars, no enlargement. 2160 px wide is twice Instagram's 1080 px
rung, so a 3:4 portrait comes out 2160x2880, a 4:5 at 2160x2700, a 3:2 landscape at 2160x1440.

Look and feel: Are.na's "Dusk" theme (its purple-tinted grey ladder), with the layout language of the magnus archive site
(hairlines, Geist + Geist Mono, lowercase mono labels, pill controls) in a frameless, rounded window.
"""
import os, sys, io, json, math, struct, threading, queue, traceback, time, ctypes, glob, faulthandler, datetime, subprocess, platform
import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox

MISSING = []
try:
    import numpy as np
except ImportError:
    MISSING.append("numpy")
try:
    from PIL import Image, ImageCms, ImageOps
    Image.MAX_IMAGE_PIXELS = None
except ImportError:
    MISSING.append("Pillow")
try:
    import tifffile
except ImportError:
    tifffile = None
try:
    import cv2
except ImportError:
    cv2 = None
try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except Exception:
    pass
try:
    from tkinterdnd2 import TkinterDnD, DND_FILES
    HAVE_DND = True
except Exception:
    HAVE_DND = False

APP_NAME = "Opmize"
APP_VERSION = "1.5"
OLD_APP_NAMES = ("IG Optimizer", "IG Downscaler")                               # settings folders from earlier names, migrated on first run
IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
FROZEN = bool(getattr(sys, "frozen", False))                                   # True inside the PyInstaller .exe / .app
APP_DIR = os.path.dirname(os.path.abspath(sys.executable if FROZEN else __file__))
RES_DIR = getattr(sys, "_MEIPASS", APP_DIR)                                    # bundled data files when frozen


def _settings_dir():
    if not FROZEN:
        return APP_DIR
    if IS_WIN:
        return os.path.join(os.environ.get("APPDATA", APP_DIR), APP_NAME)
    if IS_MAC:
        return os.path.join(os.path.expanduser("~/Library/Application Support"), APP_NAME)
    return os.path.join(os.path.expanduser("~/.config"), APP_NAME.lower().replace(" ", "-"))


SETTINGS_DIR = _settings_dir()
SETTINGS_FILE = os.path.join(SETTINGS_DIR, "settings.json")
IMAGE_EXTS = {".tif", ".tiff", ".jpg", ".jpeg", ".png", ".webp", ".bmp", ".psd", ".heic", ".heif"}
IG_MIN_RATIO, IG_MAX_RATIO = 3 / 4, 1.91          # Instagram feed frame: 3:4 (portrait) to 1.91:1 (landscape)
SUBFOLDER = "instagram_export"
WIDTH = 2160          # baked in: 2x Instagram's 1080 rung, preserved as the full master on the web (portrait and landscape)
QUALITY = 93          # baked in: JPEG quality, 4:4:4 chroma
SHARPEN = True        # baked in: light luminance-only screen sharpening
UI_SCALES = [("small", 0.85), ("medium", 1.0), ("large", 1.2), ("x-large", 1.45)]
DEFAULTS = dict(output_mode="subfolder", output_dir="", overwrite=False, open_on_export=False, ui_scale=1.0)
SRGB_ICC = b""
_CRASH_FH = None


def log_error(where, text):
    """Append to errors.log next to the settings (the window has no console)."""
    try:
        os.makedirs(SETTINGS_DIR, exist_ok=True)
        with open(os.path.join(SETTINGS_DIR, "errors.log"), "a", encoding="utf-8") as fh:
            fh.write("%s  %s\n%s\n\n" % (datetime.datetime.now().isoformat(timespec="seconds"), where, text))
    except Exception:
        pass


def setup_crash_logging():
    """Hard crashes (access violations, stack overflows) get a Python traceback in crash.log via faulthandler."""
    global _CRASH_FH
    try:
        os.makedirs(SETTINGS_DIR, exist_ok=True)
        _CRASH_FH = open(os.path.join(SETTINGS_DIR, "crash.log"), "a", encoding="utf-8")
        faulthandler.enable(file=_CRASH_FH, all_threads=True)
    except Exception:
        pass
    sys.excepthook = lambda t, v, tb: log_error("main thread", "".join(traceback.format_exception(t, v, tb)))
    try:
        threading.excepthook = lambda a: log_error("thread %s" % a.thread.name, "".join(traceback.format_exception(a.exc_type, a.exc_value, a.exc_traceback)))
    except Exception:
        pass

# ----------------------------------------------------------------------------- colour management
SRGB_M = None
if not MISSING:
    SRGB_M = np.array([[0.4360747, 0.3850649, 0.1430804],
                       [0.2225045, 0.7168786, 0.0606169],
                       [0.0139322, 0.0971045, 0.7141733]])   # sRGB linear RGB -> XYZ (D50, Bradford), as in the ICC profile


def load_srgb_profile():
    for p in (os.path.join(RES_DIR, "sRGB Color Space Profile.icm"),
              os.path.join(APP_DIR, "sRGB Color Space Profile.icm"),
              r"C:\Windows\System32\spool\drivers\color\sRGB Color Space Profile.icm",
              "/System/Library/ColorSync/Profiles/sRGB Profile.icc"):
        if os.path.exists(p):
            with open(p, "rb") as fh:
                return fh.read()
    return ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()


def srgb_decode(x):
    x = np.clip(x, 0, 1)
    return np.where(x <= 0.04045, x / 12.92, np.power((x + 0.055) / 1.055, 2.4))


def srgb_encode(x):
    x = np.clip(x, 0, 1)
    return np.where(x <= 0.0031308, 12.92 * x, 1.055 * np.power(x, 1 / 2.4) - 0.055)


def _s15(b):
    return struct.unpack(">i", b)[0] / 65536.0


def _parse_curve(d):
    typ = d[:4]
    if typ == b"curv":
        cnt = struct.unpack(">I", d[8:12])[0]
        if cnt == 0:
            return lambda x: np.clip(x, 0, 1)
        if cnt == 1:
            g = struct.unpack(">H", d[12:14])[0] / 256.0
            return lambda x, g=g: np.power(np.clip(x, 0, 1), g)
        tbl = np.frombuffer(d[12:12 + 2 * cnt], dtype=">u2").astype(np.float64) / 65535.0
        xs = np.linspace(0, 1, cnt)
        return lambda x, xs=xs, tbl=tbl: np.interp(np.clip(x, 0, 1), xs, tbl)
    if typ == b"para":
        ft = struct.unpack(">H", d[8:10])[0]
        npar = {0: 1, 1: 3, 2: 4, 3: 5, 4: 7}[ft]
        p = [_s15(d[12 + 4 * j:16 + 4 * j]) for j in range(npar)]

        def f(x, ft=ft, p=p):
            x = np.clip(x, 0, 1)
            if ft == 0:
                return np.power(x, p[0])
            g, a, b = p[0], p[1], p[2]
            base = np.power(np.maximum(a * x + b, 0), g)
            if ft == 1:
                return np.where(x >= -b / a, base, 0.0)
            if ft == 2:
                return np.where(x >= -b / a, base + p[3], p[3])
            if ft == 3:
                return np.where(x >= p[4], base, p[3] * x)
            return np.where(x >= p[4], base + p[5], p[3] * x + p[6])
        return f
    raise ValueError("unsupported curve type %r" % typ)


def icc_description(icc):
    try:
        n = struct.unpack(">I", icc[128:132])[0]
        for i in range(n):
            sig, off, sz = struct.unpack(">4sII", icc[132 + 12 * i:144 + 12 * i])
            if sig == b"desc":
                d = icc[off:off + sz]
                if d[:4] == b"desc":
                    cnt = struct.unpack(">I", d[8:12])[0]
                    return d[12:12 + cnt].split(b"\x00")[0].decode("latin-1", "replace").strip()
                if d[:4] == b"mluc":
                    ln, of = struct.unpack(">II", d[20:28])
                    return d[of:of + ln].decode("utf-16-be", "replace").strip()
    except Exception:
        pass
    try:
        return ImageCms.getProfileDescription(ImageCms.ImageCmsProfile(io.BytesIO(icc))).strip()
    except Exception:
        return "embedded profile"


def parse_icc(icc):
    """Classify an ICC profile: ('srgb'|'matrix'|'lut'|'unknown', extra)."""
    try:
        if icc[16:20] != b"RGB ":
            return "unknown", None
        n = struct.unpack(">I", icc[128:132])[0]
        tags = {}
        for i in range(n):
            sig, off, sz = struct.unpack(">4sII", icc[132 + 12 * i:144 + 12 * i])
            tags[sig.decode("latin-1")] = (off, sz)
        if all(k in tags for k in ("rXYZ", "gXYZ", "bXYZ", "rTRC", "gTRC", "bTRC")):
            M = np.zeros((3, 3))
            for j, k in enumerate(("rXYZ", "gXYZ", "bXYZ")):
                off, sz = tags[k]
                M[:, j] = [_s15(icc[off + 8 + 4 * i:off + 12 + 4 * i]) for i in range(3)]
            trcs = []
            for k in ("rTRC", "gTRC", "bTRC"):
                off, sz = tags[k]
                trcs.append(_parse_curve(icc[off:off + sz]))
            xs = np.linspace(0, 1, 1025)
            ref = srgb_decode(xs)
            is_srgb = np.abs(M - SRGB_M).max() < 0.002 and all(np.abs(t(xs) - ref).max() < 0.002 for t in trcs)
            return ("srgb" if is_srgb else "matrix"), (M, trcs)
        if "A2B0" in tags:
            return "lut", None
    except Exception:
        pass
    return "unknown", None


def to_srgb(img, icc, srgb_icc):
    """img: float32 HxWx3 encoded in the source profile. Returns (float32 sRGB image, note)."""
    if not icc:
        return img, "no profile, assumed sRGB"
    desc = icc_description(icc)
    kind, info = parse_icc(icc)
    if kind == "srgb":
        return img, "%s (= sRGB, no conversion)" % desc
    if kind == "matrix":
        M, trcs = info
        xs = np.linspace(0, 1, 4097)
        for c in range(3):
            lut = trcs[c](xs).astype(np.float32)
            img[:, :, c] = np.interp(img[:, :, c], xs.astype(np.float32), lut)
        T = (np.linalg.inv(SRGB_M) @ M).astype(np.float32)
        lin = img.reshape(-1, 3) @ T.T
        return srgb_encode(lin).reshape(img.shape).astype(np.float32), "%s converted to sRGB" % desc
    try:
        im8 = Image.fromarray(to_uint8_dither(img), "RGB")
        src = ImageCms.ImageCmsProfile(io.BytesIO(icc))
        dst = ImageCms.ImageCmsProfile(io.BytesIO(srgb_icc))
        im8 = ImageCms.profileToProfile(im8, src, dst, renderingIntent=ImageCms.Intent.RELATIVE_COLORIMETRIC, outputMode="RGB")
        return np.asarray(im8).astype(np.float32) / 255.0, "%s converted to sRGB (LittleCMS)" % desc
    except Exception:
        return img, "%s: profile not understood, colours left as is" % desc

# ----------------------------------------------------------------------------- reading (memory-safe for very large files)


def _float_rgb(arr):
    """Any uint8/uint16/float array, 2-D or 3-D with 1/2/3/4 channels -> float32 HxWx3 in [0,1] (alpha composited on white)."""
    if arr.dtype == np.uint8:
        f = arr.astype(np.float32) / 255.0
    elif arr.dtype == np.uint16:
        f = arr.astype(np.float32) / 65535.0
    elif arr.dtype in (np.float32, np.float64, np.float16):
        f = np.clip(arr.astype(np.float32), 0, 1)
    else:
        raise ValueError("unsupported pixel type %s" % arr.dtype)
    if f.ndim == 2:
        f = f[:, :, None]
    ch = f.shape[2]
    if ch == 1:
        f = np.repeat(f, 3, axis=2)
    elif ch == 2:
        f = np.repeat(f[:, :, :1], 3, axis=2) * f[:, :, 1:2] + (1 - f[:, :, 1:2])
    elif ch == 4:
        a = f[:, :, 3:4]
        f = f[:, :, :3] * a + (1 - a)
    elif ch > 4:
        f = f[:, :, :3]
    return np.ascontiguousarray(f, dtype=np.float32)


def _apply_orientation(arr, o):
    if o == 2:
        return arr[:, ::-1]
    if o == 3:
        return arr[::-1, ::-1]
    if o == 4:
        return arr[::-1]
    if o == 5:
        return np.rot90(arr[:, ::-1], 1)
    if o == 6:
        return np.rot90(arr, -1)
    if o == 7:
        return np.rot90(arr[:, ::-1], -1)
    if o == 8:
        return np.rot90(arr, 1)
    return arr


def reduce_factor(w, target_w):
    """Integer box-reduction factor that keeps at least 2x the target width for the Lanczos stage."""
    return max(1, int(w // (2 * target_w)))


def to_float_reduced(arr, f, band_px=4_000_000):
    """Convert a big uint8/uint16 array (or memmap) to float32 RGB in [0,1], box-averaged by integer factor f,
    working in horizontal bands so only a few MB are converted at a time. Partial edge blocks are averaged, never dropped."""
    H0, W0 = arr.shape[:2]
    Hr, Wr = -(-H0 // f), -(-W0 // f)
    out = np.empty((Hr, Wr, 3), np.float32)
    rows = max(f, (max(1, band_px // max(W0, 1)) // f) * f)
    ix = np.arange(0, W0, f)
    wx = np.diff(np.append(ix, W0)).astype(np.float32)
    for y0 in range(0, H0, rows):
        y1 = min(H0, y0 + rows)
        band = _float_rgb(np.asarray(arr[y0:y1]))
        if f == 1:
            out[y0:y1] = band
            continue
        iy = np.arange(0, y1 - y0, f)
        hy = np.diff(np.append(iy, y1 - y0)).astype(np.float32)
        s = np.add.reduceat(np.add.reduceat(band, iy, axis=0), ix, axis=1)
        s /= hy[:, None, None] * wx[None, :, None]
        out[y0 // f: y0 // f + len(iy)] = s
    return out


def png_bit_depth(path):
    try:
        with open(path, "rb") as fh:
            if fh.read(8) != b"\x89PNG\r\n\x1a\n":
                return None
            fh.read(8)
            return fh.read(13)[8]
    except Exception:
        return None


def read_image(path, target_w=WIDTH):
    """Returns (float32 HxWx3 in [0,1] already box-reduced to >= 2x target_w wide, (W0, H0) original size after orientation,
    icc bytes or None, list of notes)."""
    ext = os.path.splitext(path)[1].lower()
    notes = []
    if ext in (".tif", ".tiff") and tifffile is not None:
        try:
            with tifffile.TiffFile(path) as tf:
                page = tf.pages[0]
                pm = page.photometric
                if pm in (tifffile.PHOTOMETRIC.RGB, tifffile.PHOTOMETRIC.MINISBLACK):
                    t = page.tags.get("InterColorProfile")
                    icc = bytes(t.value) if t is not None else None
                    t = page.tags.get("Orientation")
                    o = int(t.value) if t is not None else 1
                    bits = page.bitspersample if isinstance(page.bitspersample, int) else page.bitspersample[0]
                    try:
                        arr = page.asarray(out="memmap")          # uncompressed files stream from disk, no RAM copy
                    except Exception:
                        arr = page.asarray()
                    H0, W0 = arr.shape[:2]
                    f = reduce_factor(W0 if o < 5 else H0, target_w)
                    img = to_float_reduced(arr, f)
                    del arr
                    notes.append("%d-bit TIFF" % bits)
                    img = _apply_orientation(img, o)
                    if o != 1:
                        notes.append("orientation applied")
                    if o >= 5:
                        W0, H0 = H0, W0
                    if page.samplesperpixel == 4 or (page.extrasamples and len(page.extrasamples)):
                        notes.append("alpha flattened on white")
                    return np.ascontiguousarray(img), (W0, H0), icc, notes
        except MemoryError:
            raise
        except Exception as e:
            notes.append("tifffile could not read it (%s), used Pillow" % e.__class__.__name__)
    if ext == ".png" and cv2 is not None:
        try:
            raw = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
            if raw is not None and raw.dtype == np.uint16:
                if raw.ndim == 3:
                    raw = raw[:, :, [2, 1, 0, 3]] if raw.shape[2] == 4 else raw[:, :, ::-1]
                with Image.open(path) as im:
                    icc = im.info.get("icc_profile")
                H0, W0 = raw.shape[:2]
                img = to_float_reduced(raw, reduce_factor(W0, target_w))
                notes.append("16-bit PNG")
                return img, (W0, H0), icc, notes
        except MemoryError:
            raise
        except Exception:
            pass
    im = Image.open(path)
    icc = im.info.get("icc_profile")
    W0, H0 = im.size
    try:
        o = im.getexif().get(274, 1)
    except Exception:
        o = 1
    if im.format == "JPEG":
        try:
            im.draft(im.mode if im.mode in ("RGB", "L", "CMYK") else "RGB", (2 * target_w, int(2 * target_w * H0 / W0) + 1))   # DCT-domain downscale for huge JPEGs
        except Exception:
            pass
    im.load()
    fmt = im.format or ext.strip(".").upper()
    if ext == ".png":
        bd = png_bit_depth(path)
        notes.append("%d-bit PNG" % bd if bd else "PNG")
        if bd and bd > 8 and im.mode == "RGB":
            notes[-1] += " read at 8 bits"
    else:
        notes.append("%s %s" % (fmt, im.mode))
    if im.mode == "CMYK":
        try:
            src = ImageCms.ImageCmsProfile(io.BytesIO(icc)) if icc else None
            if src is not None:
                dst = ImageCms.ImageCmsProfile(io.BytesIO(SRGB_ICC))
                im = ImageCms.profileToProfile(im, src, dst, renderingIntent=ImageCms.Intent.RELATIVE_COLORIMETRIC, outputMode="RGB")
                icc = SRGB_ICC
                notes.append("CMYK converted to sRGB")
            else:
                im = im.convert("RGB")
                icc = None
                notes.append("CMYK converted without a profile")
        except Exception:
            im = im.convert("RGB")
            icc = None
    if im.mode == "I;16":
        arr = np.asarray(im).astype(np.uint16)
    elif im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info):
        arr = np.asarray(im.convert("RGBA"))
        notes.append("alpha flattened on white")
    else:
        arr = np.asarray(im if im.mode == "RGB" else im.convert("RGB"))
    im.close()
    img = to_float_reduced(arr, reduce_factor(arr.shape[1], target_w))
    del arr
    img = _apply_orientation(img, o)
    if o not in (1, None):
        notes.append("orientation applied")
        if o >= 5:
            W0, H0 = H0, W0
    return np.ascontiguousarray(img), (W0, H0), icc, notes

# ----------------------------------------------------------------------------- processing


def plan(W0, H0, target_w):
    """Output geometry: the photo's own ratio at target_w wide (never cropped, padded or enlarged).
    Returns dict(img_w, img_h, enlarge_blocked, ratio, inside) where inside says whether Instagram's 3:4..1.91:1 frame fits it."""
    r = W0 / H0
    inside = IG_MIN_RATIO - 1e-6 <= r <= IG_MAX_RATIO + 1e-6
    w = min(target_w, W0)
    h = max(1, int(round(w * H0 / W0)))
    return dict(img_w=w, img_h=h, enlarge_blocked=W0 < target_w, ratio=r, inside=inside)


def resize_lanczos(img, w, h):
    H0, W0 = img.shape[:2]
    if (W0, H0) == (w, h):
        return img
    out = np.empty((h, w, 3), np.float32)
    for c in range(3):
        ch = Image.fromarray(np.ascontiguousarray(img[:, :, c]), mode="F")
        out[:, :, c] = np.asarray(ch.resize((w, h), Image.Resampling.LANCZOS, reducing_gap=None), dtype=np.float32)
    return out


def gaussian_blur(Y, sigma):
    """Exact separable Gaussian blur with reflected edges (no OpenCV needed)."""
    r = max(1, int(math.ceil(3 * sigma)))
    x = np.arange(-r, r + 1, dtype=np.float32)
    k = np.exp(-0.5 * (x / sigma) ** 2).astype(np.float32)
    k /= k.sum()
    H, W = Y.shape
    P = np.pad(Y, ((0, 0), (r, r)), mode="reflect")
    tmp = np.zeros((H, W), np.float32)
    for i in range(2 * r + 1):
        tmp += k[i] * P[:, i:i + W]
    P = np.pad(tmp, ((r, r), (0, 0)), mode="reflect")
    out = np.zeros((H, W), np.float32)
    for i in range(2 * r + 1):
        out += k[i] * P[i:i + H, :]
    return out


def sharpen_luma(img, sigma=0.7, amount=0.6, threshold=1.0 / 255):
    Y = (0.2126 * img[:, :, 0] + 0.7152 * img[:, :, 1] + 0.0722 * img[:, :, 2]).astype(np.float32)
    hp = Y - gaussian_blur(Y, sigma)
    m = np.clip((np.abs(hp) - 0.5 * threshold) / threshold, 0, 1)
    return np.clip(img + (hp * m * amount)[:, :, None], 0, 1)


def to_uint8_dither(img, seed=1234):
    rng = np.random.default_rng(seed)
    noise = rng.random(img.shape, dtype=np.float32) - 0.5
    return np.clip(np.rint(img * 255.0 + noise), 0, 255).astype(np.uint8)


def nice_ratio(w, h):
    r = w / h
    common = [("1:1", 1.0), ("4:5", 0.8), ("3:4", 0.75), ("2:3", 2 / 3), ("9:16", 9 / 16), ("5:4", 1.25), ("4:3", 4 / 3),
              ("3:2", 1.5), ("16:9", 16 / 9), ("1.91:1", 1.91), ("5:7", 5 / 7), ("7:5", 1.4)]
    name, v = min(common, key=lambda t: abs(t[1] - r))
    if abs(v - r) < 0.006:
        return name
    return "%.2f:1" % r if r >= 1 else "1:%.2f" % (1 / r)


def short_path(p, keep=2):
    """'C:\\a\\b\\c\\d' -> '…\\c\\d' for status lines."""
    parts = os.path.normpath(p).split(os.sep)
    return p if len(parts) <= keep + 1 else "…" + os.sep + os.sep.join(parts[-keep:])


def unique_path(path, overwrite):
    if overwrite or not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    i = 2
    while os.path.exists("%s (%d)%s" % (base, i, ext)):
        i += 1
    return "%s (%d)%s" % (base, i, ext)


def process_file(path, settings, srgb_icc, progress=None):
    """Full pipeline for one file. Returns a result dict."""
    t0 = time.time()
    if progress:
        progress("reading")
    target_w = int(settings.get("width", WIDTH))
    img, (W0, H0), icc, notes = read_image(path, target_w)
    p = plan(W0, H0, target_w)
    if progress:
        progress("resizing")
    small = resize_lanczos(img, p["img_w"], p["img_h"])
    del img
    small, cnote = to_srgb(small, icc, srgb_icc)
    notes.append(cnote)
    if settings.get("sharpen", SHARPEN):
        small = sharpen_luma(small)
    if not p["inside"]:
        notes.append("%s is %s than Instagram's %s frame; Instagram shows it trimmed unless you tap its fit button" % (
            nice_ratio(W0, H0), "taller" if p["ratio"] < IG_MIN_RATIO else "wider", "3:4" if p["ratio"] < IG_MIN_RATIO else "1.91:1"))
    if p["enlarge_blocked"]:
        notes.append("source is only %d px wide, not enlarged" % W0)
    a8 = to_uint8_dither(small)
    del small
    if progress:
        progress("saving")
    if settings["output_mode"] == "folder" and settings.get("output_dir"):
        out_dir = settings["output_dir"]
    else:
        out_dir = os.path.join(os.path.dirname(path), SUBFOLDER)
    os.makedirs(out_dir, exist_ok=True)
    base = os.path.splitext(os.path.basename(path))[0]
    out_path = unique_path(os.path.join(out_dir, "%s_ig%d.jpg" % (base, a8.shape[1])), settings.get("overwrite", False))
    Image.fromarray(a8, "RGB").save(out_path, "JPEG", quality=int(settings.get("quality", QUALITY)), subsampling=0, optimize=True,
                                    progressive=False, icc_profile=srgb_icc, dpi=(72, 72))
    return dict(source=path, output=out_path, in_size=(W0, H0), out_size=(a8.shape[1], a8.shape[0]), notes=notes,
                bytes=os.path.getsize(out_path), seconds=time.time() - t0, inside=p["inside"])

# ----------------------------------------------------------------------------- settings


def load_settings():
    s = dict(DEFAULTS)
    path = SETTINGS_FILE
    if not os.path.exists(path) and FROZEN:
        for old in OLD_APP_NAMES:                                               # keep settings across the rename
            cand = os.path.join(os.path.dirname(SETTINGS_DIR), old, "settings.json")
            if os.path.exists(cand):
                path = cand
                break
    try:
        with open(path, "r", encoding="utf-8") as fh:
            s.update({k: v for k, v in json.load(fh).items() if k in DEFAULTS})
    except Exception:
        pass
    if s.get("ui_scale") not in [v for _, v in UI_SCALES]:
        s["ui_scale"] = 1.0
    return s


def save_settings(s):
    try:
        os.makedirs(SETTINGS_DIR, exist_ok=True)
        with open(SETTINGS_FILE, "w", encoding="utf-8") as fh:
            json.dump(s, fh, indent=1)
    except Exception:
        pass


def open_path(p):
    """Reveal a file or folder with the platform's opener."""
    try:
        if IS_WIN:
            open_path(p)
        elif IS_MAC:
            subprocess.Popen(["open", p])
        else:
            subprocess.Popen(["xdg-open", p])
    except Exception as e:
        log_error("open_path %s" % p, str(e))


def collect_files(paths):
    files = []
    for p in paths:
        p = p.strip()
        if os.path.isdir(p):
            for name in sorted(os.listdir(p)):
                fp = os.path.join(p, name)
                if os.path.isfile(fp) and os.path.splitext(name)[1].lower() in IMAGE_EXTS:
                    files.append(fp)
        elif os.path.isfile(p) and os.path.splitext(p)[1].lower() in IMAGE_EXTS:
            files.append(p)
    return files

# ----------------------------------------------------------------------------- theme: Are.na "Dusk" (from are.na's own theme table)
DUSK = dict(gray0="#16171E", gray1="#24222C", gray2="#342E38", gray3="#4F4756", gray4="#81738C", gray5="#A798B3",
            gray6="#D5CADE", gray7="#E7DBF0", red3="#CB76A9", green3="#98DC89", blue2="#5E6DEE", blue3="#C1C4EA", alert="#FF7A30")
COLORS = dict(bg=DUSK["gray0"],            # are.na "background"
              bg2=DUSK["gray1"],           # hover / secondary surface
              hair=DUSK["gray2"],          # dividers, pill borders
              hair_soft=DUSK["gray1"],     # row separators
              ink=DUSK["gray7"],           # are.na "foreground"
              ink2=DUSK["gray6"],          # are.na "link"
              mute=DUSK["gray5"],          # are.na "slate"
              mute2=DUSK["gray4"],         # disabled / faint
              paper_ink=DUSK["gray0"],     # text on an ink-filled pill
              focus=DUSK["blue2"],         # are.na "focus": used for the drag-over state
              error=DUSK["red3"])


class Theme:
    """Fixed Dusk palette; components register and get restyled once (kept as a hook for widgets created later)."""
    def __init__(self):
        self.c = dict(COLORS)
        self.listeners = []

    def add(self, obj):
        self.listeners.append(obj)
        obj.restyle(self.c)

# ----------------------------------------------------------------------------- widgets


def round_rect(cv, x1, y1, x2, y2, r, **kw):
    r = max(1, min(r, (x2 - x1) / 2, (y2 - y1) / 2))
    pts = [x1 + r, y1, x1 + r, y1, x2 - r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y1 + r, x2, y2 - r, x2, y2 - r, x2, y2,
           x2 - r, y2, x2 - r, y2, x1 + r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y2 - r, x1, y1 + r, x1, y1 + r, x1, y1]
    return cv.create_polygon(pts, smooth=True, splinesteps=24, **kw)


class Fonts:
    def __init__(self, root, factor=1.0):
        fams = set(tkfont.families(root))

        def pick(cands):
            for f in cands:
                if f in fams:
                    return f
            return cands[-1]

        def pt(base):
            return max(6, int(round(base * factor)))
        self.sans = pick(["Geist", "Inter", "Helvetica Neue", "Segoe UI"])
        self.sans_med = pick(["Geist Medium", "Inter Medium", "Segoe UI Semibold", ""])
        self.mono = pick(["Geist Mono", "Menlo", "Cascadia Mono", "Consolas"])
        self.mono_med = pick(["Geist Mono Medium", "Cascadia Mono SemiBold", self.mono])
        # macOS groups weights under one family name, so a "medium" face is asked for as weight bold on the base family
        med = (self.sans_med,) if self.sans_med else (self.sans,)
        med_w = () if self.sans_med else ("bold",)
        self.body = (self.sans, pt(10))
        self.body_med = med + (pt(10),) + med_w
        self.head = med + (pt(15),) + med_w
        self.mono9 = (self.mono, pt(9))
        self.mono8 = (self.mono, pt(8))
        self.mono9m = (self.mono_med, pt(9))
        self.mono9u = tkfont.Font(family=self.mono, size=pt(9), underline=True)


class Hairline(tk.Frame):
    def __init__(self, parent, key="hair", **kw):
        super().__init__(parent, height=1, bd=0, highlightthickness=0, **kw)
        self.key = key

    def restyle(self, c):
        self.configure(bg=c[self.key])


class Label(tk.Label):
    """A tk.Label that follows the theme: role is one of ink, ink2, mute, mute2, error."""
    def __init__(self, parent, role="ink", bgkey="bg", **kw):
        super().__init__(parent, bd=0, highlightthickness=0, **kw)
        self.role, self.bgkey = role, bgkey

    def restyle(self, c):
        self.configure(bg=c[self.bgkey], fg=c[self.role])


class Panel(tk.Frame):
    def __init__(self, parent, bgkey="bg", **kw):
        super().__init__(parent, bd=0, highlightthickness=0, **kw)
        self.bgkey = bgkey

    def restyle(self, c):
        self.configure(bg=c[self.bgkey])


class TextLink(tk.Label):
    """Mono text link: hover underlines it and lifts to ink."""
    def __init__(self, parent, text, command, fonts, role="ink2", **kw):
        super().__init__(parent, text=text, bd=0, highlightthickness=0, cursor="hand2", font=fonts.mono9, **kw)
        self.fonts, self.role, self.command, self.c = fonts, role, command, None
        self.bind("<Enter>", lambda e: self._hover(True))
        self.bind("<Leave>", lambda e: self._hover(False))
        self.bind("<Button-1>", lambda e: self.command())

    def _hover(self, on):
        if self.c:
            self.configure(font=self.fonts.mono9u if on else self.fonts.mono9, fg=self.c["ink"] if on else self.c[self.role])

    def restyle(self, c):
        self.c = c
        self.configure(bg=c["bg"], fg=c[self.role])


class IconButton(tk.Canvas):
    """Window control: a small glyph ('close' or 'minimize') that gets a hairline ring on hover."""
    def __init__(self, parent, kind, command, scale=1.0, **kw):
        d = int(26 * scale)
        super().__init__(parent, width=d, height=d, bd=0, highlightthickness=0, cursor="hand2", **kw)
        self.kind, self.command, self.s, self.d = kind, command, scale, d
        self.c = None
        self.hover = False
        self.bind("<Enter>", lambda e: self._hov(True))
        self.bind("<Leave>", lambda e: self._hov(False))
        self.bind("<Button-1>", lambda e: self.command())

    def _hov(self, on):
        self.hover = on
        self.draw()

    def draw(self):
        self.delete("all")
        if not self.c:
            return
        c = self.c
        self.configure(bg=c["bg"])
        d, s = self.d, self.s
        fg = c["ink"] if self.hover else c["mute"]
        if self.hover:
            self.create_oval(1, 1, d - 2, d - 2, outline=c["hair"], fill=c["bg2"])
        m = d / 2
        g = 4 * s
        if self.kind == "close":
            self.create_line(m - g, m - g, m + g, m + g, fill=fg, width=1.2)
            self.create_line(m + g, m - g, m - g, m + g, fill=fg, width=1.2)
        else:
            self.create_line(m - g, m + 1, m + g, m + 1, fill=fg, width=1.2)

    def restyle(self, c):
        self.c = c
        self.draw()


class Pills(tk.Canvas):
    """Segmented mono pills. Active pill is ink on background."""
    def __init__(self, parent, fonts, options, value, command, scale=1.0, **kw):
        super().__init__(parent, bd=0, highlightthickness=0, height=int(28 * scale), **kw)
        self.fonts, self.options, self.value, self.command, self.s = fonts, options, value, command, scale
        self.c = None
        self.hover = None
        self.boxes = []
        self.bind("<Motion>", self._motion)
        self.bind("<Leave>", lambda e: self._set_hover(None))
        self.bind("<Button-1>", self._click)
        self.configure(cursor="hand2")
        self._layout()

    def _layout(self):
        f = tkfont.Font(font=self.fonts.mono9)
        x = 0
        self.boxes = []
        h = int(24 * self.s)
        for key, text in self.options:
            w = f.measure(text) + int(22 * self.s)
            self.boxes.append((key, text, x, 2, x + w, 2 + h))
            x += w + int(6 * self.s)
        self.configure(width=x)
        self.draw()

    def draw(self):
        self.delete("all")
        if not self.c:
            return
        c = self.c
        self.configure(bg=c["bg"])
        for key, text, x1, y1, x2, y2 in self.boxes:
            active = key == self.value
            hov = key == self.hover
            if active:
                round_rect(self, x1, y1, x2, y2, (y2 - y1) / 2, fill=c["ink"], outline=c["ink"])
                fg = c["paper_ink"]
            else:
                round_rect(self, x1, y1, x2, y2, (y2 - y1) / 2, fill=c["bg2"] if hov else c["bg"], outline=c["mute"] if hov else c["hair"])
                fg = c["ink"] if hov else c["ink2"]
            self.create_text((x1 + x2) / 2, (y1 + y2) / 2 + 1, text=text, font=self.fonts.mono9, fill=fg)

    def _at(self, x, y):
        for key, text, x1, y1, x2, y2 in self.boxes:
            if x1 <= x <= x2 and y1 <= y <= y2:
                return key
        return None

    def _motion(self, e):
        self._set_hover(self._at(e.x, e.y))

    def _set_hover(self, key):
        if key != self.hover:
            self.hover = key
            self.draw()

    def _click(self, e):
        key = self._at(e.x, e.y)
        if key is not None and key != self.value:
            self.value = key
            self.draw()
            self.command(key)

    def set(self, key):
        self.value = key
        self.draw()

    def restyle(self, c):
        self.c = c
        self.draw()


class DotToggle(tk.Canvas):
    """A dot (filled when on, hollow when off) and a lowercase mono word inside a hairline pill."""
    def __init__(self, parent, fonts, text, value, command, scale=1.0, **kw):
        super().__init__(parent, bd=0, highlightthickness=0, height=int(28 * scale), **kw)
        self.fonts, self.text, self.value, self.command, self.s = fonts, text, value, command, scale
        self.c = None
        self.hover = False
        f = tkfont.Font(font=self.fonts.mono9)
        self.w = f.measure(text) + int(38 * scale)
        self.configure(width=self.w, cursor="hand2")
        self.bind("<Enter>", lambda e: self._hov(True))
        self.bind("<Leave>", lambda e: self._hov(False))
        self.bind("<Button-1>", self._click)

    def _hov(self, on):
        self.hover = on
        self.draw()

    def _click(self, e):
        self.value = not self.value
        self.draw()
        self.command(self.value)

    def set(self, value):
        self.value = value
        self.draw()

    def draw(self):
        self.delete("all")
        if not self.c:
            return
        c = self.c
        self.configure(bg=c["bg"])
        h = int(24 * self.s)
        fg = c["ink"] if self.hover else c["ink2"]
        round_rect(self, 0, 2, self.w - 1, 2 + h, h / 2, fill=c["bg2"] if self.hover else c["bg"], outline=c["mute"] if self.hover else c["hair"])
        d = int(8 * self.s)
        cx, cy = int(15 * self.s), 2 + h / 2
        if self.value:
            self.create_oval(cx - d / 2, cy - d / 2, cx + d / 2, cy + d / 2, fill=fg, outline=fg)
        else:
            self.create_oval(cx - d / 2, cy - d / 2, cx + d / 2, cy + d / 2, fill="", outline=fg, width=1.5)
        self.create_text(cx + d / 2 + int(7 * self.s), cy + 1, text=self.text, anchor="w", font=self.fonts.mono9, fill=fg)

    def restyle(self, c):
        self.c = c
        self.draw()


class DropZone(tk.Canvas):
    """Dashed hairline field with the headline, the click hint and the baked-in recipe in mono."""
    def __init__(self, parent, fonts, on_click, scale=1.0, **kw):
        super().__init__(parent, bd=0, highlightthickness=0, cursor="hand2", **kw)
        self.fonts, self.on_click, self.s = fonts, on_click, scale
        self.c = None
        self.state = "idle"        # idle | hover | drag
        self.headline = "drop photos or folders here" if HAVE_DND else "click to choose photos"
        self.hint = "or click to choose files" if HAVE_DND else "drag-and-drop needs the tkinterdnd2 package"
        self.bind("<Configure>", lambda e: self.draw())
        self.bind("<Enter>", lambda e: self.set_state("hover"))
        self.bind("<Leave>", lambda e: self.set_state("idle"))
        self.bind("<Button-1>", lambda e: self.on_click())

    def set_state(self, st):
        self.state = st
        self.draw()

    def draw(self):
        self.delete("all")
        if not self.c:
            return
        c = self.c
        self.configure(bg=c["bg"])
        w, h = self.winfo_width(), self.winfo_height()
        if w < 10 or h < 10:
            return
        s = self.s
        bg = c["bg2"] if self.state == "drag" else c["bg"]
        outline = c["focus"] if self.state == "drag" else (c["mute"] if self.state == "hover" else c["hair"])
        round_rect(self, 1, 1, w - 2, h - 2, int(10 * s), fill=bg, outline=outline, dash=(4, 4) if self.state != "drag" else None)
        cy = h / 2
        self.create_text(w / 2, cy - int(12 * s), text=self.headline, font=self.fonts.head, fill=c["ink"])
        self.create_text(w / 2, cy + int(16 * s), text=self.hint, font=self.fonts.body, fill=c["ink2"])

    def restyle(self, c):
        self.c = c
        self.draw()


class ProgressLine(tk.Canvas):
    """Hairline track with an ink bar and a little diamond playhead."""
    def __init__(self, parent, scale=1.0, **kw):
        super().__init__(parent, bd=0, highlightthickness=0, height=int(14 * scale), **kw)
        self.s = scale
        self.c = None
        self.frac = None
        self.bind("<Configure>", lambda e: self.draw())

    def set(self, frac):
        self.frac = frac
        self.draw()

    def draw(self):
        self.delete("all")
        if not self.c:
            return
        c = self.c
        self.configure(bg=c["bg"])
        w, h = self.winfo_width(), self.winfo_height()
        y = h / 2
        self.create_line(0, y, w, y, fill=c["hair"], width=1)
        if self.frac is None:
            return
        x = max(0, min(w, w * self.frac))
        self.create_line(0, y, x, y, fill=c["ink"], width=2)
        d = int(4 * self.s)
        self.create_polygon(x, y - d, x + d, y, x, y + d, x - d, y, fill=c["ink"], outline=c["ink"])

    def restyle(self, c):
        self.c = c
        self.draw()


class Row(Panel):
    """One export in the list: name + notes on the left, dims + meta on the right, hairline below."""
    def __init__(self, parent, fonts, app, path, scale=1.0):
        super().__init__(parent)
        self.fonts, self.app, self.path, self.s = fonts, app, path, scale
        self.file = os.path.basename(path)
        self.state = "queued"
        self.output = None
        self.dims_text = ""
        self.hover = False
        self.error_text = ""
        self.name = Label(self, role="ink", text=self.file, font=fonts.body, anchor="w")
        self.name.grid(row=0, column=0, sticky="ew", pady=(int(12 * scale), 0))
        self.notes = Label(self, role="mute", text="queued", font=fonts.mono8, anchor="w", justify="left")
        self.notes.grid(row=1, column=0, sticky="ew", pady=(int(3 * scale), int(12 * scale)))
        self.dims = Label(self, role="ink2", text="", font=fonts.mono9, anchor="e")
        self.dims.grid(row=0, column=1, sticky="e", padx=(int(16 * scale), 0), pady=(int(12 * scale), 0))
        self.meta = Label(self, role="mute", text="", font=fonts.mono8, anchor="e")
        self.meta.grid(row=1, column=1, sticky="e", padx=(int(16 * scale), 0), pady=(int(3 * scale), int(12 * scale)))
        self.columnconfigure(0, weight=1)
        self.line = Hairline(self, key="hair_soft")
        self.line.grid(row=2, column=0, columnspan=2, sticky="ew")
        for w in (self, self.name, self.notes, self.dims, self.meta):
            w.bind("<Enter>", lambda e: self._hover(True))
            w.bind("<Leave>", lambda e: self._hover(False))
            w.bind("<Double-1>", lambda e: self.app.open_row(self))
        self.c = None

    def set_status(self, text):
        self.state = "working"
        self.notes.configure(text=text)

    def set_done(self, res):
        self.state = "done"
        self.output = res["output"]
        self.dims_text = "%dx%d" % res["out_size"]
        self.dims.configure(text="%d×%d → %d×%d" % (res["in_size"][0], res["in_size"][1], res["out_size"][0], res["out_size"][1]))
        self.meta.configure(text="%s · %.1f mb · %.1fs" % (nice_ratio(*res["in_size"]).lower(), res["bytes"] / 1e6, res["seconds"]))
        bits = []
        for n in res["notes"]:
            low = n.lower()
            if "frame" in low:
                bits.append("%s · %s than instagram's %s frame" % (nice_ratio(*res["in_size"]).lower(), "taller" if "taller" in low else "wider", "3:4" if "taller" in low else "1.91:1"))
            elif "converted to srgb" in low:
                bits.append(low.split(" converted")[0] + " → srgb")
            elif "not enlarged" in low:
                bits.append("not enlarged · source is %d px wide" % res["in_size"][0])
            elif "orientation" in low:
                bits.append("rotated per exif")
            elif "alpha" in low:
                bits.append("transparency flattened on white")
            elif "tiff" in low or "png" in low:
                bits.append(low.replace("tifffile could not read it", "tiff").strip())
        self.notes.configure(text=" · ".join(bits))
        self.notes.role = "mute" if res.get("inside", True) else "ink2"
        if self.c:
            self.restyle(self.c)

    def set_error(self, text):
        self.state = "error"
        self.error_text = text
        self.notes.role = "error"
        self.notes.configure(text="error — " + text.lower())
        if self.c:
            self.restyle(self.c)

    def _hover(self, on):
        self.hover = on
        if self.c:
            self.restyle(self.c)

    def restyle(self, c):
        self.c = c
        bgkey = "bg2" if self.hover else "bg"
        self.configure(bg=c[bgkey])
        for w in (self.name, self.notes, self.dims, self.meta):
            w.bgkey = bgkey
            w.restyle(c)
        if self.hover:
            self.name.configure(fg=c["ink"])
        self.line.restyle(c)


class ResultsList(Panel):
    """Scrollable list of rows on a canvas, with a thin scroll indicator at the right edge (no scrollbar chrome)."""
    def __init__(self, parent, fonts, app, scale=1.0):
        super().__init__(parent)
        self.fonts, self.app, self.s = fonts, app, scale
        self.cv = tk.Canvas(self, bd=0, highlightthickness=0)
        self.cv.pack(side="left", fill="both", expand=True)
        self.ind = tk.Canvas(self, width=int(6 * scale), bd=0, highlightthickness=0)
        self.ind.pack(side="right", fill="y")
        self.inner = Panel(self.cv)
        self.win = self.cv.create_window(0, 0, window=self.inner, anchor="nw")
        self.rows = []
        self.c = None
        self.empty = Label(self.inner, role="mute2", text="", font=fonts.mono9, anchor="w")
        self.empty.pack(fill="x", pady=(int(18 * scale), 0))
        self.cv.bind("<Configure>", self._on_cv_configure)
        self.inner.bind("<Configure>", lambda e: self._scrollregion())
        for w in (self.cv, self.inner):
            w.bind("<MouseWheel>", self._wheel)

    def _on_cv_configure(self, e):          # (not "_configure": that name is tkinter's own internal method)
        self.cv.itemconfigure(self.win, width=e.width)
        self._scrollregion()

    def _scrollregion(self):
        self.cv.configure(scrollregion=self.cv.bbox("all") or (0, 0, 0, 0))
        self.draw_indicator()

    def _wheel(self, e):
        units = int(-e.delta / 120) if abs(e.delta) >= 120 else (-1 if e.delta > 0 else 1)   # Windows sends +-120, macOS small ints
        self.cv.yview_scroll(units, "units")
        self.draw_indicator()

    def bind_wheel(self, widget):
        widget.bind("<MouseWheel>", self._wheel)

    def add_row(self, path):
        self.empty.pack_forget()
        row = Row(self.inner, self.fonts, self.app, path, self.s)
        row.pack(fill="x")
        self.rows.append(row)
        if self.c:
            row.restyle(self.c)
        for w in (row, row.name, row.notes, row.dims, row.meta):
            self.bind_wheel(w)
        self.inner.update_idletasks()
        self._scrollregion()
        self.cv.yview_moveto(1.0)
        self.draw_indicator()
        return row

    def clear(self):
        for r in self.rows:
            r.destroy()
        self.rows = []
        self.empty.pack(fill="x", pady=(int(18 * self.s), 0))
        self._scrollregion()

    def draw_indicator(self):
        self.ind.delete("all")
        if not self.c:
            return
        self.ind.configure(bg=self.c["bg"])
        try:
            f0, f1 = self.cv.yview()
        except tk.TclError:
            return
        if f1 - f0 >= 0.999:
            return
        h = self.ind.winfo_height()
        x = int(3 * self.s)
        self.ind.create_line(x, 0, x, h, fill=self.c["hair"])
        self.ind.create_line(x, h * f0, x, h * f1, fill=self.c["ink2"], width=2)

    def restyle(self, c):
        self.c = c
        self.configure(bg=c["bg"])
        self.cv.configure(bg=c["bg"])
        self.inner.restyle(c)
        self.empty.restyle(c)
        for r in self.rows:
            r.restyle(c)
        self.draw_indicator()

# ----------------------------------------------------------------------------- frameless window (Win32 + DWM)


def toplevel_hwnd(root):
    return ctypes.windll.user32.GetParent(root.winfo_id())


_W32 = {}


def _user32():
    """user32 with the pointer-sized signatures declared (ctypes' default int conversion truncates HWND-typed arguments)."""
    if "u" not in _W32:
        from ctypes import wintypes
        u = ctypes.windll.user32
        u.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
        u.SetWindowPos.restype = wintypes.BOOL
        u.GetWindowLongPtrW.restype = ctypes.c_longlong
        u.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
        u.SetWindowLongPtrW.restype = ctypes.c_longlong
        u.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_longlong]
        u.SetClassLongPtrW.restype = ctypes.c_ulonglong
        u.SetClassLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_longlong]
        _W32["u"] = u
    return _W32["u"]


def win32_move(hwnd, x, y):
    _user32().SetWindowPos(hwnd, 0, int(x), int(y), 0, 0, 0x0001 | 0x0004 | 0x0010)     # NOSIZE | NOZORDER | NOACTIVATE


def make_frameless(root, border_hex, bg_hex="#16171E"):
    """Windows: drop the title bar and the resize frame but keep a real minimizable top-level window; round the corners on
    Windows 11. Other platforms keep their native title bar (returns None)."""
    if not IS_WIN:
        return None
    try:
        u = _user32()
        hwnd = toplevel_hwnd(root)
        if not hwnd:
            return None
        GWL_STYLE = -16
        WS_CAPTION, WS_THICKFRAME, WS_MINIMIZEBOX, WS_SYSMENU = 0x00C00000, 0x00040000, 0x00020000, 0x00080000
        style = u.GetWindowLongPtrW(hwnd, GWL_STYLE)
        style = (style & ~WS_CAPTION & ~WS_THICKFRAME) | WS_MINIMIZEBOX | WS_SYSMENU     # no title bar, no resize frame
        u.SetWindowLongPtrW(hwnd, GWL_STYLE, style)
        u.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x0020 | 0x0002 | 0x0001 | 0x0004)       # FRAMECHANGED | NOMOVE | NOSIZE | NOZORDER
        try:                                                                           # erase with the theme colour, never black
            rb, gb, bb = int(bg_hex[1:3], 16), int(bg_hex[3:5], 16), int(bg_hex[5:7], 16)
            brush = ctypes.windll.gdi32.CreateSolidBrush((bb << 16) | (gb << 8) | rb)
            if brush and "brush" not in _W32:
                _W32["brush"] = brush
                u.SetClassLongPtrW(hwnd, -10, brush)                                   # GCLP_HBRBACKGROUND of the wrapper class
                u.SetClassLongPtrW(root.winfo_id(), -10, brush)                        # ...and of Tk's inner window class
        except Exception:
            pass
        try:
            dwm = ctypes.windll.dwmapi
            pref = ctypes.c_int(2)                                                     # DWMWCP_ROUND
            dwm.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(pref), ctypes.sizeof(pref))
            r, g, b = int(border_hex[1:3], 16), int(border_hex[3:5], 16), int(border_hex[5:7], 16)
            col = ctypes.c_uint((b << 16) | (g << 8) | r)                              # COLORREF
            dwm.DwmSetWindowAttribute(hwnd, 34, ctypes.byref(col), ctypes.sizeof(col))  # DWMWA_BORDER_COLOR
            dark = ctypes.c_int(1)
            dwm.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(dark), ctypes.sizeof(dark))  # DWMWA_USE_IMMERSIVE_DARK_MODE
        except Exception:
            pass
        return hwnd
    except Exception:
        return None


# ----------------------------------------------------------------------------- app


class App:
    def __init__(self, root):
        self.root = root
        self.settings = load_settings()
        self.jobs = queue.Queue()
        self.events = queue.Queue()
        self.items = []                 # model: dicts with path/state/res/err/row; rows are views and get rebuilt on size change
        self.rows = []
        self.outputs = {}
        self.errors = {}
        self.total = 0
        self.done = 0
        self.total_bytes = 0
        self.last_out_dir = None
        self.dpi = max(0.75, root.winfo_fpixels("1i") / 96.0) if IS_WIN else 1.0     # macOS Tk already works in points
        self.ui_factor = float(self.settings.get("ui_scale", 1.0))
        self.theme = Theme()
        self.hwnd = None
        self.frameless = IS_WIN                                                          # custom window controls only where the title bar is removed
        root.title(APP_NAME)
        root.configure(bg=COLORS["bg"])
        root.report_callback_exception = lambda t, v, tb: log_error("tk callback", "".join(traceback.format_exception(t, v, tb)))
        self.v_outmode = tk.StringVar(value=self.settings["output_mode"])
        self.v_outdir = tk.StringVar(value=self.settings["output_dir"])
        self.v_overwrite = tk.BooleanVar(value=bool(self.settings["overwrite"]))
        self.v_open = tk.BooleanVar(value=bool(self.settings.get("open_on_export", False)))
        self.batch_dirs = []            # output folders written by the current batch, opened when it finishes (if enabled)
        root.resizable(False, False)    # the window comes in four fixed sizes; no edge dragging
        self._apply_scale(first=True)
        self._build()
        self._fit_window()
        if self.frameless:
            root.bind("<Map>", lambda e: self._apply_frameless() if e.widget is root else None)   # the Win32 wrapper exists once mapped
            root.after(0, self._apply_frameless)
        self.worker = threading.Thread(target=self._worker, daemon=True, name="opmize-worker")
        self.worker.start()
        root.after(100, self._poll)
        root.protocol("WM_DELETE_WINDOW", self._close)

    # ---- scale
    def _apply_scale(self, first=False):
        self.scale = self.dpi * self.ui_factor
        self.fonts = Fonts(self.root, self.ui_factor)

    def _fit_window(self):
        """Fixed window size for the current size option: a set width, and exactly the height the content needs."""
        self.root.update_idletasks()
        w = int(round(940 * self.scale))
        h = self.bgroot.winfo_reqheight()
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        w, h = min(w, sw - 40), min(h, sh - 80)
        self.root.minsize(w, h)
        self.root.maxsize(w, h)
        self.root.geometry("%dx%d" % (w, h))
        self.window_size = (w, h)

    def set_ui_scale(self, factor):
        """Rebuild the whole window at a new size; the exports list is restored from the model."""
        if factor == self.ui_factor:
            return
        self.ui_factor = factor
        self.settings["ui_scale"] = factor
        save_settings(dict(self.settings))
        self.bgroot.destroy()
        self.theme.listeners = []
        self._apply_scale()
        self._build()
        for item in self.items:
            self._view_for(item)
        self.rows = [it["row"] for it in self.items]
        self._update_counts()
        self.status.configure(text=self._last_status)
        if self.done < self.total:
            self.progress.set(self.done / max(self.total, 1))
        self._fit_window()

    def _view_for(self, item):
        row = self.list.add_row(item["path"])
        item["row"] = row
        if item["state"] == "done":
            row.set_done(item["res"])
        elif item["state"] == "error":
            row.set_error(item["err"])
        elif item["state"] == "working":
            row.set_status(item.get("status", "working…"))
        return row

    # ---- layout
    def _build(self):
        S = lambda px: int(round(px * self.scale))
        T, F = self.theme, self.fonts
        root = self.root
        self._last_status = getattr(self, "_last_status", "")
        self.bgroot = Panel(root)
        self.bgroot.pack(fill="both", expand=True)
        T.add(self.bgroot)
        PADX = S(28)

        # top band: wordmark and window controls; the whole band (padding included) is the drag handle
        top = Panel(self.bgroot)
        top.pack(fill="x")
        T.add(top)
        chrome = Panel(top)
        chrome.pack(fill="x", padx=PADX, pady=(S(14), S(12)))
        T.add(chrome)
        self.wordmark = Label(chrome, role="ink", text=APP_NAME.lower(), font=F.body_med, anchor="w")
        self.wordmark.pack(side="left")
        T.add(self.wordmark)
        self.btn_close = IconButton(chrome, "close", self._close, self.scale)
        self.btn_min = IconButton(chrome, "minimize", self.minimize, self.scale)
        if self.frameless:
            self.btn_close.pack(side="right", padx=(S(2), 0))
            self.btn_min.pack(side="right")
        T.add(self.btn_close)
        T.add(self.btn_min)
        hl = Hairline(top)
        hl.pack(fill="x", padx=PADX)
        T.add(hl)
        self.drag_band = (top, chrome, self.wordmark, hl)
        if self.frameless:
            for w in self.drag_band:
                w.configure(cursor="fleur")
                w.bind("<ButtonPress-1>", self._drag_start)
                w.bind("<B1-Motion>", self._drag_move)
                w.bind("<ButtonRelease-1>", self._drag_end)

        # drop zone
        self.drop = DropZone(self.bgroot, F, self.choose_files, self.scale, height=S(150))
        self.drop.pack(fill="x", padx=PADX, pady=(S(20), S(16)))
        T.add(self.drop)
        if HAVE_DND:
            self._register_drop(self.drop)

        # settings: three aligned rows, mono label column on the left
        opts = Panel(self.bgroot)
        opts.pack(fill="x", padx=PADX)
        T.add(opts)

        def settings_row(label, gap):
            r = Panel(opts)
            r.pack(fill="x", pady=(0, gap))
            T.add(r)
            lab = Label(r, role="mute", text=label, font=F.mono9, anchor="w", width=13)
            lab.pack(side="left")
            T.add(lab)
            return r
        r1 = settings_row("save to", S(8))
        self.pill_out = Pills(r1, F, [("subfolder", "next to each photo"), ("folder", "a folder i choose")], self.settings["output_mode"], self._set_outmode, self.scale)
        self.pill_out.pack(side="left")
        T.add(self.pill_out)
        self.outdir_label = Label(r1, role="mute2", text="", font=F.mono8, anchor="w")
        self.outdir_label.pack(side="left", fill="x", expand=True, padx=(S(14), 0))
        T.add(self.outdir_label)
        r2 = settings_row("after export", S(8))
        self.overwrite_toggle = DotToggle(r2, F, "overwrite existing", bool(self.settings["overwrite"]), self._set_overwrite, self.scale)
        self.overwrite_toggle.pack(side="left")
        T.add(self.overwrite_toggle)
        self.open_toggle = DotToggle(r2, F, "open folder when done", bool(self.v_open.get()), self._set_open, self.scale)
        self.open_toggle.pack(side="left", padx=(S(8), 0))
        T.add(self.open_toggle)
        r3 = settings_row("window size", 0)
        self.pill_size = Pills(r3, F, [(v, name) for name, v in UI_SCALES], self.ui_factor, self.set_ui_scale, self.scale)
        self.pill_size.pack(side="left")
        T.add(self.pill_size)
        self._update_outdir_label()

        # section header: exports · count
        sec = Panel(self.bgroot)
        sec.pack(fill="x", padx=PADX, pady=(S(18), 0))
        T.add(sec)
        st = Label(sec, role="ink", text="exports", font=F.body_med, anchor="w")
        st.pack(side="left")
        T.add(st)
        self.sec_meta = Label(sec, role="mute", text="0 files", font=F.mono9, anchor="e")
        self.sec_meta.pack(side="right")
        T.add(self.sec_meta)
        hl2 = Hairline(self.bgroot)
        hl2.pack(fill="x", padx=PADX, pady=(S(10), 0))
        T.add(hl2)

        # results list (a fixed minimum height; the window is sized to fit everything below it)
        self.list = ResultsList(self.bgroot, F, self, self.scale)
        self.list.cv.configure(height=S(250))
        self.list.pack(fill="both", expand=True, padx=PADX)
        T.add(self.list)
        if HAVE_DND:
            for w in (self.list.cv, self.list.inner):
                self._register_drop(w)

        # progress + status
        self.progress = ProgressLine(self.bgroot, self.scale)
        self.progress.pack(fill="x", padx=PADX, pady=(S(6), 0))
        T.add(self.progress)
        self.status = Label(self.bgroot, role="mute", text=self._last_status, font=F.mono9, anchor="w")
        self.status.pack(fill="x", padx=PADX, pady=(S(4), 0))
        T.add(self.status)

        # footer: links · signature
        foot = Panel(self.bgroot)
        foot.pack(fill="x", padx=PADX, pady=(S(10), S(18)))
        T.add(foot)
        for text, cmd in [("choose files", self.choose_files), ("open output folder", self.open_output), ("clear list", self.clear)]:
            lk = TextLink(foot, text, cmd, F)
            lk.pack(side="left", padx=(0, S(18)))
            T.add(lk)

    def _register_drop(self, w):
        w.drop_target_register(DND_FILES)
        w.dnd_bind("<<Drop>>", self._on_drop)
        w.dnd_bind("<<DragEnter>>", self._on_drag_enter)
        w.dnd_bind("<<DragLeave>>", self._on_drag_leave)

    def _apply_frameless(self):
        hwnd = make_frameless(self.root, COLORS["hair"], COLORS["bg"])
        if hwnd:
            self.hwnd = hwnd
        return hwnd

    # ---- moving the frameless window. On Windows each motion event moves the window directly with SetWindowPos
    # (zero lag, no repaint, no modal loop that could re-enter Python); elsewhere Tk's own geometry call is used.
    def _drag_start(self, e):
        if IS_WIN and self.hwnd:
            from ctypes import wintypes
            r = wintypes.RECT()
            ctypes.windll.user32.GetWindowRect(self.hwnd, ctypes.byref(r))
            self._drag = (e.x_root - r.left, e.y_root - r.top)
        else:
            self._drag = (e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y())

    def _drag_move(self, e):
        d = getattr(self, "_drag", None)
        if not d:
            return
        x, y = e.x_root - d[0], e.y_root - d[1]
        if IS_WIN and self.hwnd:
            win32_move(self.hwnd, x, y)
        else:
            self.root.geometry("+%d+%d" % (x, y))

    def _drag_end(self, e):
        self._drag = None

    # ---- settings
    def _set_outmode(self, key):
        if key == "folder" and not os.path.isdir(self.v_outdir.get().strip()):
            d = filedialog.askdirectory(title="Choose output folder")
            if not d:
                self.pill_out.set("subfolder")
                self.v_outmode.set("subfolder")
                self.current_settings()
                self._update_outdir_label()
                return
            self.v_outdir.set(d)
        self.v_outmode.set(key)
        self.current_settings()
        self._update_outdir_label()

    def _set_overwrite(self, value):
        self.v_overwrite.set(bool(value))
        self.current_settings()

    def _set_open(self, value):
        self.v_open.set(bool(value))
        self.current_settings()

    def _update_outdir_label(self):
        if self.v_outmode.get() == "folder" and self.v_outdir.get().strip():
            self.outdir_label.configure(text="→ %s" % short_path(self.v_outdir.get().strip(), keep=3))
        else:
            self.outdir_label.configure(text="")

    def current_settings(self):
        outmode = self.v_outmode.get()
        outdir = self.v_outdir.get().strip()
        if outmode == "folder" and not os.path.isdir(outdir):
            outmode = "subfolder"
        s = dict(output_mode=outmode, output_dir=outdir, overwrite=bool(self.v_overwrite.get()),
                 open_on_export=bool(self.v_open.get()), ui_scale=self.ui_factor)
        self.settings = s
        save_settings(s)
        return s

    # ---- actions
    def minimize(self):
        try:
            self.root.iconify()
        except Exception:
            pass

    def choose_files(self):
        paths = filedialog.askopenfilenames(title="Choose photos", filetypes=[
            ("Images", "*.tif *.tiff *.jpg *.jpeg *.png *.webp *.bmp *.psd *.heic *.heif"), ("All files", "*.*")])
        if paths:
            self.add(list(paths))

    def _on_drag_enter(self, event):
        self.drop.set_state("drag")
        return event.action

    def _on_drag_leave(self, event):
        self.drop.set_state("idle")
        return event.action

    def _on_drop(self, event):
        self.drop.set_state("idle")
        try:
            paths = list(self.root.tk.splitlist(event.data))
        except Exception:
            paths = [event.data]
        self.add(paths)
        return event.action

    def _set_status(self, text):
        self._last_status = text
        self.status.configure(text=text)

    def add(self, paths):
        files = collect_files(paths)
        if not files:
            messagebox.showinfo(APP_NAME, "No supported image files found in what you dropped.")
            return
        settings = self.current_settings()
        if self.done >= self.total:
            self.batch_dirs = []                                   # a new batch starts
        for f in files:
            item = dict(path=f, state="queued", res=None, err=None, row=None)
            self.items.append(item)
            self._view_for(item)
            self.rows.append(item["row"])
            self.total += 1
            self.jobs.put((item, f, settings))
        self._update_counts()
        self._set_status("queued %d file%s" % (len(files), "" if len(files) == 1 else "s"))

    def clear(self):
        if not self.jobs.empty():
            return
        self.list.clear()
        self.items = []
        self.rows = []
        self.outputs.clear()
        self.errors.clear()
        self.total = self.done = 0
        self.total_bytes = 0
        self.progress.set(None)
        self._update_counts()
        self._set_status("")

    def open_output(self):
        d = self.last_out_dir
        if not d and self.v_outmode.get() == "folder":
            d = self.v_outdir.get()
        if d and os.path.isdir(d):
            open_path(d)
        else:
            messagebox.showinfo(APP_NAME, "Nothing exported yet. Output folders appear after the first photo is processed.")

    def open_row(self, row):
        if row.state == "error":
            messagebox.showerror(APP_NAME, self.errors.get(id(row), row.error_text))
        elif row.output and os.path.exists(row.output):
            open_path(row.output)

    def status_text(self):
        return self.status.cget("text")

    def _update_counts(self):
        n = len(self.items)
        txt = "%d file%s" % (n, "" if n == 1 else "s")
        if self.total_bytes:
            txt += " · %.1f mb" % (self.total_bytes / 1e6)
        self.sec_meta.configure(text=txt)

    # ---- worker
    def _worker(self):
        while True:
            item, path, settings = self.jobs.get()
            try:
                self.events.put(("status", item, "reading…"))
                res = process_file(path, settings, SRGB_ICC, progress=lambda st: self.events.put(("status", item, st + "…")))
                self.events.put(("done", item, res))
            except MemoryError:
                self.events.put(("error", item, "not enough memory for this file (close other apps, or free some RAM)", traceback.format_exc()))
            except BaseException as e:
                self.events.put(("error", item, "%s: %s" % (e.__class__.__name__, e), traceback.format_exc()))
                log_error("processing %s" % path, traceback.format_exc())
            finally:
                self.jobs.task_done()

    def _poll(self):
        try:
            while True:
                ev = self.events.get_nowait()
                kind, item = ev[0], ev[1]
                row = item["row"]
                if kind == "status":
                    item["state"], item["status"] = "working", ev[2]
                    row.set_status(ev[2])
                    self._set_status("processing %d of %d · %s · %s" % (self.done + 1, self.total, row.file, ev[2]))
                elif kind == "done":
                    res = ev[2]
                    item["state"], item["res"] = "done", res
                    self.done += 1
                    self.total_bytes += res["bytes"]
                    self.last_out_dir = os.path.dirname(res["output"])
                    if self.last_out_dir not in self.batch_dirs:
                        self.batch_dirs.append(self.last_out_dir)
                    self.outputs[id(item)] = res["output"]
                    row.set_done(res)
                    self.progress.set(self.done / max(self.total, 1))
                    self._set_status("done %d of %d · saved to %s" % (self.done, self.total, short_path(self.last_out_dir)))
                    self._update_counts()
                    self.list.draw_indicator()
                elif kind == "error":
                    item["state"], item["err"] = "error", ev[2]
                    self.done += 1
                    row.set_error(ev[2])
                    self.errors[id(row)] = ev[3]
                    self.progress.set(self.done / max(self.total, 1))
                    self._set_status("error on %s — double-click the row for details" % row.file)
                if self.done >= self.total and self.total:
                    self.root.after(1500, lambda: self.progress.set(None) if self.done >= self.total else None)
                    if self.v_open.get() and self.batch_dirs:
                        for d in self.batch_dirs[:3]:                 # the batch is finished: reveal its folder(s)
                            open_path(d)
                        self.batch_dirs = []
        except queue.Empty:
            pass
        except Exception:
            log_error("poll", traceback.format_exc())
        self.root.after(100, self._poll)

    def _close(self):
        try:
            self.current_settings()
        except Exception:
            pass
        self.root.destroy()

# ----------------------------------------------------------------------------- entry points


def register_fonts():
    """Load the bundled Geist faces privately for this process (no install needed). Windows: GDI; macOS: CoreText."""
    files = glob.glob(os.path.join(RES_DIR, "fonts", "*.ttf")) + glob.glob(os.path.join(APP_DIR, "fonts", "*.ttf"))
    if IS_WIN:
        for f in files:
            try:
                ctypes.windll.gdi32.AddFontResourceExW(f, 0x10, 0)
            except Exception:
                pass
    elif IS_MAC:
        try:
            cf = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
            ct = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreText.framework/CoreText")
            cf.CFStringCreateWithCString.restype = ctypes.c_void_p
            cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
            cf.CFURLCreateWithFileSystemPath.restype = ctypes.c_void_p
            cf.CFURLCreateWithFileSystemPath.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_bool]
            ct.CTFontManagerRegisterFontsForURL.restype = ctypes.c_bool
            ct.CTFontManagerRegisterFontsForURL.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p]
            for f in files:
                s = cf.CFStringCreateWithCString(None, f.encode("utf-8"), 0x08000100)   # kCFStringEncodingUTF8
                url = cf.CFURLCreateWithFileSystemPath(None, s, 0, False)               # kCFURLPOSIXPathStyle
                ct.CTFontManagerRegisterFontsForURL(url, 1, None)                       # kCTFontManagerScopeProcess
        except Exception as e:
            log_error("register_fonts (CoreText)", str(e))


def run_cli(argv):
    """Hidden batch mode: opmize --cli OUTPUT_DIR FILE [FILE ...]  (writes cli_report.json into OUTPUT_DIR)."""
    out_dir = os.path.abspath(argv[0])
    os.makedirs(out_dir, exist_ok=True)
    report = dict(frozen=FROZEN, version=APP_VERSION, python=sys.version.split()[0], have_dnd=HAVE_DND,
                  tifffile=bool(tifffile), cv2=bool(cv2), srgb_profile_bytes=len(SRGB_ICC), results=[], errors=[])
    try:
        register_fonts()
        root = TkinterDnD.Tk() if HAVE_DND else tk.Tk()
        root.withdraw()
        report["tkdnd_version"] = root.tk.call("package", "require", "tkdnd") if HAVE_DND else None
        report["tk_version"] = tk.TkVersion
        fams = set(tkfont.families(root))
        report["fonts"] = [f for f in ("Geist", "Geist Medium", "Geist Mono", "Geist Mono Medium") if f in fams]
        root.destroy()
    except Exception as e:
        report["tk_error"] = "%s: %s" % (e.__class__.__name__, e)
    settings = dict(DEFAULTS, output_mode="folder", output_dir=out_dir, overwrite=True)
    for f in collect_files(argv[1:]):
        try:
            report["results"].append(process_file(f, settings, SRGB_ICC))
        except Exception as e:
            report["errors"].append(dict(source=f, error="%s: %s" % (e.__class__.__name__, e), trace=traceback.format_exc()))
    with open(os.path.join(out_dir, "cli_report.json"), "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=1, default=str)


def main():
    setup_crash_logging()
    if IS_WIN:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass
    if "--cli" in sys.argv and not MISSING:
        run_cli(sys.argv[sys.argv.index("--cli") + 1:])
        return
    if MISSING:
        r = tk.Tk()
        r.withdraw()
        messagebox.showerror(APP_NAME, "Missing Python packages: %s\n\nInstall with:\n  python -m pip install %s" % (
            ", ".join(MISSING), " ".join(MISSING)))
        return
    register_fonts()
    root = TkinterDnD.Tk() if HAVE_DND else tk.Tk()
    if IS_WIN:
        try:
            root.tk.call("tk", "scaling", root.winfo_fpixels("1i") / 72.0)
        except Exception:
            pass
        for ico in (os.path.join(RES_DIR, "icon.ico"), os.path.join(APP_DIR, "icon.ico")):
            if os.path.exists(ico):
                try:
                    root.iconbitmap(default=ico)
                    break
                except Exception:
                    pass
    app = App(root)
    dropped = [a for a in sys.argv[1:] if os.path.exists(a)]       # files dropped onto the .exe icon, or "Open with"
    if dropped:
        root.after(300, lambda: app.add(dropped))
    root.mainloop()


if not MISSING:
    SRGB_ICC = load_srgb_profile()

if __name__ == "__main__":
    main()
