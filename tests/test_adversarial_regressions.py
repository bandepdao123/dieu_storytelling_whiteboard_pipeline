import sqlite3
from pathlib import Path

import pytest

from du_pipeline.db import Database
from du_pipeline.service import Pipeline
from du_pipeline.adapters import Role


@pytest.fixture
def app(tmp_path):
    db = Database(tmp_path / "state.db")
    pipe = Pipeline(db)
    pid = pipe.init_project("adversarial", scene_range=(1, 5))
    return pipe, db, pid, tmp_path


def fail_event(pipe, monkeypatch):
    monkeypatch.setattr(pipe, "_event", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("event fault")))


def test_resume_and_event_roll_back_together(app, monkeypatch):
    pipe, db, pid, _ = app
    pipe.pause(pid)
    fail_event(pipe, monkeypatch)
    with pytest.raises(RuntimeError): pipe.resume(pid)
    assert db.one("select state from projects where id=?", (pid,))[0] == "PAUSED"


def test_audio_and_event_roll_back_together(app, monkeypatch):
    pipe, db, pid, _ = app
    fail_event(pipe, monkeypatch)
    with pytest.raises(RuntimeError): pipe.import_audio(pid, "voice.wav", 1000, "a" * 64)
    assert db.one("select 1 from audio where project_id=?", (pid,)) is None


def test_dependency_and_event_roll_back_together(app, monkeypatch):
    pipe, db, pid, _ = app
    proposal = pipe.propose_dependency(pid, "renderer", {})
    pipe.decide_dependency(proposal, True, "owner")
    fail_event(pipe, monkeypatch)
    with pytest.raises(RuntimeError): pipe.apply_dependency(proposal)
    assert db.one("select state from impact_proposals where id=?", (proposal,))[0] == "APPROVED"


def test_add_artifact_removes_copied_orphan_on_event_failure(app, monkeypatch):
    pipe, db, pid, tmp = app
    source = tmp / "external.png"; source.write_bytes(b"image")
    root = Path(db.one("select artifact_root from projects where id=?", (pid,))[0])
    before = set(root.iterdir())
    fail_event(pipe, monkeypatch)
    with pytest.raises(RuntimeError): pipe.add_artifact(pid, "IMAGE", source)
    assert set(root.iterdir()) == before
    assert db.one("select 1 from artifacts where project_id=?", (pid,)) is None


def test_direct_job_and_event_primitives_are_closed(app):
    pipe, db, pid, _ = app
    with pytest.raises(PermissionError): pipe.create_job(pid, "FINAL")
    with pytest.raises(PermissionError): pipe.event(pid, "FORGED")
    assert db.one("select 1 from jobs where project_id=?", (pid,)) is None


def test_cancel_and_configuration_have_service_rbac(app):
    pipe, db, pid, _ = app
    with pytest.raises(PermissionError): pipe.cancel_project(pid, Role.OPERATOR)
    with pytest.raises(PermissionError): pipe.configure_project(pid, "language", "en", Role.OPERATOR)
    pipe.select_provider(pid, "local", Role.OPERATOR)
    assert db.one("select image_provider from projects where id=?", (pid,))[0] == "local"


def test_replace_scene_is_atomic_when_ingest_fails(app, monkeypatch):
    pipe, db, pid, tmp = app
    db.execute("insert into scenes(project_id,code,ord,start_ms,end_ms,text,special,state,approval_state) values(?,?,?,?,?,?,?,?,?)", (pid,"S001",1,0,1000,"x",0,"PLANNED","REQUIRED"))
    sid = db.one("select id from scenes where project_id=?", (pid,))[0]
    source = tmp / "one.png"; source.write_bytes(b"one")
    old = pipe.add_artifact(pid, "IMAGE", source, sid)
    monkeypatch.setattr(pipe, "_register_artifact", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("ingest fault")))
    with pytest.raises(RuntimeError): pipe.replace_scene_artifact(sid, source)
    assert db.one("select status from artifacts where id=?", (old,))[0] == "ACTIVE"


def test_replace_scene_removes_new_copy_when_final_audit_fails(app, monkeypatch):
    pipe, db, pid, tmp = app
    db.execute("insert into scenes(project_id,code,ord,start_ms,end_ms,text,special,state,approval_state) values(?,?,?,?,?,?,?,?,?)", (pid,"S001",1,0,1000,"x",0,"PLANNED","REQUIRED"))
    sid = db.one("select id from scenes where project_id=?", (pid,))[0]
    old_source = tmp / "old.png"; old_source.write_bytes(b"old")
    old = pipe.add_artifact(pid, "IMAGE", old_source, sid)
    old_uri = Path(db.one("select uri from artifacts where id=?", (old,))[0])
    root = Path(db.one("select artifact_root from projects where id=?", (pid,))[0])
    before = set(root.iterdir())
    new_source = tmp / "new.png"; new_source.write_bytes(b"new")
    real_event = pipe._event

    def fail_final_event(project_id, event_type, data=None):
        if event_type == "SCENE_ARTIFACT_REPLACED":
            raise RuntimeError("final audit fault")
        return real_event(project_id, event_type, data)

    monkeypatch.setattr(pipe, "_event", fail_final_event)
    with pytest.raises(RuntimeError, match="final audit fault"):
        pipe.replace_scene_artifact(sid, new_source)

    artifacts = db.all("select id,status,uri from artifacts where scene_id=?", (sid,))
    assert [(row["id"], row["status"]) for row in artifacts] == [(old, "ACTIVE")]
    assert old_uri.is_file()
    assert set(root.iterdir()) == before


def test_cross_project_artifact_update_is_rejected(app):
    pipe, db, pid, tmp = app
    other = pipe.init_project("other", scene_range=(1, 5))
    for project, code in ((pid,"S001"),(other,"S001")):
        db.execute("insert into scenes(project_id,code,ord,start_ms,end_ms,text,special,state,approval_state) values(?,?,?,?,?,?,?,?,?)", (project,code,1,0,1000,"x",0,"PLANNED","REQUIRED"))
    sid = db.one("select id from scenes where project_id=?", (pid,))[0]
    foreign_sid = db.one("select id from scenes where project_id=?", (other,))[0]
    source = tmp / "asset.png"; source.write_bytes(b"x")
    aid = pipe.add_artifact(pid, "IMAGE", source, sid)
    with pytest.raises(sqlite3.IntegrityError): db.execute("update artifacts set scene_id=? where id=?", (foreign_sid, aid))