/**
 * The operator's working surface.
 *
 * The whole design brief is one sentence: an operator doing their job at
 * normal speed should produce a labeled dataset without ever thinking about
 * the fact that they are producing a labeled dataset.
 */
import { useEffect, useState } from "react";

type Citation = {
  kind: "procedure" | "incident";
  source_id: string;
  revision?: string;
  section?: string;
  quoted: string;
  context_before?: string;
  context_after?: string;
  warning?: string;
};

type QueueItem = {
  task_id: string;
  station: string;
  trigger_reason: string;
  anomaly_class: string;
  severity: number;
  disposition: string;
  summary: string;
  agent_confidence: number;
  calibrated_confidence: number;
  grounding_rate: number;
  route: "review" | "escalate";
  routing_reasons: string[];
};

const CATEGORIES = [
  ["wrong_class", "Wrong class"],
  ["wrong_severity", "Wrong severity"],
  ["wrong_disposition", "Wrong disposition"],
  ["missing_context", "Agent lacked context"],
  ["bad_citation", "Citation doesn't support it"],
  ["agent_correct_but_stale", "Right, but already handled"],
  ["other", "Other"],
] as const;

export function AnomalyDesk() {
  const [queue, setQueue] = useState<QueueItem[]>([]);
  const [active, setActive] = useState<QueueItem | null>(null);
  const [citations, setCitations] = useState<Citation[]>([]);

  useEffect(() => {
    fetch("/api/desk/queue")
      .then((r) => r.json())
      .then((d) => setQueue(d.items));
  }, []);

  useEffect(() => {
    if (!active) return;
    // Evidence is fetched on selection rather than with the queue. The queue
    // is fifty items and each carries up to six citations with 800 characters
    // of surrounding context; loading all of it eagerly would push a megabyte
    // to render a list of one-line summaries.
    fetch(`/api/desk/task/${active.task_id}/evidence`)
      .then((r) => r.json())
      .then((d) => setCitations(d.citations));
  }, [active]);

  return (
    <div className="desk">
      <QueueList items={queue} active={active} onSelect={setActive} />
      {active && (
        <DraftPane
          item={active}
          citations={citations}
          onResolved={(id) => {
            setQueue((q) => q.filter((x) => x.task_id !== id));
            setActive(null);
          }}
        />
      )}
    </div>
  );
}

function QueueList({ items, active, onSelect }: {
  items: QueueItem[];
  active: QueueItem | null;
  onSelect: (item: QueueItem) => void;
}) {
  return (
    <ul className="queue">
      {items.map((item) => (
        <li
          key={item.task_id}
          className={item.task_id === active?.task_id ? "row sel" : "row"}
          onClick={() => onSelect(item)}
        >
          <span className={`sev sev-${item.severity}`}>S{item.severity}</span>
          <span className="station">{item.station}</span>
          <span className="trigger">{item.trigger_reason}</span>
          {/* The calibrated number, not the model's stated one. Showing the
              stated confidence would train operators to trust a number the
              system itself does not trust. */}
          <span className="conf">{(item.calibrated_confidence * 100).toFixed(0)}%</span>
          {item.grounding_rate < 1 && (
            <span className="flag" title="not all citations verified">
              ungrounded
            </span>
          )}
        </li>
      ))}
    </ul>
  );
}

