# SPDX-License-Identifier: Apache-2.0
"""Token-aware gateway in front of host-run vLLM.

Why this exists: vLLM does not expose the Bearer token in /metrics
or OTLP traces, so per-token usage is invisible. This proxy resolves
the token to a safe client_id (never the raw secret), forwards the
request to vLLM, then records usage from the response.

Security: raw tokens are never used as metric labels or trace
attributes. Only client_id and a short hash prefix are exported.
"""
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
import hashlib
import os
import time
import requests

# --- OpenTelemetry Tracing ---
from opentelemetry import trace
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.requests import RequestsInstrumentor
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

# --- Prometheus metrics ---
from prometheus_client import Counter, Histogram, start_http_server

SERVICE = "llm-gateway"
VLLM_BASE_URL = os.getenv("VLLM_BASE_URL", "http://host.docker.internal:8000").rstrip("/")
OTEL_OTLP_ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "grpc://jaeger:4317")
TOKEN_MAP_RAW = os.getenv(
    "TOKEN_MAP",
    "my-secret-token-123:client-a,my-secret-token-456:client-b",
)
STRICT_AUTH = os.getenv("STRICT_AUTH", "true").lower() in ("1", "true", "yes")
METRICS_PORT = int(os.getenv("METRICS_PORT", "8004"))
TIMEOUT_S = int(os.getenv("VLLM_TIMEOUT_S", "120"))


def parse_token_map(raw: str) -> dict:
    mapping = {}
    for item in raw.split(","):
        item = item.strip()
        if not item or ":" not in item:
            continue
        token, client = item.split(":", 1)
        token, client = token.strip(), client.strip()
        if token and client:
            mapping[token] = client
    return mapping


TOKEN_MAP = parse_token_map(TOKEN_MAP_RAW)


def token_hash_prefix(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:8]


def resolve_client(auth_header: str | None) -> tuple[str | None, str | None, str | None]:
    """Return (client_id, hash_prefix, raw_token) or (None, None, None) if missing."""
    if not auth_header or not auth_header.lower().startswith("bearer "):
        return None, None, None
    token = auth_header[7:].strip()
    if not token:
        return None, None, None
    prefix = token_hash_prefix(token)
    if token in TOKEN_MAP:
        return TOKEN_MAP[token], prefix, token
    # Unknown token: fall back to hashed identity so no secret leaks into labels.
    return f"hash-{prefix}", prefix, token


# --- Tracing setup ---
resource = Resource(attributes={SERVICE_NAME: SERVICE})
trace_provider = TracerProvider(resource=resource)
trace.set_tracer_provider(trace_provider)
otlp_exporter = OTLPSpanExporter(endpoint=OTEL_OTLP_ENDPOINT, insecure=True)
trace_provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
trace_provider.add_span_processor(BatchSpanProcessor(otlp_exporter))
tracer = trace.get_tracer(__name__)

# --- Metrics setup (plain prometheus_client, scraped on :8004/metrics) ---
REQUESTS = Counter(
    "llm_requests_total",
    "Total LLM requests seen by gateway",
    ["client_id", "model", "endpoint", "status"],
)
PROMPT_TOKENS = Counter(
    "llm_prompt_tokens_total",
    "Prompt tokens by client",
    ["client_id", "model"],
)
COMPLETION_TOKENS = Counter(
    "llm_completion_tokens_total",
    "Completion tokens by client",
    ["client_id", "model"],
)
DURATION = Histogram(
    "llm_request_duration_seconds",
    "Gateway to vLLM round trip",
    ["client_id", "model", "endpoint"],
)

start_http_server(port=METRICS_PORT)

# --- FastAPI app ---
app = FastAPI(title="LLM Token-Aware Gateway")
FastAPIInstrumentor.instrument_app(app)
RequestsInstrumentor().instrument()


@app.get("/health")
def health():
    return {"status": "ok", "vllm": VLLM_BASE_URL, "strict_auth": STRICT_AUTH}


