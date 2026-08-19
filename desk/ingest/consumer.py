"""Subscribe to line.events, admit anomalies, persist a TriageTask.

The consumer does exactly two things: decide, and durably record. It never calls
a model. Keeping the stream consumer free of model calls is what lets it keep up
with the stream, and it is what makes the agent layer restartable without
replaying the whole topic.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import uuid

from kafka import KafkaConsumer

from desk.common.events import MachineEvent
from desk.ingest.admission import AdmissionPolicy
from desk.store import db

log = logging.getLogger("desk.consumer")

TOPIC_LINE_EVENTS = os.getenv("TOPIC_LINE_EVENTS", "line.events")
CONSUMER_GROUP = os.getenv("DESK_GROUP", "anomaly-desk-v1")


def build_consumer() -> KafkaConsumer:
    return KafkaConsumer(
        TOPIC_LINE_EVENTS,
        bootstrap_servers=os.getenv("KAFKA_BOOTSTRAP", "localhost:9092"),
        group_id=CONSUMER_GROUP,
        # Manual commit. We commit only after the triage task is durably in
        # Postgres, so a crash between "read" and "write" replays the event
        # rather than dropping it. At-least-once, with the dedupe key below
        # making the duplicate harmless.
        enable_auto_commit=False,
        auto_offset_reset="latest",
        value_deserializer=lambda b: b.decode("utf-8"),
        max_poll_records=200,
    )


def task_dedupe_key(event: MachineEvent) -> str:
    """Stable per-anomaly key.

    Not the event id: a fault re-raised after a genuine clear is a NEW anomaly
    and deserves a new task, while the same physical event redelivered by Kafka
    is not. Keying on (station, fault/kind, the second it occurred) collapses
    redelivery without collapsing a genuine recurrence minutes later.
    """
    discriminator = event.fault_code or event.kind
    return f"{event.station}:{discriminator}:{event.event_ts // 1000}"


def run(dry_run: bool = False) -> int:
    policy = AdmissionPolicy()
    consumer = build_consumer()
    stopping = False

    def _stop(*_args):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    admitted = seen = 0
    for message in consumer:
        if stopping:
            break
        seen += 1
        try:
            event = MachineEvent.from_json(message.value)
        except (ValueError, TypeError) as exc:
            log.warning("undecodable event at offset %s: %s",
                        message.offset, exc)
            consumer.commit()
            continue

        decision = policy.evaluate(event)
        if not decision.admit:
            consumer.commit()
            continue

        admitted += 1
        if dry_run:
            log.info("WOULD ADMIT %s: %s", event.station, decision.reason)
            consumer.commit()
            continue

        db.insert_triage_task(
            task_id=uuid.uuid4().hex,
            dedupe_key=task_dedupe_key(event),
            station=event.station,
            event_ts=event.event_ts,
            trigger_reason=decision.reason,
            severity_floor=int(decision.floor),
            raw_event=json.dumps(event.__dict__),
        )
        # Commit AFTER the write. This ordering is the entire durability
        # argument and it is one line, which is why it gets a comment.
        consumer.commit()

    log.info("saw %d events, admitted %d (%.2f%%)",
             seen, admitted, 100.0 * admitted / max(seen, 1))
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    sys.exit(run(dry_run=parser.parse_args().dry_run))
