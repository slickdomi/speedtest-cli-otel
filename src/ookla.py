"""Ookla's official speedtest CLI, used instead of speedtest-cli when its license is accepted.

Ookla's license doesn't allow redistributing the CLI, so it isn't part of the
image: install() downloads it from Ookla when it's first needed, and only once
the user accepted Ookla's terms with SPEEDTEST_ACCEPT_OOKLA_LICENSE=true.

Run `python ookla.py --servers` to list server ids for SPEEDTEST_SERVER.
"""
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import urllib.request

VERSION = "1.2.0"
BINARY = "/opt/ookla/speedtest"
LICENSE_FLAGS = ["--accept-license", "--accept-gdpr"]
COMMAND = [BINARY, *LICENSE_FLAGS, "--format=json"]

# Docker platform (SPEEDTEST_PLATFORM from the Dockerfile) -> Ookla's build of
# the CLI and the sha256 of its tarball
BUILDS = {
    "amd64": ("x86_64", "5690596c54ff9bed63fa3732f818a05dbc2db19ad36ed68f21ca5f64d5cfeeb7"),
    "arm64": ("aarch64", "3953d231da3783e2bf8904b6dd72767c5c6e533e163d3742fd0437affa431bd3"),
    "armv7": ("armhf", "e45fcdebbd8a185553535533dd032d6b10bc8c64eee4139b1147b9c09835d08d"),
}


def accepted():
    return (os.environ.get("SPEEDTEST_ACCEPT_OOKLA_LICENSE") or "").lower() == "true"


def install():
    if os.path.exists(BINARY):
        return
    platform = os.environ.get("SPEEDTEST_PLATFORM")
    if platform not in BUILDS:
        raise RuntimeError(f"Ookla's speedtest isn't available for platform {platform!r}")
    build, sha256 = BUILDS[platform]
    install_from(f"https://install.speedtest.net/app/cli/ookla-speedtest-{VERSION}-linux-{build}.tgz", sha256, BINARY)


def install_from(url, sha256, path):
    with urllib.request.urlopen(url, timeout=60) as response:
        tarball = response.read()
    if hashlib.sha256(tarball).hexdigest() != sha256:
        raise RuntimeError(f"checksum mismatch for {url}")
    with tarfile.open(fileobj=io.BytesIO(tarball)) as tar:
        binary = tar.extractfile("speedtest").read()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "wb") as f:
        f.write(binary)
    os.chmod(path + ".tmp", 0o755)
    os.replace(path + ".tmp", path)


def error_message(speedtest):
    # errors are JSON lines on stderr, like {"type": "log", "level": "error", "message": "..."},
    # between plain text license notices
    messages = []
    for line in speedtest.stderr.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and entry.get("level") == "error":
            messages.append(entry.get("message", line))
    return "; ".join(messages) or f"no output, exit code {speedtest.returncode}"


def pick_servers(pinned, excluded):
    """Server ids to try in order, None lets speedtest pick the best one itself.

    The CLI takes a single server and can't exclude any, so pinned servers are
    tried one after the other, and excluding means using the closest server
    left from its list.
    """
    if pinned:
        return [server for server in pinned if server not in excluded]
    if not excluded:
        return [None]
    listing = subprocess.run(COMMAND + ["--servers"], capture_output=True, text=True, timeout=60)
    if listing.returncode != 0:
        raise RuntimeError(error_message(listing))
    nearby = [str(server["id"]) for server in json.loads(listing.stdout)["servers"]]
    return [server for server in nearby if server not in excluded]


def run(server=None):
    command = COMMAND + ([f"--server-id={server}"] if server else [])
    return subprocess.run(command, capture_output=True, text=True, timeout=300)


def to_speedtest_cli_format(result):
    """Converts a result to the fields speedtest-cli reports, which the dashboard reads.

    Leaves out the network interface details, which include local and public
    IP and MAC addresses.
    """
    server = result["server"]
    return {
        # bytes/s to bits/s
        "download": result["download"]["bandwidth"] * 8,
        "upload": result["upload"]["bandwidth"] * 8,
        "ping": result["ping"]["latency"],
        "jitter": result["ping"]["jitter"],
        "packet_loss": result.get("packetLoss"),
        "bytes_received": result["download"]["bytes"],
        "bytes_sent": result["upload"]["bytes"],
        "timestamp": result["timestamp"],
        "server": {
            "id": str(server["id"]),
            "sponsor": server["name"],
            "name": server["location"],
            "country": server["country"],
            "host": f"{server['host']}:{server['port']}",
        },
        "client": {"isp": result.get("isp")},
        # Ookla publishes every result on speedtest.net
        "share": result.get("result", {}).get("url"),
    }


if __name__ == "__main__":
    if not accepted():
        sys.exit("Set SPEEDTEST_ACCEPT_OOKLA_LICENSE=true to accept Ookla's license first, see the README")
    install()
    os.execv(BINARY, [BINARY, *LICENSE_FLAGS, *sys.argv[1:]])
