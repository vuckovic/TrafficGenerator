# Real DUT log samples (input for the P5 log parsers)

| File | Device | Run | Logging setup | Notes |
|---|---|---|---|---|
| `mikrotik-7.11.2-crs106.log` | MikroTik CRS106‑1C‑5S, RouterOS 7.11.2 | `p2-20261020-202630` (12 × 100, route server) | rule `topics=bgp,!packet,!raw action=memory prefix=tgen` (added by the tool) | `/log print terse`, `system,info` lines removed. Only **"Entering OpenConfirm"** is logged (topics `route,bgp,debug,state`), one per session, with the peer `IP:port`. No Established, close or route events at this level. The router clock was not synchronised (06:29 here = 20:2x on CMNODE01). The sessions are accepted one after another over 2 s, which matches setup p90 2.5 s. |
