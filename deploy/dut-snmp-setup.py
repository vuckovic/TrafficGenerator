#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Switch on read-only SNMP v2c on the lab DUTs for U5 (docs/10 §6), from CMNODE01.

  cd ~/TrafficGenerator
  python3 deploy/dut-snmp-setup.py --dry-run            # show what would be sent
  python3 deploy/dut-snmp-setup.py                      # asr920, c1841, mikrotik (all Cisco/MikroTik profiles)
  python3 deploy/dut-snmp-setup.py c1841 --save         # one device, also write memory

Per device (profiles from deploy/duts.toml):
- Cisco IOS / IOS-XE: standard ACL TGEN-SNMP permitting only this host's address towards the
  device, `snmp-server community <c> RO TGEN-SNMP`, `snmp-server ifindex persist`.
  Running configuration only unless --save.
- MikroTik RouterOS 7: community limited to this host's address /32, read-only; SNMP on.
  RouterOS keeps the change (no separate save).
Then an SNMP get of sysName from here checks it.

The community is asked for (twice, not echoed) and never written to disk, the command line or
the output. Device passwords come from TGEN_DUT_PASSWORD_<PROFILE> or are asked for; the Cisco
enable secret from TGEN_DUT_ENABLE or asked for when needed.
"""
from __future__ import annotations

import argparse
import getpass
import os
import re
import socket
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))
from tgen import inventory, snmp  # noqa: E402

ACL = "TGEN-SNMP"


def source_ip(dest: str) -> str:
    """The address this host uses towards dest (what the device's ACL must allow): a UDP
    "connect" only asks the kernel for the route, nothing is sent."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as so:
        so.connect((dest, 161))
        return so.getsockname()[0]


def cisco_commands(src: str, community: str) -> list[str]:
    return [f"no ip access-list standard {ACL}",
            f"ip access-list standard {ACL}",
            f" permit host {src}",
            " deny any log",
            "exit",
            f"snmp-server community {community} RO {ACL}",
            "snmp-server ifindex persist"]


def mikrotik_commands(src: str, community: str) -> list[str]:
    find = f'[/snmp community find name="{community}"]'
    return [f':if ([:len {find}] = 0) do={{/snmp community add name="{community}" addresses={src}/32 '
            f'read-access=yes write-access=no}} else={{/snmp community set {find} addresses={src}/32 '
            f'read-access=yes write-access=no}}',
            "/snmp set enabled=yes",
            "/snmp community print terse"]


def masked(cmds: list[str], community: str) -> list[str]:
    return [c.replace(community, "<community>") for c in cmds]


def connect(prof: dict, password: str | None):
    from netmiko import ConnectHandler
    params = dict(host=prof["ssh_host"], port=prof.get("ssh_port", 22), username=prof.get("user"),
                  fast_cli=False, conn_timeout=15, auth_timeout=20)
    if prof["adapter"] == "cisco":
        params["device_type"] = "cisco_ios"
        if prof.get("ssh_legacy"):
            from tgen.dut.cisco import check_legacy_ssh
            check_legacy_ssh()
            params.update(disabled_algorithms={"pubkeys": ["rsa-sha2-512", "rsa-sha2-256"]},
                          global_delay_factor=2, conn_timeout=30, auth_timeout=40)
        if os.environ.get("TGEN_DUT_ENABLE"):
            params["secret"] = os.environ["TGEN_DUT_ENABLE"]
    else:
        params["device_type"] = "mikrotik_routeros"
    if password:
        params["password"] = password
    else:
        params["use_keys"] = True
    return ConnectHandler(**params)


def configure(name: str, prof: dict, community: str, args) -> bool:
    snmp_host = prof.get("snmp_host") or prof["ssh_host"]
    src = source_ip(snmp_host)
    cmds = cisco_commands(src, community) if prof["adapter"] == "cisco" else mikrotik_commands(src, community)
    print(f"\n== {name} ({prof.get('label', '')}) — SSH {prof['ssh_host']}, SNMP {snmp_host}, allowed source {src}")
    for c in masked(cmds, community):
        print("   " + c)
    if args.dry_run:
        return True
    pw = os.environ.get(inventory.password_env(name)) or \
        getpass.getpass(f"   SSH password for {prof.get('user')}@{prof['ssh_host']} (empty = SSH key; not stored): ") or None
    try:
        c = connect(prof, pw)
    except Exception as e:  # noqa: BLE001  netmiko raises several types
        print(f"   FAILED: SSH login: {e}")
        return False
    try:
        if prof["adapter"] == "cisco":
            if not c.check_enable_mode():
                if not c.secret:
                    c.secret = getpass.getpass("   enable secret (not stored): ")
                c.enable()
            out = c.send_config_set(cmds)
            if "% " in out:   # IOS error marker
                print("   device said:\n" + "\n".join("     " + ln for ln in out.replace(community, "<community>").splitlines()
                                                   if "%" in ln))
            if args.save:
                c.save_config()
                print("   saved to the startup configuration")
            else:
                print("   running configuration only (use --save to keep it after a reload)")
        else:
            for cmd in cmds:
                out = c.send_command(cmd, read_timeout=30)
                if "failure" in out.lower() or "syntax error" in out.lower() or "bad command" in out.lower():
                    print("   device said: " + out.replace(community, "<community>").strip()[:300])
            if re.search(r'name=public\b.*addresses=0\.0\.0\.0/0', out):
                print("   NOTE: the default community 'public' still answers from anywhere; restrict or disable it:")
                print("         /snmp community set [find name=public] addresses=127.0.0.1/32")
    finally:
        c.disconnect()
    try:
        s = snmp.Session(snmp_host, community, port=prof.get("snmp_port", 161))
        info = s.system()
        print(f"   OK: SNMP answers — {info.get('name')} · {(info.get('descr') or '').splitlines()[0][:80]}")
        return True
    except snmp.SnmpError as e:
        print(f"   FAILED: SNMP check: {e}")
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("profiles", nargs="*", help="profiles from deploy/duts.toml (default: every Cisco and MikroTik one)")
    ap.add_argument("--dry-run", action="store_true", help="only show the commands (community masked)")
    ap.add_argument("--save", action="store_true", help="Cisco: also write memory")
    args = ap.parse_args()
    inv = inventory.load()
    names = args.profiles or [n for n, p in inv.items() if p["adapter"] in ("cisco", "mikrotik")]
    bad = [n for n in names if n not in inv or inv[n]["adapter"] not in ("cisco", "mikrotik")]
    if bad:
        print(f"unknown or unsupported profiles: {', '.join(bad)} (have: {', '.join(inv)})")
        return 2
    if not snmp.available():
        print("net-snmp tools missing: sudo apt-get install -y snmp")
        return 2
    community = "dry-run-community"
    if not args.dry_run:
        community = getpass.getpass("SNMP community to set (8-32 letters, digits, . _ -; not stored): ")
        if not re.fullmatch(r"[A-Za-z0-9._-]{8,32}", community):
            print("the community must be 8-32 characters: letters, digits, '.', '_' or '-'")
            return 2
        if community in ("public", "private") or getpass.getpass("again: ") != community:
            print("the two entries differ, or the community is a default one")
            return 2
    ok = [configure(n, inv[n], community, args) for n in names]
    print()
    if not args.dry_run and all(ok):
        print("Done. For runs without typing the community, add to /etc/tgen/dut-passwords.env (sudoedit):")
        for n in names:
            print(f"   {snmp.community_env(n)}=<community>")
        print("then: sudo systemctl restart tgen-controller")
        print("Optional, to tick SNMP by default: in deploy/duts.toml add  snmp = true  to these profiles.")
    return 0 if all(ok) else 1


if __name__ == "__main__":
    sys.exit(main())
