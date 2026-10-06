# Observability PoC Runbook

This guide runs vLLM on the host and the observability stack in Docker.
It ends with per-token visibility via the gateway.
All traffic must go through the gateway on port 8003.
Direct calls to vLLM on port 8000 have no token attribution by design.

## Prerequisites

- Python with `vllm` installed on the host.
- Docker and Docker Compose installed.
- Ports free: `8000`, `8002`, `8003`, `8004`, `8001`, `9090`, `16686`, `3000`.
- Model used everywhere: `Qwen/Qwen2.5-0.5B-Instruct`.
- Workdir for all compose commands: `vLLM/observability`.

## 1. Run vLLM On Host

Run vLLM outside Docker so local GPU and model cache are used.
Jaeger runs in Docker and publishes OTLP on `localhost:4317`, so the host process can reach it.

```sh
vllm serve Qwen/Qwen2.5-0.5B-Instruct \
  --host 0.0.0.0 --port 8000 \
  --otlp-traces-endpoint=http://localhost:4317
```

Verify vLLM is up.

```sh
curl http://localhost:8000/v1/models
curl -s http://localhost:8000/metrics | head -n 20
```

Keep this terminal running.
If port `8000` is busy, stop the old `vllm-server` container first because the compose stack no longer includes it.

## 2. Start Docker Services

Services and ports are defined in `docker-compose.yml`.
`rag-app` serves the mocked RAG API on `8002` and metrics on `8001`.
`gateway` is the only token-aware entrypoint on `8003` with metrics on `8004`.
`jaeger` serves UI on `16686` and OTLP on `4317` plus `4318`.
`prometheus` serves UI on `9090`.
`grafana` serves UI on `3000` with user `admin` and password `admin`.

Token map is fixed for the PoC.
`my-secret-token-123` maps to `client-a`.
`my-secret-token-456` maps to `client-b`.
`my-secret-token-rag` maps to `client-rag` and is used internally by `rag-app`.
Raw tokens are never used as metric labels.
Only `client_id` and a short hash prefix are exported.

Start everything.

```sh
cd vLLM/observability
docker compose down --volumes --remove-orphans
docker compose build gateway rag-app
docker compose up -d jaeger prometheus grafana gateway rag-app
docker compose ps
docker compose logs gateway rag-app --tail 80
```

Expected result is five containers `UP`: `jaeger`, `prometheus`, `grafana`, `gateway`, `rag-app`.
There is no `vllm-server` container by design.
Prometheus scrapes `rag-app:8001`, `gateway:8004`, and host vLLM on `host.docker.internal:8000`.
Grafana provisions three dashboards: `RAG Application Dashboard`, `vLLM`, and `LLM Gateway - API Token Usage`.

## 3. Test End To End

### 3.1 Gateway Health

```sh
curl http://localhost:8003/health
curl http://localhost:8003/v1/models \
  -H "Authorization: Bearer my-secret-token-123"
```

### 3.2 Direct Chat With Two Tokens

Use port `8003`, not `8000`, or per-token metrics will not be generated.

```sh
curl http://localhost:8003/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer my-secret-token-123" \
  -d '{"model":"Qwen/Qwen2.5-0.5B-Instruct","messages":[{"role":"user","content":"Explain quantum computing in one simple sentence."}]}'
```

```sh
curl http://localhost:8003/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer my-secret-token-456" \
  -d '{"model":"Qwen/Qwen2.5-0.5B-Instruct","messages":[{"role":"user","content":"Explain quantum computing in one simple sentence."}]}'
```

### 3.3 RAG Through Gateway

`rag-app` calls `http://gateway:8003` with `my-secret-token-rag`.
This validates the full path `client -> rag-app -> gateway -> vLLM`.

```sh
curl -X POST http://localhost:8002/rag \
  -H "Content-Type: application/json" \
  -d '{"query":"What is San Francisco?"}'
```

### 3.4 Verify Metrics And Traces

Check gateway metrics directly.

```sh
curl -s http://localhost:8004/metrics | grep -E "^llm_"
```

Check aggregated metrics in Prometheus.

```sh
curl -s "http://localhost:9090/api/v1/query?query=sum_by(client_id)(llm_requests_total)"
curl -s "http://localhost:9090/api/v1/query?query=sum_by(client_id)(llm_prompt_tokens_total)"
curl -s "http://localhost:9090/api/v1/query?query=sum_by(client_id)(llm_completion_tokens_total)"
```

Open the UIs.
Prometheus targets must show `gateway:8004` as `UP` at `http://localhost:9090/targets`.
Jaeger must show service `llm-gateway` with spans `llm.chat` carrying `llm.client_id` at `http://localhost:16686`.
Grafana must show the new dashboard at `http://localhost:3000`.
Useful Explore queries are `sum by (client_id) (rate(llm_requests_total[5m]))`, `sum by (client_id) (rate(llm_prompt_tokens_total[5m]))`, and `sum by (client_id) (rate(llm_completion_tokens_total[5m]))`.
For the legacy vLLM dashboard, select `model_name=Qwen/Qwen2.5-0.5B-Instruct` and range `Last 1 hour`.

## Troubleshooting

- `401 missing or unknown Bearer token` means the request hit the gateway without a mapped token.
- Fix it by using one of the three PoC tokens or by updating `TOKEN_MAP` and rebuilding `gateway`.
- Empty vLLM panels usually mean the wrong `model_name` variable or no recent traffic.
- Fix it by selecting the Qwen model, widening the time range, and rerunning the test calls.
- `502 vLLM unreachable` from the gateway means the host vLLM is down or port `8000` is blocked.
- Fix it by restarting the `vllm serve` command and checking `curl http://localhost:8000/v1/models`.
- Missing third dashboard means Grafana loaded an old volume or an old JSON file.
- Fix it by checking `grafana/dashboards/gateway-dashboard.json` exists and recreating the stack without stale volumes if needed.

## Stop

```sh
cd vLLM/observability
docker compose down --volumes --remove-orphans
```

Stop the host `vllm serve` process with `Ctrl-C`.
