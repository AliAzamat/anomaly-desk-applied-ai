"""Which events become triage tasks.

An agent pass costs money and takes seconds. The line emits events every few
hundred milliseconds per station. If every event became a triage the system
would spend thousands of dollars a day reasoning about heartbeats that say
"still running."

So there is an admission policy, and it lives in code with explicit rules rather
than inside a model. A model deciding what deserves a model is a spend
amplifier with no floor.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from desk.common.events import (
    KIND_FAULT, KIND_STATUS, KIND_THROUGHPUT,
    MODE_DOWN, MachineEvent, FAULT_FLOOR, Severity,
)


@dataclass
class AdmissionDecision:
    admit: bool
    reason: str
    # The floor severity we already know from the fault table, before any model
    # has looked at anything. The classifier can raise this, never lower it.
    floor: Severity = Severity.INFO


@dataclass
class StationWindow:
    """Rolling state per station, used to detect drift the console's own
    threshold alerts do not catch."""

    recent_cycle_ms: list[int] = field(default_factory=list)
    last_admit_ts: float = 0.0
    open_faults: set[str] = field(default_factory=set)

    def observe_cycle(self, cycle_ms: int, keep: int = 40) -> None:
        self.recent_cycle_ms.append(cycle_ms)
        if len(self.recent_cycle_ms) > keep:
            self.recent_cycle_ms.pop(0)

    def median_cycle(self) -> float | None:
        if len(self.recent_cycle_ms) < 20:
            return None
        s = sorted(self.recent_cycle_ms)
        return float(s[len(s) // 2])


# Minimum seconds between two admitted triages for the same station. A station
# in a bad state emits a burst; without this, one failure produces forty
# identical triages, forty agent passes, and forty tickets for one problem.
STATION_COOLDOWN_S = 90.0

# How far above the station's own median cycle time counts as drift. Relative to
# the station's history rather than a global constant, because the cure oven and
# the CNC trim have cycle times two orders of magnitude apart.
DRIFT_MULTIPLE = 1.8


class AdmissionPolicy:
    def __init__(self, clock=time.monotonic) -> None:
        self._windows: dict[str, StationWindow] = {}
        self._clock = clock

    def _window(self, station: str) -> StationWindow:
        return self._windows.setdefault(station, StationWindow())

    def evaluate(self, event: MachineEvent) -> AdmissionDecision:
        window = self._window(event.station)
        now = self._clock()

        if event.kind == KIND_THROUGHPUT and event.cycle_ms is not None:
            median = window.median_cycle()
            window.observe_cycle(event.cycle_ms)
            if median is not None and event.cycle_ms > median * DRIFT_MULTIPLE:
                return self._maybe_admit(
                    window, now,
                    reason=(f"cycle {event.cycle_ms}ms is "
                            f"{event.cycle_ms / median:.1f}x station median "
                            f"{median:.0f}ms"),
                    floor=Severity.MEDIUM,
                )
            return AdmissionDecision(False, "throughput within band")

        if event.kind == KIND_FAULT and event.fault_code:
            if event.active:
                # A fault already open is not a new triage. The station will
                # re-assert an active fault on every heartbeat while it is down.
                if event.fault_code in window.open_faults:
                    return AdmissionDecision(False, "fault already open")
                window.open_faults.add(event.fault_code)
                floor = FAULT_FLOOR.get(event.fault_code, Severity.MEDIUM)
                # Critical faults bypass the cooldown. A void found in NDT is
                # worth a duplicate triage; a rate limit that suppresses it is
                # a rate limit that hid the one event that mattered.
                if floor >= Severity.CRITICAL:
                    window.last_admit_ts = now
                    return AdmissionDecision(
                        True, f"critical fault {event.fault_code}", floor)
                return self._maybe_admit(
                    window, now, f"fault {event.fault_code} raised", floor)
            window.open_faults.discard(event.fault_code)
            return AdmissionDecision(False, "fault cleared")

        if event.kind == KIND_STATUS and event.mode == MODE_DOWN:
            return self._maybe_admit(
                window, now, "station reported down", Severity.HIGH)

        return AdmissionDecision(False, "no anomaly signal")

    def _maybe_admit(self, window: StationWindow, now: float,
                     reason: str, floor: Severity) -> AdmissionDecision:
        if now - window.last_admit_ts < STATION_COOLDOWN_S:
            return AdmissionDecision(
                False, f"station cooling down ({reason} suppressed)", floor)
        window.last_admit_ts = now
        return AdmissionDecision(True, reason, floor)
