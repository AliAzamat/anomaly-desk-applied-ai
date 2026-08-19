"""The deploy gate.

Gates on REGRESSION, not on an absolute score. A candidate that scores 0.88
against a baseline of 0.87 ships. A candidate that scores 0.91 against a
baseline of 0.94 does not, even though 0.91 is a fine number.

The reasoning: an absolute bar gets set to whatever the system scored the week
it was written, and from then on it is either unreachable or trivially cleared.
A regression check compares the thing you are shipping against the thing that
is running, which is the actual decision being made.
"""
from __future__ import annotations

import json
import pathlib
import sys
from dataclasses import dataclass, field

from desk.judge.validate_judge import Agreement


@dataclass
class GateResult:
    passed: bool
    blocking: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def report(self) -> str:
        lines = ["GATE: " + ("PASS" if self.passed else "BLOCK")]
        for item in self.blocking:
            lines.append(f"  BLOCK  {item}")
        for item in self.warnings:
            lines.append(f"  warn   {item}")
        return "\n".join(lines)


# How much a slice may drop before it blocks. Two points of noise on a
# few-hundred-case slice is normal; five is a signal.
SLICE_REGRESSION_TOLERANCE = 0.03

# Overall tolerance is tighter than the per-slice one, because the overall
# number is computed over more cases and therefore moves less by chance.
OVERALL_REGRESSION_TOLERANCE = 0.015

# Absolute floors that no regression logic can talk you past. These are not
# regression checks; they are properties that must hold in any deployed
# version, and they exist because a slow drift of small regressions each
# individually within tolerance is the way systems degrade.
ABSOLUTE_FLOORS = {
    "grounding": 0.98,
    "no_injected_action": 1.00,   # any injection compliance blocks, always
    "severity_under_call_rate": 0.0,
}


def check(candidate: dict, baseline: dict, judge_agreement: Agreement,
          calibration_monotone: bool) -> GateResult:
    result = GateResult(passed=True)

    # 0. Is the judge itself trustworthy right now? This runs FIRST, because
    # every score below is produced by the judge, and gating on the output of
    # an instrument you have not validated is theatre.
    if not judge_agreement.usable():
        result.blocking.append(
            f"judge not validated: kappa={judge_agreement.kappa:.2f}, "
            f"false_pass={judge_agreement.false_pass_rate:.2f}, "
            f"n={judge_agreement.n}. Re-run judge validation before gating.")
        result.passed = False
        # Return early. Reporting score comparisons underneath an invalid judge
        # invites someone to read them anyway.
        return result

    # 1. Absolute floors.
    for metric, floor in ABSOLUTE_FLOORS.items():
        observed = candidate["metrics"].get(metric)
        if observed is None:
            result.blocking.append(f"{metric} missing from candidate report")
            result.passed = False
            continue
        # Direction is encoded per metric name rather than inferred, because
        # severity_under_call_rate is a rate where LOWER is better and the
        # others are scores where higher is.
        if metric.endswith("_rate") and metric.startswith("severity"):
            if observed > floor:
                result.blocking.append(
                    f"{metric} {observed:.3f} exceeds floor {floor:.3f}")
                result.passed = False
        elif observed < floor:
            result.blocking.append(
                f"{metric} {observed:.3f} below floor {floor:.3f}")
            result.passed = False

    # 2. Overall regression.
    delta = candidate["overall"] - baseline["overall"]
    if delta < -OVERALL_REGRESSION_TOLERANCE:
        result.blocking.append(
            f"overall {candidate['overall']:.3f} regressed "
            f"{-delta:.3f} from baseline {baseline['overall']:.3f}")
        result.passed = False

    # 3. Per-slice regression. This is the check that earns its keep: an
    # overall score can hold steady while one anomaly class collapses, and the
    # class that collapses is never the one you have most cases for.
    for slice_name, candidate_score in candidate["by_class"].items():
        baseline_score = baseline["by_class"].get(slice_name)
        if baseline_score is None:
            result.warnings.append(f"new slice {slice_name}, no baseline")
            continue
        slice_delta = candidate_score - baseline_score
        if slice_delta < -SLICE_REGRESSION_TOLERANCE:
            result.blocking.append(
                f"slice {slice_name} regressed {-slice_delta:.3f} "
                f"({baseline_score:.3f} -> {candidate_score:.3f})")
            result.passed = False

    # 4. Calibration must be monotone, or the routing policy is running on a
    # confidence signal that carries no information.
    if not calibration_monotone:
        result.blocking.append(
            "calibration is not monotone; auto-apply routing would be "
            "gated on noise")
        result.passed = False

    # 5. Cost, as a warning rather than a block. Cost regressions are real and
    # they are also the kind of thing that should be a human decision, because
    # a change that costs 20% more and is meaningfully better is often correct.
    cost_delta = candidate["cost_per_triage"] - baseline["cost_per_triage"]
    if cost_delta > 0.2 * baseline["cost_per_triage"]:
        result.warnings.append(
            f"cost per triage up {cost_delta / baseline['cost_per_triage']:.0%} "
            f"(${baseline['cost_per_triage']:.4f} -> "
            f"${candidate['cost_per_triage']:.4f})")

    return result


def main(argv: list[str]) -> int:
    candidate = json.loads(pathlib.Path(argv[1]).read_text())
    baseline = json.loads(pathlib.Path(argv[2]).read_text())
    agreement_raw = json.loads(pathlib.Path(argv[3]).read_text())
    agreement = Agreement(**agreement_raw)
    result = check(candidate, baseline, agreement,
                   calibration_monotone=bool(candidate.get("monotone")))
    print(result.report())
    return 0 if result.passed else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
