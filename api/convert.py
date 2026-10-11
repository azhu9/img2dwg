"""
Vercel serverless function: POST /api/convert

Body:    the image file (raw bytes; the page sends a resized PNG, transparency kept)
Query:   width, height, mode, invert, use_alpha, threshold, min_area, despeckle, blur, smooth, line_tol
Returns: JSON { dxf, paths, nodes, width, height, profiles, lines, splines }  or  { error }
"""
import json
import os
import sys
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _core import convert  # noqa: E402

MAX_BYTES = 4 * 1024 * 1024  # Vercel caps request bodies at 4.5 MB

FLOATS = ("width", "height", "min_area", "blur", "smooth", "line_tol", "tolerance", "line_ratio")
INTS = ("threshold", "despeckle", "edge_threshold")


def parse_options(query):
    q = {k: v[0] for k, v in parse_qs(query).items() if v and v[0] != ""}
    opts = {}
    for k in FLOATS:
        if k in q:
            opts[k] = float(q[k])
    for k in INTS:
        if k in q:
            opts[k] = int(float(q[k]))
    if q.get("mode") in ("fill", "edges"):
        opts["mode"] = q["mode"]
    opts["invert"] = q.get("invert", "").lower() in ("1", "true", "yes", "on")
    opts["use_alpha"] = q.get("use_alpha", "").lower() in ("1", "true", "yes", "on")
    for k in ("width", "height"):
        if k in opts and not (0 < opts[k] < 10000):
            raise ValueError(f"{k} must be between 0 and 10000 inches")
    return opts


class handler(BaseHTTPRequestHandler):
    def _send(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return self._send(400, {"error": "No image received."})
            if length > MAX_BYTES:
                return self._send(413, {"error": "Image too large (max 4 MB)."})
            data = self.rfile.read(length)
            opts = parse_options(urlparse(self.path).query)
            return self._send(200, convert(data, opts))
        except ValueError as e:
            return self._send(422, {"error": str(e)})
        except Exception as e:  # unreadable image, tracing failure, etc.
            return self._send(500, {"error": f"Conversion failed: {e}"})

    def do_GET(self):
        self._send(200, {"ok": True, "usage": "POST an image to /api/convert?width=4"})
