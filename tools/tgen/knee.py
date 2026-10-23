# SPDX-License-Identifier: MIT
"""The knee rule, shared by tgen_series.py (deciding when to stop) and
tgen_knee_chart.py (re-deciding old series with the current rule).

A paced run is past the knee when the DUT falls behind:
  - its output is below `ratio` x offered deliveries (the expectation shrinks by
    send_window / (send_window + MRAI): a DUT that batches for MRAI seconds
    stretches the delivery span without being behind), or
  - receivers are still incomplete more than `backlog_s` after our last send
    (default 5 s + MRAI).
"""
from __future__ import annotations

RATIO = 0.9
BACKLOG_S = 5.0


def backlog_limit(mrai: float, backlog_s: float | None = None) -> float:
    return backlog_s if backlog_s is not None else BACKLOG_S + mrai


def past_knee(offered: float, output: float | None, after_last_send_s: float | None, send_window_s: float | None,
              mrai: float = 0.0, ratio: float = RATIO, backlog_s: float | None = None) -> dict:
    sw = send_window_s or 0
    expect = offered * (sw / (sw + mrai) if sw > 0 and mrai else 1)
    behind = output is not None and output < ratio * expect
    backlog = after_last_send_s is None or after_last_send_s > backlog_limit(mrai, backlog_s)
    return {"past_knee": behind or backlog, "behind": behind, "backlog": backlog, "expected_output": expect}