function DraftPane({ item, citations, onResolved }: {
  item: QueueItem;
  citations: Citation[];
  onResolved: (taskId: string) => void;
}) {
  const [overriding, setOverriding] = useState(false);

  return (
    <section className="draft">
      <h2>{item.station}</h2>
      <p className="why">{item.trigger_reason}</p>

      {/* Why this needed a human, verbatim from the routing decision. An
          operator who understands why the system asked will develop an
          accurate mental model of when it can be trusted. Hiding it produces
          operators who either rubber-stamp everything or distrust everything. */}
      <ul className="reasons">
        {item.routing_reasons.map((reason) => <li key={reason}>{reason}</li>)}
      </ul>

      <p className="summary">{item.summary}</p>
      <dl className="fields">
        <dt>Class</dt><dd>{item.anomaly_class}</dd>
        <dt>Severity</dt><dd>{item.severity}</dd>
        <dt>Disposition</dt><dd>{item.disposition}</dd>
      </dl>

      <h3>Evidence</h3>
      {citations.map((citation, i) => (
        <blockquote key={i} className={citation.warning ? "cite bad" : "cite"}>
          <cite>
            {citation.source_id}
            {citation.revision ? ` rev ${citation.revision}` : ""}
            {citation.section ? ` §${citation.section}` : ""}
          </cite>
          {/* Surrounding context is rendered dim, the quoted span bright. The
              operator can see in one glance whether the quote was lifted out
              of a sentence that reverses it — the one citation failure the
              automated verifier structurally cannot catch. */}
          <span className="ctx">{citation.context_before}</span>
          <mark>{citation.quoted}</mark>
          <span className="ctx">{citation.context_after}</span>
          {citation.warning && <em className="warn">{citation.warning}</em>}
        </blockquote>
      ))}

      {!overriding ? (
        <div className="actions">
          <button
            className="accept"
            onClick={async () => {
              await fetch(`/api/desk/task/${item.task_id}/accept`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ operator_id: currentOperator() }),
              });
              onResolved(item.task_id);
            }}
          >
            Accept
          </button>
          <button onClick={() => setOverriding(true)}>Change something</button>
        </div>
      ) : (
        <OverrideForm item={item} onDone={() => onResolved(item.task_id)} />
      )}
    </section>
  );
}

function OverrideForm({ item, onDone }: {
  item: QueueItem;
  onDone: () => void;
}) {
  const [category, setCategory] = useState<string>("");
  const [note, setNote] = useState("");
  const [severity, setSeverity] = useState<number | null>(null);
  const [anomalyClass, setAnomalyClass] = useState<string | null>(null);

  const changed = severity !== null || anomalyClass !== null;
  const canSubmit =
    category !== "" &&
    note.trim().length >= 12 &&
    (changed || category === "agent_correct_but_stale");

  return (
    <form
      className="override"
      onSubmit={async (event) => {
        event.preventDefault();
        await fetch(`/api/desk/task/${item.task_id}/override`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            operator_id: currentOperator(),
            category,
            note,
            new_severity: severity,
            new_anomaly_class: anomalyClass,
          }),
        });
        onDone();
      }}
    >
      {/* Category as buttons rather than a select. A select requires open,
          scan, click; buttons are one click. Over a few hundred overrides a
          week that difference decides whether the field is filled honestly or
          filled with whatever is first in the list. */}
      <div className="cats">
        {CATEGORIES.map(([value, label]) => (
          <button
            type="button"
            key={value}
            className={category === value ? "cat on" : "cat"}
            onClick={() => setCategory(value)}
          >
            {label}
          </button>
        ))}
      </div>

      <SeverityPicker current={item.severity} onPick={setSeverity} />

      <textarea
        placeholder="What did the agent miss? (this is the most useful thing you'll write today)"
        value={note}
        onChange={(e) => setNote(e.target.value)}
      />
      <button type="submit" disabled={!canSubmit}>
        Save correction
      </button>
    </form>
  );
}

function SeverityPicker({ current, onPick }: {
  current: number;
  onPick: (severity: number) => void;
}) {
  return (
    <div className="sevpick">
      {[0, 1, 2, 3, 4].map((value) => (
        <button
          type="button"
          key={value}
          className={value === current ? "sv agent" : "sv"}
          onClick={() => onPick(value)}
        >
          S{value}
        </button>
      ))}
    </div>
  );
}

function currentOperator(): string {
  return document.body.dataset.operatorId ?? "unknown";
}
