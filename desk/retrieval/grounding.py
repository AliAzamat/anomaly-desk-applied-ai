"""Verify that a citation says what it claims to say.

This module is the reason the whole citation type exists. An agent that emits
'[SPX-MP-4412 §5.3]' next to a sentence has produced a citation-shaped token,
and nothing about it is a guarantee. This code goes and reads the document.

Three failure modes, in increasing order of how much they should worry you:

1. Offsets are wrong but the quote is really in the document somewhere.
   Usually a counting error. Recoverable, and we recover it.
2. The quote is a paraphrase of text that IS in the document.
   The claim may still be true. We measure how far off it is.
3. The quote is not in the document at all.
   The model fabricated supporting evidence. Ungrounded, no exceptions.
"""
from __future__ import annotations

import difflib
import re
import unicodedata

from desk.schemas.citation import Citation, GroundingResult
from desk.store import db

# How close a quote must be to the source text to count as grounded. Not 1.0:
# whitespace normalization, smart quotes, and a trimmed trailing period are
# formatting differences, not fabrications. Not much lower either, because
# every point you drop here is a paraphrase you have agreed to call a quote.
SIMILARITY_FLOOR = 0.92

# How far around the claimed offsets to look before deciding the quote is not
# where the model said. Generous, because offset arithmetic is exactly the kind
# of thing models are bad at and getting the offset wrong is not the failure
# we care about.
OFFSET_SEARCH_WINDOW = 600


def normalize(text: str) -> str:
    """Normalize away the differences that are not fabrication.

    NFKC folds typographic variants (curly quotes, ligatures) onto their plain
    forms. Whitespace collapses. Case is preserved, because a procedure that
    says SHALL and one that says shall are different documents in a regulated
    environment and we are not going to erase that here.
    """
    folded = unicodedata.normalize("NFKC", text)
    return re.sub(r"\s+", " ", folded).strip()


def _load_source_body(citation: Citation) -> str | None:
    with db.conn() as c:
        if citation.kind == "procedure":
            row = c.execute(
                """
                SELECT body, revision FROM procedure_doc
                WHERE doc_id = %s
                """,
                (citation.source_id,),
            ).fetchone()
            if row is None:
                return None
            # A citation to the wrong revision is treated as a missing source.
            # The text might match the old revision perfectly and still be
            # wrong guidance, which is the entire reason revisions exist.
            if row["revision"] != citation.revision:
                return None
            return row["body"]

        row = c.execute(
            "SELECT narrative, resolution FROM incident WHERE incident_id = %s",
            (citation.source_id,),
        ).fetchone()
        if row is None:
            return None
        return f"{row['narrative']}\n\nResolution: {row['resolution']}"


def verify(citation: Citation, index: int = 0) -> GroundingResult:
    body = _load_source_body(citation)
    if body is None:
        return GroundingResult(
            citation_index=index,
            grounded=False,
            reason=(f"source {citation.source_id!r} at revision "
                    f"{citation.revision!r} does not exist"),
        )

    quote = normalize(citation.quoted)
    normalized_body = normalize(body)

    # Fast path: the quote is verbatim in the document. Offsets may still be
    # wrong; we do not care, because the substantive claim is that this text
    # appears in this source and it does.
    if quote in normalized_body:
        return GroundingResult(
            citation_index=index, grounded=True,
            reason="exact match in source", similarity=1.0)

    # Slow path: look near the claimed offsets for the closest match.
    lo = max(citation.start_offset - OFFSET_SEARCH_WINDOW, 0)
    hi = min(citation.end_offset + OFFSET_SEARCH_WINDOW, len(body))
    window = normalize(body[lo:hi])

    best = _best_window_ratio(quote, window)
    if best >= SIMILARITY_FLOOR:
        return GroundingResult(
            citation_index=index, grounded=True,
            reason=f"near match near claimed offsets (ratio {best:.3f})",
            similarity=best)

    # Last resort: is it anywhere in the document at all? If it is, the model
    # got the offsets badly wrong but did not invent the evidence, and those
    # are different bugs that deserve different reactions.
    anywhere = _best_window_ratio(quote, normalized_body)
    if anywhere >= SIMILARITY_FLOOR:
        return GroundingResult(
            citation_index=index, grounded=True,
            reason=(f"matched elsewhere in source (ratio {anywhere:.3f}); "
                    f"claimed offsets {citation.start_offset}-"
                    f"{citation.end_offset} are wrong"),
            similarity=anywhere)

    return GroundingResult(
        citation_index=index, grounded=False,
        reason=(f"quote not found in source (best ratio {max(best, anywhere):.3f}); "
                f"treated as fabricated"),
        similarity=max(best, anywhere))


def _best_window_ratio(needle: str, haystack: str) -> float:
    """Best similarity of `needle` against any same-length window of haystack.

    Sliding by a quarter of the needle length rather than by one character.
    Exact-position precision is not needed — we only want to know whether text
    approximately like this exists — and the quarter-step is roughly 4x cheaper
    than a full scan with no measurable change in the verdict.
    """
    if not needle or not haystack:
        return 0.0
    n = len(needle)
    if n >= len(haystack):
        return difflib.SequenceMatcher(None, needle, haystack).ratio()

    step = max(n // 4, 1)
    best = 0.0
    for start in range(0, len(haystack) - n + 1, step):
        ratio = difflib.SequenceMatcher(
            None, needle, haystack[start:start + n]).ratio()
        if ratio > best:
            best = ratio
            if best >= 0.999:
                break
    return best


def verify_all(citations: list[Citation]) -> list[GroundingResult]:
    return [verify(c, i) for i, c in enumerate(citations)]


def grounding_rate(results: list[GroundingResult]) -> float:
    """Fraction of citations that verified.

    Returns 0.0 for no citations, not 1.0. A response with zero citations is
    maximally ungrounded, and returning 1.0 for the empty case would give the
    system a perfect grounding score by simply never citing anything.
    """
    if not results:
        return 0.0
    return sum(1 for r in results if r.grounded) / len(results)
