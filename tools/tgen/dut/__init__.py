# SPDX-License-Identifier: MIT
"""Device-under-test adapters: deploy / verify / remove the BGP neighbours of a
run over SSH. One class per platform, same interface (see base.Dut)."""
from __future__ import annotations

from .base import TAG, Dut, DutError
from .bird import Bird
from .cisco import Cisco
from .frr import Frr, Quagga
from .mikrotik import MikroTik

# --dut value -> (class, default SSH user)
ADAPTERS: dict[str, tuple[type[Dut], str]] = {
    "mikrotik": (MikroTik, "admin"),
    "cisco": (Cisco, "admin"),
    "bird-ssh": (Bird, "root"),
    "frr": (Frr, "root"),
    "quagga": (Quagga, "root"),
}


def make(kind: str, host: str, user: str | None = None, **kw) -> Dut:
    cls, default_user = ADAPTERS[kind]
    return cls(host, user or default_user, **kw)


__all__ = ["ADAPTERS", "TAG", "Dut", "DutError", "make", "Bird", "Cisco", "Frr", "Quagga", "MikroTik"]
