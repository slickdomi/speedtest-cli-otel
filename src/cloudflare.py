"""Speedtest against Cloudflare's network, like speed.cloudflare.com.

Uses the endpoints of Cloudflare's own speed test library
(github.com/cloudflare/speedtest): download from __down, upload to __up.
Unlike that library, results aren't sent back to Cloudflare.
"""
import datetime
import http.client
import os
import socket
import statistics
import threading
import time
import urllib.parse
import uuid

# tests point this at a fake
URL = os.environ.get("SPEEDTEST_CLOUDFLARE_URL") or "https://speed.cloudflare.com"
TIMEOUT = 15

# TCP connections timed for the latency
PINGS = 20
# download and upload each run over this many connections at once, and leave
# the first WARM_UP seconds out of the speed, while TCP ramps up
CONNECTIONS = 8
WARM_UP = 1
# per request, the most Cloudflare's library asks for, 250 MB down and 50 MB up,
# so a test takes few requests, which its rate limit may count: at 1 Gbit/s,
# about one per connection down and two up. Transfers stop when time is up
DOWNLOAD_SIZE = 250 * 1000 * 1000
CHUNK = 256 * 1024
UPLOAD_SIZE = 50 * 1000 * 1000 // CHUNK * CHUNK
# how long to stop testing after Cloudflare rate limits a test without saying
RATE_LIMIT_PAUSE = 60 * 60


class Stopped(Exception):
    """Time is up for an upload."""


class RateLimited(RuntimeError):
    """Cloudflare turned a request away with 429 Too Many Requests."""

    def __init__(self, retry_after):
        super().__init__("Cloudflare rate limited the test")
        self.retry_after = retry_after


def connect():
    parts = urllib.parse.urlsplit(URL)
    connection = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
    return connection(parts.netloc, timeout=TIMEOUT)


def path(endpoint, **query):
    query["x"] = uuid.uuid4().hex
    return f"{urllib.parse.urlsplit(URL).path}/{endpoint}?{urllib.parse.urlencode(query)}"


def check(response):
    if response.status == 429:
        # Retry-After can be an HTTP date too, which isn't worth parsing for this
        retry_after = response.getheader("Retry-After") or ""
        raise RateLimited(int(retry_after) if retry_after.isdigit() else RATE_LIMIT_PAUSE)
    if response.status != 200:
        raise RuntimeError(f"HTTP {response.status} {response.reason}")


def data_center():
    """The Cloudflare data center that answers, e.g. "BRU" from a CF-Ray of "8a1b2c3d4e5f6789-BRU"."""
    connection = connect()
    try:
        connection.request("GET", path("__down", bytes=0))
        response = connection.getresponse()
        response.read()
        check(response)
        return (response.getheader("CF-Ray") or "").rpartition("-")[2] or None
    finally:
        connection.close()


def ping():
    """Latency samples in ms.

    Times TCP connections, which only the network takes part in. Cloudflare's
    library times requests instead, minus the time Cloudflare says it took to
    answer, but its speed test can take over a second to answer one.
    """
    parts = urllib.parse.urlsplit(URL)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    address = socket.getaddrinfo(parts.hostname, port, type=socket.SOCK_STREAM)[0][4][:2]
    samples = []
    for _ in range(PINGS):
        start = time.perf_counter()
        socket.create_connection(address, timeout=TIMEOUT).close()
        samples.append((time.perf_counter() - start) * 1000)
    return samples


def download(counted, stopping):
    buffer = memoryview(bytearray(CHUNK))
    connection = connect()
    try:
        while not stopping.is_set():
            connection.request("GET", path("__down", bytes=DOWNLOAD_SIZE))
            response = connection.getresponse()
            check(response)
            while not stopping.is_set() and (read := response.readinto(buffer)):
                counted(read)
    finally:
        connection.close()


def upload(counted, stopping):
    # random, so nothing on the way can compress it
    data = memoryview(os.urandom(CHUNK))

    def body():
        for _ in range(UPLOAD_SIZE // CHUNK):
            if stopping.is_set():
                raise Stopped
            yield data
            counted(CHUNK)

    headers = {"Content-Type": "application/octet-stream", "Content-Length": str(UPLOAD_SIZE)}
    connection = connect()
    try:
        while not stopping.is_set():
            connection.request("POST", path("__up"), body(), headers)
            response = connection.getresponse()
            response.read()
            check(response)
    except Stopped:
        pass
    finally:
        connection.close()


def measure(transfer, duration):
    """Runs transfer over CONNECTIONS connections at once for duration seconds.

    Returns the speed in bits/s, leaving out the first WARM_UP seconds, and
    how many bytes were transferred.
    """
    counts = [0] * CONNECTIONS
    stopping = threading.Event()
    errors = []

    def connection(i):
        def counted(n):
            counts[i] += n
        try:
            transfer(counted, stopping)
        except Exception as e:
            # cutting the transfers short when time is up can fail them
            if not stopping.is_set():
                errors.append(e)

    def wait_until(deadline):
        while time.monotonic() < deadline and not errors:
            time.sleep(0.05)

    threads = [threading.Thread(target=connection, args=(i,), daemon=True) for i in range(CONNECTIONS)]
    start = time.monotonic()
    for thread in threads:
        thread.start()
    wait_until(start + WARM_UP)
    warmed_up, warmed_up_at = sum(counts), time.monotonic()
    wait_until(start + duration)
    done, done_at = sum(counts), time.monotonic()
    stopping.set()
    for thread in threads:
        thread.join(TIMEOUT)
    if errors:
        # being rate limited is what matters, when other connections failed too
        raise next((e for e in errors if isinstance(e, RateLimited)), errors[0])
    return (done - warmed_up) * 8 / (done_at - warmed_up_at), sum(counts)


def run(duration):
    """Tests against Cloudflare, returns the result in speedtest-cli's format."""
    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    server = data_center()
    pings = ping()
    download_speed, received = measure(download, duration)
    upload_speed, sent = measure(upload, duration)
    return {
        "download": download_speed,
        "upload": upload_speed,
        "ping": statistics.median(pings),
        # how much each latency sample differs from the one before, on average
        "jitter": statistics.mean(abs(a - b) for a, b in zip(pings, pings[1:])),
        "bytes_received": received,
        "bytes_sent": sent,
        "timestamp": timestamp,
        "server": {"id": server, "sponsor": "Cloudflare", "name": server, "host": urllib.parse.urlsplit(URL).netloc},
    }
