from pathlib import Path
import pytest
from du_pipeline.db import Database
from du_pipeline.service import Pipeline


def test_project_plan_retry_audit_artifact_and_retention(tmp_path):
    db = Database(tmp_path / "x.db")
    p = Pipeline(db)
    pid = p.init_project("Demo", "vi", seed=7, scene_range=(1, 10))
    p.import_audio(pid, "audio.wav", 20_000, "a" * 64)
    p.import_srt(pid, [(0, 6_000, "mot"), (6_000, 12_000, "hai"), (12_000, 19_800, "ba")])
    scenes = p.plan_scenes(pid)
    assert scenes[0]["code"] == "S001" and scenes[0]["approval_state"] == "REQUIRED"
    sid = scenes[0]["id"]
    failed = tmp_path / "failed.bin"; failed.write_bytes(b"bad")
    for n in range(1, 4): p.record_image_attempt(sid, False, str(failed) if n == 1 else None, "oops")
    assert not failed.exists()
    assert p.scene(sid)["state"] == "BLOCKED"
    assert p.scene(scenes[1]["id"])["state"] != "BLOCKED"
    with pytest.raises(ValueError): p.record_image_attempt(sid, True)
    good = tmp_path / "good.bin"; good.write_bytes(b"ok")
    aid = p.add_artifact(pid, "image", str(good))
    row = db.one("select * from artifacts where id=?", (aid,))
    assert len(row["sha256"]) == 64 and row["version"] == 1
    p.observe_cost(pid, "image", "USD", "0.25")
    with pytest.raises(Exception): db.execute("update cost_observations set amount='9'")


def test_dependency_requires_approval_and_pause(tmp_path):
    p = Pipeline(Database(tmp_path / "x.db")); pid = p.init_project("D", "en")
    proposal = p.propose_dependency(pid, "style", {"scenes": ["S001"]})
    with pytest.raises(PermissionError): p.apply_dependency(proposal)
    p.decide_dependency(proposal, True, "owner"); p.apply_dependency(proposal)
    p.pause(pid); assert p.status(pid)["project"]["state"] == "PAUSED"
    p.resume(pid); assert p.status(pid)["project"]["state"] == "ACTIVE"
