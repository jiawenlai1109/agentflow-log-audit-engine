"""鉴权底座：口令哈希 + 带过期与作用域的签名 token。

口令用 PBKDF2-HMAC-SHA256 + 每用户随机盐（比对走常数时间）；token 是自包含的
签名串（sub/scope/exp/jti），不落地存储，因此 scope 隔离必须靠验签时强制判定：
API token 打不开产物目录，media token 也调不动业务接口。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time

logger = logging.getLogger("agentflow.auth")

SCOPE_API = "api"
SCOPE_MEDIA = "media"

PBKDF2_ITERATIONS = 240_000
PBKDF2_ALGORITHM = "sha256"
API_TOKEN_TTL_SECONDS = int(os.getenv("APP_TOKEN_TTL_SECONDS", "43200"))
MEDIA_TOKEN_TTL_SECONDS = int(os.getenv("APP_MEDIA_TOKEN_TTL_SECONDS", "900"))


def _load_secret() -> bytes:
    configured = os.getenv("APP_SECRET", "").strip()
    if configured:
        return configured.encode("utf-8")
    # 未配置时退化为进程内随机密钥：token 随重启失效，比固定弱密钥安全，故只告警不阻断
    logger.warning("APP_SECRET 未配置，使用进程内随机密钥（重启后所有 token 失效）")
    return secrets.token_bytes(32)


SECRET = _load_secret()


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


# ---------------------------------------------------------------- 口令


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(PBKDF2_ALGORITHM, password.encode(), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_{PBKDF2_ALGORITHM}${PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, iterations, salt_hex, digest_hex = stored.split("$")
        if algorithm != f"pbkdf2_{PBKDF2_ALGORITHM}":
            return False
        expected = bytes.fromhex(digest_hex)
        actual = hashlib.pbkdf2_hmac(
            PBKDF2_ALGORITHM, password.encode(), bytes.fromhex(salt_hex), int(iterations)
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


# ---------------------------------------------------------------- token


def make_token(
    username: str,
    scope: str = SCOPE_API,
    ttl: int | None = None,
    run_scope: str | None = None,
) -> str:
    """签发 compact 签名 token。run_scope 用于媒体 token 限定只能读某次 run 的产物。"""
    lifetime = API_TOKEN_TTL_SECONDS if ttl is None else ttl
    now = int(time.time())
    payload = {
        "sub": username,
        "scope": scope,
        "iat": now,
        "exp": now + lifetime,
        "jti": secrets.token_hex(8),
    }
    if run_scope:
        payload["run"] = run_scope
    body = _b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    return f"{body}.{_b64encode(_sign(body))}"


def _sign(body: str) -> bytes:
    return hmac.new(SECRET, body.encode("ascii"), hashlib.sha256).digest()


def decode_token(
    token: str, expected_scope: str | None = SCOPE_API
) -> dict[str, object] | None:
    """验签 + 过期 + 作用域三重校验；任一不过返回 None（调用方转 401）。"""
    if not token or token.count(".") != 1:
        return None
    body, signature = token.split(".")
    try:
        if not hmac.compare_digest(_sign(body), _b64decode(signature)):
            return None
        payload = json.loads(_b64decode(body))
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if expected_scope is not None and payload.get("scope") != expected_scope:
        return None
    exp = payload.get("exp")
    if not isinstance(exp, (int, float)) or exp <= time.time():
        return None
    if not isinstance(payload.get("sub"), str) or not payload["sub"]:
        return None
    return payload


def token_ttl(scope: str = SCOPE_API) -> int:
    return MEDIA_TOKEN_TTL_SECONDS if scope == SCOPE_MEDIA else API_TOKEN_TTL_SECONDS
