"""Opt-in root-delivery closure checks; references never publish private files.

The teacher adapter calls this hook. It inspects only declared text documents,
does not crawl a workspace, and treats factual quality separately from file
closure. This is a bounded plain-text reader, not a full Markdown/browser parser.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
from pathlib import Path, PureWindowsPath
from urllib.parse import unquote, urljoin, urlsplit

from moumou.sandbox.local import LocalJail

CONTENT_QUALITY_RULES = """Quality protocol:
- Check externally verifiable claims against the actual cited passage, not a title, snippet or
  unsupported summary. Record the source, dates and limitations. Missing evidence is unverified,
  not false; distinguish observation, inference, assumption and proposal.
- Preserve each metric's measurement/as-of date and each rule's effective date. A publication
  date or system's inception does not date every number or current rule. Do not turn an
  example's duration into the metric's definition.
- Preserve units, populations and denominators; independently recompute arithmetic, percentages
  and percentage-point changes. Do not reverse conditional probabilities or transfer sample
  findings to another population without evidence. A plausible number is not evidence.
- Describe the source chain actually accessed. A first-party statement reproduced on a secondary
  page remains indirectly accessed; preserve the original speaker and the page read. Do not
  label it a direct primary-page read.
- Preserve narrower supported findings with their source limitations. Missing primary or
  comparable data neither erases a limited secondary report nor disproves every untested
  assumption. Evaluate each claim separately.
- Cross-check final prose and tables against the evidence after editing, including verification
  notes themselves. Describe emitted characters and selected-slice coverage accurately. Execution,
  structural checks, model DONE and self-checks are not factual truth or human acceptance.
- Another role's unseen history does not prove that it performed no checks. Use native
  provenance or state uncertainty. A fetched page, a served character window and understanding
  are different; do not claim full reading from a limited response.
- Resolve byline and publication-link placeholders. Omit unprovided values or mark an unpublished
  draft; never invent a person, URL, publication decision or verified value.
- Arrange peer review of final writer output with existing specialist roles and explicit file
  dependencies. Preserve the reviewed version; changed claims need review. An unchecked summary
  must not silently change a date, definition, denominator or conditional qualification.
- Derive checks from the requested outcome. Act within existing authorization; ask only about
  blocking ambiguity or new critical actions, not invented word/page quotas. Do not claim a
  minimum length, fixed report name or source count the requester never required.
- Inspect final bytes and local references. Declare intended supporting files/assets in
  result.files or correct/remove their references; never export private data merely because it
  was mentioned. Before repeating an edit, compare current bytes, correct only what remains
  and verify again. Keep unknowns and failures visible; never relabel them as success.
