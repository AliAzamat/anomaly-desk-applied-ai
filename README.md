Anomaly Desk — An Agentic Triage System With a Judge Scoring It

An advanced applied-AI capstone built on top of a running operations console. A production line emits a continuous stream of machine events; a subset of them are anomalies that a human anomaly desk triages today. You build a multi-agent workflow that does the first pass — a classifier agent returning a typed schema downstream code validates rather than a paragraph of prose, a retrieval agent that must cite the historical incident or procedure section it relied on, and a drafting agent with real tool access that assembles a resolution. You then build the part most teams skip: an LLM-as-judge harness that scores correctness and citation grounding, including verifying the cited span actually appears in the cited source. You red-team the system with contradictory events, missing telemetry, and prompt-injected ticket text. You trace every agent hop for latency and cost. And you end on the tension that defines the job — the automated judge says the system is at 0.91 while operators override 22 percent of its calls, and the gap between those two scoreboards is the work.

## Stack
- Python
- React
- PostgreSQL
- Kafka
- multi-agent workflows
- structured outputs
- retrieval with citations
- LLM-as-judge
- distributed tracing
- Docker
- Kubernetes
