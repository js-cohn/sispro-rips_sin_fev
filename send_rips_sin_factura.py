#!/usr/bin/env python3

import argparse
import gzip
import http.client
import json
import os
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from getpass import getpass
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent
ENV_FILE = ROOT_DIR / ".env"
DEFAULT_BASE_URL = "https://localhost:9443"
DEFAULT_COMPOSE_FILE = ROOT_DIR / "runtime" / "docker-compose.yml"
TRANSIENT_HTTP_STATUSES = {502, 503, 504}
DEFAULT_HTTP_TIMEOUT_SECONDS = 60
DEFAULT_API_READY_TIMEOUT_SECONDS = 300
DEFAULT_API_READY_RETRY_INTERVAL_SECONDS = 3
DEFAULT_SEND_RETRY_ATTEMPTS = 3
DEFAULT_SEND_RETRY_INTERVAL_SECONDS = 2


class RequestTransportError(RuntimeError):
    def __init__(self, message, *, reason=None):
        super().__init__(message)
        self.reason = reason


class RetryableHttpError(RuntimeError):
    def __init__(self, status, payload):
        super().__init__(f"Transient HTTP {status}: {payload}")
        self.status = status
        self.payload = payload


def load_env_file(env_file):
    if not env_file.is_file():
        return

    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip()


def env_bool(name, default):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def env_value(name, default=None):
    value = os.environ.get(name)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


def env_int(name):
    value = env_value(name)
    return int(value) if value is not None else None


def env_int_value(name, default):
    value = env_int(name)
    return default if value is None else value


def parse_args():
    parser = argparse.ArgumentParser(
        description="Load .env, start the local FEV-RIPS Docker stack, and send a batch of RIPS sin factura reports."
    )
    parser.add_argument(
        "batch_dir",
        type=Path,
        help="Folder inside batches/ that contains the report subfolders.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Send reports even if a .result.json sidecar already exists.",
    )
    parser.add_argument(
        "--verify-tls",
        action="store_true",
        help="Verify the local certificate instead of skipping TLS validation.",
    )
    parser.add_argument(
        "--no-compose",
        action="store_true",
        help="Do not run docker compose up -d before sending.",
    )
    return parser.parse_args()


def create_ssl_context(verify_tls):
    return ssl.create_default_context() if verify_tls else ssl._create_unverified_context()


def request_bytes(url, method, body=None, headers=None, context=None, timeout_seconds=DEFAULT_HTTP_TIMEOUT_SECONDS):
    request = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(request, context=context, timeout=timeout_seconds) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()
    except (TimeoutError, socket.timeout) as exc:
        raise RequestTransportError("Request timed out", reason=exc) from exc
    except http.client.RemoteDisconnected as exc:
        raise RequestTransportError("Remote endpoint closed the connection", reason=exc) from exc
    except urllib.error.URLError as exc:
        raise RequestTransportError("Request transport error", reason=exc.reason or exc) from exc


def parse_response_body(body):
    text = body.decode("utf-8", errors="replace")
    if not text.strip():
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def load_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def discover_reports(batch_dir):
    files = sorted(
        path
        for path in batch_dir.rglob("*.json")
        if path.is_file() and not path.name.endswith(".result.json")
    )
    if not files:
        raise FileNotFoundError(f"No JSON report files found under {batch_dir}")
    return files


def detect_default_nit(report_files):
    first_report = load_json(report_files[0])
    return first_report.get("numDocumentoIdObligado")


def is_retryable_http_status(status):
    return status in TRANSIENT_HTTP_STATUSES


def format_transport_error(exc):
    reason = exc.reason if isinstance(exc, RequestTransportError) else exc
    return str(reason or exc)


def is_retryable_startup_transport_error(exc):
    reason = exc.reason if isinstance(exc, RequestTransportError) else exc
    if isinstance(reason, (TimeoutError, socket.timeout, ConnectionRefusedError, ConnectionResetError, ConnectionAbortedError)):
        return True
    if isinstance(reason, OSError) and getattr(reason, "errno", None) in {61, 111}:
        return True
    text = str(reason).lower()
    return any(token in text for token in {"connection refused", "timed out", "connection reset", "connection aborted", "eof occurred"})


