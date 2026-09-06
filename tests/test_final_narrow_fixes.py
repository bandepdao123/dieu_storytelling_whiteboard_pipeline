import pytest
from qa_helpers import ready_qa

from du_pipeline.adapters import Role
from du_pipeline.contracts import QAEvidence
from du_pipeline.db import Database
from du_pipeline.service import Pipeline


def make_project(tmp_path, count, name="x"):
    p = Pipeline(Database(tmp_path / f"{name}.db"))
    pid = p.init_project(name, scene_range=(count, count))
    duration = count * 6000
    p.import_audio(pid, "audio.wav", duration, "a" * 64)
    p.import_srt(pid, [(i * 6000, (i + 1) * 6000, f"cue {i}") for i in range(count)])
    return p, pid, p.plan_scenes(pid)


@pytest.mark.parametrize("count", [50, 360])
def test_image_executor_streams_full_production_boundaries_without_queue_overflow(tmp_path, count):
    p, _pid, scenes = make_project(tmp_path, count, str(count))
    # This test isolates queue streaming; pilot gating is exercised by the vertical slice.
    p._pilot_ready = lambda _pid: True
    result = p.execute_images([s["id"] for s in scenes], {})
    assert len(result) == count
    assert all(s["state"] == "IMAGE_READY" for s in result)
    assert p.scheduler.queued == []
    assert p.scheduler.running["image"] == 0


def test_empty_project_cannot_cross_any_production_gate(tmp_path):
    p = Pipeline(Database(tmp_path / "empty.db")); pid = p.init_project("empty", scene_range=(1, 1))
    sheet = tmp_path / "sheet.png"; sheet.write_bytes(b"sheet")
    qa = QAEvidence({"all": True}, 1.0, "fake")
    with pytest.raises(PermissionError): p.start_batch(pid)
    with pytest.raises(PermissionError): p.approve_post_batch(pid, qa, str(sheet), "reviewer")
    p.db.execute("insert into approvals(project_id,gate,decision,actor,created_at) values(?,?,?,?,datetime('now'))", (pid, "POST_BATCH", "APPROVED", "forged"))
    with pytest.raises(PermissionError): p.start_animation(pid)


@pytest.mark.parametrize("role", [Role.OPERATOR, Role.OWNER])
@pytest.mark.parametrize("stage", ["image", "batch", "qa", "animation", "assembly", "final", "upload"])
def test_discord_stage_run_cannot_bypass_production_gates(tmp_path, role, stage):
    p = Pipeline(Database(tmp_path / f"{role.value}-{stage}.db")); pid = p.init_project("empty", scene_range=(1, 1))
    with pytest.raises((PermissionError, ValueError)):
        p.dispatch_discord(f"du-chay-cong-doan {pid} {stage}", role)
    assert not p.db.one("select 1 from jobs where project_id=?", (pid,))


def test_complete_fake_vertical_slice_persists_gate_and_delivery_artifacts(tmp_path):
    p, pid, scenes = make_project(tmp_path, 6, "slice")
    ids = [s["id"] for s in scenes]
    p.execute_images(ids[:5], {})
    passed = QAEvidence({"composition": True, "style": True}, .99, "fake-ai")
    for s in scenes[:5]:
        ready_qa(p,s)
        p.record_scene_qa(s["id"], passed); p.decide_scene(pid, s["code"], "APPROVED", "reviewer")
    assert p.start_batch(pid)
    p.execute_images(ids[5:], {})
    ready_qa(p,scenes[5])
    p.record_scene_qa(ids[5], passed)
    sheet = tmp_path / "contact.png"; sheet.write_bytes(b"contact-sheet")
    contact = p.approve_post_batch(pid, passed, str(sheet), "reviewer")
    assert contact.sha256
    assert p.start_animation(pid)
    animation = tmp_path / "animation.mp4"; animation.write_bytes(b"animation")
    animation_id = p.add_artifact(pid, "ANIMATION", str(animation))
    assembly = tmp_path / "final.mp4"; assembly.write_bytes(b"assembled-final")
    final_id = p.add_artifact(pid, "FINAL", str(assembly), parent_id=animation_id)
    final = p.db.one("select * from artifacts where id=?", (final_id,))
    assert final["parent_id"] == animation_id and final["sha256"]
    assert p.db.one("select 1 from approvals where project_id=? and gate='POST_BATCH' and decision='APPROVED'", (pid,))