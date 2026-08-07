import contextlib
import gzip
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import time
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

ROOT_DIR = Path(__file__).resolve().parent


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load module {name} from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def docker_context_status(timeout_seconds: int = 3):
    try:
        completed = subprocess.run(
            ["docker", "context", "ls", "--format", "{{.Name}}"],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return [], [], None

    if completed.returncode != 0:
        return [], [], None

    context_names = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    responding_contexts = []
    current_context_error = None

    def docker_run(args):
        return subprocess.run(
            ["docker", *args],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )

    try:
        current = docker_run(["info"])
        if current.returncode == 0:
            responding_contexts.append(os.environ.get("DOCKER_CONTEXT", "default"))
        else:
            current_context_error = (current.stderr or current.stdout or "").strip() or None
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    for context_name in context_names:
        try:
            if docker_run(["--context", context_name, "info"]).returncode == 0 and context_name not in responding_contexts:
                responding_contexts.append(context_name)
        except subprocess.TimeoutExpired:
            continue

    return context_names, responding_contexts, current_context_error



def is_container_runtime_running(timeout_seconds: int = 3) -> bool:
    """Return True if any reachable Docker CLI backend can talk to a daemon.

    This checks the same client path `send_rips_sin_factura.py` relies on, and
    it walks every known Docker context so all installed backends are covered.
    """

    _, responding_contexts, _ = docker_context_status(timeout_seconds=timeout_seconds)
    return bool(responding_contexts)


def format_container_runtime_skip_message(timeout_seconds: int = 3) -> str:
    context_names, responding_contexts, current_context_error = docker_context_status(timeout_seconds=timeout_seconds)
    lines = ["No container runtime is responding through the Docker CLI."]
    lines.append("  DOCKER_HOST=" + os.environ.get("DOCKER_HOST", "(not set)"))
    if context_names:
        lines.append("  Discovered Docker contexts:")
        lines.extend(f"    - {name}" for name in context_names)
    else:
        lines.append("  Discovered Docker contexts: (none)")
    if responding_contexts:
        lines.append("  Responding Docker contexts:")
        lines.extend(f"    - {name}" for name in responding_contexts)
    else:
        lines.append("  Responding Docker contexts: (none)")
    if current_context_error:
        lines.append("  docker info error from current context:")
        lines.extend(f"    {line}" for line in current_context_error.splitlines())
    lines.append("  Switch to a working Docker context with:")
    lines.append("    docker context use <name>")
    return "\n".join(lines)


sender = load_module("sender", ROOT_DIR / "send_rips_sin_factura.py")
proxy = load_module("proxy", ROOT_DIR / "runtime" / "fevrips_proxy.py")


class ContainerRuntimeCheck(unittest.TestCase):
    """Checks whether at least one Docker-compatible backend is reachable.

    The runtime is optional for the local logic tests in this file, so we skip
    this check when no Docker-compatible backend is responding.
    """

    def test_container_runtime_is_responding(self):
        if not is_container_runtime_running():
            self.skipTest(format_container_runtime_skip_message())


class ContainerRuntimeProbeTests(unittest.TestCase):
    def test_runtime_check_finds_any_working_context(self):
        responses = [
            mock.Mock(returncode=0, stdout="default\ncolima\n"),
            mock.Mock(returncode=1, stdout="", stderr="cannot connect to the Docker daemon"),
            mock.Mock(returncode=1, stdout="", stderr="permission denied"),
            mock.Mock(returncode=0, stdout=""),
        ]

        with mock.patch.object(subprocess, "run", side_effect=responses) as run_mock:
            self.assertTrue(is_container_runtime_running())

        self.assertEqual(run_mock.call_count, 4)

    def test_skip_message_includes_context_names_responding_contexts_and_error(self):
        responses = [
            mock.Mock(returncode=0, stdout="default\ncolima\n"),
            mock.Mock(returncode=1, stdout="", stderr="cannot connect to the Docker daemon"),
            mock.Mock(returncode=1, stdout="", stderr="permission denied while trying to connect to the Docker API"),
            mock.Mock(returncode=0, stdout=""),
        ]

        with mock.patch.object(subprocess, "run", side_effect=responses):
            message = format_container_runtime_skip_message()

        self.assertIn("Discovered Docker contexts:", message)
        self.assertIn("- default", message)
        self.assertIn("- colima", message)
        self.assertIn("Responding Docker contexts:", message)
        self.assertIn("- colima", message)
        self.assertIn("docker info error from current context:", message)
        self.assertIn("cannot connect to the Docker daemon", message)


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


class SenderUtilityTests(unittest.TestCase):
    def test_parse_response_body_handles_json_text_and_empty_payloads(self):
        self.assertEqual(sender.parse_response_body(b'{"a":1}'), {"a": 1})
        self.assertEqual(sender.parse_response_body(b"plain text"), "plain text")
        self.assertIsNone(sender.parse_response_body(b"   "))

    def test_extract_token_walks_nested_payloads(self):
        payload = {"outer": {"access_token": "tok-123"}}
        self.assertEqual(sender.extract_token(payload), "tok-123")

    def test_extract_token_raises_when_no_token_exists(self):
        with self.assertRaises(RuntimeError):
            sender.extract_token({"login": True})

    def test_build_request_body_compresses_expected_json(self):
        raw = gzip.decompress(sender.build_request_body({"numNota": "7"}))
        self.assertEqual(
            json.loads(raw.decode("utf-8")),
            {"rips": {"numNota": "7"}, "xmlFevFile": ""},
        )

    def test_summarize_validation_formats_useful_variants(self):
        self.assertEqual(
            sender.summarize_validation(
                {"ResultadosValidacion": [{"Codigo": "E01", "Descripcion": "bad"}]}
            ),
            "E01: bad",
        )
        self.assertEqual(
            sender.summarize_validation({"ResultadosValidacion": [{"Codigo": "E01"}]}),
            "E01",
        )
        self.assertEqual(
            sender.summarize_validation({"ResultadosValidacion": [{"Descripcion": "bad"}]}),
            "bad",
        )
        self.assertIsNone(sender.summarize_validation({"ResultadosValidacion": []}))

    def test_build_result_wraps_error_payloads(self):
        report_payload = {"numNota": "123", "numDocumentoIdObligado": "9001"}
        parsed = {
            "ResultState": False,
            "Modulo": "mod",
            "ProcesoId": "proc",
            "CodigoUnicoValidacion": "cuv",
            "ResultadosValidacion": [{"Codigo": "E01", "Descripcion": "bad"}],
        }

        result = sender.build_result(
            report_payload,
            "error",
            502,
            parsed,
            error_message="boom",
        )

        self.assertEqual(
            result,
            {
                "status": "error",
                "http_status": 502,
                "numNota": "123",
                "numDocumentoIdObligado": "9001",
                "ResultState": False,
                "Modulo": "mod",
                "ProcesoId": "proc",
                "CodigoUnicoValidacion": "cuv",
                "validation_summary": "E01: bad",
                "response": {"error": "boom", "response": parsed},
            },
        )

    def test_write_sidecar_creates_expected_relative_payload(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            batch_dir = Path(tmp_dir) / "batch"
            report_path = batch_dir / "nested" / "report.json"
            report_path.parent.mkdir(parents=True)
            report_path.write_text("{}", encoding="utf-8")

            result = {"status": "ok", "http_status": 200, "numNota": "7", "response": {}}
            sidecar = sender.write_sidecar(report_path, batch_dir, result)
            payload = json.loads(sidecar.read_text(encoding="utf-8"))

            self.assertEqual(payload["source"], "nested/report.json")
            self.assertEqual(payload["sidecar"], "nested/report.result.json")
            self.assertEqual(payload["status"], "ok")
            self.assertEqual(payload["http_status"], 200)
            self.assertIn("sentAt", payload)


class SenderRetryTests(unittest.TestCase):
    def test_retryable_transport_errors_are_classified_conservatively(self):
        refused = sender.RequestTransportError("failed", reason=ConnectionRefusedError("refused"))
        timeout = sender.RequestTransportError("failed", reason=TimeoutError("timed out"))

        self.assertTrue(sender.is_retryable_send_transport_error(refused))
        self.assertFalse(sender.is_retryable_send_transport_error(timeout))
        self.assertTrue(sender.is_retryable_startup_transport_error(timeout))

    def test_login_with_retries_retries_transient_api_startup_failures(self):
        with mock.patch.object(sender, "login", side_effect=[sender.RetryableHttpError(503, {"x": 1}), "token"]) as login_mock, mock.patch.object(sender.time, "monotonic", return_value=0), mock.patch.object(sender.time, "sleep") as sleep_mock, contextlib.redirect_stdout(io.StringIO()):
            token = sender.login_with_retries(
                "https://example",
                "CC",
                "100",
                "pw",
                "9001",
                None,
                None,
                False,
                None,
                1,
                10,
                2,
            )

        self.assertEqual(token, "token")
        self.assertEqual(login_mock.call_count, 2)
        sleep_mock.assert_called_once_with(2)

    def test_send_report_maps_status_from_response_state(self):
        report_payload = {"numNota": "7", "numDocumentoIdObligado": "9001"}
        cases = [
            (200, b'{"ResultState":true,"Modulo":"M","ProcesoId":"P","CodigoUnicoValidacion":"C"}', "ok"),
            (200, b'{"ResultState":false,"ResultadosValidacion":[{"Codigo":"E01","Descripcion":"bad"}]}', "rejected"),
            (500, b'{"message":"nope"}', "error"),
        ]

        for http_status, raw_body, expected in cases:
            with self.subTest(expected=expected), mock.patch.object(
                sender,
                "request_bytes",
                return_value=(http_status, {}, raw_body),
            ), contextlib.redirect_stdout(io.StringIO()):
                result = sender.send_report("https://example", "token", report_payload, None, 1)

            self.assertEqual(result["status"], expected)
            self.assertEqual(result["http_status"], http_status)
            self.assertEqual(result["numNota"], "7")
            self.assertEqual(result["numDocumentoIdObligado"], "9001")

    def test_send_report_with_retries_returns_error_after_transient_http_failures(self):
        report_path = Path("/tmp/demo/367.json")
        with tempfile.TemporaryDirectory() as tmp_dir:
            report_path = Path(tmp_dir) / "367.json"
            report_path.write_text(json.dumps({"numNota": "7", "numDocumentoIdObligado": "9001"}), encoding="utf-8")

            with mock.patch.object(sender, "send_report", side_effect=sender.RetryableHttpError(503, {"message": "retry"})), mock.patch.object(sender.time, "sleep"), contextlib.redirect_stdout(io.StringIO()):
                result = sender.send_report_with_retries(
                    "https://example",
                    "token",
                    report_path,
                    None,
                    1,
                    2,
                    0,
                )

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["http_status"], 503)
        self.assertEqual(result["response"], {"error": "Transient HTTP error after 2 attempt(s)", "response": {"message": "retry"}})


class SenderMainTests(unittest.TestCase):
    def test_main_processes_one_report_and_writes_sidecar(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            batch_dir = Path(tmp_dir) / "batch"
            report_dir = batch_dir / "nested"
            report_dir.mkdir(parents=True)
            report_path = report_dir / "report.json"
            report_path.write_text(
                json.dumps({"numNota": "7", "numDocumentoIdObligado": "9001"}),
                encoding="utf-8",
            )

            args = Namespace(batch_dir=batch_dir, force=False, verify_tls=False, no_compose=True)
            with mock.patch.dict(os.environ, {"SISPRO_PASSWORD": "pw"}, clear=False), mock.patch.object(
                sender,
                "parse_args",
                return_value=args,
            ), mock.patch.object(sender, "login_with_retries", return_value="token"), mock.patch.object(
                sender,
                "send_report_with_retries",
                return_value={
                    "status": "ok",
                    "http_status": 200,
                    "numNota": "7",
                    "numDocumentoIdObligado": "9001",
                    "response": {},
                },
            ), contextlib.redirect_stdout(io.StringIO()):
                exit_code = sender.main()

            self.assertEqual(exit_code, 0)
            sidecar = report_path.with_name("report.result.json")
            self.assertTrue(sidecar.exists())
            payload = json.loads(sidecar.read_text(encoding="utf-8"))
            self.assertEqual(payload["source"], "nested/report.json")
            self.assertEqual(payload["status"], "ok")
            self.assertEqual(payload["http_status"], 200)

    def test_main_skips_reports_that_already_have_sidecars(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            batch_dir = Path(tmp_dir) / "batch"
            report_dir = batch_dir / "nested"
            report_dir.mkdir(parents=True)
            report_path = report_dir / "report.json"
            report_path.write_text("{}", encoding="utf-8")
            report_path.with_name("report.result.json").write_text("{}", encoding="utf-8")

            args = Namespace(batch_dir=batch_dir, force=False, verify_tls=False, no_compose=True)
            with mock.patch.object(sender, "parse_args", return_value=args), contextlib.redirect_stdout(io.StringIO()):
                exit_code = sender.main()

            self.assertEqual(exit_code, 0)


if __name__ == "__main__":
    unittest.main()
