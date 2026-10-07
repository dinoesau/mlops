# SPDX-License-Identifier: Apache-2.0
"""Token-aware gateway in front of host-run vLLM.

Why this exists: vLLM does not expose the Bearer token in /metrics
or OTLP traces, so per-token usage is invisible. This proxy resolves
the token to a safe identity (project/name, never the raw secret),
forwards the request to vLLM, then records usage from the response.

Security: raw tokens are never used as metric labels or trace
attributes. Only project, key_name, client_id and a short hash prefix
are exported.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from typing import TypeVar, Union

import requests
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

# --- OpenTelemetry Tracing ---
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.requests import RequestsInstrumentor
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

# --- Prometheus metrics ---
from prometheus_client import Counter, Histogram, start_http_server

SERVICE = "llm-gateway"
VLLM_BASE_URL = os.getenv("VLLM_BASE_URL", "http://host.docker.internal:8000").rstrip(
    "/"
)
OTEL_OTLP_ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "grpc://jaeger:4317")
TOKEN_MAP_RAW = os.getenv("TOKEN_MAP", "")
API_KEYS_JSON_RAW = os.getenv("API_KEYS_JSON", "")
STRICT_AUTH = os.getenv("STRICT_AUTH", "true").lower() in ("1", "true", "yes")
ALLOW_UNKNOWN_KEYS = os.getenv("ALLOW_UNKNOWN_KEYS", "false").lower() in (
    "1",
    "true",
    "yes",
)
METRICS_PORT = int(os.getenv("METRICS_PORT", "8004"))
TIMEOUT_S = int(os.getenv("VLLM_TIMEOUT_S", "120"))

FREE_PROJECT = "free"
UNKNOWN_PROJECT = "anomaly"
ANONYMOUS_NAME = "anomaly"
ANONYMOUS_CLIENT = "anonymous"
AUTH_SCHEME = "bearer "

ATTR_CLIENT = "llm.client_id"
ATTR_PROJECT = "llm.project"
ATTR_KEY_NAME = "llm.key_name"
ATTR_ENDPOINT = "llm.endpoint"
ATTR_MODEL = "llm.model"
ATTR_HASH = "llm.token_hash_prefix"
ATTR_STATUS = "llm.status"
ATTR_ERROR = "llm.error"

# --- Result railway (total functions, no raise in core) ---

T = TypeVar("T")
E = TypeVar("E")


@dataclass(frozen=True, slots=True)
class Ok[T]:
    value: T


@dataclass(frozen=True, slots=True)
class Err[E]:
    error: E


Result = Union[Ok[T], Err[E]]


# --- Stratified domain errors (no secrets inside) ---


@dataclass(frozen=True, slots=True)
class InvalidKeyEntry:
    reason: str


@dataclass(frozen=True, slots=True)
class DuplicateToken:
    detail: str


@dataclass(frozen=True, slots=True)
class DuplicateKeyName:
    detail: str


DomainError = Union[InvalidKeyEntry, DuplicateToken, DuplicateKeyName]


class GatewayError(Exception):
    def __init__(self, message: str, cause: BaseException) -> None:
        super().__init__(f"{message}: {cause}")
        self.message = message
        self.cause = cause


# --- Value objects (frozen, single parse at edge) ---


@dataclass(frozen=True, slots=True)
class ApiKey:
    project: str
    key_name: str
    disabled: bool = False

    @property
    def client_id(self) -> str:
        return f"{self.project}/{self.key_name}"

    @staticmethod
    def parse(raw: object) -> Result[tuple[str, ApiKey], InvalidKeyEntry]:
        if not isinstance(raw, dict):
            return Err(InvalidKeyEntry(reason="key entry must be an object"))
        token = raw.get("token")
        name = raw.get("name")
        project_raw = raw.get("project")
        disabled_raw = raw.get("disabled", False)
        if not isinstance(token, str) or not token.strip():
            return Err(InvalidKeyEntry(reason="missing token"))
        if not isinstance(name, str) or not name.strip():
            return Err(InvalidKeyEntry(reason="missing name"))
        project = normalize_project(project_raw)
        name_norm = name.strip()
        if "/" in name_norm or not name_norm:
            return Err(InvalidKeyEntry(reason="invalid name"))
        if not isinstance(disabled_raw, bool):
            return Err(InvalidKeyEntry(reason="invalid disabled flag"))
        return Ok(
            (
                token.strip(),
                ApiKey(project=project, key_name=name_norm, disabled=disabled_raw),
            )
        )


@dataclass(frozen=True, slots=True)
class AuthInfo:
    project: str
    key_name: str
    client_id: str
    hash_prefix: str
    disabled: bool = False

    @staticmethod
    def parse(
        auth_header: str | None, registry: dict[str, ApiKey]
    ) -> tuple[AuthInfo | None, str | None]:
        if not auth_header or not auth_header.lower().startswith(AUTH_SCHEME):
            return None, None
        token = auth_header[len("Bearer ") :].strip()
        if not token:
            return None, None
        prefix = token_hash_prefix(token)
        entry = registry.get(token)
        if entry is not None:
            return (
                AuthInfo(
                    project=entry.project,
                    key_name=entry.key_name,
                    client_id=entry.client_id,
                    hash_prefix=prefix,
                    disabled=entry.disabled,
                ),
                token,
            )
        return None, token


def normalize_project(raw: object) -> str:
    if not isinstance(raw, str):
        return FREE_PROJECT
    cleaned = raw.strip()
    if not cleaned or cleaned == FREE_PROJECT:
        return FREE_PROJECT
    return cleaned


def token_hash_prefix(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:8]


# Hardcoded example keys (placeholders only, no real secrets).
# Includes app-rag/production, app-rag/staging, app-etl/production,
# one free key without project, one disabled key,
# and plan-investigacion keys (mi-fiscalia-*).
# Unknown tokens are never accepted; blocked attempts are exported
# under project/key_name "anomaly" for visibility.
DEFAULT_KEYS: list[dict[str, object]] = [
    {"token": "poc-app-rag-prod-001", "project": "app-rag", "name": "production"},
    {"token": "poc-app-rag-stag-002", "project": "app-rag", "name": "staging"},
    {"token": "poc-app-etl-prod-003", "project": "app-etl", "name": "production"},
    {"token": "poc-free-client-a-004", "name": "client-a"},
    {
        "token": "poc-disabled-005",
        "project": "app-rag",
        "name": "revoked",
        "disabled": True,
    },
    {
        "token": "mi-fiscalia-local",
        "project": "plan-investigacion",
        "name": "mi-fiscalia-local",
    },
    {
        "token": "mi-fiscalia-test",
        "project": "plan-investigacion",
        "name": "mi-fiscalia-test",
    },
    {
        "token": "mi-fiscalia-devel",
        "project": "plan-investigacion",
        "name": "mi-fiscalia-devel",
    },
    {
        "token": "mi-fiscalia-staging",
        "project": "plan-investigacion",
        "name": "mi-fiscalia-staging",
    },
    {
        "token": "mi-fiscalia-production",
        "project": "plan-investigacion",
        "name": "mi-fiscalia-production",
    },
]


def parse_token_map(raw: str) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for item in raw.split(","):
        item = item.strip()
        if not item or ":" not in item:
            continue
        token, client = item.split(":", 1)
        token, client = token.strip(), client.strip()
        if token and client:
            mapping[token] = client
    return mapping


def legacy_client_to_key(client: str) -> Result[ApiKey, InvalidKeyEntry]:
    cleaned = client.strip()
    if not cleaned:
        return Err(InvalidKeyEntry(reason="empty legacy client"))
    if "/" in cleaned:
        parts = cleaned.split("/", 1)
        project = normalize_project(parts[0])
        name = parts[1].strip()
        if not name:
            return Err(InvalidKeyEntry(reason="invalid legacy name"))
        return Ok(ApiKey(project=project, key_name=name))
    return Ok(ApiKey(project=FREE_PROJECT, key_name=cleaned))


def parse_keys(
    defaults: list[dict[str, object]],
    token_map_raw: str,
    api_keys_json_raw: str,
) -> Result[dict[str, ApiKey], DomainError]:
    registry: dict[str, ApiKey] = {}
    seen_names: set[str] = set()

    def insert(token: str, key: ApiKey, origin: str) -> Result[None, DomainError]:
        if token in registry:
            return Err(DuplicateToken(detail=f"duplicate token from {origin}"))
        if key.client_id in seen_names:
            return Err(
                DuplicateKeyName(detail=f"duplicate {key.client_id} from {origin}")
            )
        registry[token] = key
        seen_names.add(key.client_id)
        return Ok(None)

    for idx, raw in enumerate(defaults):
        parsed = ApiKey.parse(raw)
        if isinstance(parsed, Err):
            return Err(parsed.error)
        token, key = parsed.value
        done = insert(token, key, f"defaults[{idx}]")
        if isinstance(done, Err):
            return Err(done.error)

    legacy = parse_token_map(token_map_raw)
    for token, client in legacy.items():
        if token in registry:
            continue
        converted = legacy_client_to_key(client)
        if isinstance(converted, Err):
            return Err(converted.error)
        done = insert(token, converted.value, "TOKEN_MAP")
        if isinstance(done, Err):
            return Err(done.error)

    if api_keys_json_raw.strip():
        try:
            decoded: object = json.loads(api_keys_json_raw)
        except json.JSONDecodeError as e:
            return Err(InvalidKeyEntry(reason=f"invalid API_KEYS_JSON: {e.msg}"))
        if not isinstance(decoded, list):
            return Err(InvalidKeyEntry(reason="API_KEYS_JSON must be a list"))
        for idx, raw in enumerate(decoded):
            parsed = ApiKey.parse(raw)
            if isinstance(parsed, Err):
                return Err(parsed.error)
            token, key = parsed.value
            done = insert(token, key, f"API_KEYS_JSON[{idx}]")
            if isinstance(done, Err):
                return Err(done.error)

    return Ok(registry)


def _build_registry_or_fail() -> dict[str, ApiKey]:
    result = parse_keys(DEFAULT_KEYS, TOKEN_MAP_RAW, API_KEYS_JSON_RAW)
    if isinstance(result, Err):
        err = result.error
        if isinstance(err, DuplicateToken):
            raise RuntimeError(f"duplicate token: {err.detail}")
        if isinstance(err, DuplicateKeyName):
            raise RuntimeError(f"duplicate project/name: {err.detail}")
        raise RuntimeError(f"invalid key entry: {err.reason}")
    return result.value


KEY_REGISTRY: dict[str, ApiKey] = _build_registry_or_fail()

# Legacy alias for backward compatibility.
TOKEN_MAP: dict[str, str] = {t: k.client_id for t, k in KEY_REGISTRY.items()}


def resolve_client(
    auth_header: str | None,
) -> tuple[str | None, str | None, str | None]:
    info, raw_token = AuthInfo.parse(auth_header, KEY_REGISTRY)
    if info is None:
        if raw_token is None:
            return None, None, None
        prefix = token_hash_prefix(raw_token)
        return f"hash-{prefix}", prefix, raw_token
    return info.client_id, info.hash_prefix, raw_token


def resolve_auth(auth_header: str | None) -> tuple[AuthInfo | None, str | None]:
    return AuthInfo.parse(auth_header, KEY_REGISTRY)


def is_blocked(info: AuthInfo | None, raw_token: str | None) -> bool:
    if info is None:
        if raw_token is None:
            return True
        # Unknown key: pass as anomaly/anomaly only when explicitly allowed.
        return not ALLOW_UNKNOWN_KEYS
    if info.disabled:
        return True
    if raw_token is None:
        return True
    return False


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
    ["client_id", "project", "key_name", "model", "endpoint", "status"],
)
PROMPT_TOKENS = Counter(
    "llm_prompt_tokens_total",
    "Prompt tokens by client",
    ["client_id", "project", "key_name", "model"],
)
COMPLETION_TOKENS = Counter(
    "llm_completion_tokens_total",
    "Completion tokens by client",
    ["client_id", "project", "key_name", "model"],
)
DURATION = Histogram(
    "llm_request_duration_seconds",
    "Gateway to vLLM round trip",
    ["client_id", "project", "key_name", "model", "endpoint"],
)

start_http_server(port=METRICS_PORT)

# --- FastAPI app ---
app = FastAPI(title="LLM Token-Aware Gateway")
FastAPIInstrumentor.instrument_app(app)
RequestsInstrumentor().instrument()


@app.get("/health")
def health() -> dict[str, object]:
    projects = sorted({key.project for key in KEY_REGISTRY.values()})
    return {
        "status": "ok",
        "vllm": VLLM_BASE_URL,
        "strict_auth": STRICT_AUTH,
        "projects": projects,
    }


def _forward(
    method: str, path: str, body: dict[str, object] | None, headers: dict[str, str]
) -> requests.Response:
    url = f"{VLLM_BASE_URL}{path}"
    propagator = TraceContextTextMapPropagator()
    propagator.inject(headers)
    try:
        if method == "GET":
            return requests.get(url, headers=headers, timeout=TIMEOUT_S)
        return requests.post(url, json=body, headers=headers, timeout=TIMEOUT_S)
    except requests.RequestException as e:
        raise GatewayError(message="vLLM unreachable", cause=e) from e


def _labels(info: AuthInfo | None, fallback_client: str) -> tuple[str, str, str]:
    if info is None:
        return fallback_client, UNKNOWN_PROJECT, ANONYMOUS_NAME
    return info.client_id, info.project, info.key_name


def _gateway_proxy(
    request: Request, endpoint: str, body: dict[str, object] | None
) -> Response:
    auth = request.headers.get("authorization")
    info, raw_token = resolve_auth(auth)

    if STRICT_AUTH and is_blocked(info, raw_token):
        blocked_client = (
            f"hash-{token_hash_prefix(raw_token)}"
            if raw_token
            else ANONYMOUS_CLIENT
        )
        blocked_model_obj: object = (body or {}).get("model", "unknown")
        REQUESTS.labels(
            blocked_client,
            UNKNOWN_PROJECT,
            ANONYMOUS_NAME,
            str(blocked_model_obj),
            endpoint,
            "blocked",
        ).inc()
        return JSONResponse(
            status_code=401,
            content={"error": "missing or unknown Bearer token"},
        )

    if info is None:
        fallback_prefix = token_hash_prefix(raw_token) if raw_token else "none"
        fallback_client = f"hash-{fallback_prefix}" if raw_token else ANONYMOUS_CLIENT
    else:
        fallback_client = info.client_id
    label_client, label_project, label_key = _labels(info, fallback_client)
    hash_prefix = info.hash_prefix if info is not None else None
    model_obj: object = (body or {}).get("model", "unknown")
    model = str(model_obj)

    fwd_headers = {"Content-Type": "application/json"}
    if raw_token:
        # Forward auth so vLLM --api-key keeps working if enabled.
        fwd_headers["Authorization"] = f"Bearer {raw_token}"

    start = time.time()
    with tracer.start_as_current_span("llm.chat") as span:
        span.set_attribute(ATTR_CLIENT, label_client)
        span.set_attribute(ATTR_PROJECT, label_project)
        span.set_attribute(ATTR_KEY_NAME, label_key)
        span.set_attribute(ATTR_ENDPOINT, endpoint)
        span.set_attribute(ATTR_MODEL, model)
        if hash_prefix:
            span.set_attribute(ATTR_HASH, hash_prefix)

        try:
            resp = _forward("POST", endpoint, body or {}, dict(fwd_headers))
        except GatewayError as e:
            REQUESTS.labels(
                label_client, label_project, label_key, model, endpoint, "gateway_error"
            ).inc()
            span.set_attribute(ATTR_STATUS, "gateway_error")
            span.set_attribute(ATTR_ERROR, f"{e.message}: {e.cause}")
            return JSONResponse(
                status_code=502, content={"error": f"vLLM unreachable: {e.cause}"}
            )

        elapsed = time.time() - start
        status_label = "ok" if resp.ok else f"upstream_{resp.status_code}"
        REQUESTS.labels(
            label_client, label_project, label_key, model, endpoint, status_label
        ).inc()
        DURATION.labels(
            label_client, label_project, label_key, model, endpoint
        ).observe(elapsed)
        span.set_attribute(ATTR_STATUS, status_label)
        span.set_attribute("llm.duration_s", elapsed)

        try:
            data = resp.json()
        except ValueError:
            return Response(
                content=resp.content,
                status_code=resp.status_code,
                media_type=resp.headers.get("content-type", "application/json"),
            )

        usage_obj: object = data.get("usage", {}) if isinstance(data, dict) else {}
        usage: dict[str, object] = usage_obj if isinstance(usage_obj, dict) else {}
        prompt_raw = usage.get("prompt_tokens", 0)
        completion_raw = usage.get("completion_tokens", 0)
        prompt_t = int(prompt_raw or 0) if isinstance(prompt_raw, int) else 0
        completion_t = (
            int(completion_raw or 0) if isinstance(completion_raw, int) else 0
        )
        total_raw = usage.get("total_tokens", prompt_t + completion_t)
        total_t = (
            int(total_raw or 0)
            if isinstance(total_raw, int)
            else prompt_t + completion_t
        )
        if prompt_t:
            PROMPT_TOKENS.labels(label_client, label_project, label_key, model).inc(
                prompt_t
            )
        if completion_t:
            COMPLETION_TOKENS.labels(label_client, label_project, label_key, model).inc(
                completion_t
            )
        span.set_attribute("llm.prompt_tokens", prompt_t)
        span.set_attribute("llm.completion_tokens", completion_t)
        span.set_attribute("llm.total_tokens", total_t)

        return JSONResponse(status_code=resp.status_code, content=data)


@app.post("/v1/chat/completions")
async def chat_completions(request: Request) -> Response:
    body = await request.json()
    if not isinstance(body, dict):
        body = {}
    return _gateway_proxy(request, "/v1/chat/completions", body)


@app.post("/v1/completions")
async def completions(request: Request) -> Response:
    body = await request.json()
    if not isinstance(body, dict):
        body = {}
    return _gateway_proxy(request, "/v1/completions", body)


@app.get("/v1/models")
def models(request: Request) -> JSONResponse:
    auth = request.headers.get("authorization")
    info, raw_token = resolve_auth(auth)
    if STRICT_AUTH and is_blocked(info, raw_token):
        return JSONResponse(
            status_code=401, content={"error": "missing or unknown Bearer token"}
        )
    headers: dict[str, str] = {}
    if raw_token:
        headers["Authorization"] = f"Bearer {raw_token}"
    label_client = info.client_id if info is not None else ANONYMOUS_CLIENT
    with tracer.start_as_current_span("llm.models") as span:
        span.set_attribute(ATTR_CLIENT, label_client)
        span.set_attribute(ATTR_PROJECT, info.project if info else UNKNOWN_PROJECT)
        span.set_attribute(ATTR_KEY_NAME, info.key_name if info else ANONYMOUS_NAME)
        if info is not None:
            span.set_attribute(ATTR_HASH, info.hash_prefix)
        try:
            resp = _forward("GET", "/v1/models", None, headers)
            return JSONResponse(status_code=resp.status_code, content=resp.json())
        except GatewayError as e:
            return JSONResponse(
                status_code=502, content={"error": f"vLLM unreachable: {e.cause}"}
            )
        except ValueError as e:
            return JSONResponse(
                status_code=502, content={"error": f"invalid upstream response: {e}"}
            )
