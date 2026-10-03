"""The contract with the web app, checked against what the web app REALLY returned.

`fixtures/contract/push_body.json` is a body this app's sync engine produced;
`pull_result.json` is what the web app's real handlers answered when that body
was pushed, a status was changed on the web, and a pull followed (written by
the web repo's `desktop-contract.test.ts`). So neither side is tested against
a hand-written guess of the other.
"""

from __future__ import annotations

import json

from app.data.models import GeneratedResume
from app.data.types import from_iso
from tests.factories import make_store
from tests.helpers import FIXTURES

PUSH = json.loads((FIXTURES / "contract" / "push_body.json").read_text(encoding="utf-8"))
PULL = json.loads((FIXTURES / "contract" / "pull_result.json").read_text(encoding="utf-8"))


def test_a_fresh_pc_rebuilds_its_records_from_a_full_pull(tmp_path):
    store = make_store(tmp_path)
    merged = store.merge_remote(PULL["full"]["generatedResumes"], PULL["full"]["deleted"])
    assert merged.changed == len(PUSH["generatedResumes"]) and merged.missing == ()

    by_id = {row.id: row for row in store.list_resumes()}
    for sent in PUSH["generatedResumes"]:
        local = by_id[sent["id"]]
        assert (local.company, local.role, local.status) == (sent["company"], sent["role"], sent["status"])
        assert local.jd_text == sent["jdText"].strip()
        assert json.loads(local.resume_json) == json.loads(sent["resumeJson"])
        assert local.created_at == from_iso(sent["createdAt"])
        assert local.dirty is False and local.server_id
    assert isinstance(PULL["full"]["cursor"], str) and PULL["full"]["cursor"]


def test_a_status_changed_on_the_web_is_merged_from_a_status_only_pull(tmp_path):
    store = make_store(tmp_path)
    store.merge_remote(PULL["full"]["generatedResumes"], [])
    changed = PULL["changed"]["generatedResumes"]
    assert len(changed) == 1 and "resumeJson" not in changed[0]  # status-only: no heavy fields

    result = store.merge_remote(changed, PULL["changed"]["deleted"])
    assert result.changed == 1 and result.missing == ()
    local = store.get_resume(changed[0]["id"])
    assert local.status == "rejected"
    assert local.rejected_at is not None and local.applied_at is not None
    assert local.dirty is False


def test_a_status_only_pull_for_an_unknown_record_asks_for_the_full_one(tmp_path):
    store = make_store(tmp_path)
    result = store.merge_remote(PULL["changed"]["generatedResumes"], [])
    assert result.changed == 0 and result.missing == (PULL["changed"]["generatedResumes"][0]["id"],)
    assert store.resume_count() == 0


def test_the_push_body_has_exactly_the_fields_the_web_schema_names():
    resume_keys = {
        "id",
        "candidateName",
        "role",
        "company",
        "jdUrl",
        "applyUrl",
        "jdText",
        "chatUrl",
        "resumeJson",
        "status",
        "createdAt",
        "statusChangedAt",
        "appliedAt",
        "shortlistedAt",
        "rejectedAt",
    }
    job_keys = {"id", "sourceSite", "url", "role", "company", "attentionReason", "createdAt", "deletedAt"}
    assert all(set(row) == resume_keys for row in PUSH["generatedResumes"])
    assert all(set(row) == job_keys for row in PUSH["savedJobs"])
    assert set(PUSH) == {"schemaVersion", "accountId", "origin", "generatedResumes", "savedJobs"}
    assert "LOCAL-ONLY" not in json.dumps(PUSH)
    assert GeneratedResume.__table__.c.resume_json.type.python_type is str
