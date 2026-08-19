"""The classifier agent.

One job: read the anomaly and the recent station context, return a
`Classification`. It does not retrieve, it does not draft, it does not act.

Splitting this out is not architectural neatness. It is because this is the step
whose accuracy we can measure against a ground-truth label, and a step you can
measure in isolation is a step you can improve in isolation.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Callable

from pydantic import ValidationError

from desk.common.events import FAULT_FLOOR, Severity
from desk.common.stations import is_upstream_of
from desk.schemas.triage import Classification, ValidationFailure
from desk.ops.trace import Span

log = logging.getLogger("desk.classifier")

MAX_REPAIR_ATTEMPTS = 2

SYSTEM_PROMPT = """\
You are triaging an anomaly on a composite structures production line.

Return ONLY a JSON object matching this schema. No prose before or after.

{schema}

Rules:
- `root_station` must be the station that CAUSED the anomaly, which may be
  upstream of the station that detected it. It must be one of: {stations}
- `severity` must be at least {floor}. This anomaly's fault code carries that
  floor from the reliability table. You may raise it. You may not lower it.
- `confidence` is your probability that a domain expert would agree with this
  classification. Anchor it: 0.9 means you would be right nine times in ten.
- If a signal you need is absent from the context, name it in `missing_inputs`
  and lower your confidence. Do not infer a value you were not given.
"""


@dataclass
class ClassifierResult:
    classification: Classification
    attempts: int
    repaired: bool
    raw_responses: list[str]


class ClassifierAgent:
    def __init__(self, complete: Callable[..., str], model: str) -> None:
        # `complete(messages, model) -> str`. Injected rather than constructed
        # so the eval harness and the red-team runner can substitute a recorded
        # or adversarial completion function without touching this class.
        self._complete = complete
        self._model = model

    def run(self, task: dict[str, Any], context: str, span: Span) -> ClassifierResult:
        floor = int(task["severity_floor"])
        messages = [
            {"role": "system", "content": self._system(floor)},
            {"role": "user", "content": self._user(task, context)},
        ]

        raw_responses: list[str] = []
        for attempt in range(1, MAX_REPAIR_ATTEMPTS + 2):
            with span.child(f"classify.attempt.{attempt}") as child:
                raw = self._complete(messages=messages, model=self._model)
                child.set("response_chars", len(raw))
            raw_responses.append(raw)

            try:
                classification = self._parse(raw, task, floor)
            except ValidationFailure as failure:
                if attempt > MAX_REPAIR_ATTEMPTS:
                    span.set("classify.outcome", "unrecoverable")
                    raise
                log.info("repair attempt %d: %s", attempt, failure)
                span.set(f"classify.repair.{attempt}", str(failure))
                # Feed the error back rather than retrying blind. A blind retry
                # re-samples the same distribution and usually fails the same
                # way; the error message moves the model off the bad mode.
                messages.append({"role": "assistant", "content": raw})
                messages.append({
                    "role": "user",
                    "content": (f"That output was rejected: {failure}\n"
                                f"Return corrected JSON only."),
                })
                continue

            span.set("classify.outcome", "ok")
            return ClassifierResult(
                classification=classification,
                attempts=attempt,
                repaired=attempt > 1,
                raw_responses=raw_responses,
            )

        raise AssertionError("unreachable")

    def _parse(self, raw: str, task: dict[str, Any],
               floor: int) -> Classification:
        text = raw.strip()
        # Strip a markdown fence if the model added one. Tolerated because it
        # is a formatting artifact with no semantic content. Contrast with the
        # severity floor below, which is NOT tolerated, because that would be a
        # semantic change made silently on the model's behalf.
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0]

        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValidationFailure(f"not valid JSON: {exc}", raw, 0) from exc

        try:
            classification = Classification.model_validate(payload)
        except ValidationError as exc:
            raise ValidationFailure(
                "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}"
                          for e in exc.errors()),
                raw, 0,
            ) from exc

        # Post-schema semantic checks. These cannot live in the pydantic model
        # because they need the task, not just the field values.
        if classification.severity < floor:
            raise ValidationFailure(
                f"severity {classification.severity} is below the floor "
                f"{floor} set by the fault table",
                raw, 0,
            )

        trigger_station = task["station"]
        root = classification.root_station
        if root != trigger_station and not is_upstream_of(root, trigger_station):
            raise ValidationFailure(
                f"root_station {root} is downstream of the detecting station "
                f"{trigger_station}; work does not flow backwards",
                raw, 0,
            )

        return classification

    def _system(self, floor: int) -> str:
        from desk.common.stations import STATIONS
        return SYSTEM_PROMPT.format(
            schema=json.dumps(Classification.model_json_schema(), indent=2),
            stations=", ".join(STATIONS),
            floor=floor,
        )

    def _user(self, task: dict[str, Any], context: str) -> str:
        return (
            f"Station: {task['station']}\n"
            f"Trigger: {task['trigger_reason']}\n"
            f"Event: {json.dumps(task['raw_event'])}\n\n"
            f"Recent station context:\n{context}"
        )
