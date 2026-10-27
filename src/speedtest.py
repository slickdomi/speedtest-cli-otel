import os
import json
import schedule
import time
import datetime
import subprocess

import ookla
from otel import send_logs, send_error

# speedtest-cli counts every failed latency check as 3600 s, so a ping this high
# means the server didn't answer them and the upload number is garbage too
FAILED_PING_MS = 600000

def server_ids(name):
    # Comma-separated server ids, e.g. "62493" or "62493,12627"
    return [server_id.strip() for server_id in (os.environ.get(name) or "").split(",") if server_id.strip()]

def build_command():
    # speedtest.net can hand out far-away servers over https, see the README
    if (os.environ.get("SPEEDTEST_SECURE") or "true").lower() == "false":
        cmd = ["speedtest-cli", "--json"]
    else:
        cmd = ["speedtest-cli", "--secure", "--json"]
    for server_id in server_ids("SPEEDTEST_SERVER"):
        cmd += ["--server", server_id]
    for server_id in server_ids("SPEEDTEST_EXCLUDE"):
        cmd += ["--exclude", server_id]
    return cmd

def run_speedtest_cli():
    """Returns speedtest-cli's result, or None after reporting why there isn't one."""
    try:
        speedtest = subprocess.run(build_command(), capture_output=True, text=True, timeout=300)
    except Exception as e:
        send_error("Error running speedtest-cli", e)
        print("Fail sending running speedtest-cli")
        return None

    if speedtest.returncode != 0 or not speedtest.stdout:
        # speedtest-cli says what went wrong on stderr, e.g. "ERROR: No matched servers: 1234"
        reason = speedtest.stderr.strip() or f"no output, exit code {speedtest.returncode}"
        send_error("speedtest-cli failed", reason)
        print("speedtest-cli failed:", reason)
        return None

    result = json.loads(speedtest.stdout)
    if result["ping"] >= FAILED_PING_MS:
        # typically a server that redirects to https, which speedtest-cli can't follow
        reason = f"latency checks against server {result['server']['id']} failed, dropping its results"
        send_error("speedtest-cli failed", reason)
        print("speedtest-cli failed:", reason)
        return None
    return result

def run_ookla():
    """Returns Ookla's result in speedtest-cli's format, or None after reporting why there isn't one."""
    try:
        ookla.install()
        servers = ookla.pick_servers(server_ids("SPEEDTEST_SERVER"), server_ids("SPEEDTEST_EXCLUDE"))
    except Exception as e:
        send_error("Error setting up Ookla's speedtest", e)
        print("Fail setting up Ookla's speedtest")
        return None

    failures = []
    for server in servers:
        try:
            speedtest = ookla.run(server)
        except Exception as e:
            failures.append(f"server {server}: {e}" if server else str(e))
            continue
        if speedtest.returncode == 0 and speedtest.stdout:
            return ookla.to_speedtest_cli_format(json.loads(speedtest.stdout))
        reason = ookla.error_message(speedtest)
        failures.append(f"server {server}: {reason}" if server else reason)

    reason = "; ".join(failures) or "every server is excluded"
    send_error("speedtest failed", reason)
    print("speedtest failed:", reason)
    return None

def run_speedtest():
    print("Running speedtest ", datetime.datetime.now().isoformat())
    try:
        result = run_ookla() if ookla.accepted() else run_speedtest_cli()
        if result is None:
            return
        send_logs(result)
    except Exception as e:
        print("Fail sending logs")
        send_error("Error parsing logs", e)
        return
    print("Success")

every_seconds_from = int(os.environ.get("every_seconds_from") or 1500)
every_seconds_to = int(os.environ.get("every_seconds_to") or 2100)
schedule.every(every_seconds_from).to(every_seconds_to).seconds.do(run_speedtest)

print("Starting speedtest every", every_seconds_from, "to", every_seconds_to, "seconds")
print("Using", "Ookla's speedtest" if ookla.accepted() else "speedtest-cli")
run_speedtest()

while True:
    schedule.run_pending()
    time.sleep(1)
