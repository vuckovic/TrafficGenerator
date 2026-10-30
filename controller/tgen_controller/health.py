# SPDX-License-Identifier: MIT
"""AC6 self-measurement (spec §10.2, docs/08 §6): API response time, live delivery delay,
event-loop lag, controller CPU and free memory."""
from __future__ import annotations

import asyncio
import time
from collections import deque

API_P95_MS, LIVE_DELAY_MS, LOOP_LAG_MS = 500.0, 2000.0, 2000.0
MEM_RESERVE_MB = 4800.0


def mem_available_mb() -> float | None:
    try:
        for line in open("/proc/meminfo"):
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) / 1024
    except OSError:
        pass
    return None


class Health:
    def __init__(self, mem_reserve_mb: float = MEM_RESERVE_MB):
        self.req: deque = deque()            # (t, ms) of API requests, last 60 s
        self.ws_delay_ms = 0.0
        self.loop_lag_ms = 0.0
        self.cpu_pct = 0.0
        self.mem_reserve_mb = mem_reserve_mb
        self.run_max = self._zero()
        self._cpu_prev = (time.monotonic(), time.process_time())

    @staticmethod
    def _zero() -> dict:
        return {"api_p95_ms": 0.0, "ws_delay_ms": 0.0, "loop_lag_ms": 0.0, "cpu_pct": 0.0}

    def reset_run(self) -> None:
        self.run_max = self._zero()

    def request(self, ms: float) -> None:
        now = time.monotonic()
        self.req.append((now, ms))
        while self.req and self.req[0][0] < now - 60:
            self.req.popleft()

    def api_p95(self) -> float:
        v = sorted(ms for _, ms in self.req)
        return v[int(0.95 * (len(v) - 1))] if v else 0.0

    def ws_sent(self, delay_ms: float) -> None:
        self.ws_delay_ms = delay_ms
        self._max("ws_delay_ms", delay_ms)

    def _max(self, k: str, v: float) -> None:
        self.run_max[k] = max(self.run_max[k], round(v, 1))

    async def loop_monitor(self) -> None:
        """100 ms timer drift = event-loop lag; also samples controller CPU once a second."""
        n = 0
        while True:
            t0 = time.monotonic()
            await asyncio.sleep(0.1)
            self.loop_lag_ms = max(0.0, (time.monotonic() - t0 - 0.1) * 1000)
            self._max("loop_lag_ms", self.loop_lag_ms)
            n += 1
            if n % 10 == 0:
                w, c = time.monotonic(), time.process_time()
                pw, pc = self._cpu_prev
                self.cpu_pct = 100 * (c - pc) / max(1e-6, w - pw)
                self._cpu_prev = (w, c)
                self._max("cpu_pct", self.cpu_pct)
                self._max("api_p95_ms", self.api_p95())

    def snapshot(self) -> dict:
        mem = mem_available_mb()
        return {"api_p95_ms": round(self.api_p95(), 1), "ws_delay_ms": round(self.ws_delay_ms, 1),
                "loop_lag_ms": round(self.loop_lag_ms, 1), "cpu_pct": round(self.cpu_pct, 1),
                "mem_available_mb": round(mem) if mem is not None else None}

    def violations(self) -> list[str]:
        out = []
        if self.api_p95() > API_P95_MS:
            out.append(f"API p95 {self.api_p95():.0f} ms > {API_P95_MS:.0f} ms")
        if self.ws_delay_ms > LIVE_DELAY_MS:
            out.append(f"live delay {self.ws_delay_ms:.0f} ms > {LIVE_DELAY_MS:.0f} ms")
        if self.loop_lag_ms > LOOP_LAG_MS:
            out.append(f"controller starved: loop lag {self.loop_lag_ms:.0f} ms")
        mem = mem_available_mb()
        if mem is not None and self.mem_reserve_mb and mem < self.mem_reserve_mb:
            out.append(f"free memory {mem:.0f} MB < reservation {self.mem_reserve_mb:.0f} MB")
        return out
