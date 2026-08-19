"""The model judge, for the part rules cannot reach.

What it grades: whether the resolution's REASONING is sound given the evidence.
Not whether the class matches — a rule does that. Not whether citations verify
— code does that. The residue is "does this argument follow from this
evidence", which is a reading task.

The judge is an instrument. An instrument you have not calibrated is a source
of numbers, not measurements.
"""
from __future__ import annotations

import json
import logging
import random
from dataclasses import dataclass
from typing import Any, Callable

from pydantic import BaseModel, Field, ValidationError

from desk.judge.graders import EvalCase, GradeResult, Verdict
from desk.schemas.resolution import Resolution

log = logging.getLogger("desk.judge")


class JudgeVerdict(BaseModel):
    """The judge's own structured output. Same discipline as everything else:
    a judge returning prose is a judge whose output has to be parsed by
    another model, and now you have two problems."""

    model_config = {"extra": "forbid"}

    reasoning_sound: bool
    # Does the cited evidence actually support the conclusion drawn from it?
    # Distinct from grounding: grounding asks whether the quote is real, this
    # asks whether the quote implies what the summary says it implies.
    evidence_supports_conclusion: bool
    # Anything the resolution asserts that no cited source establishes.
    unsupported_claims: list[str] = Field(default_factory=list, max_length=5)
    # Bounded, because an unbounded justification field invites the judge to
    # write an essay and the essay does not go anywhere.
    justification: str = Field(min_length=20, max_length=500)


JUDGE_PROMPT = """\
You are grading one anomaly triage. You are NOT re-doing the triage.

Grade only this: given the evidence quoted below, does the resolution's
reasoning follow?

Do not reward length. Do not reward confident phrasing. A short resolution that
follows from its evidence is better than a long one that does not.

If the resolution asserts something no quoted source establishes, list it in
`unsupported_claims` even if the assertion is probably true. Probably true and
established by the evidence are different, and only one of them is your job.

EVIDENCE:
{evidence}

RESOLUTION:
{resolution}

Return only JSON matching:
{schema}
"""


@dataclass
class JudgeResult:
    verdict: JudgeVerdict
    # Which sample this was, when self-consistency sampling is on.
    samples: int
    agreement: float          # fraction of samples agreeing with the majority


class ModelJudge:
    def __init__(self, complete: Callable[..., str], model: str,
                 samples: int = 3) -> None:
        self._complete = complete
        self._model = model
        # Odd number, so a majority always exists. Three is enough to catch a
        # judge that is genuinely uncertain; five costs 67% more for a small
        # improvement in agreement estimates.
        self._samples = samples

    def grade(self, case: EvalCase, resolution: Resolution,
              evidence_blocks: list[str]) -> JudgeResult:
        # Shuffle evidence order per sample. Position bias is real: a judge
        # weights the first block it reads more heavily, so a fixed order
        # makes the score depend on retrieval ranking rather than on content.
        verdicts: list[JudgeVerdict] = []
        for i in range(self._samples):
            blocks = list(evidence_blocks)
            random.Random(f"{case.case_id}:{i}").shuffle(blocks)
            prompt = JUDGE_PROMPT.format(
                evidence="\n\n---\n\n".join(blocks),
                resolution=resolution.model_dump_json(indent=2),
                schema=json.dumps(JudgeVerdict.model_json_schema(), indent=2),
            )
            raw = self._complete(
                messages=[{"role": "user", "content": prompt}],
                model=self._model)
            try:
                verdicts.append(JudgeVerdict.model_validate_json(
                    raw.strip().removeprefix("```json").removeprefix("```")
                       .removesuffix("```")))
            except ValidationError as exc:
                log.warning("judge sample %d unparseable: %s", i, exc)

        if not verdicts:
            raise RuntimeError(f"judge produced no valid verdict for {case.case_id}")

        sound = sum(1 for v in verdicts if v.reasoning_sound)
        majority_sound = sound * 2 > len(verdicts)
        agreement = max(sound, len(verdicts) - sound) / len(verdicts)

        # Return a verdict from the majority side rather than synthesizing one.
        # A synthetic verdict combining three justifications is text no model
        # produced and no human wrote, and it is the thing someone will quote
        # in a review meeting.
        representative = next(v for v in verdicts
                              if v.reasoning_sound == majority_sound)
        return JudgeResult(verdict=representative, samples=len(verdicts),
                           agreement=agreement)


def to_grade_result(judge: JudgeResult) -> GradeResult:
    verdict = judge.verdict
    passed = verdict.reasoning_sound and verdict.evidence_supports_conclusion
    return GradeResult(
        grader="judge_reasoning",
        verdict=Verdict.PASS if passed else Verdict.FAIL,
        score=1.0 if passed else 0.0,
        detail=verdict.justification,
        deterministic=False,
    )
