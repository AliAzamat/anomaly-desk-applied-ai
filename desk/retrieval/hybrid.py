"""Retrieval over the two corpora.

Hybrid, because the two halves fail differently. Vector search finds a chunk
about thermal runaway when the query says "oven too hot" and shares no words
with it. Lexical search finds `F-THERM-OVER` and `SPX-MP-4412`, which are exact
tokens a vector model will happily place near a dozen similar-looking codes.

An anomaly query contains both kinds of thing, always. It has natural language
from the operator and it has identifiers from the machine.
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from typing import Any, Callable

from desk.common.stations import STATION_INDEX, is_upstream_of
from desk.store import db


@dataclass
class Retrieved:
    kind: str                # "procedure" | "incident"
    source_id: str
    revision: str | None
    section: str | None
    text: str
    start_offset: int
    end_offset: int
    score: float
    # Why this was retrieved, for the trace. When an operator says the agent
    # cited something irrelevant, this is how you find out whether the
    # retriever or the model made the mistake.
    signals: dict[str, float]


# Reciprocal rank fusion constant. 60 is the value from the original RRF paper
# and it is a shallow optimum — the ranking barely moves between 40 and 80, so
# it is not worth tuning until something else is fixed.
RRF_K = 60

# Incident half-life. An incident from three years ago on equipment that has
# since been rebuilt is often actively misleading, so recency is a multiplier
# on relevance rather than a filter. A filter would throw away the one time
# this exact thing happened in 2019.
INCIDENT_HALFLIFE_DAYS = 240.0


def _rrf(rank: int) -> float:
    return 1.0 / (RRF_K + rank)


def _recency_weight(occurred_at: dt.datetime, now: dt.datetime) -> float:
    age_days = max((now - occurred_at).total_seconds() / 86400.0, 0.0)
    return 0.5 ** (age_days / INCIDENT_HALFLIFE_DAYS)


class HybridRetriever:
    def __init__(self, embed: Callable[[str], list[float]]) -> None:
        self._embed = embed

    def search(self, query: str, station: str, fault_code: str | None,
               k: int = 8, now: dt.datetime | None = None) -> list[Retrieved]:
        now = now or dt.datetime.now(dt.timezone.utc)
        vector = self._embed(query)

        proc = self._fuse(
            self._proc_lexical(query, limit=25),
            self._proc_vector(vector, limit=25),
        )
        inc = self._fuse(
            self._incident_lexical(query, station, fault_code, limit=25),
            self._incident_vector(vector, station, limit=25),
        )

        results: list[Retrieved] = []
        for row, score, signals in proc:
            results.append(Retrieved(
                kind="procedure",
                source_id=row["doc_id"],
                revision=row["revision"],
                section=row["section"],
                text=row["text"],
                start_offset=row["start_offset"],
                end_offset=row["end_offset"],
                score=score,
                signals=signals,
            ))
        for row, score, signals in inc:
            weight = _recency_weight(row["occurred_at"], now)
            # A resolution that did not hold is retrievable but heavily
            # demoted. It is real evidence — "we tried this and it came back" —
            # so deleting it loses information, but it must never outrank a
            # fix that worked.
            if not row["resolution_held"]:
                weight *= 0.25
            signals = {**signals, "recency": weight}
            results.append(Retrieved(
                kind="incident",
                source_id=row["incident_id"],
                revision=None,
                section=None,
                text=f"{row['narrative']}\n\nResolution: {row['resolution']}",
                start_offset=0,
                end_offset=len(row["narrative"]) + len(row["resolution"]) + 14,
                score=score * weight,
                signals=signals,
            ))

        results.sort(key=lambda r: r.score, reverse=True)
        return results[:k]

    def _fuse(self, lexical: list[dict], vectorish: list[dict]
              ) -> list[tuple[dict, float, dict[str, float]]]:
        """Reciprocal rank fusion over two ranked lists.

        RRF rather than a weighted score sum because the two scores are not
        commensurable. A cosine similarity of 0.82 and a ts_rank of 0.31 have
        no common unit, and any weighting you pick is a guess that stops being
        right the moment you change the embedding model. Ranks are comparable
        by construction.
        """
        key = "chunk_id" if lexical and "chunk_id" in lexical[0] else "incident_id"
        merged: dict[str, tuple[dict, float, dict[str, float]]] = {}

        for rank, row in enumerate(lexical, start=1):
            merged[row[key]] = (row, _rrf(rank), {"lexical_rank": float(rank)})
        for rank, row in enumerate(vectorish, start=1):
            ident = row[key]
            if ident in merged:
                existing_row, score, signals = merged[ident]
                merged[ident] = (existing_row, score + _rrf(rank),
                                 {**signals, "vector_rank": float(rank)})
            else:
                merged[ident] = (row, _rrf(rank), {"vector_rank": float(rank)})

        return sorted(merged.values(), key=lambda t: t[1], reverse=True)

    def _proc_lexical(self, query: str, limit: int) -> list[dict[str, Any]]:
        with db.conn() as c:
            return c.execute(
                """
                SELECT ch.chunk_id, ch.doc_id, ch.section, ch.text,
                       ch.start_offset, ch.end_offset, d.revision,
                       ts_rank(ch.tsv, plainto_tsquery('english', %s)) AS rank
                FROM procedure_chunk ch
                JOIN procedure_doc d ON d.doc_id = ch.doc_id
                -- Superseded procedures are excluded at the SQL level rather
                -- than filtered in Python. A revision that is no longer in
                -- force must not be retrievable at all; leaving it to
                -- application code means one forgotten call site cites it.
                WHERE d.superseded_at IS NULL
                  AND ch.tsv @@ plainto_tsquery('english', %s)
                ORDER BY rank DESC
                LIMIT %s
                """,
                (query, query, limit),
            ).fetchall()

    def _proc_vector(self, vector: list[float], limit: int) -> list[dict[str, Any]]:
        with db.conn() as c:
            return c.execute(
                """
                SELECT ch.chunk_id, ch.doc_id, ch.section, ch.text,
                       ch.start_offset, ch.end_offset, d.revision,
                       ch.embedding <=> %s::vector AS distance
                FROM procedure_chunk ch
                JOIN procedure_doc d ON d.doc_id = ch.doc_id
                WHERE d.superseded_at IS NULL
                ORDER BY distance ASC
                LIMIT %s
                """,
                (vector, limit),
            ).fetchall()

    def _incident_lexical(self, query: str, station: str,
                          fault_code: str | None, limit: int) -> list[dict[str, Any]]:
        # Incidents from THIS station and any station upstream of it. Downstream
        # incidents are excluded because they cannot have caused this anomaly,
        # the same physical constraint the classifier check encodes.
        candidates = [s for s in STATION_INDEX
                      if s == station or is_upstream_of(s, station)]
        with db.conn() as c:
            return c.execute(
                """
                SELECT incident_id, station, fault_code, occurred_at,
                       narrative, resolution, resolution_held,
                       ts_rank(tsv, plainto_tsquery('english', %s)) AS rank
                FROM incident
                WHERE station = ANY(%s)
                  AND (%s IS NULL OR fault_code = %s OR fault_code IS NULL)
                  AND tsv @@ plainto_tsquery('english', %s)
                ORDER BY rank DESC
                LIMIT %s
                """,
                (query, candidates, fault_code, fault_code, query, limit),
            ).fetchall()

    def _incident_vector(self, vector: list[float], station: str,
                         limit: int) -> list[dict[str, Any]]:
        candidates = [s for s in STATION_INDEX
                      if s == station or is_upstream_of(s, station)]
        with db.conn() as c:
            return c.execute(
                """
                SELECT incident_id, station, fault_code, occurred_at,
                       narrative, resolution, resolution_held,
                       embedding <=> %s::vector AS distance
                FROM incident
                WHERE station = ANY(%s)
                ORDER BY distance ASC
                LIMIT %s
                """,
                (vector, candidates, limit),
            ).fetchall()
