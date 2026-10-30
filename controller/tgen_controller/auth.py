# SPDX-License-Identifier: MIT
"""One admin password (decision D2): scrypt hash, signed session cookie, login lock-out."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from collections import defaultdict, deque

COOKIE = "tgen_session"
N, R, P = 2 ** 15, 8, 1


def hash_password(pw: str) -> str:
    salt = os.urandom(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=N, r=R, p=P, maxmem=64 * 1024 * 1024)
    return f"scrypt${N}${R}${P}${salt.hex()}${h.hex()}"


def check_password(pw: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, h = stored.split("$")
        if algo != "scrypt":
            return False
        got = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p),
                             maxmem=64 * 1024 * 1024)
        return hmac.compare_digest(got.hex(), h)
    except (ValueError, TypeError):
        return False


def make_token(secret: bytes, hours: float) -> str:
    body = base64.urlsafe_b64encode(json.dumps({"exp": time.time() + hours * 3600}).encode()).decode()
    sig = hmac.new(secret, body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def valid_token(secret: bytes, token: str | None) -> bool:
    if not token or "." not in token:
        return False
    body, sig = token.rsplit(".", 1)
    if not hmac.compare_digest(hmac.new(secret, body.encode(), hashlib.sha256).hexdigest(), sig):
        return False
    try:
        return json.loads(base64.urlsafe_b64decode(body))["exp"] > time.time()
    except (ValueError, KeyError):
        return False


class Lockout:
    """5 failed logins from one address within 15 min block it for 15 min."""

    def __init__(self, max_failures: int = 5, window_s: float = 900, block_s: float = 900):
        self.max, self.window, self.block = max_failures, window_s, block_s
        self.fails: dict[str, deque] = defaultdict(deque)
        self.blocked: dict[str, float] = {}

    def is_blocked(self, addr: str) -> float:
        until = self.blocked.get(addr, 0)
        return max(0.0, until - time.time())

    def failure(self, addr: str) -> None:
        now = time.time()
        q = self.fails[addr]
        q.append(now)
        while q and q[0] < now - self.window:
            q.popleft()
        if len(q) >= self.max:
            self.blocked[addr] = now + self.block
            q.clear()

    def success(self, addr: str) -> None:
        self.fails.pop(addr, None)
