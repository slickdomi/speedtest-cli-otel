"""Smoke tests for the built image.

Runs speedtest.py against fake speedtest-cli and Ookla speedtest binaries and a
fake OTLP collector, and checks what gets exported. Meant to run inside the
image, which is what `docker build --target test .` does, so CI covers every
platform it builds.
"""
import gzip
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest

sys.path.insert(0, "/code")
import ookla  # noqa: E402

# arm images are tested under emulation, where startup is slow
TIMEOUT = 120

RESULT = {
    "download": 93456789.1,
    "upload": 41234567.8,
    "ping": 12.3,
    "bytes_sent": 52428800,
    "server": {"id": "62493", "name": "Smoke test"},
}

# a real result from Ookla's speedtest 1.2.0, with made up addresses
OOKLA_RESULT = {
    "type": "result",
    "timestamp": "2026-10-27T00:30:18Z",
    "ping": {"jitter": 0.649, "latency": 2.061, "low": 1.707, "high": 3.006},
    "download": {"bandwidth": 115794089, "bytes": 428841128, "elapsed": 3706,
                 "latency": {"iqm": 10.078, "low": 2.983, "high": 13.038, "jitter": 0.778}},
    "upload": {"bandwidth": 57415139, "bytes": 626203602, "elapsed": 11615,
               "latency": {"iqm": 3.169, "low": 1.63, "high": 18.956, "jitter": 0.863}},
    "packetLoss": 0,
    "isp": "Proximus",
    "interface": {"internalIp": "192.168.1.23", "name": "eth0", "macAddr": "02:42:AC:11:00:02",
                  "isVpn": False, "externalIp": "203.0.113.7"},
    "server": {"id": 63249, "host": "ultraspeed1.proximus.be", "port": 8080, "name": "Proximus",
               "location": "Brussels", "country": "Belgium", "ip": "198.51.100.9"},
    "result": {"id": "c2be1152-bffe-4cd5-a40b-38d43f0bb6da",
               "url": "https://www.speedtest.net/result/c/c2be1152-bffe-4cd5-a40b-38d43f0bb6da",
               "persisted": True},
}

OOKLA_SERVERS = [
    {"id": 63249, "host": "ultraspeed1.proximus.be", "port": 8080, "name": "Proximus",
     "location": "Brussels", "country": "Belgium"},
    {"id": 54889, "host": "speedtest.behostings.com", "port": 8080, "name": "Behostings",
     "location": "Brussels", "country": "Belgium"},
    {"id": 45280, "host": "speedtestnetgauge2.voo.be", "port": 8080, "name": "VOO",
     "location": "Brussels", "country": "Belgium"},
]

# Ookla's speedtest prints these on stderr around its JSON log lines
OOKLA_LICENSE_NOTICE = (
    "==============================================================================\n\n"
    "You may only use this Speedtest software and information generated\n"
    "from it for personal, non-commercial use, through a command line\n"
    "interface on a personal computer.\n\n"
    "License acceptance recorded. Continuing.\n\n"
)


def ookla_error(message):
    return OOKLA_LICENSE_NOTICE + json.dumps(
        {"type": "log", "timestamp": "2026-10-27T00:30:02Z", "message": message, "level": "error"}
    ) + "\n"


def respond(stdout="", stderr="", exit_code=0):
    return {"stdout": stdout, "stderr": stderr, "exit": exit_code}


