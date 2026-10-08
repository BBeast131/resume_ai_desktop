"""Which saved jobs are the same job?

Two listings are treated as one job when their company and role match after
normalising: the same posting is often found on JobRight and on HiringCafe, or
re-posted under a new link, and each copy would otherwise get its own resume.
(The same *link* saved twice is already refused when a job is saved.)

The key is deliberately conservative. It ignores case, accents, punctuation,
a legal suffix on the company ("Inc", "LLC") and a bracketed note on the role
("(Remote)"), and nothing else: "Senior Engineer" and "Engineer" at one
company stay two jobs. A listing without a company or a role has no key and is
never called a duplicate.
"""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

_BRACKETED = re.compile(r"\([^)]*\)|\[[^\]]*\]|\{[^}]*\}")
_NOT_WORD = re.compile(r"[^a-z0-9+#]+")
_COMPANY_SUFFIXES = frozenset(
    {"inc", "incorporated", "llc", "ltd", "limited", "corp", "corporation", "co", "company", "gmbh", "plc", "lp", "llp"}
)
_ROLE_WORDS = {"sr": "senior", "jr": "junior", "snr": "senior", "mgr": "manager", "eng": "engineer", "engr": "engineer"}
#: The separator between the two halves of a key; cannot occur inside either.
_SEPARATOR = "|"


def _words(text: str) -> list[str]:
    plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return _NOT_WORD.sub(" ", plain.casefold().replace("&", " and ")).split()


@lru_cache(maxsize=8192)
def company_key(company: str | None) -> str:
    words = _words(company or "")
    while len(words) > 1 and words[-1] in _COMPANY_SUFFIXES:
        words.pop()
    return " ".join(words)


@lru_cache(maxsize=8192)
def role_key(role: str | None) -> str:
    words = _words(_BRACKETED.sub(" ", role or ""))
    return " ".join(_ROLE_WORDS.get(word, word) for word in words)


def job_key(company: str | None, role: str | None) -> str | None:
    """The identity of a job for duplicate checks, or None when it cannot be told."""
    left, right = company_key(company), role_key(role)
    if not left or not right:
        return None
    return f"{left}{_SEPARATOR}{right}"