"""

_TEXT_SUFFIXES = frozenset({".md", ".txt", ".html", ".htm", ".css"})
_MAX_DOCUMENT_BYTES = 512 * 1024
_MAX_TOTAL_BYTES = 2 * 1024 * 1024
_MAX_DOCUMENTS = 64
_MAX_REFERENCES = 256
_INLINE_LINK = re.compile(r'!?\[[^\]\n]*\]\(\s*(<[^>\n]+>|(?:\\.|[^\s()])+)(?:\s+"[^"]*")?\s*\)')
_DEFINITION = re.compile(r"(?m)^ {0,3}\[([^\]\n]+)\]:\s*(<[^>\n]+>|\S+)[^\n]*")
_REFERENCE_USE = re.compile(r"!?\[([^\]\n]+)\](?:\[([^\]\n]*)\])?")
_URI = re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s<>`\"']+")
_WEB_CITATION = re.compile(
    r"(?i)(?<![\w/])(?:[a-z0-9-]+\.)+(?:com|org|net|edu|gov|io|hk|cn|ac)\b"
    r"(?:/[^\s<>`\"']*)?"
)
_MENTION = re.compile(
    r"(?i)(?<![\w/#:@])(?:[A-Z]:[\\/]|\.{1,2}[\\/])?"
    r"(?:[\w.-]+[\\/])*[\w.-]+\.(?:md|txt|html?)(?:[?#][^\s<>`\"')\]]*)?"
)
_READ_DIRECTIVE = re.compile(
    r"(?i)\b(?:see|read|refer|reports?|appendix|companion|supporting|attached|files?|artifacts?|sources?|synthesis)\b"
    r"|参见|详见|配套|附录|附件|报告|文件|阅读|请见|来源|参考|引文"
)
_CSS_URL = re.compile(r"(?i)url\(\s*(['\"]?)(.*?)\1\s*\)")
_CSS_IMPORT = re.compile(r"(?i)@import\s+(['\"])(.*?)\1")


@dataclass(frozen=True)
class _Reference:
    target: str
    line: int
    kind: str
    base: str = ""


class DeliveryQualityError(ValueError):
    """Correctable delivery feedback, never permission to export more files."""


def _mask(value: str) -> str:
    return "".join("\n" if char == "\n" else " " for char in value)


def _without_examples(text: str, *, skip_indented: bool = True) -> str:
    lines, fence = [], None
    for line in text.splitlines(keepends=True):
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if fence is not None:
            lines.append(_mask(line))
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= len(fence):
                fence = None
        elif marker:
            fence = marker[1]
            lines.append(_mask(line))
        elif skip_indented and re.match(r"^(?: {4}|\t)", line):
            lines.append(_mask(line))
        else:
            lines.append(line)
    text = "".join(lines)
    return re.sub(r"(?s)<!--.*?-->", lambda match: _mask(match[0]), text)


class _HTMLReferences(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.references: list[_Reference] = []
        self.skip = 0
        self.style = False
        self.base = ""

    def handle_starttag(self, tag, attributes):
        if tag in {"pre", "code"}:
            self.skip += 1
        if self.skip:
            return
        values = dict(attributes)
        if tag == "base" and not self.base:
            self.base = values.get("href") or ""
        if tag == "style":
            self.style = True
        for name in ("src", "poster"):
            if values.get(name):
                self.references.append(_Reference(values[name], self.getpos()[0], "html_asset"))
        if tag == "object" and values.get("data"):
            self.references.append(_Reference(values["data"], self.getpos()[0], "html_asset"))
        if tag != "base" and values.get("href"):
            self.references.append(_Reference(values["href"], self.getpos()[0], "html_link"))
        if values.get("srcset") and "data:" not in values["srcset"].casefold():
            for entry in values["srcset"].split(","):
                if entry.strip():
                    self.references.append(_Reference(
                        entry.split()[0], self.getpos()[0], "html_asset",
                    ))
        if values.get("style"):
            self._css(values["style"], self.getpos()[0])

    def handle_endtag(self, tag):
        if tag in {"pre", "code"} and self.skip:
            self.skip -= 1
        if tag == "style":
            self.style = False

    def handle_data(self, data):
        if self.style and not self.skip:
            self._css(data, self.getpos()[0])

    def _css(self, data, line):
        data = re.sub(r"(?s)/\*.*?\*/", lambda match: _mask(match[0]), data)
        for match in _CSS_URL.finditer(data):
            self.references.append(_Reference(match[2], line + data[:match.start()].count("\n"),
                                              "html_asset"))
        for match in _CSS_IMPORT.finditer(data):
            self.references.append(_Reference(match[2], line + data[:match.start()].count("\n"),
                                              "html_asset"))


def _references(text: str, *, css: bool = False, html: bool = False) -> list[_Reference]:
    text = _without_examples(text, skip_indented=not (css or html))
    parser = _HTMLReferences()
    if css:
        parser._css(text, 1)
    else:
        parser.feed(text)
    found = [_Reference(ref.target, ref.line, ref.kind, parser.base) for ref in parser.references]
    if css:
        return found
    plain = re.sub(r"(?is)<(pre|script|style)\b[^>]*>.*?</\1\s*>",
                   lambda match: _mask(match[0]), text)
    plain = re.sub(r"(?is)<code\b[^>]*>(.*?)</code\s*>", r"`\1`", plain)
    for match in _INLINE_LINK.finditer(plain):
        found.append(_Reference(match[1].strip("<>"), plain[:match.start()].count("\n") + 1,
                                "markdown_link"))
    plain = _INLINE_LINK.sub(lambda match: _mask(match[0]), plain)
    definitions = {
        match[1].casefold(): match[2].strip("<>") for match in _DEFINITION.finditer(plain)
    }
    plain = _DEFINITION.sub(lambda match: _mask(match[0]), plain)
    for match in _REFERENCE_USE.finditer(plain):
        label = (match[2] if match[2] else match[1]).casefold()
        if label in definitions:
            found.append(_Reference(definitions[label], plain[:match.start()].count("\n") + 1,
                                    "markdown_link"))
    plain = _URI.sub(lambda match: _mask(match[0]), plain)
    plain = _WEB_CITATION.sub(lambda match: _mask(match[0]), plain)
    plain = re.sub(r"<[^>]*>", lambda match: _mask(match[0]), plain)
    for number, line in enumerate(plain.splitlines(), 1):
        if not (_READ_DIRECTIVE.search(line) or _MENTION.fullmatch(line.strip(" -*•\t`'\""))):
            continue
        # Inline command snippets are examples, not claims that their arguments
        # are companion deliverables. Bare prose still supports explicit names.
        def inline(match, line_number=number):
            target = match[1]
            if (re.search(r"(?i)\.(?:md|txt|html?)(?:[?#].*)?$", target)
                    and not re.match(
                        r"(?i)^(?:python3?|cat|type|git|pytest|echo|cd|ls)\s", target)):
                found.append(_Reference(target, line_number, "text_reference"))
            return _mask(match[0])
        line = re.sub(r"`([^`\n]+)`", inline, line)
        for match in _MENTION.finditer(line):
            found.append(_Reference(match[0], number, "text_reference"))
    return found


def _is_secret(path: str) -> bool:
    return any(part.casefold() == ".env" or
               (part.casefold().startswith(".env.") and part.casefold() != ".env.example")
               for part in PureWindowsPath(path).parts)


def _local_target(reference: _Reference, source: Path, jail: LocalJail) -> Path | None:
    value = unescape(reference.target).strip().strip("<>")
    if not value or value.startswith(("#", "//")):
        return None
    if PureWindowsPath(value).drive or value.startswith("\\\\"):
        raise ValueError("absolute_local_reference")
    value = value.replace("\\", "/")
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc:
        if parsed.scheme.casefold() == "file":
            raise ValueError("file_uri_reference")
        return None
    if reference.base:
        value = urljoin(reference.base, value)
        parsed = urlsplit(value)
        if parsed.scheme or parsed.netloc:
            return None
    relative = unquote(parsed.path, errors="strict")
    if not relative:
        return None
    if relative.endswith("/"):
        return None  # Navigation directories are not file declarations.
    if relative.startswith("/"):
        if reference.kind == "text_reference":
            raise ValueError("absolute_local_reference")
        if not Path(relative).suffix:
            return None  # HTML/server routes such as /api/status are not files.
        path = relative.lstrip("/")
    else:
        path = (source.parent.relative_to(jail.root) / relative).as_posix()
    if _is_secret(path):
        raise PermissionError("private_secret_reference")
    resolved = jail.resolve_artifact(path)
    if _is_secret(resolved.relative_to(jail.root).as_posix()):
        raise PermissionError("private_secret_reference")
    return resolved


def validate_delivery(node, workspace: Path, result) -> None:
    """Validate explicit root document closure when a teacher adapter opts in.

    Nothing is added to result.files. Only declared text is read; referenced
    targets are checked through LocalJail without reading their content.
    """
    if node.parent_id is not None:
        return
    jail = LocalJail(Path(workspace))
    declared = set()
    for relative in result.files:
        if _is_secret(relative):
            raise DeliveryQualityError(
                "[private_declaration] Secret files cannot be a delivery source."
            )
        try:
            if Path(relative).is_absolute() or PureWindowsPath(relative).anchor:
                raise ValueError("declarations must be workspace-relative")
            resolved = jail.resolve_artifact(relative)
        except (OSError, ValueError, RuntimeError) as exc:
            raise DeliveryQualityError(f"[unsafe_declaration] {relative}: {exc}") from exc
        if _is_secret(jail.relative(resolved)):
            raise DeliveryQualityError(
                "[private_declaration] Secret files cannot be a delivery source."
            )
        declared.add(resolved)
    documents = sorted(path for path in declared if path.suffix.casefold() in _TEXT_SUFFIXES)
    if len(documents) > _MAX_DOCUMENTS:
        raise DeliveryQualityError(
            f"[scan_limit] More than {_MAX_DOCUMENTS} text documents; split delivery."
        )
    issues, total_bytes, references = [], 0, 0

    def count_reference(label):
        nonlocal references
        references += 1
        if references > _MAX_REFERENCES:
            issues.append(
                f"[scan_limit] {label}: too many reference candidates; simplify delivery."
            )
            return False
        return True

    for source in documents:
        label = jail.relative(source)
        if total_bytes >= _MAX_TOTAL_BYTES:
            issues.append(
                f"[scan_limit] {label}: total inspection limit reached; split delivery."
            )
            break
        try:
            with source.open("rb") as stream:
                raw = stream.read(min(_MAX_DOCUMENT_BYTES + 1, _MAX_TOTAL_BYTES - total_bytes + 1))
            total_bytes += len(raw)
            if len(raw) > _MAX_DOCUMENT_BYTES or total_bytes > _MAX_TOTAL_BYTES:
                issues.append(
                    f"[scan_limit] {label}: text exceeds bounded reference inspection; split it."
                )
                continue
            text = raw.decode("utf-8-sig")
        except (OSError, UnicodeError) as exc:
            issues.append(
                f"[unreadable_document] {label}: {type(exc).__name__}; provide readable UTF-8 text."
            )
            continue
        seen = set()
        for reference in _references(text, css=source.suffix.casefold() == ".css",
                                     html=source.suffix.casefold() in {".html", ".htm"}):
            key = (reference.target, reference.base, reference.kind)
            if key in seen:
                continue
            seen.add(key)
            location = f"{label}:{reference.line}"
            try:
                target = _local_target(reference, source, jail)
            except (OSError, ValueError, RuntimeError) as exc:
                if not count_reference(label):
                    break
                private = any(word in str(exc) for word in ("private", "protected_path"))
                classification = "private_reference" if private else "unsafe_reference"
                issues.append(
                    f"[{classification}] {location}: {reference.target!r}; "
                    "remove/correct this reference."
                )
                continue
            if target is None:
                continue
            if not count_reference(label):
                break
            relative = jail.relative(target)
            try:
                exists = target.is_file()
            except OSError as exc:
                issues.append(
                    f"[unreadable_reference] {location}: {relative!r} metadata unavailable "
                    f"({type(exc).__name__}); correct its access or remove the reference."
                )
                continue
            if not exists:
                issues.append(
                    f"[missing_reference] {location}: {relative!r} is not an existing file; "
                    "create and explicitly declare it, or correct/remove the reference."
                )
            elif target not in declared:
                issues.append(
                    f"[undeclared_reference] {location}: {relative!r} exists but is not in "
                    "result.files; explicitly include the intended public file "
                    "or correct/remove its reference."
                )
        if references > _MAX_REFERENCES:
            break
    if issues:
        details = "\n".join(issues[:12])
        remaining = f"\n... {len(issues) - 12} further issues." if len(issues) > 12 else ""
        raise DeliveryQualityError(
            "Final delivery references are incomplete or unsafe. "
            "No files were added or published.\n"
            + details + remaining
        )
