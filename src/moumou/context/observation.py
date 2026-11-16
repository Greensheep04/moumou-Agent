"""Conservative presence observations from the exact messages sent to a model."""

from __future__ import annotations

import hashlib
from collections import Counter


def memory_prompt_observation(messages, hits, lessons_header):
    sections = []
    for message in messages:
        text = message.get("content")
        if message.get("role") == "system" and isinstance(text, str) and lessons_header in text:
            sections.append(text.split(lessons_header, 1)[1].split("\n## ", 1)[0])
    candidates = [(record.id, record.lesson,
                   hashlib.sha256(record.lesson.encode("utf-8")).hexdigest()) for record in hits]
    counts = Counter(digest for _, _, digest in candidates)
    bullets = [line[2:] for section in sections for line in section.splitlines()
               if line.startswith("- ")]
    matched, unknown = [], []
    for record_id, lesson, digest in candidates:
        # A prefix of a longer lesson is not evidence that this record was injected.
        # Multiline/nested bullets have ambiguous boundaries, so keep them unknown.
        ambiguous_prefix = any(other != lesson and lesson in other
                               for _, other, _ in candidates)
        full = bool(lesson) and "\n" not in lesson and "\r" not in lesson and lesson in bullets
        if full and counts[digest] == 1 and not ambiguous_prefix:
            matched.append({"id": record_id, "sha256": digest})
        else:
            prefix = lesson[:80]
            partial = len(prefix) >= 40 and any(prefix in section for section in sections)
            reason = ("ambiguous_lesson_identity" if full else
                      "partial_or_unknown" if partial else "not_observed")
            unknown.append({"id": record_id, "sha256": digest, "status": reason})
    return {
        "observation_method": "exact_single_line_system_lesson_bullet; multiline_unknown",
        "identity_basis": "unique current retrieval content; ID need not appear in the prompt",
        "matched_ids": [item["id"] for item in matched],
        "full_matches": matched,
        "partial_or_unknown": unknown,
        "section_sha256": [hashlib.sha256(text.encode("utf-8")).hexdigest()
                           for text in sections],
        "interpretation": "observed presence only; not model use, reward, or causal benefit",
    }