def is_retryable_send_transport_error(exc):
    reason = exc.reason if isinstance(exc, RequestTransportError) else exc
    if isinstance(reason, ConnectionRefusedError):
        return True
    if isinstance(reason, OSError) and getattr(reason, "errno", None) in {61, 111}:
        return True
    return "connection refused" in str(reason).lower()


def extract_token(payload):
    preferred_keys = {"token", "access_token", "accesstoken", "bearer", "jwttoken", "jwt", "data"}

    def walk(value):
        if isinstance(value, str):
            candidate = value.strip()
            return candidate or None
        if isinstance(value, dict):
            for key in value:
                if key.lower() in preferred_keys:
                    token = walk(value[key])
                    if token:
                        return token
            for child in value.values():
                token = walk(child)
                if token:
                    return token
        if isinstance(value, list):
            for item in value:
                token = walk(item)
                if token:
                    return token
        return None

    token = walk(payload)
    if not token:
        raise RuntimeError(f"Could not extract token from LoginSISPRO response: {payload!r}")
    return token


def login(
    base_url,
    identification_type,
    identification_number,
    password,
    nit,
    tipo_usuario,
    tipo_mecanismo_validacion,
    reps,
    context,
    http_timeout_seconds,
):
    payload = {
        "persona": {
            "identificacion": {
                "tipo": identification_type,
                "numero": identification_number,
            }
        },
        "clave": password,
        "nit": nit,
    }
    if tipo_usuario:
        payload["tipoUsuario"] = tipo_usuario
    if tipo_mecanismo_validacion is not None:
        payload["tipoMecanismoValidacion"] = tipo_mecanismo_validacion
    if reps:
        payload["reps"] = True

    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    status, _, raw_body = request_bytes(
        f"{base_url.rstrip('/')}/api/Auth/LoginSISPRO",
        "POST",
        body=body,
        headers={"Content-Type": "application/json"},
        context=context,
        timeout_seconds=http_timeout_seconds,
    )
    parsed = parse_response_body(raw_body)
    if is_retryable_http_status(status):
        raise RetryableHttpError(status, parsed)
    if not 200 <= status < 300:
        raise RuntimeError(f"LoginSISPRO failed with HTTP {status}: {parsed}")
    if not isinstance(parsed, dict):
        raise RuntimeError(f"LoginSISPRO returned a non-JSON payload: {parsed!r}")
    if parsed.get("login") is False:
        raise RuntimeError(f"LoginSISPRO rejected the credentials/payload: {parsed}")
    return extract_token(parsed)


def login_with_retries(
    base_url,
    identification_type,
    identification_number,
    password,
    nit,
    tipo_usuario,
    tipo_mecanismo_validacion,
    reps,
    context,
    http_timeout_seconds,
    ready_timeout_seconds,
    retry_interval_seconds,
):
    deadline = time.monotonic() + max(0, ready_timeout_seconds)
    attempt = 0

    while True:
        attempt += 1
        try:
            return login(
                base_url,
                identification_type,
                identification_number,
                password,
                nit,
                tipo_usuario,
                tipo_mecanismo_validacion,
                reps,
                context,
                http_timeout_seconds,
            )
        except RetryableHttpError as exc:
            message = f"HTTP {exc.status}"
        except RequestTransportError as exc:
            if not is_retryable_startup_transport_error(exc):
                raise
            message = format_transport_error(exc)
        except RuntimeError:
            raise

        if time.monotonic() >= deadline:
            raise RuntimeError(f"API not ready before timeout: {message}")

        wait_seconds = min(retry_interval_seconds, max(0, deadline - time.monotonic()))
        print(f"[WAIT] API not ready yet ({message}). Retrying in {wait_seconds:.0f}s...")
        time.sleep(wait_seconds)


def build_request_body(rips_payload):
    payload = {"rips": rips_payload, "xmlFevFile": ""}
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return gzip.compress(encoded)


def summarize_validation(parsed_response):
    if not isinstance(parsed_response, dict):
        return None
    results = parsed_response.get("ResultadosValidacion")
    if not isinstance(results, list) or not results:
        return None
    first = results[0]
    if not isinstance(first, dict):
        return None
    code = first.get("Codigo")
    description = first.get("Descripcion")
    if code and description:
        return f"{code}: {description}"
    if code:
        return str(code)
    if description:
        return description
    return None


