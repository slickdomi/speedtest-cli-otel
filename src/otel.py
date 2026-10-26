import os

from opentelemetry._logs import SeverityNumber, set_logger_provider
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor

service_name = os.environ.get("service_name")
instance_id = os.environ.get("instance_id")
endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")

logger_provider = LoggerProvider(
    resource=Resource.create(
        {
            "service.name": service_name,
            "service.instance.id": instance_id,
        }
    ),
)
set_logger_provider(logger_provider)

exporter = OTLPLogExporter(
    endpoint=endpoint,
)

logger_provider.add_log_record_processor(BatchLogRecordProcessor(exporter))
# Emitting directly instead of through the logging module keeps the result a
# structured body, which the dashboard reads fields from
logger = logger_provider.get_logger(service_name)

def send_logs(logs_data):
    logger.emit(severity_number=SeverityNumber.INFO, severity_text="INFO", body=logs_data)

def send_error(*error_message):
    # exceptions can't be encoded by the exporter and would drop the whole batch
    logger.emit(
        severity_number=SeverityNumber.ERROR,
        severity_text="ERROR",
        body=": ".join(str(part) for part in error_message),
    )
