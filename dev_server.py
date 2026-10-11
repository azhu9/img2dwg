"""
Run the app locally without Vercel:   python dev_server.py   ->  http://localhost:8000
(Vercel ignores this file; it uses api/convert.py and public/ directly.)
"""
import os
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, "api"))
from convert import handler as ApiHandler  # noqa: E402


class DevHandler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=os.path.join(ROOT, "public"), **kw)

    def do_POST(self):
        if self.path.startswith("/api/convert"):
            return ApiHandler.do_POST(self)
        self.send_error(404)

    _send = ApiHandler._send


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    print(f"Serving on http://localhost:{port}")
    ThreadingHTTPServer(("127.0.0.1", port), DevHandler).serve_forever()
