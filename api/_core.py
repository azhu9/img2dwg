"""
Core raster -> vector DXF conversion (no OpenCV, so it fits in a Vercel function).

Pipeline:
  image -> grayscale -> blur -> threshold (or edges) -> despeckle
        -> Potrace curve fitting (also drops tiny dots/holes)
        -> straighten near-straight curves into true lines
        -> scale to inches, center on origin -> DXF (LINE + SPLINE) and SVG preview path
"""
import io

import ezdxf
import numpy as np
import potrace
from PIL import Image, ImageFilter

DEFAULTS = {
    "width": None,          # final width in inches (or give height)
    "height": None,         # final height in inches
    "mode": "fill",         # "fill" = solid shapes, "edges" = line drawings / photos
    "invert": False,        # shape is light on a dark background
    "use_alpha": False,     # transparent images: every visible pixel is shape (keeps white parts)
    "threshold": None,      # 0-255, None = automatic (Otsu)
    "min_area": 0.05,       # drop dots/holes smaller than this % of the image area
    "despeckle": 3,         # speckle filter size in px (0 = off)
    "blur": 1,              # pre-blur radius in px
    "smooth": 1.0,          # Potrace alphamax: 0 = all corners, 1.33 = very round
    "tolerance": 0.2,       # Potrace curve merging tolerance (px)
    "line_tol": 1.5,        # curves bowing less than this (px) may become lines...
    "line_ratio": 0.007,    # ...if the bow is also under this fraction of their length
    "edge_threshold": 40,   # edges mode sensitivity (lower = more edges)
    "max_px": 2000,
}


# ----------------------------------------------------------------------------- image -> mask

def _otsu(gray):
    hist = np.bincount(gray.ravel(), minlength=256).astype(np.float64)
    total = gray.size
    sum_all = np.dot(np.arange(256), hist)
    w_b = sum_b = 0.0
    best_t, best_var = 127, -1.0
    for t in range(256):
        w_b += hist[t]
        if w_b == 0:
            continue
        w_f = total - w_b
        if w_f == 0:
            break
        sum_b += t * hist[t]
        m_b, m_f = sum_b / w_b, (sum_all - sum_b) / w_f
        var = w_b * w_f * (m_b - m_f) ** 2
        if var > best_var:
            best_var, best_t = var, t
    return best_t


def load_gray(data, use_alpha=False):
    """Grayscale image to trace. With use_alpha, the alpha channel becomes the image
    (opaque = dark = shape), so white artwork on a transparent background is kept."""
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception:
        raise ValueError("Couldn't read that image. Try a PNG or JPG.")
    if img.mode in ("RGBA", "LA", "P", "PA") or "transparency" in img.info:
        img = img.convert("RGBA")
        alpha = img.getchannel("A")
        if use_alpha and alpha.getextrema()[0] < 128:
            return alpha.point(lambda a: 255 - a), True
        bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
        img = Image.alpha_composite(bg, img)
    return img.convert("L"), False


def make_mask(gray, o):
    """Return bool array, True = shape."""
    w, h = gray.size
    if max(w, h) > o["max_px"]:
        f = o["max_px"] / max(w, h)
        gray = gray.resize((max(1, int(w * f)), max(1, int(h * f))), Image.LANCZOS)
    elif max(w, h) < 800:  # small images: upscale so curve fitting has room to work
        f = 800 / max(w, h)
        gray = gray.resize((int(w * f), int(h * f)), Image.BICUBIC)

    if o["blur"] > 0:
        gray = gray.filter(ImageFilter.GaussianBlur(o["blur"]))

    if o["mode"] == "edges":
        edges = np.asarray(gray.filter(ImageFilter.FIND_EDGES))
        shape = edges > o["edge_threshold"]
        img = Image.fromarray((shape * 255).astype(np.uint8)).filter(ImageFilter.MaxFilter(3))
        shape = np.asarray(img) > 127
    else:
        arr = np.asarray(gray)
        if o.get("_alpha"):
            t = 127                      # alpha mask: opaque = shape
        else:
            t = _otsu(arr) if o["threshold"] is None else int(o["threshold"])
        shape = arr <= t                 # dark shape on light background
        if o["invert"] and not o.get("_alpha"):
            shape = ~shape

    k = int(o["despeckle"])
    if k > 0:
        k = k if k % 2 else k + 1        # Pillow filters need an odd size
        img = Image.fromarray((shape * 255).astype(np.uint8))
        img = img.filter(ImageFilter.MinFilter(k)).filter(ImageFilter.MaxFilter(k))  # open
        img = img.filter(ImageFilter.MaxFilter(k)).filter(ImageFilter.MinFilter(k))  # close
        shape = np.asarray(img) > 127
    return shape


