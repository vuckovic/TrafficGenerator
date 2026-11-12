#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""snmpd `pass` handler for the tests: a fake BGP4-MIB and CISCO-BGP4-MIB (cbgpPeer2) for the
peers listed in $TGEN_SNMP_PEERS (comma separated), all Established, prefixes and update counters
growing with time. Usage in snmpd.conf: pass .1.3.6.1.2.1.15 /path/bgp_pass.py (and .1.3.6.1.4.1.9.9.187)."""
import os
import sys
import time


def ip_key(ip):
    return tuple(int(x) for x in ip.split("."))


def table():
    peers = sorted((p for p in os.environ.get("TGEN_SNMP_PEERS", "10.100.0.20,10.100.0.21").split(",") if p), key=ip_key)
    k = int(time.time()) % 100000
    rows = []
    for col, typ, val in ((2, "integer", lambda i, ip: 6), (10, "counter", lambda i, ip: 100 * k + i),
                          (11, "counter", lambda i, ip: 50 * k + i)):
        for i, ip in enumerate(peers):
            rows.append(((1, 3, 6, 1, 2, 1, 15, 3, 1, col) + ip_key(ip), typ, val(i, ip)))
    for i, ip in enumerate(peers):
        rows.append(((1, 3, 6, 1, 4, 1, 9, 9, 187, 1, 2, 8, 1, 1, 1, 4) + ip_key(ip) + (1, 1), "gauge", 500 + i))
    return sorted(rows)


def main():
    mode, oid = sys.argv[1], tuple(int(x) for x in sys.argv[2].strip(".").split("."))
    rows = table()
    hit = None
    if mode == "-g":
        hit = next((r for r in rows if r[0] == oid), None)
    elif mode == "-n":
        hit = next((r for r in rows if r[0] > oid), None)
    if hit:
        print("." + ".".join(map(str, hit[0])))
        print(hit[1])
        print(hit[2])


if __name__ == "__main__":
    main()
