# 07 — Agent service API (P3a)

The agent runs as a permanent service on every node, `tgen-agent serve`. The controller on CMNODE01 drives it over the **management network**. The one-shot CLI mode (`tgen-agent --plan … --out …`, used by `tgen_p2.py`) stays available.

```
tgen-agent serve --listen 192.168.100.12:7700 --token-file /etc/tgen/agent.token [--iface eth0]
```

**Conventions:**
- Every request carries `Authorization: Bearer <token>` (the shared token); without it the agent answers `401`.
- Bodies are JSON.
- Times are **absolute, in ms since the Unix epoch**. Node clocks agree within 1 ms (chrony), so "at" means the same moment on every node.
- One run at a time.

## Run states

`idle` → `loaded` (plan accepted, router addresses configured) → `running` (first phase started) → `finished` (ended, stopped, or controller lost) → `idle` (after `DELETE /v1/run`).

## Endpoints

| Method and path | Purpose | Answer |
|---|---|---|
| `GET /v1/status` | health, version, node, state, run id, clock | `200 {version, node, state, run_id, now_ms, routers, sessions: {Established: n, …}}` |
| `PUT /v1/run` | load a plan (same JSON as `plan-<node>.json` today, plus `iface` and `seed`); adds the routers' /32 addresses on `iface` | `200 {routers}` · `409` a run is loaded · `400 {error}` invalid plan |
| `POST /v1/run/phase` | schedule a phase at an absolute time (see below); may be called many times | `202 {id}` · `409` not loaded/finished |
| `GET /v1/run/stats` | live counters, plus the latency samples recorded since the previous call | `200 {seq, t_ms, totals, by_state, per_router[], samples{…}}` |
| `POST /v1/run/stop` | stop now: Cease to the DUT, close all sessions | `200` |
| `GET /v1/run/result` | full result (same shape as `result.json` today, plus churn and flap data) | `200` · `409` not finished |
| `DELETE /v1/run` | stop if needed, remove the router addresses, free memory | `200` |

**Watchdog:** while a run is `running`, every authenticated request counts as a heartbeat. If none arrives for **5 s** (`controller_timeout_s` in the plan), the agent stops the run by itself (spec §10, item 4). The controller polls `stats` every second anyway.

## Phases (`POST /v1/run/phase`)

```json
{"type": "churn", "at_ms": 1792530000000, "params": {"rate": 50, "mix": {"announce": 40, "withdraw": 40, "attr": 20}}}
```

Each phase starts at `at_ms` on every router of the node. A new announce, churn or withdraw phase replaces the running one. Steps (for example churn 100 → 2 000 /s in steps) are separate phase calls from the controller, each with its own `at_ms`.

| Type | Params | What every router does |
|---|---|---|
| `establish` | `per_s` (stagger, 0 = all at once) | opens its BGP session; retries 1 s, 2 s, then every `connect_retry_s` |
| `announce` | `rate` (prefixes/s, 0 = max) | sends its whole prefix set (initial load) |
| `steady` | — | nothing new; sessions and keepalives only (a marker for the timeline) |
| `churn` | `rate` (operations/s per router), `mix` {announce, withdraw, attr} % | random operations on its own prefixes: **withdraw** an announced prefix, **announce** a withdrawn one, or re‑announce one with a changed **attribute**. Operations in the same 10 ms tick and of the same kind share an UPDATE, up to `per_update` |
| `flap` | `routers` [names], `kind`: `reset` · `silent` · `withdraw_all`, `down_s` | only the named routers:<br>• **reset** sends NOTIFICATION Cease (6/4), closes, reconnects after `down_s` and re‑announces;<br>• **silent** sends nothing at all (no keepalives, UPDATEs or churn, since any message resets the hold timer), so the DUT's hold timer expires;<br>• **withdraw_all** withdraws everything and re‑announces after `down_s`.<br>Resets in a flap are counted as **planned**, not unexpected |
| `withdraw` | `rate` (0 = max) | withdraws all its announced prefixes |
| `teardown` | `rate`, `close` (bool, default false) | withdraws everything. Closing is normally left to `end`, scheduled a grace period later, so every router first receives everyone's withdrawals. `close: true` closes right after its own withdrawals |
| `end` | — | stops: Cease, close, state `finished` |

**Generation tag (churn latency):** each churn announcement carries one large community `(router ASN, 0x7467, generation)`, numbered per UPDATE. Receivers record (prefix, generation, arrival), so latency is joined per announcement, not only per prefix.
- The initial load carries no tag, so P2 behaviour is unchanged.
- One tag per UPDATE, not per prefix, so the DUT can still pack the prefixes of one UPDATE together.

## Live statistics (`GET /v1/run/stats`)

```json
{"seq": 1842, "t_ms": 1792530001000,
 "by_state": {"Established": 10},
 "totals": {"prefixes_sent": 100000, "prefixes_withdrawn": 0, "updates_sent": 1000, "nlri_rx": 590000,
            "withdrawn_rx": 0, "routes_held": 590000, "converged": 10, "resets": 0, "planned_resets": 0,
            "looped_rx": 0, "send_lag_p99_ms": 0.4},
 "per_router": [{"name": "R01", "state": "Established", "held": 59000, "sent": 10000, "rx": 59000, "resets": 0}],
 "samples": {"tx": [[key, gen, kind, t_ns]], "rx": [[key, gen, kind, t_ns]]}}
```

- Counters are cumulative; the controller derives rates from the difference between polls.
- `samples` are the latency samples recorded since the previous `stats` call (each call hands them over once), for prefixes with `hash % live_sample_mod == 0`. `kind`: 1 announce, 2 withdraw, 3 attribute change; `gen` 0 = initial load. The default is `live_sample_mod` = 1 000, about 0.1 % of prefixes, a few kB/s per node.
- The full sample set (`sample_mod`, default 100) is in the final result.

## Security
- The API listens on the management address only, never on the traffic network.
- The shared token lives in `/etc/tgen/agent.token`, mode 0600, created by the installer.
- The service user has only `CAP_NET_ADMIN`, needed to add and remove the routers' /32 addresses.

## Tests
- `tests/agent/test_serve.py` (root, BIRD): six agents on one box against BIRD on loopback. It drives establish → initial load → steady → churn (40 ops/s, mix 40/40/20) → flap (reset, silent, withdraw_all on three routers) → teardown → end, and checks:
  - live stats and latency join;
  - churn latency per (prefix, generation);
  - planned versus unexpected resets;
  - address cleanup, the watchdog, and token / plan validation.
- Unit tests: plan without `timing`, interface‑name validation, phase parsing (`agent/tests/test_plan.cpp`).
- The CLI mode used by `tools/tgen_p2.py` is unchanged; the simulation regression passes.

## Implementation notes
- `Run` (`agent/src/run.cpp`) owns a node's plan, io threads and routers. Phases, stats, results and stop are posted to each router's thread; nothing is shared between threads.
- The final NOTIFICATION is written non‑blocking, so an overloaded DUT that has stopped reading cannot block a router thread.
- HTTP: cpp‑httplib 0.18.3, single header vendored in `agent/third_party/httplib/` (MIT). The agent binary stays self‑contained (only libc).
- Reconnect after a failure: 1 s, 2 s, then every `connect_retry_s`. Before, it was a flat 5 s, which likely explains the 11 s session setup p90 in the cluster P2 run, where BIRD refused the first attempts.
