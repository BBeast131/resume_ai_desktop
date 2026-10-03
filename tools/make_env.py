"""Create `.env` from `.env.example`, filling the two public Supabase values.

The values are copied from the web app's `apps/web/.env.local` (they are the
ones every browser already receives). Nothing is printed except what happened:
no value is ever shown.

    python tools/make_env.py [path-to-web-.env.local]
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SOURCE = ROOT.parent / "resume_builder" / "apps" / "web" / ".env.local"
MAPPING = {
    "NEXT_PUBLIC_SUPABASE_URL": "SUPABASE_URL",
    "NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY": "SUPABASE_PUBLISHABLE_KEY",
}


def read_values(path: Path, names: set[str]) -> dict[str, str]:
    found: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        match = re.match(r"^\s*(?:export\s+)?([A-Z0-9_]+)\s*=\s*(.*?)\s*$", line)
        if not match or match.group(1) not in names:
            continue
        value = match.group(2)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if value:
            found[match.group(1)] = value
    return found


def is_filled(path: Path) -> bool:
    return len(read_values(path, set(MAPPING.values()))) == len(MAPPING)


def main() -> int:
    target = ROOT / ".env"
    if target.exists() and is_filled(target):
        print(".env is already filled in. Nothing to do.")
        return 0

    source = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_SOURCE
    if not source.exists():
        print(f"Could not find {source}.")
        print("Copy .env.example to .env and fill in SUPABASE_URL and SUPABASE_PUBLISHABLE_KEY yourself.")
        return 1

    values = read_values(source, set(MAPPING))
    missing = [name for name in MAPPING if name not in values]
    if missing:
        print("These are not set in the web app's .env.local: " + ", ".join(missing))
        return 1

    key = values["NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY"]
    if key.startswith("sb_secret_") or "service_role" in key:
        print("The publishable key in .env.local looks like a secret key. Not copied.")
        return 1
    if not values["NEXT_PUBLIC_SUPABASE_URL"].startswith("https://"):
        print("NEXT_PUBLIC_SUPABASE_URL is not an https address. Not copied.")
        return 1

    base = target if target.exists() else ROOT / ".env.example"
    text = base.read_text(encoding="utf-8-sig")
    for web_name, name in MAPPING.items():
        line = f"{name}={values[web_name]}"
        text, count = re.subn(rf"(?m)^{name}=.*$", lambda _m, line=line: line, text)
        if count == 0:
            text = text.rstrip("\n") + "\n" + line + "\n"
    target.write_text(text, encoding="utf-8", newline="\n")
    print(".env written: SUPABASE_URL and SUPABASE_PUBLISHABLE_KEY copied from the web app's .env.local.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
