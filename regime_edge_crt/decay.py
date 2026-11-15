"""decay.py — the CUSUM edge-decay monitor (EA rules VERBATIM).

Ported unchanged from ``crt_ev_optimizer.py::DecayMonitor``, which itself
replays ``CRT_Auction_Reversal_AIO_EA_v5_CONT.mq4``'s ComputeMonitors rules
1:1 (fidelity checklist §7 of the Task 67 assessment):

    k = max(kappa_min, kappa_frac * max(mu0, 0) / sd0)      # drift allowance
    S = max(0, S - z - k),  z = (x - mu0) / sd0              # one-sided CUSUM
    OFF      if S >= h_off OR mean(last roll_n) <= roll_off_ev
    CAUTION  elif S >= h_caution
    OFF -> CAUTION only after react_n trades with mean > 0    # reactivation

CRT-1's live engine imports THIS module — one source of truth for the decay
rules, never a second port.
"""
from __future__ import annotations

import numpy as np

NORMAL, CAUTION, OFF = 0, 1, 2
NAMES = {0: "NORMAL", 1: "CAUTION", 2: "OFF"}


class DecayMonitor:
    NORMAL, CAUTION, OFF = 0, 1, 2
    NAMES = {0: "NORMAL", 1: "CAUTION", 2: "OFF"}

    def __init__(self, mu0, sd0, a):
        self.mu0 = mu0
        self.sd0 = sd0 if sd0 and sd0 > 0 else 1.0
        self.k = max(a.kappa_min, a.kappa_frac * max(mu0, 0.0) / self.sd0)
        self.a = a
        self.S = 0.0
        self.hist: list = []
        self.state = self.NORMAL

    def update(self, x):
        a = self.a
        z = (x - self.mu0) / self.sd0
        self.S = max(0.0, self.S - z - self.k)
        self.hist.append(x)
        roll = self.hist[-a.roll_n:]
        raw = self.NORMAL
        if self.S >= a.h_off or (len(roll) >= a.roll_n and np.mean(roll) <= a.roll_off_ev):
            raw = self.OFF
        elif self.S >= a.h_caution:
            raw = self.CAUTION
        if self.state == self.OFF and raw != self.OFF:
            rec = self.hist[-a.react_n:]
            if len(rec) >= a.react_n and np.mean(rec) > 0:
                raw = self.CAUTION
            else:
                raw = self.OFF
        self.state = raw
        return raw