def build_result(report_payload, status_label, http_status, parsed, error_message=None):
    result_state = parsed.get("ResultState") if isinstance(parsed, dict) else None
    response = parsed
    if error_message:
        response = {"error": error_message, "response": parsed}
    return {
        "status": status_label,
        "http_status": http_status,
        "numNota": report_payload.get("numNota"),
        "numDocumentoIdObligado": report_payload.get("numDocumentoIdObligado"),
        "ResultState": result_state,
        "Modulo": parsed.get("Modulo") if isinstance(parsed, dict) else None,
        "ProcesoId": parsed.get("ProcesoId") if isinstance(parsed, dict) else None,
        "CodigoUnicoValidacion": parsed.get("CodigoUnicoValidacion") if isinstance(parsed, dict) else None,
        "validation_summary": summarize_validation(parsed),
        "response": response,
    }


def send_report(
    base_url,
    token,
    report_payload,
    context,
    http_timeout_seconds,
):
    body = build_request_body(report_payload)
    status, _, raw_body = request_bytes(
        f"{base_url.rstrip('/')}/api/PaquetesFevRips/CargarRipsSinFactura",
        "POST",
        body=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Content-Encoding": "gzip",
        },
        context=context,
        timeout_seconds=http_timeout_seconds,
    )
    parsed = parse_response_body(raw_body)
    if is_retryable_http_status(status):
        raise RetryableHttpError(status, parsed)
    result_state = parsed.get("ResultState") if isinstance(parsed, dict) else None
    if 200 <= status < 300 and result_state is True:
        status_label = "ok"
    elif 200 <= status < 300 and result_state is False:
        status_label = "rejected"
    else:
        status_label = "error"
    return build_result(report_payload, status_label, status, parsed)


def send_report_with_retries(
    base_url,
    token,
    report_path,
    context,
    http_timeout_seconds,
    retry_attempts,
    retry_interval_seconds,
):
    report_payload = load_json(report_path)

    for attempt in range(1, retry_attempts + 1):
        try:
            return send_report(
                base_url,
                token,
                report_payload,
                context,
                http_timeout_seconds,
            )
        except RetryableHttpError as exc:
            if attempt == retry_attempts:
                return build_result(
                    report_payload,
                    "error",
                    exc.status,
                    exc.payload,
                    error_message=f"Transient HTTP error after {attempt} attempt(s)",
                )
            print(f"[RETRY] {report_path.name} -> transient HTTP {exc.status}, retrying in {retry_interval_seconds}s...")
            time.sleep(retry_interval_seconds)
        except RequestTransportError as exc:
            if not is_retryable_send_transport_error(exc) or attempt == retry_attempts:
                return build_result(
                    report_payload,
                    "error",
                    0,
                    None,
                    error_message=format_transport_error(exc),
                )
            print(f"[RETRY] {report_path.name} -> {format_transport_error(exc)}, retrying in {retry_interval_seconds}s...")
            time.sleep(retry_interval_seconds)


def sidecar_path(report_path):
    return report_path.with_name(f"{report_path.stem}.result.json")


