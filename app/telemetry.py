"""
OpenTelemetry setup — push metrics/traces/logs do OTLP collectora.

Lokalnie: OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317 (grafana/otel-lgtm)
Grafana Cloud: OTEL_EXPORTER_OTLP_ENDPOINT=https://otlp-gateway-...grafana.net/otlp

Bez endpointu → setup_telemetry() jest no-opem, apka działa normalnie.
"""
import logging
import os

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

logger = logging.getLogger("auditra.telemetry")

_SERVICE_NAME = os.getenv("OTEL_SERVICE_NAME", "auditra-backend")


def setup_telemetry(app, engine) -> None:
    """Instrumentuje FastAPI + SQLAlchemy + HTTPX. Wywołaj raz przy starcie apki."""
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint:
        print("[TELEMETRY] OTEL_EXPORTER_OTLP_ENDPOINT nie ustawione — telemetry WYŁĄCZONE", flush=True)
        return

    resource = Resource.create({
        "service.name": _SERVICE_NAME,
        "service.version": os.getenv("OTEL_SERVICE_VERSION", "0.2.0"),
        "deployment.environment": os.getenv("OTEL_DEPLOYMENT_ENVIRONMENT", "local"),
    })

    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(tracer_provider)

    metric_reader = PeriodicExportingMetricReader(
        OTLPMetricExporter(),
        export_interval_millis=15_000,
    )
    meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
    metrics.set_meter_provider(meter_provider)

    FastAPIInstrumentor.instrument_app(app)
    SQLAlchemyInstrumentor().instrument(engine=engine.sync_engine)
    HTTPXClientInstrumentor().instrument()

    print(f"[TELEMETRY] AKTYWNA → {endpoint} (service={_SERVICE_NAME})", flush=True)


# ── Custom business metrics (gotowe do użycia w serwisach) ─────────────────
_meter = metrics.get_meter("auditra")

gemini_tokens_counter = _meter.create_counter(
    name="auditra.gemini.tokens",
    description="Tokeny Gemini zużyte per model/agent",
    unit="tokens",
)

gemini_latency_histogram = _meter.create_histogram(
    name="auditra.gemini.latency",
    description="Latencja wywołań Gemini per agent",
    unit="ms",
)

briefing_generated_counter = _meter.create_counter(
    name="auditra.briefing.generated",
    description="Liczba wygenerowanych briefingów (morning/manual)",
)

briefing_skipped_counter = _meter.create_counter(
    name="auditra.briefing.skipped",
    description="Liczba pominiętych briefingów (pusty kontekst / hallucination guard)",
)

cfo_route_counter = _meter.create_counter(
    name="auditra.cfo.route",
    description="Zliczanie trasi Agentic CFO (instant/analysis/strategic_debate)",
)
