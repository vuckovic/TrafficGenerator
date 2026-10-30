# SPDX-License-Identifier: MIT
"""Async client for the agent service API (docs/07). One instance per node."""
from __future__ import annotations

import asyncio

import httpx

from .settings import Node


class AgentError(RuntimeError):
    pass


class Agent:
    def __init__(self, node: Node, token: str, client: httpx.AsyncClient):
        self.node, self.token, self.c = node, token, client
        self.base = f"http://{node.agent}"

    async def _req(self, method: str, path: str, body=None, timeout: float = 5.0, ok=(200, 202)) -> dict:
        try:
            r = await self.c.request(method, self.base + path, json=body, timeout=timeout,
                                     headers={"Authorization": f"Bearer {self.token}"})
        except httpx.HTTPError as e:
            raise AgentError(f"{self.node.name}: {method} {path}: {e.__class__.__name__} {e}") from e
        try:
            data = r.json() if r.content else {}
        except ValueError:
            data = {"error": r.text[:200]}
        if r.status_code not in ok:
            raise AgentError(f"{self.node.name}: {method} {path} -> {r.status_code} {data.get('error', '')}")
        return data

    async def status(self) -> dict:
        return await self._req("GET", "/v1/status", timeout=3)

    async def load(self, plan: dict) -> dict:
        return await self._req("PUT", "/v1/run", plan, timeout=120)

    async def phase(self, typ: str, at_ms: int, params: dict | None = None) -> dict:
        return await self._req("POST", "/v1/run/phase", {"type": typ, "at_ms": at_ms, "params": params or {}})

    async def stats(self) -> dict:
        return await self._req("GET", "/v1/run/stats", timeout=3)

    async def stop(self) -> dict:
        return await self._req("POST", "/v1/run/stop", timeout=10)

    async def result(self) -> dict:
        return await self._req("GET", "/v1/run/result", timeout=120)

    async def delete(self) -> dict:
        return await self._req("DELETE", "/v1/run", timeout=60)


async def each(agents: list[Agent], fn, *args, return_exceptions: bool = False):
    """Run fn(agent, *args) on every agent concurrently; results in node order."""
    return await asyncio.gather(*(fn(a, *args) for a in agents), return_exceptions=return_exceptions)
