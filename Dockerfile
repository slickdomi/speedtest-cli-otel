FROM alpine:3.24 AS base

RUN mkdir /code
WORKDIR /code

ENV every_seconds_from 1500
ENV every_seconds_to 2100
ENV service_name speedtest
ENV instance_id 1

ENV OTEL_EXPORTER_OTLP_ENDPOINT http://192.168.0.3:4318/v1/logs
ENV OTEL_EXPORTER_OTLP_LOGS_TIMEOUT 15
ENV OTEL_RESOURCE_ATTRIBUTES ""
ENV OTEL_EXPORTER_OTLP_TIMEOUT 15

RUN apk update
RUN apk add --no-cache speedtest-cli bash py3-pip
COPY requirements.txt /code/
RUN pip install --break-system-packages -r /code/requirements.txt
COPY src/* /code/
ENTRYPOINT ["python", "/code/speedtest.py"]

# docker build --target test . runs the smoke tests inside the image
FROM base AS test
COPY tests/ /tests/
RUN python /tests/smoke.py

FROM base
