# Speedtest-cli with OpenTelemetry

To fight the unwinable battle of bad internet connections, this service runs a speedtest-cli every few minutes and sends the results to an OpenTelemetry collector. This allows for monitoring of the internet connection over time. I'm attaching a Grafana dashboard that can be used to visualize the data.

![Preview of the grafana dashboard](screenshot.png)

## Installation and configuration

Get prebuilt docker image from dockerhub:
```
docker run -e OTEL_EXPORTER_OTLP_ENDPOINT='http://192.168.0.2:4137/v1/logs' --name speedtest slickdomi/speedtest-cli-otel:latest
```

Configure the service using the following environment variables:

By default the service will run a speedtest about once an hour, every 55-65 minutes (3300-3900 seconds), and send the results to the OpenTelemetry collector.
```
every_seconds_from 3300
every_seconds_to 3900
```

```
service_name speedtest
instance_id 1
OTEL_EXPORTER_OTLP_ENDPOINT http://192.168.0.2:4137/v1/logs
```

Pin or exclude speedtest servers with comma-separated server ids:
```
SPEEDTEST_SERVER 59333,12627
SPEEDTEST_EXCLUDE 1234
```

For other OTEL env variables refer to https://opentelemetry.io/docs/languages/sdk-configuration/otlp-exporter/

Docker container available at https://hub.docker.com/r/slickdomi/speedtest-cli-otel

## Choosing the speedtest

### speedtest-cli (default)

[speedtest-cli](https://github.com/sivel/speedtest-cli) is open source and part of the image, but it has been archived since 2021 and works with fewer and fewer servers. When several servers are pinned, the one with the lowest latency is used. List ids with `docker run --rm --entrypoint speedtest-cli slickdomi/speedtest-cli-otel:latest --secure --list`, on the same network as the service. Only the ~10 servers that list shows can be pinned, any other id makes speedtest-cli fail with `No matched servers`.

By default the service talks to speedtest.net over https, and speedtest.net may then only offer servers far away (Spain and Portugal from Belgium, for example). `SPEEDTEST_SECURE false` switches to http, which offers the nearby ones; list them by dropping `--secure` from the command above. Many of those redirect to https, which speedtest-cli can't follow, so their latency checks fail and their upload numbers are garbage. Such results are reported as an error instead of being sent, so pin servers that work.
```
SPEEDTEST_SECURE true
```

### Ookla's Speedtest CLI

The service can use Ookla's official [Speedtest CLI](https://www.speedtest.net/apps/cli) instead. It works with every server and measures fast connections more accurately, but it isn't open source and its license doesn't allow redistributing it, so it's not included in the image. When you opt in, the service downloads it from Ookla when it starts and checks its checksum.
```
SPEEDTEST_ACCEPT_OOKLA_LICENSE true
```

**By setting this you accept Ookla's [End User License Agreement](https://www.speedtest.net/about/eula), [Terms of Use](https://www.speedtest.net/about/terms) and [Privacy Policy](https://www.speedtest.net/about/privacy).** Among other things, the license only allows personal, non-commercial use on a personal computer you own or control, and not on routers, modems or other non-personal devices. Ookla collects data that may identify you, such as your IP address, device identifiers and location, and may share it with internet providers, hardware manufacturers and regulators. Ookla also publishes every result on speedtest.net; the service sends its link as `share`.

Ookla's CLI only takes a single server, so pinned servers are tried in order and the first one that works is used, and with `SPEEDTEST_EXCLUDE` the closest server that isn't excluded is used. List nearby servers with:
```
docker run --rm -e SPEEDTEST_ACCEPT_OOKLA_LICENSE=true --entrypoint python slickdomi/speedtest-cli-otel:latest /code/ookla.py --servers
```

Runs rotate over several servers, so the results don't all come from the same one: each run starts one server further down the pinned servers, or down the nearby ones that aren't excluded when none are pinned, and falls back on the ones after it. Nearby servers are listed once a day. Pin a few servers to choose which ones it rotates through. Set this to `false` to always start with the first pinned server, or, when none are pinned, to let Ookla's CLI pick the best one. Rotation only works with Ookla's CLI.
```
SPEEDTEST_ROTATE true
```

Ookla limits how often the CLI may run from one public IP address. The limit isn't published, and it seems to have tightened around 30 October 2026: since then, people report Ookla turning tests away when they run every 20 minutes, but not every 30 minutes. Every device sharing your public IP address counts towards it, and so does every restart of the service, which tests right away. Listing servers is limited too. When Ookla turns the CLI away with `Limit reached: Too many requests received`, that's reported as the error, no other server is tried, and the service stops testing for 2 hours, because trying again sooner seems to make the limit last longer. Running once an hour, the default, stays under it; don't go below 30 minutes between runs, and leave more room when other devices on your connection run speedtests too.

Results are converted to speedtest-cli's fields, so the dashboard works with both, and add `jitter` and `packet_loss`. The network interface details Ookla reports, which include local and public IP and MAC addresses, aren't sent.

## Development

Dependencies are pinned in `requirements.txt` and bumped by Dependabot, except Ookla's CLI, whose version and checksums are in `src/ookla.py`. `tests/smoke.py` runs the service inside the image against fake speedtest binaries and a fake OpenTelemetry collector, and CI runs it for every platform before pushing. To run it locally:
```
docker build --target test .
```

## License

This project is licensed under the GPL-3.0, see [LICENSE](LICENSE). Ookla's Speedtest CLI isn't part of it or of the image; it's proprietary software covered by Ookla's own license. Ookla and Speedtest are trademarks of Ookla, LLC, and this project isn't affiliated with or endorsed by Ookla.
