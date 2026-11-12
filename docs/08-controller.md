# 08 — Controller (P3b)

`tgen-controller` runs on CMNODE01. It replaces the command-line tools for the web interface: it plans a run, arms the agents, configures the DUT, drives the phases on one timeline, streams live data, collects the results, runs the analysis and keeps the history. The web interface (P3c) is a client of this API; scripts (`tgen-cli`) use the same API.

Spec references: architecture §3, scenario §5, controller §10, CMNODE01 reservation §10.2, AC6. Agent API: doc 07.

## 1. Process and files

- One Python process: FastAPI on uvicorn, asyncio. The scenario engine, agent polling and the live WebSocket share the event loop.
- Blocking work never runs on the event loop. DUT SSH work runs in a thread. Analysis runs in a separate process at `nice 10`, so the API stays fast (§10.2).
- Listens on **127.0.0.1:8080**. HTTPS for `bgp.elfak.net` is terminated by **Caddy**, which also obtains and renews the Let's Encrypt certificate (HTTP-01) and redirects HTTP to HTTPS.
- **Port forwarding** (decision 2026-11-01; public 443 stays with the router's own interface):
  - public **80 → CMNODE01:80**: the Let's Encrypt check, and the redirect;
  - public **8443 → CMNODE01:443**: the web interface, `https://bgp.elfak.net:8443`.
- **Data:**
  - `/var/lib/tgen/controller.db`: SQLite history (§5).
  - `/var/lib/tgen/runs/<run-id>/`: per-run files, in the same layout as a `tgen_p2.py` result folder (`plan-*.json`, `result-*.json`, `meta.json`, `report.json`, `summary.txt`, `dut-*`). The command-line tools (knee chart, series) also work on these folders.
- **Configuration:**
  - `deploy/cluster.env`: nodes, addresses, agent port and token file.
  - `deploy/duts.toml`: the DUT inventory.
  - `/etc/tgen/controller.toml`: admin password hash, session secret, limits.

## 2. Security

- **One admin password** (decision D2), stored as a scrypt hash. Set with `tgen-controller set-password`.
- **Login** returns a signed session cookie: HttpOnly, SameSite=Strict, Secure behind HTTPS, valid 12 h.
- **Lock-out:** 5 failed logins from one address within 15 min block that address for 15 min. This applies whether or not port 443 is opened to the internet.
- **API access:** every endpoint except `/api/health` and `/api/login` needs the session.
- **DUT passwords** are passed with the request that needs them (test, push, run). They are kept in memory for that run only, never written to disk or the database.
- **Agents:** reached on the management network with the shared token (`/etc/tgen/agent.token`, doc 07).

## 3. API

All bodies are JSON. Times are ms since the Unix epoch.

| Method and path | Purpose |
|---|---|
| `POST /api/login` `{password}` · `POST /api/logout` · `GET /api/me` | session |
| `GET /api/health` | version, uptime, AC6 figures (§6); no login needed |
| `GET /api/cluster` | per node: management/traffic address, agent state, version, run id, clock offset to the controller |
| `GET /api/presets` | scenario presets (§4.2) |
| `GET /api/duts` | device inventory (no secrets) |
| `POST /api/duts/{profile}/{action}` `{password?, routers?, role?}` | `test` · `preview` · `push` · `remove` · `status`, the same as `tgen_dut.py` (the UI's DUT buttons, U12–U16) |
| `POST /api/runs` `{scenario}` | validate, create and start a run → `202 {id}`; `409` if a run is active |
| `GET /api/runs?limit=` · `GET /api/runs/{id}` | history; one run with phases, events, summary, gate |
| `POST /api/runs/{id}/stop` | stop now: every agent sends Cease within 1 s (§10.1 item 4) |
| `GET /api/runs/{id}/report` · `GET /api/runs/{id}/files/{name}` | `report.json`; any file of the run folder |
| `DELETE /api/runs/{id}` | delete a finished run and its folder |
| `WS /ws/live` | live feed (§4.4) |
| `POST /api/schemes/plan` · `POST /api/schemes` · `GET /api/schemes` · `GET /api/schemes/{id}` · `POST /api/schemes/{id}/stop` · `GET /api/topology` | test scheme and network topology (P3c‑2, doc 09 §10) |
| `GET /api/runs/{id}/report.csv` · `PUT /api/runs/{id}/notes {notes}` · `POST /api/runs/{id}/regenerate` | report export, notes (X5), analysis again (X6) — P3c‑3 |
| `POST /api/password {current, new}` | change the admin password (S6); the hash is kept in the database, `tgen-controller set-password` resets it |
| `GET/PUT /api/settings/limits` | safety limits (S3): max routers (180), max prefixes in total (2 000 000), max duration (controller.toml, 3600 s), max data rate (1000 Mb/s, used from P6), DUT subnet allow‑list; `422` for a run above a limit |
| `POST /api/nodes/{name}/restart` | restart that node's agent (S1, agent 0.4.2); `409` during a run or scheme |

## 4. Scenario engine

### 4.1 Scenario

```json
{"name": "ASR-920 knee",
 "dut": {"profile": "asr920", "role": "router", "configure": true, "keep_config": false, "mrai": 0, "password": "…"},
 "routers": {"total": 60, "prefixes": 10000, "per_update": 10, "seed": 1},
 "phases": [
   {"type": "establish", "per_s": 0, "timeout_s": 60},
   {"type": "announce", "rate": 0, "timeout_s": 300},
   {"type": "steady", "duration_s": 60},
   {"type": "churn", "rate": 100, "mix": {"announce": 40, "withdraw": 40, "attr": 20},
    "step": {"add": 100, "every_s": 30, "max": 2000}, "duration_s": 600},
   {"type": "flap", "kind": "reset", "routers": 3, "down_s": 10, "duration_s": 60},
   {"type": "teardown", "rate": 0, "timeout_s": 120}],
 "limits": {"max_duration_s": 3600}}
```

- **`dut`:** either `profile` (from the inventory) or explicit `adapter`, `ip`, `ssh_host`, `user`, `asn`. Without a DUT adapter (`"configure": false`), the DUT must already be configured. `"bird"` = the stand-in on CMNODE06, started by the controller.
- **`routers`:** planned with `tools/tgen/planner.py`:
  - CMNODE01 at weight 0.7;
  - DUT, node and inventory addresses are skipped.

### 4.2 Timeline

Each phase is sent to every agent with an absolute start time 1 s ahead, so all nodes switch at the same moment.

| Phase | Next phase starts when |
|---|---|
| establish | every session is Established, or `timeout_s` |
| announce | every receiver has converged, or `timeout_s` |
| steady, churn, flap | `duration_s` has passed. Churn steps are separate churn phases, `every_s` apart. |
| withdraw, teardown | every receiver holds 0 routes, or `timeout_s` |
| end | sent 5 s after teardown (grace period, doc 07) |

**Presets (§5):**
- Quick check: 1–2–3–6.
- Initial convergence: 1–2–6.
- Throughput knee: 1–2–4 stepped–6.
- Resilience: 1–2–3–5–3–6.
- Full IXP day: all phases.

### 4.3 Run lifecycle

`created → preparing → running → finishing → done | failed | stopped`

1. **preparing:**
   - check that every agent answers and is idle, and that clocks agree within 50 ms (the agents report `now_ms`);
   - read the DUT's facts and port, apply the memory guard;
   - push the DUT configuration;
   - load the plans (`PUT /v1/run`; the agents add the router addresses and send gratuitous ARP).
2. **running:**
   - schedule the phases;
   - poll every agent's `stats` once a second;
   - poll the DUT's sessions and memory every 10 s (memory guard → stop);
   - stop at `max_duration_s`.
3. **finishing:**
   - fetch every agent's result;
   - read the DUT's log, port counters and resources, then remove its configuration (unless `keep_config`);
   - `DELETE /v1/run` on every agent;
   - analysis (`tools/tgen/analyze.py`) and gate;
   - write `report.json` and `summary.txt`, and record the summary in SQLite.
4. **Failure:** any failure in 1–3 still runs the cleanup: stop and delete on the agents, DUT configuration removal. The run ends `failed`, with the reason as an event.

One run at a time. A controller restart during a run marks the run `failed`. The agents' 5 s watchdog has already stopped them.

### 4.4 Live feed (`WS /ws/live`)

Once a second, one message:
```json
{"type": "tick", "t_ms": 1792530001000, "run": "r-20261030-221500", "state": "running", "phase": {"idx": 3, "type": "churn", "rate": 400},
 "sessions": {"Established": 60}, "routes_held": 590000, "rates": {"sent_s": 4000, "rx_s": 236000, "withdrawn_s": 0},
 "latency_ms": {"p50": 48, "p99": 312, "n": 412}, "bytes_rx_s": 1200000,
 "nodes": [{"node": "CMNODE01", "routers": 7, "cpu_pct": 38, "send_lag_p99_ms": 0.4}],
 "controller": {"api_p95_ms": 12, "ws_delay_ms": 3, "loop_lag_ms": 1, "cpu_pct": 4}}
```
- **Latency:** joined from the agents' live samples, as in the agent API.
- **Events** (phase start, reset, DUT warning, guard) are separate `{"type": "event"}` messages.
- **Storage:** every tick is also stored (`live.jsonl` and SQLite), so the report and history can redraw it.

## 5. Data model (SQLite)

| Table | Columns |
|---|---|
| `runs` | id, name, state, created/started/ended ms, scenario (json, without passwords), dut facts (json), summary (json: key figures + gate), error, dir |
| `phases` | run_id, idx, type, params (json), at_ms, end_ms |
| `events` | run_id, t_ms, kind, severity, text |
| `ticks` | run_id, t_ms, json |
| `schemes` | id, state, created_ms, data (json: plan, progress, results, comparison, knee series) — P3c‑2 |
| `settings` | key, value (json): `password_hash` (S6), `limits` (S3) — P3c‑3; `runs.notes` holds the report notes (X5) |

## 6. AC6 self-measurement (§10.2)

The controller measures itself continuously:
- **API response time:** every request, p95 over the last minute;
- **live delivery delay:** WebSocket send time minus the time of the data in the tick;
- **event-loop lag:** a 100 ms timer's drift;
- **controller CPU and free memory.**

**Thresholds:**
- API p95 < 500 ms;
- live delay < 2 s;
- loop lag < 2 s;
- free memory ≥ the 4.8 GB reservation.

**On a violation:**
- the engine **holds the scenario**: the current phase continues, but the next phase does not start until the controller is healthy again;
- an event and a warning go to the UI.

The run's maxima are stored with it, and shown in M6 and in the report header.

## 7. Agent changes for P3b

- `stats` also reports the node's CPU busy % since the previous call, and the agent's CPU seconds. This feeds F12 / AC3 and the live view.

## 8. Deployment

- **`deploy/systemd/tgen-controller.service`** (§10.2 settings): `CPUWeight=1000`, `MemoryMin=4.8G`, `OOMScoreAdjust=-500`, `Nice=-5`; user `tgen`.
- **`tgen-agent.service` on every node** (doc 07), installed by the Ansible playbook: user `tgen`, the shared token copied from CMNODE01, cgroup limits on CMNODE01.
- **Caddy:** `bgp.elfak.net { reverse_proxy 127.0.0.1:8080 }`.
- **Python packages:** Debian packages (`python3-fastapi`, `python3-uvicorn`, `python3-httpx`, `python3-pydantic`, `python3-websockets`, `python3-netmiko`); no pip on the nodes.

## 9. Tests

- **Unit:** scenario validation, timeline, auth and lock-out, AC6 bookkeeping.
- **End to end, one Linux box** (`tests/controller/test_e2e.py`, root):
  - six real agents on 127.0.0.1:7701–7706 and BIRD on loopback;
  - the controller runs a quick check and a churn/flap scenario through the API;
  - follows the WebSocket, stops a run, checks the report, the gate, cleanup and the AC6 figures.

### Status (2026-10-30)

- Controller 0.1.0 with agent 0.4.0. Unit tests: 12 pass. End to end on one box: all checks pass, in three runs:
  - quick check against BIRD, gate PASS;
  - stepped churn and a 2-router flap, then a user stop: agents stopped within 1 s, no unexpected resets;
  - a Cisco profile through the netmiko mock: config pushed with MRAI 0, polled, and removed afterwards; attribute checks INFO.
- AC6 maxima in the test: API p95 104 ms (the scrypt login), live delay 3 ms, loop lag 3 ms.
- **First cluster run 2026-11-03: PASS** (r-20261103-181240, ASR-920, 12 × 1000, MRAI 0): 12/12 Established, convergence 5.7 s, 0 missing, latency p50 2.98 s / p99 5.65 s, DUT output 26 052 prefixes/s; API p95 max 7.2 ms. The DUT's log read at the end clashed with the last poll on one SSH session; fixed (778a331).
- Deployment: `deploy/ansible/services.yml`, `deploy/systemd/tgen-controller.service`, `deploy/caddy/Caddyfile`, `deploy/controller.example.toml`.

**Additions to §3 found while building:**
- `GET /api/runs/{id}/ticks`: the stored live ticks, so the UI can redraw a finished run.
- `GET /api/runs/{id}/files`: list of the run folder (`routers.json` is written while preparing, for the routers table).
- **P3c‑1 (2026‑11‑03):**
  - `GET /api/i18n` (no login): `ui/i18n/labels.csv` as JSON.
  - `POST /api/dut/{action}` `{dut, routers}`: the DUT buttons for any device, a profile or a custom one (`/api/duts/{profile}/{action}` stays).
  - The scenario's `dut` block takes per‑run overrides: `ssh_port`, `max_routes`, `min_free_mem_pct`, `link_mbps`, `interface`, `ssh_legacy`, `sudo`, `bird_include`, and `enable` (Cisco enable secret, never stored). Inventory values are filled in; explicit fields win.
  - The live tick also carries `routers` (`[name, node, state, held, sent, rx]`, live only, not stored), `dut_port` (interface, speed, duplex, `mbps`, `use_pct`, `errors` since the start; DUT polls read the port counters too), `dut_load`, each node's `temp_c`, and the controller's health.
  - `/` serves the web interface (`ui/web`) and `/i18n/flags` the flags.
- **Fixes found by the browser test:**
  - The analysis worker is now spawned, not forked. A forked worker kept the listening port open after the controller stopped.
  - A new live client no longer sees the last tick of a finished run.

## 10. Not in P3b

- SNMP collectors and DUT log parsing (P5).
- Report figures and PDF (P4).
- The data plane (P6).
- Storing DUT credentials encrypted. For now they are passed per request.
