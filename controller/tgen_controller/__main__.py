# SPDX-License-Identifier: MIT
"""tgen-controller: serve | set-password.

  python3 -m tgen_controller serve [--host 127.0.0.1] [--port 8080]
  python3 -m tgen_controller set-password        # asks twice, writes the scrypt hash to controller.toml
"""
from __future__ import annotations

import argparse
import getpass
import os
import re
import sys
from pathlib import Path

from . import __version__
from .auth import hash_password


def set_password(config: Path) -> int:
    pw = os.environ.get("TGEN_ADMIN_PASSWORD") or getpass.getpass("new admin password: ")
    if not os.environ.get("TGEN_ADMIN_PASSWORD") and pw != getpass.getpass("again: "):
        print("passwords differ", file=sys.stderr)
        return 1
    if len(pw) < 10:
        print("use at least 10 characters", file=sys.stderr)
        return 1
    line = f'password_hash = "{hash_password(pw)}"'
    text = config.read_text() if config.exists() else ""
    if re.search(r"(?m)^password_hash\s*=.*$", text):
        text = re.sub(r"(?m)^password_hash\s*=.*$", lambda _: line, text)
    else:
        text = line + "\n" + text
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(text)
    config.chmod(0o640)
    print(f"password set in {config}; restart tgen-controller to apply")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="tgen-controller", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=__version__)
    ap.add_argument("--config", default=os.environ.get("TGEN_CONTROLLER_CONFIG", "/etc/tgen/controller.toml"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8080)
    sub.add_parser("set-password")
    a = ap.parse_args()
    if a.cmd == "set-password":
        return set_password(Path(a.config))
    import uvicorn

    from .app import create_app
    from .settings import load
    cfg = load(a.config)
    if not cfg.password_hash:
        print("warning: no admin password set (tgen-controller set-password); logins will fail", file=sys.stderr)
    uvicorn.run(create_app(cfg), host=a.host, port=a.port, log_level="info", proxy_headers=True,
                forwarded_allow_ips="127.0.0.1", ws_ping_interval=20)
    return 0


if __name__ == "__main__":
    sys.exit(main())
