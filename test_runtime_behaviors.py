import importlib.util
import json
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


sender = load_module("sender", ROOT_DIR / "send_rips_sin_factura.py")
proxy = load_module("proxy", ROOT_DIR / "runtime" / "fevrips_proxy.py")


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
        refused = sender.RequestTransportError("failed", reason=ConnectionRefusedError("refused"))
        timeout = sender.RequestTransportError("failed", reason=TimeoutError("timed out"))

        self.assertTrue(sender.is_retryable_send_transport_error(refused))
        self.assertFalse(sender.is_retryable_send_transport_error(timeout))

    def test_sidecar_path_is_stable(self):
        report_path = Path("/tmp/demo/367.json")
        expected = Path("/tmp/demo/367.result.json")
        self.assertEqual(sender.sidecar_path(report_path), expected)


if __name__ == "__main__":
    unittest.main()
