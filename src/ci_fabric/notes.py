"""Release-notes composer for ci-fabric.

Edits RELEASE_NOTES.md in place so a release's notes can be accumulated
incrementally instead of hand-edited at release time. The file is the same
one consumed by ``[release].changelog`` (default ``RELEASE_NOTES.md``).
"""

from __future__ import annotations

from pathlib import Path

from .util import read_text, write_text_atomic

_UNRELEASED = "## Unreleased"
_SECTIONS = ("Added", "Changed", "Fixed", "Removed", "Deprecated", "Security")


def _ensure_unreleased(text: str, section: str) -> str:
    if _UNRELEASED not in text:
        if text and not text.endswith("\n"):
            text += "\n"
        if text.strip():
            text += "\n"
        text += f"{_UNRELEASED}\n\n### {section}\n\n"
        return text
    # find the ### section under Unreleased
    idx = text.index(_UNRELEASED)
    # search for ### section between Unreleased and next ##
    rest = text[idx:]
    next_major = rest.find("\n## ", len(_UNRELEASED))
    block = rest if next_major == -1 else rest[:next_major]
    header = f"### {section}"
    if header not in block:
        # insert before the next major section (or append)
        insert_at = idx + (len(rest) if next_major == -1 else next_major)
        text = text[:insert_at] + f"\n### {section}\n\n" + text[insert_at:]
    return text


def add_note(path: Path, message: str, section: str = "Added") -> None:
    if section not in _SECTIONS:
        raise ValueError(f"unknown section {section!r}; choose from {_SECTIONS}")
    text = read_text(path) if path.is_file() else ""
    text = _ensure_unreleased(text, section)
    # find the position right after the ### section header within Unreleased
    idx = text.index(_UNRELEASED)
    rest = text[idx:]
    next_major = rest.find("\n## ", len(_UNRELEASED))
    block_end = idx + (len(rest) if next_major == -1 else next_major)
    header = f"### {section}"
    header_idx = text.index(header, idx)
    # find end of header line
    line_end = text.index("\n", header_idx) + 1
    # skip blank lines
    pos = line_end
    while pos < block_end and text[pos : pos + 1] == "\n":
        pos += 1
    bullet = f"- {message.strip()}\n"
    text = text[:pos] + bullet + text[pos:]
    write_text_atomic(path, text if text.endswith("\n") else text + "\n")


def clear_unreleased(path: Path) -> bool:
    if not path.is_file():
        return False
    text = read_text(path)
    idx = text.find(_UNRELEASED)
    if idx == -1:
        return False
    next_major = text.find("\n## ", idx + len(_UNRELEASED))
    text = text[:idx].rstrip() + "\n" if next_major == -1 else text[:idx] + text[next_major + 1 :]
    write_text_atomic(path, text if text.endswith("\n") else text + "\n")
    return True


def show(path: Path) -> str:
    return read_text(path) if path.is_file() else ""
