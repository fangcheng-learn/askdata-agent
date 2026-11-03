from __future__ import annotations

import secrets
import hashlib
import hmac
import time
import re
from dataclasses import dataclass

from . import public_demo


@dataclass(frozen=True)
class AuthUser:
    user_id: str
    username: str
    display_name: str
    role: str

    def public(self) -> dict[str, str]:
        return {
            "user_id": self.user_id,
            "username": self.username,
            "display_name": self.display_name,
            "role": self.role,
        }


class AuthService:
    """进程内账号认证；令牌在服务重启后失效。"""

    ACCOUNTS = {
        "admin": {
            "password": "admin123",
            "user": AuthUser("demo_admin", "admin", "系统管理员", "admin"),
        },
        "sales": {
            "password": "sales123",
            "user": AuthUser("demo_current_sales", "sales", "销售分析员", "current_sales"),
        },
        "mock": {
            "password": "mock123",
            "user": AuthUser("demo_analyst", "mock", "Mock 数据分析员", "analyst"),
        },
    }

    def __init__(self) -> None:
        self._tokens: dict[str, AuthUser] = {}

    def login(self, username: str, password: str) -> tuple[str, AuthUser] | None:
        if public_demo.enabled():
            if username.strip() != "admin" or not public_demo.verify_admin_password(password):
                return None
            user = AuthUser(public_demo.ADMIN_USER_ID, "admin", "管理员", "admin")
            # Cloud administrators use the same public dataset, with their own history.
            self._register_guest(user)
            token = secrets.token_urlsafe(32)
            self._tokens[token] = user
            return token, user
        account = self.ACCOUNTS.get(username.strip())
        if not account or not secrets.compare_digest(str(account["password"]), password):
            return None
        token = secrets.token_urlsafe(32)
        user = account["user"]
        self._tokens[token] = user
        return token, user

    def guest_login(self, existing: str | None = None) -> tuple[str, AuthUser]:
        if not public_demo.enabled():
            raise PermissionError("访客试用未启用")
        user = self.authenticate("Bearer " + existing) if existing else None
        visitor_id = user.user_id if user and user.role == "visitor" else "visitor_" + secrets.token_hex(16)
        # Persist identity across restarts without persisting credentials in the image.
        expires = int(time.time()) + 30 * 86400
        body = f"{visitor_id}.{expires}"
        signature = hmac.new(public_demo.signing_key(), body.encode(), hashlib.sha256).hexdigest()
        user = AuthUser(visitor_id, visitor_id, "试用访客", "visitor")
        self._register_guest(user)
        return body + "." + signature, user

    @staticmethod
    def _register_guest(user: AuthUser) -> None:
        from .access_control import AccessController
        AccessController.USER_ROLES[user.user_id] = ("visitor",)

    def authenticate(self, authorization: str | None) -> AuthUser | None:
        if not authorization:
            return None
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token:
            return None
        if public_demo.enabled():
            if token in self._tokens:
                return self._tokens[token]
            parts = token.split(".")
            if len(parts) != 3 or not re.fullmatch(r"visitor_[0-9a-f]{32}", parts[0]):
                return None
            visitor_id, expiry, signature = parts
            if not re.fullmatch(r"[0-9]{1,12}", expiry) or int(expiry) <= time.time():
                return None
            expected = hmac.new(public_demo.signing_key(),
                                (visitor_id + "." + expiry).encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected, signature):
                return None
            user = AuthUser(visitor_id, visitor_id, "试用访客", "visitor")
            self._register_guest(user)
            return user
        return self._tokens.get(token)

    def logout(self, authorization: str | None) -> None:
        if not authorization:
            return
        _, _, token = authorization.partition(" ")
        self._tokens.pop(token, None)
