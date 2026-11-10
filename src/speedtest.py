import os
import sys
import json
import itertools
import schedule
import time
import datetime
import subprocess

import cloudflare
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

def rotating():
    # on unless turned off, so the results don't all come from the same server
    return (os.environ.get("SPEEDTEST_ROTATE") or "true").lower() != "false"

# counts runs that rotate servers with SPEEDTEST_ROTATE
rotations = itertools.count()

def rotated(servers):
    # each run starts one server further, and falls back on the ones after it
    if not servers:
        return servers
    start = next(rotations) % len(servers)
    return servers[start:] + servers[:start]

# engine -> time.monotonic() until which its runs are skipped, after it was rate limited
paused_until = {}

def pause(engine, seconds):
    """Skips the engine's runs for that many seconds, returns that for the error."""
    paused_until[engine] = time.monotonic() + seconds
    return f"pausing for {seconds // 60} minutes"

def run_ookla():
    """Returns Ookla's result in speedtest-cli's format, or None after reporting why there isn't one."""
    rotate = rotating()
    try:
        ookla.install()
        servers = ookla.pick_servers(server_ids("SPEEDTEST_SERVER"), server_ids("SPEEDTEST_EXCLUDE"), rotate)
    except ookla.RateLimited as e:
        send_error("Error setting up Ookla's speedtest", f"{e}; {pause('ookla', ookla.RATE_LIMIT_PAUSE)}")
        print("Ookla rate limited listing servers")
        return None
    except Exception as e:
        send_error("Error setting up Ookla's speedtest", e)
        print("Fail setting up Ookla's speedtest")
        return None
    if rotate:
        servers = rotated(servers)

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
        if speedtest.returncode == ookla.RATE_LIMITED:
            # the other servers would be turned away too, and each try counts against the limit
            failures.append(pause("ookla", ookla.RATE_LIMIT_PAUSE))
            break

    reason = "; ".join(failures) or "every server is excluded"
    send_error("speedtest failed", reason)
    print("speedtest failed:", reason)
    return None

# seconds that download and upload each take with Cloudflare
duration = int(os.environ.get("SPEEDTEST_DURATION") or 8)

def run_cloudflare():
    """Returns Cloudflare's result, or None after reporting why there isn't one."""
    try:
        return cloudflare.run(duration)
    except cloudflare.RateLimited as e:
        reason = f"{e}; {pause('cloudflare', e.retry_after)}"
    except Exception as e:
        reason = str(e)
    send_error("Cloudflare's speedtest failed", reason)
    print("Cloudflare's speedtest failed:", reason)
    return None

ENGINES = {"speedtest-cli": run_speedtest_cli, "ookla": run_ookla, "cloudflare": run_cloudflare}
# comma-separated, taking turns; accepting Ookla's license used to be what
# switched to its speedtest
engines = [
    engine.strip().lower()
    for engine in (os.environ.get("SPEEDTEST_ENGINE") or ("ookla" if ookla.accepted() else "speedtest-cli")).split(",")
    if engine.strip()
]
if not engines or any(engine not in ENGINES for engine in engines):
    sys.exit(f"Unknown SPEEDTEST_ENGINE {os.environ.get('SPEEDTEST_ENGINE')!r}, use one or more of {', '.join(ENGINES)}")
if "ookla" in engines and not ookla.accepted():
    sys.exit("Set SPEEDTEST_ACCEPT_OOKLA_LICENSE=true to accept Ookla's license first, see the README")
if "cloudflare" in engines and duration <= cloudflare.WARM_UP:
    sys.exit(f"SPEEDTEST_DURATION has to be at least {cloudflare.WARM_UP + 1}")
# counts runs, for the engines to take turns
runs = itertools.count()

def run_speedtest():
    engine = engines[next(runs) % len(engines)]
    if time.monotonic() < paused_until.get(engine, 0):
        print("Skipping", engine, "for now, it rate limited the speedtest")
        return
    print("Running", engine, datetime.datetime.now().isoformat())
    try:
        result = ENGINES[engine]()
        if result is None:
            return
        # so results can be told apart when engines take turns
        send_logs({**result, "engine": engine})
    except Exception as e:
        print("Fail sending logs")
        send_error("Error parsing logs", e)
        return
    print("Success")

every_seconds_from = int(os.environ.get("every_seconds_from") or 3300)
every_seconds_to = int(os.environ.get("every_seconds_to") or 3900)
schedule.every(every_seconds_from).to(every_seconds_to).seconds.do(run_speedtest)

print("Starting speedtest every", every_seconds_from, "to", every_seconds_to, "seconds")
print("Using", ", ".join(engines))
run_speedtest()

while True:
    schedule.run_pending()
    time.sleep(1)
