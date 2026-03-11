#!/usr/bin/env python3

import base64
import hashlib
import json
import os
import ssl
import time
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
DISABLE_CACHE = os.environ.get("FEVRIPS_PROXY_DISABLE_CACHE", "").strip().lower() in {"1", "true", "yes", "y", "on"}
TABLE_CACHE_TTL_SECONDS = int(os.environ.get("FEVRIPS_PROXY_TABLE_CACHE_TTL_SECONDS", "300"))
METADATA_CACHE_TTL_SECONDS = int(os.environ.get("FEVRIPS_PROXY_METADATA_CACHE_TTL_SECONDS", "0"))


def cache_ttl_for(path):
    if DISABLE_CACHE:
        return None
    if path.startswith("/externalsync-queryapi/TablasReferencia/FechaModificacion/"):
        return METADATA_CACHE_TTL_SECONDS if METADATA_CACHE_TTL_SECONDS > 0 else None
    if path.startswith("/fevrips-api/api/SincronizacionDatos/"):
        return TABLE_CACHE_TTL_SECONDS if TABLE_CACHE_TTL_SECONDS > 0 else None
    return None


def cache_file_for(path):
    digest = hashlib.sha256(path.encode("utf-8")).hexdigest()
    return CACHE_DIR / f"{digest}.cache"


def encode_cache_entry(status, headers, payload):
    return json.dumps(
        {
            "cached_at": time.time(),
            "status": status,
            "headers": headers,
            "body_b64": base64.b64encode(payload).decode("ascii"),
        },
        ensure_ascii=False,
    ).encode("utf-8")


def decode_cache_entry(cache_path, ttl_seconds):
    try:
        entry = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        cache_path.unlink(missing_ok=True)
        return None

    cached_at = entry.get("cached_at")
    if not isinstance(cached_at, (int, float)):
        cache_path.unlink(missing_ok=True)
        return None

    if ttl_seconds is not None and time.time() - cached_at > ttl_seconds:
        cache_path.unlink(missing_ok=True)
        return None

    try:
        payload = base64.b64decode(entry["body_b64"])
    except (KeyError, ValueError, TypeError):
        cache_path.unlink(missing_ok=True)
        return None

    return entry.get("status", 200), entry.get("headers", {}), payload


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

        cache_ttl = cache_ttl_for(parsed.path)
        if method == "GET" and cache_ttl is not None:
            cache_path = cache_file_for(self.path)
            if cache_path.exists():
                cached_entry = decode_cache_entry(cache_path, cache_ttl)
                if cached_entry is not None:
                    status, headers, payload = cached_entry
                    self.respond(status, headers, payload)
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

        if method == "GET" and status == 200 and cache_ttl is not None:
            cache_path = cache_file_for(self.path)
            cache_path.write_bytes(encode_cache_entry(status, headers, payload))

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
