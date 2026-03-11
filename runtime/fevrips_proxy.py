#!/usr/bin/env python3

import hashlib
import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


REMOTE_ORIGIN = "https://fevrips.sispro.gov.co"
CACHE_DIR = Path(os.environ.get("FEVRIPS_PROXY_CACHE", "/cache"))
LISTEN_HOST = os.environ.get("FEVRIPS_PROXY_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("FEVRIPS_PROXY_PORT", "8080"))
TIMEOUT_SECONDS = int(os.environ.get("FEVRIPS_PROXY_TIMEOUT", "900"))


def cacheable_path(path):
    return (
        path.startswith("/fevrips-api/api/SincronizacionDatos/")
        or path.startswith("/externalsync-queryapi/TablasReferencia/FechaModificacion/")
    )


def cache_file_for(path):
    digest = hashlib.sha256(path.encode("utf-8")).hexdigest()
    return CACHE_DIR / f"{digest}.cache"


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        self.forward()

    def do_POST(self):
        self.forward()

    def do_PUT(self):
        self.forward()

    def do_DELETE(self):
        self.forward()

    def do_PATCH(self):
        self.forward()

    def log_message(self, fmt, *args):
        return

    def forward(self):
        CACHE_DIR.mkdir(parents=True, exist_ok=True)

        parsed = urllib.parse.urlsplit(self.path)
        if parsed.scheme and parsed.netloc:
            self.send_error(400, "Absolute URLs are not supported")
            return

        remote_url = urllib.parse.urljoin(REMOTE_ORIGIN, self.path)
        method = self.command.upper()
        body = None
        if method in {"POST", "PUT", "PATCH"}:
            length = int(self.headers.get("Content-Length", "0") or "0")
            body = self.rfile.read(length) if length else None

        if method == "GET" and cacheable_path(parsed.path):
            cache_path = cache_file_for(self.path)
            if cache_path.exists():
                payload = cache_path.read_bytes()
                self.respond(200, {"Content-Type": "application/json; charset=utf-8"}, payload)
                return

        request_headers = {}
        for key, value in self.headers.items():
            lowered = key.lower()
            if lowered in {
                "host",
                "content-length",
                "connection",
                "accept-encoding",
                "transfer-encoding",
            }:
                continue
            request_headers[key] = value

        request = urllib.request.Request(
            remote_url,
            data=body,
            headers=request_headers,
            method=method,
        )

        context = ssl.create_default_context()
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS, context=context) as response:
                payload = response.read()
                headers = dict(response.headers.items())
                status = response.status
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            headers = dict(exc.headers.items())
            status = exc.code
        except Exception as exc:
            payload = json.dumps(
                {"proxy_error": str(exc), "remote_url": remote_url},
                ensure_ascii=False,
            ).encode("utf-8")
            self.respond(502, {"Content-Type": "application/json; charset=utf-8"}, payload)
            return

        if method == "GET" and status == 200 and cacheable_path(parsed.path):
            cache_path = cache_file_for(self.path)
            cache_path.write_bytes(payload)

        self.respond(status, headers, payload)

    def respond(self, status, headers, payload):
        self.send_response(status)
        sent_length = False
        for key, value in headers.items():
            lowered = key.lower()
            if lowered in {
                "transfer-encoding",
                "connection",
                "content-encoding",
            }:
                continue
            if lowered == "content-length":
                sent_length = True
            self.send_header(key, value)
        if not sent_length:
            self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        if payload:
            self.wfile.write(payload)


def main():
    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), ProxyHandler)
    server.serve_forever()


if __name__ == "__main__":
    main()
