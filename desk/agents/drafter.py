"""The drafting agent.

Reads the classification and the retrieved evidence, calls read tools to fill
gaps, and produces a Resolution with citations and proposed actions.

This is the agent with the most freedom in the system, which is exactly why it
has no ability to change anything.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from pydantic import ValidationError

from desk.agents import tools
from desk.retrieval.grounding import verify_all, grounding_rate
from desk.retrieval.hybrid import Retrieved
from desk.schemas.citation import GroundingResult
from desk.schemas.resolution import Resolution
from desk.schemas.triage import Classification
from desk.ops.trace import Span

log = logging.getLogger("desk.drafter")

MAX_TOOL_STEPS = 5
DRAFT_DEADLINE_S = 25.0

SYSTEM_PROMPT = """\
You are drafting a resolution for a production-line anomaly.

You have a classification, retrieved procedure text, and past incidents. You may
call read tools to fill gaps. You may NOT take actions — propose them.

Return ONLY JSON matching:
{schema}

Citation rules, enforced by code after you answer:
- Every citation must quote text VERBATIM from the source you name.
- Procedure citations must name the revision shown in the evidence block.
- `start_offset` and `end_offset` are the character offsets of your quote in
  that source, as given in the evidence block.
- If the evidence does not support a claim, do not make the claim. Put the
  gap in `open_questions` instead.

Do not propose an action that duplicates an existing hold. Call get_open_holds.
"""


@dataclass
class DraftResult:
    resolution: Resolution
    grounding: list[GroundingResult] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    partial: bool = False
    regrounded: bool = False


class DrafterAgent:
    def __init__(self, complete: Callable[..., dict], model: str) -> None:
        self._complete = complete
        self._model = model

    def run(self, classification: Classification, evidence: list[Retrieved],
            task: dict[str, Any], span: Span) -> DraftResult:
        started = time.perf_counter()
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT.format(
                schema=json.dumps(Resolution.model_json_schema(), indent=2))},
            {"role": "user", "content": self._user(classification, evidence, task)},
        ]
        tool_specs = [
            {"name": s.name, "description": s.description,
             "parameters": s.parameters}
            for s in tools.REGISTRY.values() if not s.is_write
        ]

        recorded: list[dict[str, Any]] = []
        for step in range(MAX_TOOL_STEPS):
            elapsed = time.perf_counter() - started
            # Check before the call, with an estimate of what a call costs. A
            # step started at 24.1 seconds against a 25 second deadline burns
            # the remaining budget and returns nothing usable.
            if elapsed + 4.0 > DRAFT_DEADLINE_S:
                span.set("draft.stopped_on", "deadline")
                return self._finish(messages, recorded, span, partial=True)

            with span.child(f"draft.step.{step}") as child:
                response = self._complete(
                    messages=messages, model=self._model, tools=tool_specs)
                child.set("has_tool_call", bool(response.get("tool_call")))

            call = response.get("tool_call")
            if not call:
                messages.append({"role": "assistant",
                                 "content": response["content"]})
                return self._finish(messages, recorded, span, partial=False)

            with span.child(f"tool.{call['name']}") as child:
                result = tools.invoke(call["name"], call.get("arguments", {}))
                child.set("result_chars", len(result))
            recorded.append({"name": call["name"],
                             "arguments": call.get("arguments", {}),
                             "result": result[:2000]})
            messages.append({"role": "assistant", "tool_call": call})
            messages.append({"role": "tool", "name": call["name"],
                             "content": result})

        span.set("draft.stopped_on", "tool_step_limit")
        return self._finish(messages, recorded, span, partial=True)

    def _finish(self, messages: list[dict], recorded: list[dict],
                span: Span, partial: bool) -> DraftResult:
        """Parse the final answer, verify its citations, and reground once."""
        raw = self._last_assistant_text(messages)
        resolution = self._parse(raw)
        grounding = verify_all(resolution.citations)
        rate = grounding_rate(grounding)
        span.set("draft.grounding_rate", rate)

        # One regrounding attempt when a citation fails verification. The model
        # gets told exactly which quote was not found and is asked to either
        # correct it or drop the claim. One attempt, not a loop: a model that
        # cannot ground a claim on the second try is asserting something the
        # evidence does not contain, and asking again just produces a
        # better-disguised fabrication.
        failed = [g for g in grounding if not g.grounded]
        regrounded = False
        if failed and not partial:
            span.set("draft.reground", len(failed))
            detail = "\n".join(
                f"- citation {g.citation_index}: {g.reason}" for g in failed)
            messages.append({"role": "user", "content": (
                f"These citations failed verification:\n{detail}\n"
                f"Correct the quote and offsets, or remove the claim they "
                f"support and add it to open_questions. Return the full JSON.")})
            response = self._complete(messages=messages, model=self._model,
                                      tools=[])
            try:
                resolution = self._parse(response["content"])
                grounding = verify_all(resolution.citations)
                regrounded = True
                span.set("draft.grounding_rate_after", grounding_rate(grounding))
            except ValueError as exc:
                # The regrounding attempt produced unparseable output. Keep the
                # original resolution with its failed grounding rather than
                # losing the work entirely; the routing layer will see the low
                # grounding rate and escalate.
                log.warning("regrounding failed to parse: %s", exc)

        return DraftResult(resolution=resolution, grounding=grounding,
                           tool_calls=recorded, partial=partial,
                           regrounded=regrounded)

    def _parse(self, raw: str) -> Resolution:
        text = raw.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0]
        try:
            return Resolution.model_validate_json(text)
        except ValidationError as exc:
            raise ValueError(f"resolution did not validate: {exc}") from exc

    def _last_assistant_text(self, messages: list[dict]) -> str:
        for message in reversed(messages):
            if message["role"] == "assistant" and message.get("content"):
                return message["content"]
        raise ValueError("no assistant text in conversation")

    def _user(self, classification: Classification,
              evidence: list[Retrieved], task: dict[str, Any]) -> str:
        blocks = []
        for item in evidence:
            header = (f"[{item.kind}:{item.source_id}"
                      f"{' rev ' + item.revision if item.revision else ''}"
                      f"{' §' + item.section if item.section else ''}"
                      f" offsets {item.start_offset}-{item.end_offset}]")
            blocks.append(f"{header}\n{item.text}")
        return (
            f"Station: {task['station']}\n"
            f"Trigger: {task['trigger_reason']}\n\n"
            f"Classification:\n{classification.model_dump_json(indent=2)}\n\n"
            f"Evidence:\n\n" + "\n\n---\n\n".join(blocks)
        )
