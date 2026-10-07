# SPDX-License-Identifier: Apache-2.0
"""Vertical slices for project/name keys: parse, resolve, proxy, metrics, health."""

import os

os.environ.setdefault("METRICS_PORT", "18004")
os.environ.setdefault("OTEL_EXPORTER_OTLP_ENDPOINT", "grpc://localhost:4317")

from fastapi.testclient import TestClient

import gateway as gw
from gateway import (
    ApiKey,
    AuthInfo,
    DuplicateKeyName,
    DuplicateToken,
    Err,
    InvalidKeyEntry,
    Ok,
    is_blocked,
    normalize_project,
    parse_keys,
    token_hash_prefix,
)


def test_normalize_free() -> None:
    assert normalize_project(None) == "free"
    assert normalize_project("") == "free"
    assert normalize_project("free") == "free"
    assert normalize_project("app-rag") == "app-rag"


def test_parse_defaults_has_ten_entries() -> None:
    result = parse_keys(gw.DEFAULT_KEYS, "", "")
    assert isinstance(result, Ok)
    registry = result.value
    assert len(registry) == 10
    assert registry["poc-app-rag-prod-001"].client_id == "app-rag/production"
    assert registry["poc-app-rag-stag-002"].client_id == "app-rag/staging"
    assert registry["poc-app-etl-prod-003"].client_id == "app-etl/production"
    # Free key without project normalizes to free/.
    assert registry["poc-free-client-a-004"].client_id == "free/client-a"
    assert registry["poc-free-client-a-004"].project == "free"
    assert registry["poc-disabled-005"].disabled is True
    # plan-investigacion keys.
    assert (
        registry["mi-fiscalia-local"].client_id
        == "plan-investigacion/mi-fiscalia-local"
    )
    assert (
        registry["mi-fiscalia-production"].client_id
        == "plan-investigacion/mi-fiscalia-production"
    )


def test_parse_rejects_duplicate_token() -> None:
    dup = list(gw.DEFAULT_KEYS) + [
        {"token": "poc-app-rag-prod-001", "project": "x", "name": "y"}
    ]
    result = parse_keys(dup, "", "")
    assert isinstance(result, Err)
    assert isinstance(result.error, DuplicateToken)


def test_parse_rejects_duplicate_project_name() -> None:
    dup = list(gw.DEFAULT_KEYS) + [
        {"token": "poc-unique-999", "project": "app-rag", "name": "production"}
    ]
    result = parse_keys(dup, "", "")
    assert isinstance(result, Err)
    assert isinstance(result.error, DuplicateKeyName)


def test_parse_rejects_invalid_entry() -> None:
    result = parse_keys([{"token": "", "name": ""}], "", "")
    assert isinstance(result, Err)
    assert isinstance(result.error, InvalidKeyEntry)


def test_resolve_prod_vs_staging_distinct() -> None:
    result = parse_keys(gw.DEFAULT_KEYS, "", "")
    assert isinstance(result, Ok)
    registry = result.value
    prod, raw_prod = AuthInfo.parse("Bearer poc-app-rag-prod-001", registry)
    stag, raw_stag = AuthInfo.parse("Bearer poc-app-rag-stag-002", registry)
    assert prod is not None and stag is not None
    assert prod.project == "app-rag" and prod.key_name == "production"
    assert stag.project == "app-rag" and stag.key_name == "staging"
    assert prod.client_id == "app-rag/production"
    assert stag.client_id == "app-rag/staging"
    assert raw_prod == "poc-app-rag-prod-001"
    assert raw_stag == "poc-app-rag-stag-002"
    assert len(prod.hash_prefix) == 8
    assert prod.hash_prefix == token_hash_prefix("poc-app-rag-prod-001")


def test_resolve_free_and_disabled() -> None:
    result = parse_keys(gw.DEFAULT_KEYS, "", "")
    assert isinstance(result, Ok)
    registry = result.value
    free, _ = AuthInfo.parse("Bearer poc-free-client-a-004", registry)
    assert free is not None
    assert free.project == "free"
    assert free.client_id == "free/client-a"
    revoked, raw = AuthInfo.parse("Bearer poc-disabled-005", registry)
    assert revoked is not None
    assert revoked.disabled is True
    assert is_blocked(revoked, raw) is True


def test_resolve_unknown_and_missing() -> None:
    result = parse_keys(gw.DEFAULT_KEYS, "", "")
    assert isinstance(result, Ok)
    registry = result.value
    info, raw = AuthInfo.parse("Bearer no-such-token", registry)
    assert info is None
    assert raw == "no-such-token"
    assert is_blocked(info, raw) is True
    info2, raw2 = AuthInfo.parse(None, registry)
    assert info2 is None and raw2 is None
    assert is_blocked(info2, raw2) is True


def test_api_key_value_object_derives_client_id() -> None:
    key = ApiKey(project="app-rag", key_name="production")
    assert key.client_id == "app-rag/production"


