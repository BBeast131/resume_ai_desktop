"""Plain text from the files the user imports: the original resume and the prompt.

A port of the extension's `extract-resume-text.ts`, with the same clean-up and
the same limits. Everything happens on this machine; nothing is uploaded.

Errors are `ResumeTextError` with a message written for the person at the
keyboard. No message ever contains text from the file.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

MAX_RESUME_BYTES = 8 * 1024 * 1024
MAX_RESUME_CHARS = 250_000
#: The web app's `promptMax`.
MAX_PROMPT_CHARS = 200_000
MAX_PROMPT_BYTES = 4 * 1024 * 1024

RESUME_SUFFIXES = (".pdf", ".docx", ".txt", ".md")
PROMPT_SUFFIXES = (".txt", ".md")

# What JavaScript's `String.prototype.trim` removes (the reference trims with it).
_JS_WHITESPACE = "\t\n\x0b\x0c\r \xa0                　﻿"


class ResumeTextError(Exception):
    """The file could not be turned into usable text. The message is safe to show."""


def clean_extracted_text(text: str) -> str:
    """The reference's `cleanExtractedText`: tidy line ends and runs of blanks."""
    text = text.replace("\x00", "")
    text = text.replace("\r\n", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip(_JS_WHITESPACE)


def _read_bytes(path: Path, limit: int, too_large: str) -> bytes:
    try:
        if not path.is_file():
            raise ResumeTextError("That file could not be found.")
        if path.stat().st_size > limit:
            raise ResumeTextError(too_large)
        return path.read_bytes()
    except OSError as error:
        raise ResumeTextError("That file could not be opened.") from error


def _decode_text(data: bytes) -> str:
    """UTF-8 (with or without a BOM), UTF-16 with a BOM, else Windows-1252."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError:
            pass
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        # Notepad's old "ANSI" default. Bytes cp1252 leaves undefined are replaced.
        text = data.decode("cp1252", errors="replace")
    if "\x00" in text:
        raise ResumeTextError("That does not look like a text file.")
    return text


def _pdf_text(path: Path) -> str:
    from pypdf import PdfReader

    try:
        reader = PdfReader(str(path))
        if reader.is_encrypted:
            try:
                unlocked = reader.decrypt("")
            except Exception as error:  # a missing crypto backend raises here
                raise ResumeTextError("That PDF is password-protected. Save an unprotected copy first.") from error
            if not unlocked:
                raise ResumeTextError("That PDF is password-protected. Save an unprotected copy first.")
        pages = [page.extract_text() or "" for page in reader.pages]
    except ResumeTextError:
        raise
    except Exception as error:  # a damaged PDF fails in many different ways
        raise ResumeTextError("Could not read that PDF.") from error
    return "\n\n".join(pages)


def _docx_text(path: Path) -> str:
    from docx import Document
    from docx.document import Document as DocumentObject
    from docx.table import Table, _Cell
    from docx.text.paragraph import Paragraph

    def blocks(container: DocumentObject | _Cell) -> Iterator[str]:
        # Paragraphs and tables in document order; a resume laid out as a
        # table keeps its reading order.
        for item in container.iter_inner_content():
            if isinstance(item, Paragraph):
                yield item.text
            elif isinstance(item, Table):
                # A merged cell is reported once per column and row it spans.
                # The elements are kept (not just their ids) so an id cannot be reused.
                seen: dict[int, object] = {}
                for row in item.rows:
                    for cell in row.cells:
                        element = cell._tc
                        if id(element) in seen:
                            continue
                        seen[id(element)] = element
                        yield from blocks(cell)

    try:
        document = Document(str(path))
        return "\n".join(blocks(document))
    except OSError as error:
        raise ResumeTextError("Could not read that Word document.") from error
    except Exception as error:  # a damaged archive fails in many different ways
        raise ResumeTextError("That file does not look like a valid .docx resume.") from error


def extract_resume_text(path: Path) -> str:
    """Plain text of an original resume: .pdf, .docx, .txt or .md."""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix not in RESUME_SUFFIXES:
        raise ResumeTextError("Choose a .pdf, .docx or .txt resume file.")

    data = _read_bytes(path, MAX_RESUME_BYTES, "That resume file is too large (limit 8 MB).")

    if suffix == ".pdf":
        text = _pdf_text(path)
    elif suffix == ".docx":
        text = _docx_text(path)
    else:
        text = _decode_text(data)

    cleaned = clean_extracted_text(text)
    if not cleaned:
        if suffix == ".pdf":
            raise ResumeTextError("Could not read any text from that file. Scanned image PDFs are not supported.")
        raise ResumeTextError("Could not read any text from that file. It looks empty.")
    if len(cleaned) > MAX_RESUME_CHARS:
        raise ResumeTextError(f"Extracted text is {len(cleaned):,} characters. The limit is {MAX_RESUME_CHARS:,}.")
    return cleaned


def read_prompt_file(path: Path) -> str:
    """The generation prompt from a .txt or .md file, trimmed as the web app stores it."""
    path = Path(path)
    if path.suffix.lower() not in PROMPT_SUFFIXES:
        raise ResumeTextError("Choose a .txt or .md prompt file.")

    data = _read_bytes(path, MAX_PROMPT_BYTES, "That prompt file is too large.")
    text = _decode_text(data).replace("\r\n", "\n").strip(_JS_WHITESPACE)
    if not text:
        raise ResumeTextError("The prompt file is empty.")
    if len(text) > MAX_PROMPT_CHARS:
        raise ResumeTextError(f"The prompt is {len(text):,} characters. The limit is {MAX_PROMPT_CHARS:,}.")
    return text
