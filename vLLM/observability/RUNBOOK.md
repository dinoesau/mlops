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

Keys are hardcoded in `gateway/gateway.py` as `DEFAULT_KEYS` for the PoC.
`poc-app-rag-prod-001` maps to `app-rag/production`.
`poc-app-rag-stag-002` maps to `app-rag/staging`.
`poc-app-etl-prod-003` maps to `app-etl/production`.
`poc-free-client-a-004` has no `project` and normalizes to `free/client-a`.
`poc-disabled-005` maps to `app-rag/revoked` with `disabled:true` and always returns `401`.
`rag-app` uses `poc-app-rag-prod-001` via `RAG_LLM_TOKEN`.
Raw tokens are never used as metric labels.
Only `client_id`, `project`, `key_name` and a short hash prefix are exported.
`TOKEN_MAP` and `API_KEYS_JSON` remain as optional overrides only.

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

### UI Endpoints

| Service | URL | Notes |
| --- | --- | --- |
| Grafana | http://localhost:3000 | User `admin`, password `admin`. Gateway dashboard `LLM Gateway - API Token Usage`. |
| Prometheus | http://localhost:9090 | Targets at `/targets`. Gateway job `gateway:8004`. |
| Jaeger | http://localhost:16686 | Service `llm-gateway`, spans `llm.chat` with `llm.project`. |
| Gateway API | http://localhost:8003/health | Health lists `projects` without secrets. |
| Gateway metrics | http://localhost:8004/metrics | Filter `^llm_` for `project,key_name` series. |
| RAG API | http://localhost:8002/rag | POST entrypoint. Metrics on `:8001`. |
| vLLM host | http://localhost:8000/v1/models | Direct access has no token attribution. |

## 3. Test End To End

### 3.1 Gateway Health

```sh
curl http://localhost:8003/health
curl http://localhost:8003/v1/models \
  -H "Authorization: Bearer poc-app-rag-prod-001"
```

`GET /health` lists `projects` without secrets.
`GET /v1/models` propagates identity and enforces `401` for unknown or disabled keys.

### 3.2 Direct Chat Prod Vs Staging

Use port `8003`, not `8000`, or per-project metrics will not be generated.

```sh
curl http://localhost:8003/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer poc-app-rag-prod-001" \
  -d '{"model":"Qwen/Qwen2.5-0.5B-Instruct","messages":[{"role":"user","content":"Explain quantum computing in one simple sentence."}]}'
```

```sh
curl http://localhost:8003/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer poc-app-rag-stag-002" \
  -d '{"model":"Qwen/Qwen2.5-0.5B-Instruct","messages":[{"role":"user","content":"Explain quantum computing in one simple sentence."}]}'
```

Prod uses `app-rag/production`, staging uses `app-rag/staging`.
Metrics split by `project,key_name` proves the separation.

### 3.3 RAG Through Gateway

`rag-app` calls `http://gateway:8003` with `poc-app-rag-prod-001`.
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
curl -s "http://localhost:9090/api/v1/query?query=sum_by(project,key_name)(llm_requests_total)"
curl -s "http://localhost:9090/api/v1/query?query=sum_by(project,key_name)(llm_prompt_tokens_total)"
curl -s "http://localhost:9090/api/v1/query?query=sum_by(project,key_name)(llm_completion_tokens_total)"
```

Open the UIs.
Prometheus targets must show `gateway:8004` as `UP` at `http://localhost:9090/targets`.
Jaeger must show service `llm-gateway` with spans `llm.chat` carrying `llm.project`, `llm.key_name`, `llm.client_id` at `http://localhost:16686`.
Grafana must show the new dashboard at `http://localhost:3000`.
Useful Explore queries are `sum by (project, key_name) (rate(llm_requests_total[5m]))`, `sum by (project, key_name) (rate(llm_prompt_tokens_total[5m]))`, and `sum by (project, key_name) (rate(llm_completion_tokens_total[5m]))`.
For the legacy vLLM dashboard, select `model_name=Qwen/Qwen2.5-0.5B-Instruct` and range `Last 1 hour`.
Sankey flow `project -> project/key_name -> model` uses variable `$metric` (default `llm_requests_total`, switchable to `llm_prompt_tokens_total`).
Query is `sum by (project,key_name,model) (rate($metric[5m]))`.
Mapping is level1 `project`, level2 `project/key_name`, level3 `model` into `series.links` as `source,target,value`.
If `volkovlabs-echarts-panel` is missing, use fallback table panel `Flow project/key/model table` with the same query.
Validate with `docker compose up -d grafana`, rerun prod vs staging calls from `3.2` to `Qwen/Qwen2.5-0.5B-Instruct`, then check `http://localhost:3000` dashboard `LLM Gateway - API Token Usage`.
Expected capture is branches `app-rag -> app-rag/production -> Qwen/Qwen2.5-0.5B-Instruct` and `app-rag -> app-rag/staging -> Qwen/Qwen2.5-0.5B-Instruct`.
`Llama-3.1-8B-Instruct` is an imaginary model used only as example to show a second `model` branch, not real prod traffic.

### 3.5 Load Test

`load_test.sh` sends 100 POSTs to `http://localhost:8002/rag` with a 10s pause between calls.
Traffic flows through `rag-app -> gateway -> vLLM` using `app-rag/production`.
Run it after the stack is `UP` to populate Prometheus, Jaeger, and Grafana.

```sh
cd vLLM/observability
chmod +x load_test.sh
./load_test.sh
```

Stop early with `Ctrl-C` if you only need a few minutes of data.
Verify with `curl -s http://localhost:8004/metrics | grep -E "^llm_"`.
Check Grafana `LLM Gateway - API Token Usage` for `app-rag/production` growth.

## Troubleshooting

- `401 missing or unknown Bearer token` means the request hit the gateway without a mapped token.
- Fix it by using one of the PoC `poc-*` keys or by updating `API_KEYS_JSON` and rebuilding `gateway`.
- Empty vLLM panels usually mean the wrong `model_name` variable or no recent traffic.
- Fix it by selecting the Qwen model, widening the time range, and rerunning the test calls.
- `502 vLLM unreachable` from the gateway means the host vLLM is down or port `8000` is blocked.
- Fix it by restarting the `vllm serve` command and checking `curl http://localhost:8000/v1/models`.
- Missing third dashboard means Grafana loaded an old volume or an old JSON file.
- Fix it by checking `grafana/dashboards/gateway-dashboard.json` exists and recreating the stack without stale volumes if needed.
- `docker compose build gateway` failing on `apt-get update` with `403` from `deb.debian.org` means the sandbox proxy blocks Debian repos.
- Work around it with a throwaway Python 3.12 image without the `apt` step (needs `setuptools==80.9.0` for OTEL `pkg_resources`), tagged as `observability-gateway:latest`, then `docker compose up -d --force-recreate gateway`.

## Stop

```sh
cd vLLM/observability
docker compose down --volumes --remove-orphans
```

Stop the host `vllm serve` process with `Ctrl-C`.