def _client() -> TestClient:
    return TestClient(gw.app)


def test_proxy_401_for_unknown_missing_disabled() -> None:
    c = _client()
    # Unknown token.
    r = c.post(
        "/v1/chat/completions",
        json={"model": "m", "messages": []},
        headers={"Authorization": "Bearer unknown-xyz"},
    )
    assert r.status_code == 401
    # Missing token.
    r2 = c.post("/v1/chat/completions", json={"model": "m", "messages": []})
    assert r2.status_code == 401
    # Disabled key.
    r3 = c.post(
        "/v1/chat/completions",
        json={"model": "m", "messages": []},
        headers={"Authorization": "Bearer poc-disabled-005"},
    )
    assert r3.status_code == 401


def test_proxy_502_preserves_cause(monkeypatch: object) -> None:
    c = _client()

    def _boom(
        method: str, path: str, body: dict[str, object] | None, headers: dict[str, str]
    ) -> object:
        raise gw.GatewayError(
            message="vLLM unreachable", cause=ConnectionError("refused")
        )

    monkeypatch.setattr(gw, "_forward", _boom)  # type: ignore[attr-defined]
    r = c.post(
        "/v1/chat/completions",
        json={"model": "m", "messages": []},
        headers={"Authorization": "Bearer poc-app-rag-prod-001"},
    )
    assert r.status_code == 502
    assert "refused" in r.json()["error"]


def test_metrics_labels_include_project_key_name() -> None:
    # Behavioral check via public .labels() API (no private access).
    # If project/key_name labels were missing, these calls would fail.
    gw.REQUESTS.labels(
        "app-rag/production", "app-rag", "production", "m", "/v1/chat/completions", "ok"
    ).inc(0)
    gw.PROMPT_TOKENS.labels("app-rag/production", "app-rag", "production", "m").inc(0)
    gw.COMPLETION_TOKENS.labels("app-rag/production", "app-rag", "production", "m").inc(
        0
    )
    gw.DURATION.labels(
        "app-rag/production", "app-rag", "production", "m", "/v1/chat/completions"
    ).observe(0)


def test_health_lists_projects_without_secrets() -> None:
    c = _client()
    r = c.get("/health")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ok"
    assert "app-rag" in data["projects"]
    assert "free" in data["projects"]
    assert "plan-investigacion" in data["projects"]
    body = r.text
    for secret in (
        "poc-app-rag-prod-001",
        "poc-app-rag-stag-002",
        "poc-app-etl-prod-003",
        "poc-free-client-a-004",
        "poc-disabled-005",
        "mi-fiscalia-local",
        "mi-fiscalia-test",
        "mi-fiscalia-devel",
        "mi-fiscalia-staging",
        "mi-fiscalia-production",
    ):
        assert secret not in body


def test_models_401_for_unknown() -> None:
    c = _client()
    r = c.get("/v1/models", headers={"Authorization": "Bearer unknown-xyz"})
    assert r.status_code == 401


def test_blocked_unknown_recorded_as_anomaly() -> None:
    c = _client()
    labels = (
        "hash-" + token_hash_prefix("intruder-001"),
        "anomaly",
        "anomaly",
        "m",
        "/v1/chat/completions",
        "blocked",
    )
    before = gw.REQUESTS.labels(*labels)._value.get()
    r = c.post(
        "/v1/chat/completions",
        json={"model": "m", "messages": []},
        headers={"Authorization": "Bearer intruder-001"},
    )
    assert r.status_code == 401
    assert gw.REQUESTS.labels(*labels)._value.get() == before + 1


def test_unknown_labels_fall_back_to_anomaly() -> None:
    assert gw.UNKNOWN_PROJECT == "anomaly"
    assert gw.ANONYMOUS_NAME == "anomaly"


def test_allow_unknown_keys_flag_passes_as_anomaly(monkeypatch: object) -> None:
    monkeypatch.setattr(gw, "ALLOW_UNKNOWN_KEYS", True)

    usage = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}

    class _FakeResp:
        ok = True
        status_code = 200
        headers = {"content-type": "application/json"}
        content = b'{"usage": {"prompt_tokens": 1}}'

        def json(self) -> dict[str, object]:
            return {"usage": dict(usage)}

    def _ok(
        method: str, path: str, body: dict[str, object] | None, headers: dict[str, str]
    ) -> object:
        return _FakeResp()

    monkeypatch.setattr(gw, "_forward", _ok)  # type: ignore[attr-defined]
    labels = (
        "hash-" + token_hash_prefix("intruder-002"),
        "anomaly",
        "anomaly",
        "m",
        "/v1/chat/completions",
        "ok",
    )
    before = gw.REQUESTS.labels(*labels)._value.get()
    r = _client().post(
        "/v1/chat/completions",
        json={"model": "m", "messages": []},
        headers={"Authorization": "Bearer intruder-002"},
    )
    assert r.status_code == 200
    assert gw.REQUESTS.labels(*labels)._value.get() == before + 1
