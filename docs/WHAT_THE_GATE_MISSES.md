# What the gate cannot catch

Written down because an undocumented blind spot becomes an assumed strength.
Every item here is a real failure this system can ship with a green gate.

## 1. The eval set no longer represents production

The frozen set is frozen, which is what makes it useful as a regression check
and also what makes it stale. The line changed a supplier in March; the eval
set has no cases from that material. The gate is green and the anomaly mix in
production has moved.

**How you find out:** the two-scoreboard report. The judge score holds and the
override rate rises. That divergence is the only alarm for this.

**What to do:** re-sample production quarterly, label the new cases, add them.
Never remove old cases — the set only grows, or your regression check compares
against a moving target.

## 2. The graders measure something operators do not care about

Class, severity, disposition, grounding, required sources. A resolution can
score 1.0 on every one of those and still be a summary an operator cannot act
on, written in a way that buries the useful sentence.

**How you find out:** override notes. When `other` and `wrong_disposition`
carry notes that all say some version of "it's not wrong, it's just not
useful," the graders are measuring the wrong thing.

**What to do:** add a grader for it. This is how the grader suite should grow —
from things operators actually complained about, not from things that were easy
to grade.

## 3. The judge and the humans agree, and both are wrong

Judge agreement is measured against human labels. If your labelers share a
misconception with the model, kappa is high and the system is wrong in a way
nothing here detects.

**How you find out:** downstream outcomes. A quality escape found at final QA
that traces back to a triage everyone agreed with.

**What to do:** sample a small set for review by someone from a different
discipline — a reliability engineer rather than a floor operator. Expensive,
slow, and the only control for this.

## 4. Slow drift within tolerance

Every deploy regresses one slice by 0.025 against a 0.03 tolerance. Twelve
deploys later the slice is down 0.3 and every individual gate passed.

**How you find out:** the absolute floors catch this for the metrics that have
them, which is why they exist alongside the regression checks. For metrics
without a floor, nothing catches it.

**What to do:** compare against a QUARTERLY baseline as well as the immediately
previous one. Two comparisons, and the long one is what sees drift.

## 5. Cost regressions that are technically warnings

Cost is a warning, not a block, deliberately. That means it can be ignored
twelve times in a row and nobody has to justify anything.

**How you find out:** monthly spend against forecast.

**What to do:** turn the warning into a block above some cumulative threshold,
not a per-deploy one. The per-deploy version is the wrong granularity for a
metric that drifts.

## 6. Everything about the anomalies that never became triages

The admission policy is code and the gate never evaluates it. A cooldown that
suppresses the wrong thing, a fault code missing from `FAULT_FLOOR`, a drift
multiple set too high — none of it appears in any score in this project, because
a task that was never created cannot be graded.

**How you find out:** you do not, from anything in this repo. You find out when
a station fails and someone asks why the desk said nothing.

**What to do:** a monthly audit sampling admission REJECTIONS and having a human
confirm the rejection was right. It is the only measurement of coverage, and it
is the one every team skips.
