import importlib.util
import json
import os
import socket
import tempfile
import time
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def is_container_runtime_running(timeout_seconds: int = 3) -> bool:
    """Return True if a Docker-compatible API socket is reachable and responding.

    Works with any runtime that exposes the Docker API: Docker, Podman, OrbStack,
    colima, etc. Checks DOCKER_HOST first, then falls back to /var/run/docker.sock.
    """
    docker_host = os.environ.get("DOCKER_HOST", "")
    if docker_host.startswith("unix://"):
        sock_path = docker_host[7:]
    elif docker_host.startswith("unix:"):
        sock_path = docker_host[5:]
    else:
        sock_path = "/var/run/docker.sock"

    if not Path(sock_path).is_socket():
        return False

    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout_seconds)
            s.connect(sock_path)
            # Minimal HTTP request to the Docker API /_ping endpoint.
            # Any Docker-compatible runtime responds with "200 OK" or "OK".
            s.sendall(b"GET /_ping HTTP/1.0\r\nHost: localhost\r\n\r\n")
            response = s.recv(1024).decode("utf-8", errors="replace")
            status_line = response.split("\r\n")[0] if response else ""
            return "200" in status_line or "OK" in status_line
    except (OSError, socket.timeout):
        return False


sender = load_module("sender", ROOT_DIR / "send_rips_sin_factura.py")
proxy = load_module("proxy", ROOT_DIR / "runtime" / "fevrips_proxy.py")


class ContainerRuntimeCheck(unittest.TestCase):
    """Fails loudly if no container runtime is responding.

    This catches the case where Docker/Podman/OrbStack is installed but the
    daemon isn't running (e.g. forgot to start it, crashed, or stale socket).
    """

    def test_container_runtime_is_responding(self):
        if not is_container_runtime_running():
            self.fail(
                "No container runtime is responding on the Docker API socket.\n"
                "  DOCKER_HOST=" + os.environ.get("DOCKER_HOST", "(not set)") + "\n"
                "  Start it with one of:\n"
                "    podman machine start\n"
                "    open -a Docker      # Docker Desktop\n"
                "    open -a OrbStack    # OrbStack"
            )


class ProxyCacheTests(unittest.TestCase):
    def test_cache_entry_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            cache_path = Path(tmp_dir) / "entry.cache"
            headers = {"Content-Type": "application/json; charset=utf-8"}
            payload = b'{"ok":true}'

            cache_path.write_bytes(proxy.encode_cache_entry(200, headers, payload))

            self.assertEqual(
                proxy.decode_cache_entry(cache_path, 60),
                (200, headers, payload),
            )

    def test_expired_cache_entry_is_removed(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            cache_path = Path(tmp_dir) / "entry.cache"
            entry = json.loads(proxy.encode_cache_entry(200, {}, b"{}").decode("utf-8"))
            entry["cached_at"] = time.time() - 120
            cache_path.write_text(json.dumps(entry), encoding="utf-8")

            self.assertIsNone(proxy.decode_cache_entry(cache_path, 60))
            self.assertFalse(cache_path.exists())


class SenderRetryTests(unittest.TestCase):
    def test_retryable_send_transport_errors_are_conservative(self):
        refused = sender.RequestTransportError(
            "failed", reason=ConnectionRefusedError("refused")
        )
        timeout = sender.RequestTransportError(
            "failed", reason=TimeoutError("timed out")
        )

        self.assertTrue(sender.is_retryable_send_transport_error(refused))
        self.assertFalse(sender.is_retryable_send_transport_error(timeout))

    def test_sidecar_path_is_stable(self):
        report_path = Path("/tmp/demo/367.json")
        expected = Path("/tmp/demo/367.result.json")
        self.assertEqual(sender.sidecar_path(report_path), expected)


if __name__ == "__main__":
    unittest.main()
