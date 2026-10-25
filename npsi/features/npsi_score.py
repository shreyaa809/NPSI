"""
The core N-PSI metric: windowed "some"/"full" stall fraction per link,
directly mirroring Linux PSI semantics (/proc/pressure/*) but applied to a
network link's queue instead of CPU/memory/IO.

"some" = fraction of ticks in the window where at least one probe observed
a stall on this link. "full" = fraction of ticks where the link was so
backed up that even NIC-ring transmission was blocked (i.e. everyone
waiting, not just someone).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class NPsiScore:
    some: float
    full: float


def compute_npsi_score(some_flags: list[bool], full_flags: list[bool]) -> NPsiScore:
    n = len(some_flags)
    if n == 0:
        return NPsiScore(some=0.0, full=0.0)
    return NPsiScore(
        some=sum(1 for f in some_flags if f) / n,
        full=sum(1 for f in full_flags if f) / n,
    )