def write_sidecar(report_path, batch_dir, result):
    path = sidecar_path(report_path)
    payload = {
        "source": str(report_path.relative_to(batch_dir)),
        "sidecar": str(path.relative_to(batch_dir)),
        "sentAt": datetime.now(timezone.utc).isoformat(),
        **result,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def print_result(relative_path, result):
    note = result.get("numNota") or "?"
    status = result["status"]
    http_status = result["http_status"]
    if status == "ok":
        print(f"[OK] {relative_path} nota {note} -> HTTP {http_status} CUV={result.get('CodigoUnicoValidacion')}")
        return
    if status == "rejected":
        summary = result.get("validation_summary") or "validation rejected"
        print(f"[RECHAZADO] {relative_path} nota {note} -> HTTP {http_status} {summary}")
        return
    print(f"[ERROR] {relative_path} nota {note} -> HTTP {http_status} {result.get('response')}")


def maybe_pause(enabled):
    if not enabled or not sys.stdin.isatty():
        return True

    answer = input("Enter to continue, q to stop: ").strip().lower()
    return answer != "q"


def ensure_compose_up(compose_file):
    subprocess.run(
        ["docker", "compose", "-f", str(compose_file), "up", "-d"],
        check=True,
        cwd=ROOT_DIR,
    )


def main():
    load_env_file(ENV_FILE)
    args = parse_args()
    base_url = env_value("FEVRIPS_BASE_URL", DEFAULT_BASE_URL)
    compose_file = Path(env_value("FEVRIPS_COMPOSE_FILE", str(DEFAULT_COMPOSE_FILE))).expanduser().resolve()
    identification_type = env_value("SISPRO_ID_TYPE", "CC")
    identification_number = env_value("SISPRO_ID_NUMBER")
    tipo_usuario = env_value("SISPRO_TIPO_USUARIO")
    tipo_mecanismo_validacion = env_int("SISPRO_TIPO_MECANISMO_VALIDACION")
    reps = env_bool("SISPRO_REPS", False)
    pause_between = sys.stdin.isatty() and env_bool("FEVRIPS_PAUSE_BETWEEN", True)
    http_timeout_seconds = max(1, env_int_value("FEVRIPS_HTTP_TIMEOUT_SECONDS", DEFAULT_HTTP_TIMEOUT_SECONDS))
    api_ready_timeout_seconds = max(0, env_int_value("FEVRIPS_API_READY_TIMEOUT_SECONDS", DEFAULT_API_READY_TIMEOUT_SECONDS))
    api_ready_retry_interval_seconds = max(
        1,
        env_int_value(
        "FEVRIPS_API_READY_RETRY_INTERVAL_SECONDS",
        DEFAULT_API_READY_RETRY_INTERVAL_SECONDS,
        ),
    )
    send_retry_attempts = max(1, env_int_value("FEVRIPS_SEND_RETRY_ATTEMPTS", DEFAULT_SEND_RETRY_ATTEMPTS))
    send_retry_interval_seconds = max(
        1,
        env_int_value(
        "FEVRIPS_SEND_RETRY_INTERVAL_SECONDS",
        DEFAULT_SEND_RETRY_INTERVAL_SECONDS,
        ),
    )

    batch_dir = args.batch_dir.expanduser().resolve()
    if not batch_dir.is_dir():
        raise FileNotFoundError(f"Batch directory not found: {batch_dir}")

    if not args.no_compose:
        if not compose_file.is_file():
            raise FileNotFoundError(f"Compose file not found: {compose_file}")
        ensure_compose_up(compose_file)

    report_files = discover_reports(batch_dir)
    pending_files = []
    skipped_count = 0
    for report_path in report_files:
        rel_path = report_path.relative_to(batch_dir)
        if not args.force and sidecar_path(report_path).exists():
            print(f"[SKIP] {rel_path} -> sidecar exists")
            skipped_count += 1
            continue
        pending_files.append(report_path)

    if not pending_files:
        print(f"No pending reports found under {batch_dir}. skipped={skipped_count}")
        return 0

    nit = env_value("SISPRO_NIT") or detect_default_nit(pending_files)
    identification_number = identification_number or nit
    if not nit:
        raise RuntimeError("NIT could not be inferred. Set SISPRO_NIT in .env.")
    if not identification_number:
        raise RuntimeError("Identification number could not be inferred. Set SISPRO_ID_NUMBER in .env.")

    password = env_value("SISPRO_PASSWORD") or getpass("SISPRO password: ")
    context = create_ssl_context(args.verify_tls)
    token = login_with_retries(
        base_url,
        identification_type,
        identification_number,
        password,
        nit,
        tipo_usuario,
        tipo_mecanismo_validacion,
        reps,
        context,
        http_timeout_seconds,
        api_ready_timeout_seconds,
        api_ready_retry_interval_seconds,
    )

    ok_count = 0
    rejected_count = 0
    error_count = 0

    for index, report_path in enumerate(pending_files, 1):
        relative_path = report_path.relative_to(batch_dir)
        result = send_report_with_retries(
            base_url,
            token,
            report_path,
            context,
            http_timeout_seconds,
            send_retry_attempts,
            send_retry_interval_seconds,
        )
        print_result(relative_path, result)

        if result["status"] in {"ok", "rejected"}:
            write_sidecar(report_path, batch_dir, result)

        if result["status"] == "ok":
            ok_count += 1
        elif result["status"] == "rejected":
            rejected_count += 1
        else:
            error_count += 1

        if index < len(pending_files) and not maybe_pause(pause_between):
            print("Stopped by user.")
            break

    processed = ok_count + rejected_count + error_count
    print(
        f"Processed {processed} pending report(s). "
        f"ok={ok_count} rejected={rejected_count} error={error_count} skipped={skipped_count}"
    )
    return 1 if error_count else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        raise SystemExit(130)
