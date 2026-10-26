"""Smoke tests for the built image.

Runs speedtest.py against a fake speedtest-cli and a fake OTLP collector and
checks what gets exported. Meant to run inside the image, which is what
`docker build --target test .` does, so CI covers every platform it builds.
"""
import gzip
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest

# arm images are tested under emulation, where startup is slow
TIMEOUT = 120

RESULT = {
    "download": 93456789.1,
    "upload": 41234567.8,
    "ping": 12.3,
    "bytes_sent": 52428800,
    "server": {"id": "62493", "name": "Smoke test"},
}

FAKE_SPEEDTEST_CLI = """#!/bin/sh
echo "$@" > "$FAKE_ARGS"
cat "$FAKE_RESULT"
"""

records = []


def to_python(value):
    kind = value.WhichOneof("value")
    if kind == "kvlist_value":
        return {kv.key: to_python(kv.value) for kv in value.kvlist_value.values}
    if kind == "array_value":
        return [to_python(v) for v in value.array_value.values]
    return getattr(value, kind) if kind else None


class Collector(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        if self.headers.get("Content-Encoding") == "gzip":
            body = gzip.decompress(body)
        request = ExportLogsServiceRequest()
        request.ParseFromString(body)
        for resource_logs in request.resource_logs:
            resource = {kv.key: to_python(kv.value) for kv in resource_logs.resource.attributes}
            for scope_logs in resource_logs.scope_logs:
                for record in scope_logs.log_records:
                    records.append((resource, record.severity_text, to_python(record.body)))
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


collector = HTTPServer(("127.0.0.1", 0), Collector)
threading.Thread(target=collector.serve_forever, daemon=True).start()


def run_speedtest(result, expected_records, **env):
    """Runs speedtest.py until it has exported expected_records log records.

    Returns the records, the arguments speedtest-cli got and speedtest.py's output.
    """
    records.clear()
    with tempfile.TemporaryDirectory() as tmp:
        fake = os.path.join(tmp, "speedtest-cli")
        with open(fake, "w") as f:
            f.write(FAKE_SPEEDTEST_CLI)
        os.chmod(fake, 0o755)
        with open(os.path.join(tmp, "result"), "w") as f:
            f.write(result)

        with open(os.path.join(tmp, "output"), "w+") as output:
            proc = subprocess.Popen(
                [sys.executable, "-W", "default::DeprecationWarning", "/code/speedtest.py"],
                env={
                    **os.environ,
                    "PATH": tmp + os.pathsep + os.environ["PATH"],
                    "PYTHONUNBUFFERED": "1",
                    "FAKE_ARGS": os.path.join(tmp, "args"),
                    "FAKE_RESULT": os.path.join(tmp, "result"),
                    "OTEL_EXPORTER_OTLP_ENDPOINT": f"http://127.0.0.1:{collector.server_port}/v1/logs",
                    "OTEL_BLRP_SCHEDULE_DELAY": "100",
                    "service_name": "speedtest-smoke",
                    "instance_id": "smoke-1",
                    **env,
                },
                stdout=output,
                stderr=subprocess.STDOUT,
            )
            deadline = time.monotonic() + TIMEOUT
            while len(records) < expected_records and time.monotonic() < deadline and proc.poll() is None:
                time.sleep(0.1)
            # give anything unexpected a chance to arrive too
            time.sleep(1)
            proc.terminate()
            proc.wait()
            output.seek(0)
            log = output.read()

        try:
            with open(os.path.join(tmp, "args")) as f:
                args = f.read().strip()
        except FileNotFoundError:
            args = None
    return list(records), args, log


def expect(ok, message, log=""):
    if not ok:
        sys.exit(f"FAIL: {message}\n--- speedtest.py output ---\n{log}")


def test_speedtest_cli_runs():
    # the real one, so a Python upgrade it doesn't survive fails the build
    run = subprocess.run(["speedtest-cli", "--version"], capture_output=True, text=True)
    expect(run.returncode == 0, "speedtest-cli --version failed", run.stdout + run.stderr)


def test_result_is_exported():
    got, args, log = run_speedtest(
        json.dumps(RESULT), 1, SPEEDTEST_SERVER="62493, 12627", SPEEDTEST_EXCLUDE="1234"
    )
    expect(
        args == "--secure --json --server 62493 --server 12627 --exclude 1234",
        f"unexpected speedtest-cli args: {args!r}",
        log,
    )
    expect(len(got) == 1, f"expected 1 log record, got {got}", log)
    resource, severity, body = got[0]
    expect(severity == "INFO", f"expected INFO, got {severity}", log)
    expect(body == RESULT, f"exported body doesn't match the speedtest result: {body}", log)
    expect(
        resource.get("service.name") == "speedtest-smoke"
        and resource.get("service.instance.id") == "smoke-1",
        f"unexpected resource attributes: {resource}",
        log,
    )
    expect("Warning" not in log, "speedtest.py printed warnings", log)


def test_errors_are_exported():
    got, _, log = run_speedtest("", 2)
    bodies = [(severity, body) for _, severity, body in got]
    expect(len(bodies) == 2, f"expected 2 log records, got {bodies}", log)
    expect(bodies[0] == ("ERROR", "No result from speedtest-cli"), f"unexpected first error: {bodies[0]}", log)
    expect(
        bodies[1][0] == "ERROR" and bodies[1][1].startswith("Error parsing logs: "),
        f"unexpected second error: {bodies[1]}",
        log,
    )


if __name__ == "__main__":
    for test in (test_speedtest_cli_runs, test_result_is_exported, test_errors_are_exported):
        test()
        print("ok", test.__name__)
