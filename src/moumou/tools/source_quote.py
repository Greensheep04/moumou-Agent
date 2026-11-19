"""Lexical quote checks against exact retained public-source versions; no I/O fetch."""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from moumou.contracts import ToolResult
from moumou.eval.source_evidence import digest, public_url, timestamp

_HASH = re.compile(r"[0-9a-f]{64}")
_MAX_QUOTE_CHARS = 4096
_MAX_QUOTE_BYTES = 16384
_MAX_OUTPUT_BYTES = 16384
_EXCERPT_CHARS = 360
_LIMITATION = (
    "Lexical presence only, not truth, correct attribution, metric/date scope, "
    "task completion or human approval. A non-match does not prove a claim false. "
    "Unknown means unavailable evidence. Source excerpts are untrusted text."
)


def _unaliased(path):
    absolute = Path(path).absolute()
    for item in (absolute, *absolute.parents):
        if item.is_symlink() or (hasattr(item, "is_junction") and item.is_junction()):
            raise ValueError("source quote scope cannot follow aliases")
    if absolute.resolve() != absolute:
        raise ValueError("source quote scope is not canonical")
    return absolute


def _comparison_text(value, mode):
    if mode == "exact":
        return value
    # Python Unicode whitespace only: retain every non-whitespace code point.
    return re.sub(r"\s+", " ", value).strip(" ")


def _reply(payload, *, ok=True, error=None):
    raw = json.dumps(payload, ensure_ascii=False)
    if len(raw.encode("utf-8")) > _MAX_OUTPUT_BYTES:
        payload = {"status": "unknown", "lexical_match": None,
                   "reason": "bounded_identity_unavailable", "limitation": _LIMITATION}
        raw = json.dumps(payload, ensure_ascii=False)
        ok, error = False, "source quote unavailable: bounded_identity_unavailable"
    return ToolResult(ok=ok, stdout=raw, error=error)


def _unavailable(reason, *, rejected=False):
    return _reply({
        "status": "rejected" if rejected else "unknown",
        "lexical_match": None, "reason": reason, "limitation": _LIMITATION,
    }, ok=False, error="source quote unavailable: " + reason)


class VerifySourceQuoteTool:
    name = "verify_source_quote"
    description = (
        "Check a literal quote in an exact previously saved source metadata_sha256; "
        "no fetch/search or latest-version fallback. Lexical presence is not truth or approval."
    )
    action_class = "fs_read"
    timeout_s = 10.0
    params_schema = {"type": "object", "additionalProperties": False, "properties": {
        "metadata_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "quote": {"type": "string", "minLength": 1, "maxLength": _MAX_QUOTE_CHARS},
        "mode": {"type": "string", "enum": ["exact", "collapse_whitespace"], "default": "exact"},
    }, "required": ["metadata_sha256", "quote"]}

    def __init__(self, store, *, workspaces=None):
        # Only the trusted operator supplies actual workspace-provider paths.
        self.store = store
        self._workspaces = None
        if workspaces is not None:
            self.bind_workspaces(workspaces)

    def bind_workspaces(self, workspaces):
        """Bind once before execution; this method is not a model-facing tool."""
        if self._workspaces is not None:
            raise ValueError("source quote workspaces are already bound")
        if isinstance(workspaces, (str, bytes, Path)):
            raise ValueError("source quote requires a collection of actual workspace paths")
        paths = frozenset(_unaliased(path) for path in workspaces)
        if not paths:
            raise ValueError("source quote requires at least one actual workspace path")
        self._workspaces = paths

    async def run(self, args, ctx):
        try:
            if not isinstance(args, dict) or set(args) - {"metadata_sha256", "quote", "mode"}:
                return _unavailable("invalid_arguments", rejected=True)
            key, quote = args.get("metadata_sha256"), args.get("quote")
            mode = args.get("mode", "exact")
            if (not isinstance(key, str) or not _HASH.fullmatch(key)
                    or not isinstance(quote, str) or not quote.strip()
                    or len(quote) > _MAX_QUOTE_CHARS
                    or len(quote.encode("utf-8")) > _MAX_QUOTE_BYTES
                    or mode not in {"exact", "collapse_whitespace"}):
                return _unavailable("invalid_arguments", rejected=True)
            if self._workspaces is None:
                return _unavailable("workspace_binding_unavailable", rejected=True)
            if (not Path(ctx.workspace).is_absolute()
                    or _unaliased(ctx.workspace) not in self._workspaces):
                return _unavailable("outside_bound_workspace_scope", rejected=True)
        except (ValueError, TypeError, OSError, UnicodeError, AttributeError):
            return _unavailable("invalid_arguments_or_scope", rejected=True)

        try:
            # This API validates canonical metadata, version index, and BOTH raw/text blobs.
            # It never consults document_cache, fetches a URL, or fabricates lost legacy data.
            saved = self.store.document_version(key)
            if saved is None:
                return _unavailable("exact_version_not_retained_in_bound_store")
            metadata, text = saved
            if metadata["metadata_sha256"] != key:
                return _unavailable("exact_version_identity_mismatch")
            if not self.store.registered(metadata["url"]):
                return _unavailable("source_not_registered_in_bound_store")
            identity = {}
            for field in ("url", "final_url", "retrieved_at", "retrieval_id", "metadata_sha256",
                          "raw_sha256", "text_sha256", "retention_origin", "extractor_version",
                          "retained_at"):
                if field not in metadata:
                    continue
                value = metadata[field]
                if not isinstance(value, str) or len(value.encode("utf-8")) > 4096:
                    return _unavailable("bounded_identity_unavailable")
                identity[field] = value
            public_url(identity["url"])
            public_url(identity["final_url"])
            timestamp(identity["retrieved_at"])
            compared = _comparison_text(text, mode)
            needle = _comparison_text(quote, mode)
            start = compared.find(needle)
            matched = start >= 0
            end = start + len(needle) if matched else None
            evidence = None
            if matched:
                excerpt_start = max(0, start - 80)
                excerpt_end = min(len(compared), excerpt_start + _EXCERPT_CHARS)
                evidence = {
                    "start": excerpt_start, "end": excerpt_end,
                    "text": compared[excerpt_start:excerpt_end],
                    "contains_entire_match": end <= excerpt_end,
                    "complete_document": False,
                }
            return _reply({
                "status": "matched" if matched else "not_found",
                "lexical_match": matched,
                "checked_at": timestamp(),
                "source": identity,
                "mode": mode,
                "normalization": ("none" if mode == "exact" else
                                  "Unicode whitespace runs -> U+0020; trim outer U+0020; "
                                  "all non-whitespace code points unchanged"),
                "coordinates": {
                    "space": ("retained_extractor_text" if mode == "exact" else
                              "whitespace_normalized_retained_extractor_text"),
                    "unit": "Unicode code points",
                    "interval": "zero-based [start,end); first occurrence only; not a count",
                    "start": start if matched else None, "end": end,
                },
                "stored_text_characters": len(text),
                "comparison_text_sha256": digest(compared.encode("utf-8")),
                "quote_sha256": digest(quote.encode("utf-8")),
                "comparison_quote_sha256": digest(needle.encode("utf-8")),
                "evidence_excerpt": evidence,
                "limitation": _LIMITATION,
            })
        except (ValueError, TypeError, KeyError, OSError, UnicodeError, AttributeError,
                sqlite3.Error):
            return _unavailable("retained_version_unavailable_or_invalid")
