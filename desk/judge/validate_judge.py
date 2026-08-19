"""Validate the judge against human labels.

The question this answers: when the judge says a resolution's reasoning is
sound, how often does a domain expert agree?

Until you have this number, the judge score is a number your system produces
about itself. Reporting it as a quality metric before measuring judge-human
agreement is reporting the output of an uncalibrated instrument.
"""
from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass

from desk.judge.graders import Verdict


@dataclass
class Agreement:
    n: int
    raw_agreement: float
    # Cohen's kappa. Raw agreement is misleading when the classes are
    # imbalanced: if 90% of resolutions are sound, a judge that says "sound"
    # every time scores 0.90 raw agreement while carrying zero information.
    kappa: float
    # The two error directions, separately, because they cost different things.
    # A judge that passes bad work lets regressions through the gate. A judge
    # that fails good work makes the gate block deploys for no reason and gets
    # switched off within a month.
    false_pass_rate: float
    false_fail_rate: float

    def usable(self) -> bool:
        """Whether this judge is good enough to gate on.

        0.6 kappa is the conventional 'substantial agreement' threshold. The
        false-pass constraint is stricter because that is the failure that
        reaches production.
        """
        return self.kappa >= 0.6 and self.false_pass_rate <= 0.10


def cohens_kappa(judge: list[bool], human: list[bool]) -> float:
    n = len(judge)
    if n == 0:
        return 0.0
    observed = sum(1 for j, h in zip(judge, human) if j == h) / n
    judge_pos = sum(judge) / n
    human_pos = sum(human) / n
    expected = judge_pos * human_pos + (1 - judge_pos) * (1 - human_pos)
    if expected >= 1.0:
        # Both raters constant and identical. Kappa is undefined; returning 0
        # is the conservative reading, because a constant rater carries no
        # information regardless of how well it happens to agree.
        return 0.0
    return (observed - expected) / (1 - expected)


def validate(labels_path: pathlib.Path,
             judge_verdicts: dict[str, Verdict]) -> Agreement:
    """Compare judge verdicts against a human-labeled file.

    The human labels must come from someone who did NOT see the judge's
    verdict. Label leakage here is not a subtle statistical concern; a labeler
    shown the judge's answer agrees with it, and the resulting kappa is a
    measure of how persuasive the judge's phrasing is.
    """
    human: list[bool] = []
    judged: list[bool] = []
    for line in labels_path.read_text().splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        case_id = record["case_id"]
        if case_id not in judge_verdicts:
            continue
        human.append(bool(record["human_sound"]))
        judged.append(judge_verdicts[case_id] == Verdict.PASS)

    n = len(human)
    if n == 0:
        return Agreement(0, 0.0, 0.0, 1.0, 1.0)

    raw = sum(1 for j, h in zip(judged, human) if j == h) / n
    human_bad = [i for i, h in enumerate(human) if not h]
    human_good = [i for i, h in enumerate(human) if h]
    false_pass = (sum(1 for i in human_bad if judged[i]) / len(human_bad)
                  if human_bad else 0.0)
    false_fail = (sum(1 for i in human_good if not judged[i]) / len(human_good)
                  if human_good else 0.0)

    return Agreement(n=n, raw_agreement=raw,
                     kappa=cohens_kappa(judged, human),
                     false_pass_rate=false_pass,
                     false_fail_rate=false_fail)