# ----------------------------------------------------------------------------- curve helpers

def _dist_to_chord(p, a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    L = (dx * dx + dy * dy) ** 0.5
    if L < 1e-9:
        return ((p[0] - a[0]) ** 2 + (p[1] - a[1]) ** 2) ** 0.5
    return abs(dx * (p[1] - a[1]) - dy * (p[0] - a[0])) / L


def bezier_points(p0, c1, c2, p1, n=16):
    t = np.linspace(0, 1, n)[:, None]
    p0, c1, c2, p1 = map(np.asarray, (p0, c1, c2, p1))
    return ((1 - t) ** 3) * p0 + 3 * ((1 - t) ** 2) * t * c1 + 3 * (1 - t) * t ** 2 * c2 + t ** 3 * p1


def straighten(segs, tol, ratio):
    """Split 'half bend, half straight' curves, turn flat curves into lines, merge collinear lines."""

    def is_flat(s):
        pts = bezier_points(*s[1:], n=12)
        dev = max(_dist_to_chord(p, s[1], s[4]) for p in pts)
        chord = ((s[4][0] - s[1][0]) ** 2 + (s[4][1] - s[1][1]) ** 2) ** 0.5
        return dev < tol and dev <= ratio * max(chord, 1e-9)

    def split(s, depth):
        p0, c1, c2, p1 = map(np.asarray, s[1:])
        if depth == 0 or np.linalg.norm(p1 - p0) < 12 or is_flat(s):
            return [s]
        T = lambda v: (float(v[0]), float(v[1]))
        best, best_len = None, 0.0
        for tt in (0.2, 0.35, 0.5, 0.65, 0.8):
            a, b, c = p0 + (c1 - p0) * tt, c1 + (c2 - c1) * tt, c2 + (p1 - c2) * tt
            d, e = a + (b - a) * tt, b + (c - b) * tt
            m = d + (e - d) * tt
            L = ("bezier", T(p0), T(a), T(d), T(m))
            R = ("bezier", T(m), T(e), T(c), T(p1))
            fl, fr = is_flat(L), is_flat(R)
            if fl != fr:
                straight_len = np.linalg.norm(m - (p0 if fl else p1))
                if straight_len > best_len:
                    best, best_len = (L, R, fl), straight_len
        if best is None:
            return [s]
        L, R, fl = best
        return ([L] + split(R, depth - 1)) if fl else (split(L, depth - 1) + [R])

    expanded = []
    for s in segs:
        expanded += split(s, 3) if s[0] == "bezier" else [s]

    flat = []
    for s in expanded:
        if s[0] == "bezier" and is_flat(s):
            s = ("line", s[1], s[4])
        if s[0] == "line" and abs(s[2][0] - s[1][0]) + abs(s[2][1] - s[1][1]) < 1e-6:
            continue
        flat.append(s)
    for k, s in enumerate(flat):  # start the loop at a curve so straight runs aren't split
        if s[0] == "bezier":
            flat = flat[k:] + flat[:k]
            break

    out, run = [], []

    def flush():
        if not run:
            return
        pts = [run[0][1]] + [r[2] for r in run]
        i = 0
        while i < len(pts) - 1:
            j = i + 1
            while j + 1 < len(pts) and all(_dist_to_chord(pts[m], pts[i], pts[j + 1]) < tol
                                           for m in range(i + 1, j + 1)):
                j += 1
            out.append(("line", pts[i], pts[j]))
            i = j
        run.clear()

    for s in flat:
        if s[0] == "line":
            run.append(s)
        else:
            flush()
            out.append(s)
    flush()
    return out


def trace(shape, o):
    min_px = int((o["min_area"] / 100.0) * shape.shape[0] * shape.shape[1])
    bm = potrace.Bitmap(~shape)  # potrace traces the False pixels
    plist = bm.trace(turdsize=max(2, min_px), alphamax=o["smooth"],
                     opticurve=True, opttolerance=o["tolerance"])
    loops = []
    for curve in plist:
        segs = []
        sp = curve.start_point
        p0 = (sp.x, sp.y)
        for s in curve.segments:
            e = (s.end_point.x, s.end_point.y)
            if s.is_corner:
                c = (s.c.x, s.c.y)
                segs += [("line", p0, c), ("line", c, e)]
            else:
                segs.append(("bezier", p0, (s.c1.x, s.c1.y), (s.c2.x, s.c2.y), e))
            p0 = e
        loop = straighten(segs, o["line_tol"], o["line_ratio"])
        if loop:
            loops.append(loop)
    return loops


# ----------------------------------------------------------------------------- output

def _transform(loops, o):
    pts = []
    for loop in loops:
        for s in loop:
            pts.extend([s[1], s[2]] if s[0] == "line" else bezier_points(*s[1:]))
    pts = np.asarray(pts, dtype=float)
    (xmin, ymin), (xmax, ymax) = pts.min(axis=0), pts.max(axis=0)
    bw, bh = xmax - xmin, ymax - ymin
    if o["width"]:
        k = float(o["width"]) / bw
    elif o["height"]:
        k = float(o["height"]) / bh
    else:
        k = 4.0 / max(bw, bh)   # default: longest side 4 in
    cx, cy = (xmin + xmax) / 2, (ymin + ymax) / 2
    return (lambda p: ((p[0] - cx) * k, -(p[1] - cy) * k)), bw * k, bh * k


def build_outputs(loops, tf):
    doc = ezdxf.new("R2010")
    doc.units = ezdxf.units.IN
    doc.header["$INSUNITS"] = 1      # inches
    doc.header["$MEASUREMENT"] = 0   # imperial
    msp = doc.modelspace()
    n_lines = n_splines = 0
    paths, nodes = [], []
    f = lambda v: f"{v:.4f}".rstrip("0").rstrip(".")
    for loop in loops:
        d = []
        start = tf(loop[0][1])
        d.append(f"M{f(start[0])} {f(-start[1])}")
        i = 0
        while i < len(loop):
            s = loop[i]
            nodes.append(tf(s[1]))
            if s[0] == "line":
                a, b = tf(s[1]), tf(s[2])
                msp.add_line(a, b)
                d.append(f"L{f(b[0])} {f(-b[1])}")
                n_lines += 1
                i += 1
                continue
            ctrl = [tf(s[1])]
            j = i
            while j < len(loop) and loop[j][0] == "bezier":
                c1, c2, e = tf(loop[j][2]), tf(loop[j][3]), tf(loop[j][4])
                ctrl += [c1, c2, e]
                d.append(f"C{f(c1[0])} {f(-c1[1])} {f(c2[0])} {f(-c2[1])} {f(e[0])} {f(-e[1])}")
                j += 1
            nseg = j - i
            sp = msp.add_spline(dxfattribs={"degree": 3})
            sp.control_points = ctrl
            sp.knots = [0.0] * 4 + [float(k) for k in range(1, nseg) for _ in range(3)] + [float(nseg)] * 4
            n_splines += 1
            i = j
        d.append("Z")
        paths.append(" ".join(d))
    buf = io.StringIO()
    doc.write(buf)
    return buf.getvalue(), paths, nodes, n_lines, n_splines


def convert(image_bytes, options=None):
    o = dict(DEFAULTS)
    o.update({k: v for k, v in (options or {}).items() if k in DEFAULTS and v is not None})
    gray, o["_alpha"] = load_gray(image_bytes, bool(o["use_alpha"]) and o["mode"] == "fill")
    shape = make_mask(gray, o)
    if not shape.any():
        raise ValueError("No shapes found. Try Invert, Edges mode, or a manual threshold.")
    loops = trace(shape, o)
    if not loops:
        raise ValueError("No shapes found. Try Invert, Edges mode, a lower Dot removal, or a manual threshold.")
    tf, w, h = _transform(loops, o)
    dxf, paths, nodes, n_lines, n_splines = build_outputs(loops, tf)
    return {
        "dxf": dxf,
        "paths": paths,
        "nodes": [[round(float(x), 4), round(float(-y), 4)] for x, y in nodes],
        "width": round(float(w), 3),
        "height": round(float(h), 3),
        "profiles": len(loops),
        "lines": n_lines,
        "splines": n_splines,
    }
