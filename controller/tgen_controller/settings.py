# SPDX-License-Identifier: MIT
"""Controller settings: /etc/tgen/controller.toml (TGEN_CONTROLLER_CONFIG) + cluster.env + DUT inventory."""
from __future__ import annotations

import os
import secrets
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import REPO


@dataclass
class Node:
    name: str
    mgmt_ip: str
    traffic_ip: str
    agent: str            # host:port of the agent API
    weight: float = 1.0


@dataclass
class Settings:
    config_path: Path
    data_dir: Path
    password_hash: str
    secret: bytes
    nodes: list[Node]
    agent_token: str
    traffic_iface: str | None = None
    secure_cookie: bool = True
    session_hours: float = 12.0
    controller_node: str = "CMNODE01"
    reserved_ips: list[str] = field(default_factory=list)
    max_duration_s: float = 3600.0
    clock_tolerance_ms: float = 50.0
    raw: dict = field(default_factory=dict)

    @property
    def runs_dir(self) -> Path:
        return self.data_dir / "runs"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "controller.db"


def load(path: str | os.PathLike | None = None) -> Settings:
    path = Path(path or os.environ.get("TGEN_CONTROLLER_CONFIG", "/etc/tgen/controller.toml"))
    raw = tomllib.loads(path.read_text()) if path.exists() else {}
    data_dir = Path(raw.get("data_dir") or os.environ.get("TGEN_DATA_DIR", "/var/lib/tgen"))
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "runs").mkdir(exist_ok=True)

    # session signing key: generated once, kept next to the database
    key_file = Path(raw.get("secret_file") or data_dir / "session.key")
    if not key_file.exists():
        key_file.write_bytes(secrets.token_bytes(32))
        key_file.chmod(0o600)

    from tgen_p2 import load_env  # cluster.env parser (bash-sourced, as in the CLI tools)
    env = load_env(Path(raw.get("cluster_env") or REPO / "deploy" / "cluster.env"))
    names = env["NODE_HOSTNAMES"]
    mbase, tbase = int(env["MGMT_HOST_BASE"]), int(env["TRAFFIC_HOST_BASE"])
    port = int(env.get("AGENT_PORT") or 7700)
    overrides = raw.get("agents", {})
    ctl = raw.get("controller_node", names[0])
    nodes = [Node(n, f'{env["MGMT_PREFIX"]}.{mbase + i + 1}', f'{env["TRAFFIC_PREFIX"]}.{tbase + i + 1}',
                  overrides.get(n, f'{env["MGMT_PREFIX"]}.{mbase + i + 1}:{port}'),
                  0.7 if n == ctl else 1.0) for i, n in enumerate(names)]
    token_file = Path(raw.get("agent_token_file") or env.get("AGENT_TOKEN_FILE") or "/etc/tgen/agent.token")
    token = token_file.read_text().strip() if token_file.exists() else ""
    return Settings(config_path=path, data_dir=data_dir, password_hash=raw.get("password_hash", ""),
                    secret=key_file.read_bytes(), nodes=nodes, agent_token=token,
                    traffic_iface=raw.get("traffic_iface"), secure_cookie=raw.get("secure_cookie", True),
                    session_hours=raw.get("session_hours", 12.0), controller_node=ctl,
                    reserved_ips=env.get("TRAFFIC_RESERVED", []),
                    max_duration_s=raw.get("max_duration_s", 3600.0),
                    clock_tolerance_ms=raw.get("clock_tolerance_ms", 50.0), raw=raw)