def _forward(method: str, path: str, body: dict | None, headers: dict):
    url = f"{VLLM_BASE_URL}{path}"
    propagator = TraceContextTextMapPropagator()
    propagator.inject(headers)
    if method == "GET":
        return requests.get(url, headers=headers, timeout=TIMEOUT_S)
    return requests.post(url, json=body, headers=headers, timeout=TIMEOUT_S)


def _gateway_proxy(request: Request, endpoint: str, body: dict | None):
    auth = request.headers.get("authorization")
    client_id, hash_prefix, raw_token = resolve_client(auth)

    if STRICT_AUTH and client_id is None:
        return JSONResponse(
            status_code=401,
            content={"error": "missing or unknown Bearer token"},
        )

    label_client = client_id or "anonymous"
    model = (body or {}).get("model", "unknown")

    fwd_headers = {"Content-Type": "application/json"}
    if raw_token:
        # Forward auth so vLLM --api-key keeps working if enabled.
        fwd_headers["Authorization"] = f"Bearer {raw_token}"

    start = time.time()
    with tracer.start_as_current_span("llm.chat") as span:
        span.set_attribute("llm.client_id", label_client)
        span.set_attribute("llm.endpoint", endpoint)
        span.set_attribute("llm.model", str(model))
        if hash_prefix:
            span.set_attribute("llm.token_hash_prefix", hash_prefix)

        try:
            resp = _forward("POST", endpoint, body or {}, dict(fwd_headers))
        except Exception as e:
            REQUESTS.labels(label_client, str(model), endpoint, "gateway_error").inc()
            span.set_attribute("llm.status", "gateway_error")
            span.set_attribute("llm.error", str(e))
            return JSONResponse(status_code=502, content={"error": f"vLLM unreachable: {e}"})

        elapsed = time.time() - start
        status_label = "ok" if resp.ok else f"upstream_{resp.status_code}"
        REQUESTS.labels(label_client, str(model), endpoint, status_label).inc()
        DURATION.labels(label_client, str(model), endpoint).observe(elapsed)
        span.set_attribute("llm.status", status_label)
        span.set_attribute("llm.duration_s", elapsed)

        try:
            data = resp.json()
        except Exception:
            return Response(
                content=resp.content,
                status_code=resp.status_code,
                media_type=resp.headers.get("content-type", "application/json"),
            )

        usage = data.get("usage", {}) if isinstance(data, dict) else {}
        prompt_t = int(usage.get("prompt_tokens", 0) or 0)
        completion_t = int(usage.get("completion_tokens", 0) or 0)
        total_t = int(usage.get("total_tokens", prompt_t + completion_t) or 0)
        if prompt_t:
            PROMPT_TOKENS.labels(label_client, str(model)).inc(prompt_t)
        if completion_t:
            COMPLETION_TOKENS.labels(label_client, str(model)).inc(completion_t)
        span.set_attribute("llm.prompt_tokens", prompt_t)
        span.set_attribute("llm.completion_tokens", completion_t)
        span.set_attribute("llm.total_tokens", total_t)

        return JSONResponse(status_code=resp.status_code, content=data)


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    return _gateway_proxy(request, "/v1/chat/completions", body)


@app.post("/v1/completions")
async def completions(request: Request):
    body = await request.json()
    return _gateway_proxy(request, "/v1/completions", body)


@app.get("/v1/models")
def models(request: Request):
    auth = request.headers.get("authorization")
    client_id, _, raw_token = resolve_client(auth)
    headers: dict = {}
    if raw_token:
        headers["Authorization"] = f"Bearer {raw_token}"
    with tracer.start_as_current_span("llm.models") as span:
        span.set_attribute("llm.client_id", client_id or "anonymous")
        try:
            resp = _forward("GET", "/v1/models", None, headers)
            return JSONResponse(status_code=resp.status_code, content=resp.json())
        except Exception as e:
            return JSONResponse(status_code=502, content={"error": f"vLLM unreachable: {e}"})
