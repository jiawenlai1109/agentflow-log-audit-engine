"""极简 Token 鉴权（本地单机；多用户时替换为正式认证）。"""

from __future__ import annotations

import hashlib
import hmac

from app.config import SECRET


def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def make_token(username: str) -> str:
    return hmac.new(SECRET.encode("utf-8"), username.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_token(token: str, username: str) -> bool:
    return hmac.compare_digest(token, make_token(username))
