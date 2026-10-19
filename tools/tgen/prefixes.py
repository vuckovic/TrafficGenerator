# SPDX-License-Identifier: MIT
"""Prefix and ASN generation for emulated routers (reproducible from a seed)."""
from __future__ import annotations

import ipaddress
import random
from typing import Iterable

DEFAULT_POOL = ["10.0.0.0/8"]
DEFAULT_EXCLUDE = ["10.100.0.0/16"]          # traffic subnet (spec §18)
PRIVATE_ASN32 = (4200000000, 4294967294)


def pool_capacity(pool: Iterable[str], lmin: int, lmax: int) -> int:
    """Upper bound of distinct prefixes the pool can supply (exclusions ignored)."""
    total = 0
    for p in pool:
        net = ipaddress.ip_network(p, strict=False)
        for length in range(max(lmin, net.prefixlen), lmax + 1):
            total += 2 ** (length - net.prefixlen)
    return total


def generate_prefixes(count: int, seed: int, pool: Iterable[str] = DEFAULT_POOL,
                      lmin: int = 24, lmax: int = 28,
                      exclude: Iterable[str] = DEFAULT_EXCLUDE, taken: set | None = None) -> list[str]:
    """`count` distinct random prefixes from `pool`, lengths lmin..lmax, not inside
    any `exclude` network and not already in `taken` (which is updated)."""
    pool_nets = [ipaddress.ip_network(p, strict=False) for p in pool]
    excl = [ipaddress.ip_network(e, strict=False) for e in exclude]
    cap = pool_capacity(pool, lmin, lmax)
    taken = taken if taken is not None else set()
    if count + len(taken) > cap * 0.5:
        raise ValueError(f"pool too small: {count} prefixes requested, pool holds about {cap} "
                         f"at /{lmin}-/{lmax}; use a larger pool or a wider length range")
    rnd = random.Random(seed)
    out: list[str] = []
    while len(out) < count:
        net = rnd.choice(pool_nets)
        length = rnd.randint(max(lmin, net.prefixlen), lmax)
        host_bits = 32 - length
        span = 2 ** (length - net.prefixlen)
        addr = int(net.network_address) + (rnd.randrange(span) << host_bits)
        key = (addr, length)
        if key in taken:
            continue
        cand = ipaddress.ip_network((addr, length))
        if any(cand.subnet_of(e) or e.subnet_of(cand) for e in excl):
            continue
        taken.add(key)
        out.append(str(cand))
    return out


def generate_asns(count: int, seed: int, lo: int = PRIVATE_ASN32[0], hi: int = PRIVATE_ASN32[1],
                  exclude: Iterable[int] = ()) -> list[int]:
    rnd = random.Random(seed ^ 0xA5A5)
    ex = set(exclude)
    if hi - lo + 1 - len(ex) < count:
        raise ValueError("ASN range too small")
    out: list[int] = []
    seen: set[int] = set()
    while len(out) < count:
        a = rnd.randint(lo, hi)
        if a in ex or a in seen:
            continue
        seen.add(a)
        out.append(a)
    return out


def key_to_prefix(key: int) -> str:
    """Inverse of the agent's Prefix::key(): (addr << 8) | len."""
    return f"{ipaddress.ip_address(key >> 8)}/{key & 0xFF}"
