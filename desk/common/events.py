"""The event contract we inherit from the factory console project.

We do not redefine the machine event. The console project owns that schema and
this service is a downstream consumer of it. Redefining it here would create two
sources of truth for a shape that is already deployed, and the second one would
drift within a month.

What we DO define is what an anomaly is, which the console project deliberately
has no opinion about because it has no LLM in it.
"""
from __future__ import annotations

import enum
import json
from dataclasses import dataclass
from typing import Any, Optional

# Kinds and modes, mirrored from services.common.constants in the console
# project. Mirrored rather than imported because this service deploys
# separately; the values are pinned by a contract test that fails if the
# upstream constants change.
KIND_STATUS = "status"
KIND_THROUGHPUT = "throughput"
KIND_FAULT = "fault"

MODE_RUNNING = "running"
MODE_IDLE = "idle"
MODE_DOWN = "down"


@dataclass(frozen=True)
class MachineEvent:
    """One event from one station, as it arrives on `line.events`."""

    station: str
    event_ts: int
    kind: str
    seq: int
    mode: Optional[str] = None
    units: Optional[int] = None
    cycle_ms: Optional[int] = None
    fault_code: Optional[str] = None
    active: Optional[bool] = None
    event_id: str = ""

    @staticmethod
    def from_json(raw: str) -> "MachineEvent":
        d: dict[str, Any] = json.loads(raw)
        known = MachineEvent.__dataclass_fields__.keys()
        # Ignore unknown fields rather than raising. The upstream producer is
        # allowed to add fields without coordinating a deploy with us; that is
        # the whole point of a versioned event contract. Raising here would mean
        # every upstream feature launch takes this service down.
        return MachineEvent(**{k: v for k, v in d.items() if k in known})


class Severity(enum.IntEnum):
    """Ordered so comparisons work. IntEnum, not Enum, because the routing
    policy asks `severity >= Severity.HIGH` and that must be a real comparison
    rather than a lookup table someone forgets to update."""

    INFO = 0
    LOW = 1
    MEDIUM = 2
    HIGH = 3
    CRITICAL = 4


# Fault codes the line emits, with the floor severity each one carries
# regardless of what any model later concludes. This table is written by the
# reliability engineers, not by us, and the classifier agent cannot lower a
# value below its floor.
FAULT_FLOOR: dict[str, Severity] = {
    "F-THERM-OVER": Severity.HIGH,        # cure oven over temperature
    "F-THERM-UNDER": Severity.MEDIUM,
    "F-VAC-LOSS": Severity.HIGH,          # vacuum bag leak during layup
    "F-SPINDLE-VIB": Severity.MEDIUM,     # CNC spindle vibration out of band
    "F-NDT-VOID": Severity.CRITICAL,      # non-destructive test found a void
    "F-TORQUE-SEQ": Severity.HIGH,        # bolt torque sequence violated
    "F-COMMS-LOSS": Severity.LOW,         # station stopped reporting
    "F-CAL-STALE": Severity.LOW,          # calibration past due
}
