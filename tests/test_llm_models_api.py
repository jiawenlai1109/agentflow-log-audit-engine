"""G2b：型号可用性进 Web 的那条只读接口。

要守的三件事，重要性从高到低：

1. **一次真实调用都不许发**。这个接口挂在页面上，页面会被刷新、会被预取。
   一条 GET 就烧钱正是缺陷 #14（`mode` 未校验）的形状，所以这里用"urlopen 一旦
   被调用就直接抛"来断，而不是靠读代码相信它没有。
2. **没测过不能说可用**。`unprobed` 与 `usable` 必须是两个值：把没数据说成放行，
   等于前端替用户做了一个没有实测背书的决定。缓存属于别的主机、或来自旧版探针，
   同样算没测过。
3. **凭据不出后端**。响应里任何字段都不许出现 key 原文。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.db import execute
from app.main import app
from app.security import hash_password

SECRET = "sk-secret-should-never-leave-the-backend"
HOST = "gateway.test"


def _login(client: TestClient, username: str) -> None:
    execute(
        "INSERT OR IGNORE INTO users (username, password_hash, role) VALUES (?, ?, 'user')",
        (username, hash_password("pw")),
    )
    response = client.post("/api/auth/login", json={"username": username, "password": "pw"})
    assert response.status_code == 200, response.text
    client.headers["Authorization"] = f"Bearer {response.json()['token']}"


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    """已登录的客户端：接口本身要求鉴权，每条用例都从登录态开始测。"""
    from app.db import init_db

    init_db()
    with TestClient(app) as test_client:
        _login(test_client, "llm-user")
        yield test_client


def _anonymous() -> TestClient:
    return TestClient(app)


def _report(models: list[dict[str, Any]], *, host: str = HOST, version: int = 1) -> dict[str, Any]:
    import time

    return {
        "probe_version": version,
        # 用"刚刚生成"而不是写死一个时间戳：新鲜度判据是相对的，写死了会在某天悄悄变红
        "generated_at": int(time.time()) - 60,
        "generated_at_text": "2026-10-07 00:00:00",
        "base_host": host,
        "models": models,
    }


def _wire(
    monkeypatch,
    tmp_path: Path,
    report: dict[str, Any] | None,
    *,
    model: str = "work-model",
    host: str = HOST,
) -> Path:
    """把接口的两个输入接出来：配置指向哪个缓存文件、这台端点叫什么。"""
    path = tmp_path / "llm_preflight.json"
    if report is not None:
        path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    config = {
        "llm": {
            "model": model,
            "base_url": f"https://{host}/v1",
            "preflight_cache": str(path),
        }
    }
    monkeypatch.setattr("app.routers.llm.load_config", lambda *a, **k: config)
    # 成本红线：任何一次真实请求都要当场炸出来，而不是"看起来没有"
    def _boom(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("只读接口不该发真实调用（一次 GET 就是一次计费）")

    monkeypatch.setattr("agentflow.core.llm.urllib.request.urlopen", _boom)
    return path


def test_the_endpoint_never_fires_a_real_call(monkeypatch, client, tmp_path):
    _wire(monkeypatch, tmp_path, _report([{"model": "work-model", "verdict": "usable", "note": "默认档可用"}]))
    response = client.get("/api/llm/models")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "probed"


def test_requires_login_before_leaking_the_capability_map():
    """型号清单 + 端点主机名合起来就是"这系统接的是哪家上游"，与 /api/packs 同级。"""
    response = _anonymous().get("/api/llm/models")
    assert response.status_code in (401, 403), response.status_code


def test_missing_cache_is_reported_as_unprobed_not_usable(monkeypatch, client, tmp_path):
    _wire(monkeypatch, tmp_path, None)
    payload = client.get("/api/llm/models").json()
    assert payload["status"] == "unprobed", payload
    assert payload["configured"]["verdict"] == "unprobed", payload
    # "未测"要有主语：现在配的是哪个型号、哪台端点，页面才知道该把这句话挂在哪
    assert payload["configured"]["model"] == "work-model", payload
    assert payload["base_host"] == HOST, payload
    assert payload["models"] == []
    assert "preflight" in payload["hint"], "没结论时要给出一条能行动的话，不能只说没有"


def test_cache_from_another_host_does_not_clear_this_one(monkeypatch, client, tmp_path):
    """换 endpoint 就是一次新的预检：别的站的结论不能拿来给这个站放行。"""
    _wire(
        monkeypatch,
        tmp_path,
        _report([{"model": "work-model", "verdict": "usable", "note": "别台机器上能用"}], host="other.test"),
    )
    payload = client.get("/api/llm/models").json()
    assert payload["base_host"] == HOST and payload["status"] == "unprobed", payload
    assert payload["configured"]["verdict"] == "unprobed", payload


def test_stale_probe_version_is_not_a_conclusion(monkeypatch, client, tmp_path):
    """判据换过一版，旧结论就不能当新的用（`PROBE_VERSION` 存在的全部理由）。"""
    _wire(
        monkeypatch,
        tmp_path,
        _report([{"model": "work-model", "verdict": "usable", "note": "旧探针"}], version=999),
    )
    payload = client.get("/api/llm/models").json()
    assert payload["configured"]["verdict"] == "unprobed", payload
    assert payload["probe_version_current"] is False, payload


def test_verdicts_are_passed_through_verbatim(monkeypatch, client, tmp_path):
    """四档结论各有成因，页面要能分开画：可用 / 需关思考 / 别关思考 / 不可用。"""
    _wire(
        monkeypatch,
        tmp_path,
        _report(
            [
                {"model": "a-usable", "verdict": "usable", "note": "默认档就能出正文"},
                {"model": "b-disable", "verdict": "usable_if_thinking_disabled", "note": "要关思考"},
                {"model": "c-keep", "verdict": "do_not_disable_thinking", "note": "关了反而坏"},
                {"model": "d-dead", "verdict": "unusable", "note": "两档都不行"},
            ]
        ),
    )
    payload = client.get("/api/llm/models").json()
    by_model = {item["model"]: item["verdict"] for item in payload["models"]}
    assert by_model == {
        "a-usable": "usable",
        "b-disable": "usable_if_thinking_disabled",
        "c-keep": "do_not_disable_thinking",
        "d-dead": "unusable",
    }, by_model
    assert payload["status"] == "probed" and payload["fresh"] is True, payload


def test_the_key_never_appears_in_the_response(monkeypatch, client, tmp_path):
    """缓存里没有 key，接口也不许从环境里把它带出去。"""
    import os

    monkeypatch.setenv("OPENAI_API_KEY", SECRET)
    _wire(
        monkeypatch,
        tmp_path,
        _report(
            [
                {
                    "model": "work-model",
                    "verdict": "unusable",
                    "note": f"上游回显了请求头：Bearer {SECRET}",
                    "levels": {"default": {"error": "llm_error", "message": SECRET, "seconds": 1.0}},
                }
            ]
        ),
    )
    response = client.get("/api/llm/models")
    assert SECRET not in response.text, "预检备注里回显的凭据被转述出去了"
    assert os.getenv("OPENAI_API_KEY") == SECRET, "测试自己没把环境变量弄坏"


def test_response_shape_is_stable(monkeypatch, client, tmp_path):
    _wire(monkeypatch, tmp_path, _report([{"model": "work-model", "verdict": "usable", "note": ""}]))
    _login(client, "shape-user")
    payload = client.get("/api/llm/models").json()
    assert set(payload) == {
        "base_host",
        "status",
        "fresh",
        "checked_at",
        "probe_version",
        "probe_version_current",
        "cache_file",
        "configured",
        "thinking_config",
        "models",
        "hint",
    }, sorted(payload)
