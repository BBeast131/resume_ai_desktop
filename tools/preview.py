"""Render the app's screens to PNG files, offscreen, with sample data.

    python tools/preview.py out_dir

For checking the look against the design without a display.
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def sample(ctx) -> None:
    import random

    from tests.factories import add_job, add_resume

    random.seed(7)
    now = datetime.now(UTC)
    companies = ["Stripe", "OpenAI", "Coinbase", "Anthropic", "Amazon", "Google", "Datadog", "Figma", "Notion", "Ramp"]
    roles = ["Backend Engineer", "Software Engineer", "Staff Engineer", "Senior Engineer", "Platform Engineer"]
    for n in range(28):
        created = now - timedelta(days=random.randint(0, 6), hours=random.randint(0, 20))
        resume = add_resume(ctx.store, n, now=created, company=companies[n % 10], role=roles[n % 5])
        roll = random.random()
        if roll < 0.45:
            ctx.store.set_status(resume.id, "applied", now=created + timedelta(hours=2))
        if roll < 0.2:
            ctx.store.set_status(resume.id, "shortlisted", now=created + timedelta(hours=5))
        elif roll < 0.32:
            ctx.store.set_status(resume.id, "rejected", now=created + timedelta(hours=6))
    for n in range(100, 106):
        add_job(
            ctx.store,
            n,
            site="jobright" if n % 2 else "hiring_cafe",
            company=companies[n % 10],
            role=roles[n % 5],
            now=now - timedelta(days=106 - n),
        )
    job = ctx.store.list_saved_jobs()[2]
    ctx.store.set_attention(job.id, "page needs sign-in")


def main() -> int:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "preview")
    out.mkdir(parents=True, exist_ok=True)
    only = set(sys.argv[2:])
    os.environ["RESUME_AI_DATA_DIR"] = str(out / "_data")

    from PySide6.QtWidgets import QApplication

    app = QApplication(sys.argv[:1])
    import asyncio

    import qasync

    loop = qasync.QEventLoop(app, set_running_loop=True, already_running=True)
    asyncio.set_event_loop(loop)
    from app.ui.styles import apply_theme

    apply_theme(app)

    import shutil

    from tests.fake_api import make_context

    shutil.rmtree(out / "_data", ignore_errors=True)
    ctx = make_context(out / "_data", role="admin")
    sample(ctx)
    ctx.api.users = [
        {
            "id": "11111111-1111-4111-8111-111111111111",
            "fullName": "Anthony UI",
            "email": "anthonyui.dev@gmail.com",
            "role": "admin",
            "createdAt": "2026-06-01T10:00:00.000Z",
            "applicationCount": 28,
        },
        {
            "id": "22222222-2222-4222-8222-222222222222",
            "fullName": "John Smith",
            "email": "john@example.com",
            "role": "user",
            "createdAt": "2026-06-02T10:00:00.000Z",
            "applicationCount": 15,
        },
        {
            "id": "33333333-3333-4333-8333-333333333333",
            "fullName": "Sarah Lee",
            "email": "sarah@example.com",
            "role": "user",
            "createdAt": "2026-06-05T10:00:00.000Z",
            "applicationCount": 4,
        },
    ]

    from tools import preview_screens

    for name, widget in preview_screens.build(ctx, only):
        app.processEvents()
        widget.grab().save(str(out / f"{name}.png"))
        print("wrote", name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