# stands in for both speedtest-cli and Ookla's speedtest: records how it was
# called and answers as FAKE_SPEC says for the server it was asked for
FAKE_SPEEDTEST = r"""#!/usr/bin/env python3
import json, os, sys
with open(os.environ["FAKE_CALLS"], "a") as f:
    f.write(json.dumps([os.path.basename(sys.argv[0]), *sys.argv[1:]]) + "\n")
with open(os.environ["FAKE_SPEC"]) as f:
    spec = json.load(f)
if "--servers" in sys.argv:
    print(json.dumps({"type": "serverList", "servers": spec["servers"]}))
    sys.exit(0)
server = next((arg.split("=", 1)[1] for arg in sys.argv if arg.startswith("--server-id=")), "*")
response = spec["responses"].get(server) or spec["responses"]["*"]
sys.stdout.write(response["stdout"])
sys.stderr.write(response["stderr"])
sys.exit(response["exit"])
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


def write_executable(path, content):
    with open(path, "w") as f:
        f.write(content)
    os.chmod(path, 0o755)


def run_speedtest(responses, expected_records, servers=(), **env):
    """Runs speedtest.py until it has exported expected_records log records.

    responses maps a server id, or "*" for any other, to what the fakes print
    and exit with; servers is what Ookla's fake lists. Returns the records, the
    commands the fakes were called with and speedtest.py's output.
    """
    records.clear()
    with tempfile.TemporaryDirectory() as tmp:
        write_executable(os.path.join(tmp, "speedtest-cli"), FAKE_SPEEDTEST)
        # where ookla.install() would put the real one, so it doesn't download it
        os.makedirs(os.path.dirname(ookla.BINARY), exist_ok=True)
        write_executable(ookla.BINARY, FAKE_SPEEDTEST)
        with open(os.path.join(tmp, "spec"), "w") as f:
            json.dump({"responses": responses, "servers": list(servers)}, f)

        try:
            with open(os.path.join(tmp, "output"), "w+") as output:
                proc = subprocess.Popen(
                    [sys.executable, "-W", "default::DeprecationWarning", "/code/speedtest.py"],
                    env={
                        **os.environ,
                        "PATH": tmp + os.pathsep + os.environ["PATH"],
                        "PYTHONUNBUFFERED": "1",
                        "FAKE_CALLS": os.path.join(tmp, "calls"),
                        "FAKE_SPEC": os.path.join(tmp, "spec"),
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
        finally:
            os.remove(ookla.BINARY)

        try:
            with open(os.path.join(tmp, "calls")) as f:
                calls = [json.loads(line) for line in f]
        except FileNotFoundError:
            calls = []
    return list(records), calls, log


def expect(ok, message, log=""):
    if not ok:
        sys.exit(f"FAIL: {message}\n--- speedtest.py output ---\n{log}")


def test_speedtest_cli_runs():
    # the real one, so a Python upgrade it doesn't survive fails the build
    run = subprocess.run(["speedtest-cli", "--version"], capture_output=True, text=True)
    expect(run.returncode == 0, "speedtest-cli --version failed", run.stdout + run.stderr)


def test_result_is_exported():
    got, calls, log = run_speedtest(
        {"*": respond(json.dumps(RESULT))}, 1, SPEEDTEST_SERVER="62493, 12627", SPEEDTEST_EXCLUDE="1234"
    )
    expect(
        calls == [["speedtest-cli", "--secure", "--json", "--server", "62493", "--server", "12627", "--exclude", "1234"]],
        f"unexpected speedtest calls: {calls}",
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


def test_speedtest_cli_failure_is_exported():
    # what speedtest-cli does when a pinned server isn't one of the nearby ones
    got, _, log = run_speedtest(
        {"*": respond(stderr="ERROR: No matched servers: 62493\n", exit_code=1)}, 1, SPEEDTEST_SERVER="62493"
    )
    bodies = [(severity, body) for _, severity, body in got]
    expect(
        bodies == [("ERROR", "speedtest-cli failed: ERROR: No matched servers: 62493")],
        f"expected one error with speedtest-cli's message, got {bodies}",
        log,
    )


def test_failed_latency_is_dropped():
    # what speedtest-cli reports for a server that redirects to https
    garbage = {**RESULT, "ping": 1800000.0, "upload": 3646234567.8, "server": {"id": "59333"}}
    got, calls, log = run_speedtest(
        {"*": respond(json.dumps(garbage))}, 1, SPEEDTEST_SECURE="false", SPEEDTEST_SERVER="59333"
    )
    expect(calls == [["speedtest-cli", "--json", "--server", "59333"]], f"unexpected speedtest calls: {calls}", log)
    bodies = [(severity, body) for _, severity, body in got]
    expect(
        bodies == [("ERROR", "speedtest-cli failed: latency checks against server 59333 failed, dropping its results")],
        f"expected one error instead of the garbage result, got {bodies}",
        log,
    )


def test_unparsable_result_is_exported():
    got, _, log = run_speedtest({"*": respond("not json")}, 1)
    bodies = [(severity, body) for _, severity, body in got]
    expect(len(bodies) == 1, f"expected 1 log record, got {bodies}", log)
    expect(
        bodies[0][0] == "ERROR" and bodies[0][1].startswith("Error parsing logs: "),
        f"unexpected error: {bodies[0]}",
        log,
    )


OOKLA = ["speedtest", "--accept-license", "--accept-gdpr", "--format=json"]


def test_ookla_result_is_converted():
    # the first pinned server fails, so the next one is used
    got, calls, log = run_speedtest(
        {
            "59333": respond(stderr=ookla_error("Cannot read from socket"), exit_code=2),
            "*": respond(json.dumps(OOKLA_RESULT), stderr=OOKLA_LICENSE_NOTICE),
        },
        1,
        SPEEDTEST_ACCEPT_OOKLA_LICENSE="true",
        SPEEDTEST_SERVER="59333, 63249",
    )
    expect(
        calls == [OOKLA + ["--server-id=59333"], OOKLA + ["--server-id=63249"]],
        f"unexpected speedtest calls: {calls}",
        log,
    )
    bodies = [(severity, body) for _, severity, body in got]
    # in speedtest-cli's format for the dashboard, and without the interface's addresses
    expected = {
        "download": 115794089 * 8,
        "upload": 57415139 * 8,
        "ping": 2.061,
        "jitter": 0.649,
        "packet_loss": 0,
        "bytes_received": 428841128,
        "bytes_sent": 626203602,
        "timestamp": "2026-10-27T00:30:18Z",
        "server": {"id": "63249", "sponsor": "Proximus", "name": "Brussels", "country": "Belgium",
                   "host": "ultraspeed1.proximus.be:8080"},
        "client": {"isp": "Proximus"},
        "share": "https://www.speedtest.net/result/c/c2be1152-bffe-4cd5-a40b-38d43f0bb6da",
    }
    expect(bodies == [("INFO", expected)], f"unexpected records: {bodies}", log)
    expect("Warning" not in log, "speedtest.py printed warnings", log)


def test_ookla_excluded_servers_are_skipped():
    got, calls, log = run_speedtest(
        {"*": respond(json.dumps(OOKLA_RESULT))},
        1,
        servers=OOKLA_SERVERS,
        SPEEDTEST_ACCEPT_OOKLA_LICENSE="true",
        SPEEDTEST_EXCLUDE="63249",
    )
    expect(
        calls == [OOKLA + ["--servers"], OOKLA + ["--server-id=54889"]],
        f"expected the closest server that isn't excluded, got calls {calls}",
        log,
    )
    expect([severity for _, severity, _ in got] == ["INFO"], f"unexpected records: {got}", log)


def test_ookla_failure_is_exported():
    got, calls, log = run_speedtest(
        {"*": respond(stderr=ookla_error("Configuration - No servers defined (NoServersException)"), exit_code=2)},
        1,
        SPEEDTEST_ACCEPT_OOKLA_LICENSE="true",
    )
    expect(calls == [OOKLA], f"unexpected speedtest calls: {calls}", log)
    bodies = [(severity, body) for _, severity, body in got]
    expect(
        bodies == [("ERROR", "speedtest failed: Configuration - No servers defined (NoServersException)")],
        f"expected one error with speedtest's message, got {bodies}",
        log,
    )


def test_ookla_install():
    platform = os.environ.get("SPEEDTEST_PLATFORM")
    expect(platform in ookla.BUILDS, f"no build of Ookla's speedtest for platform {platform!r}")

    with tempfile.TemporaryDirectory() as tmp:
        binary = b"#!/bin/sh\necho fake speedtest\n"
        tarball = io.BytesIO()
        with tarfile.open(fileobj=tarball, mode="w:gz") as tar:
            for name, content in (("speedtest", binary), ("speedtest.md", b"docs")):
                info = tarfile.TarInfo(name)
                info.size = len(content)
                tar.addfile(info, io.BytesIO(content))
        with open(os.path.join(tmp, "speedtest.tgz"), "wb") as f:
            f.write(tarball.getvalue())
        url = f"file://{tmp}/speedtest.tgz"
        path = os.path.join(tmp, "bin", "speedtest")

        try:
            ookla.install_from(url, "0" * 64, path)
            expect(False, "a tarball with the wrong checksum got installed")
        except RuntimeError:
            pass
        expect(not os.path.exists(path), "a tarball with the wrong checksum left a binary behind")

        ookla.install_from(url, hashlib.sha256(tarball.getvalue()).hexdigest(), path)
        run = subprocess.run([path], capture_output=True, text=True)
        expect(run.stdout == "fake speedtest\n", f"installed binary doesn't run: {run}")


if __name__ == "__main__":
    for test in (
        test_speedtest_cli_runs,
        test_result_is_exported,
        test_speedtest_cli_failure_is_exported,
        test_failed_latency_is_dropped,
        test_unparsable_result_is_exported,
        test_ookla_result_is_converted,
        test_ookla_excluded_servers_are_skipped,
        test_ookla_failure_is_exported,
        test_ookla_install,
    ):
        test()
        print("ok", test.__name__)
