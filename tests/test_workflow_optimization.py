import json
import pytest
from du_pipeline.adapters import Role
from du_pipeline.db import Database
from du_pipeline.service import Pipeline


def _planned(tmp_path, count):
    db = Database(tmp_path / f"{count}.db")
    p = Pipeline(db)
    pid = p.init_project("benchmark", scene_range=(1, 360))
    duration = count * 4000
    p.import_audio(pid, "audio.wav", duration, "a" * 64)
    p.import_srt(pid, [(i * 4000, (i + 1) * 4000, f"cue {i}") for i in range(count)])
    p.plan_scenes(pid)
    return db, p, pid


def test_status_summary_is_compact_and_does_not_embed_scenes(tmp_path):
    db, pipeline, pid = _planned(tmp_path, 360)
    summary = pipeline.status_summary(pid)
    encoded = json.dumps(summary, separators=(",", ":")).encode()
    assert "scenes" not in summary
    assert summary["scene_counts"]["total"] == 360
    assert summary["scene_counts"]["PLANNED"] == 360
    assert len(encoded) < len(json.dumps(pipeline.status(pid), separators=(",", ":")).encode())


@pytest.mark.parametrize("role", list(Role))
def test_summary_roles_query_ceiling_and_zero_writes(tmp_path, role):
    db, pipeline, pid = _planned(tmp_path, 50); selects=[]
    db.conn.set_trace_callback(lambda sql: selects.append(sql) if sql.lstrip().upper().startswith("SELECT") else None)
    before=db.conn.total_changes; result=pipeline.status_summary(pid, role)
    assert db.conn.total_changes == before
    assert len(selects) <= pipeline.STATUS_SUMMARY_QUERY_CEILING
    assert set(result) == {"project","scene_counts","approval_counts","qa_counts","gates","current_jobs","stale_job_count","indicators"}


def test_summary_invalid_role_and_missing_project(tmp_path):
    pipeline=Pipeline(Database(tmp_path/"empty.db"))
    with pytest.raises(PermissionError): pipeline.status_summary("missing", "ADMIN")
    with pytest.raises(ValueError, match="project not found"): pipeline.status_summary("missing", Role.OWNER)


def test_summary_lifecycle_counts_gates_jobs_and_bounded_text(tmp_path):
    db,pipeline,pid=_planned(tmp_path,50); stamp="2026-01-02T03:04:05+00:00"
    db.execute("update projects set name=?,state='BLOCKED',blocked_reason=? where id=?",("n"*500,"r"*1000,pid))
    db.execute("update scenes set state='IMAGE_READY',qa_state='PASS',approval_state='APPROVED' where ord<=5 and project_id=?",(pid,))
    evidence=pipeline._manifest_hash(pid)
    db.execute("insert into approvals(project_id,gate,decision,actor,created_at,project_version,evidence_sha256) values(?,?,?,?,?,?,?)",(pid,"POST_BATCH","APPROVED","a"*500,stamp,1,evidence))
    db.execute("insert into jobs(project_id,kind,state,created_at,updated_at,project_version) values(?,?,?,?,?,?)",(pid,"BATCH_IMAGE","RUNNING",stamp,stamp,1))
    db.execute("insert into jobs(project_id,kind,state,created_at,updated_at,project_version) values(?,?,?,?,?,?)",(pid,"OLD","FAILED",stamp,stamp,0))
    result=pipeline.status_summary(pid)
    assert len(result["project"]["name"]) <= pipeline.STATUS_TEXT_LIMITS["project_name"]
    assert len(result["project"]["blocked_reason"]) <= pipeline.STATUS_TEXT_LIMITS["blocked_reason"]
    assert result["project"]["blocked"] is True
    assert result["scene_counts"]["IMAGE_READY"]==5 and result["qa_counts"]["PASS"]==5
    assert result["approval_counts"]["APPROVED"]==5
    assert result["gates"]["post_batch"]["current"] is True
    assert {"kind":"BATCH_IMAGE","state":"RUNNING","count":1} in result["current_jobs"]
    assert result["stale_job_count"]==1
    assert all(set(x) <= {"code","timestamp"} for x in result["indicators"])



@pytest.mark.parametrize("state",["PAUSED","BLOCKED","COMPLETED"])
def test_summary_project_states(tmp_path,state):
    db,pipeline,pid=_planned(tmp_path,1)
    db.execute("update projects set state=?,blocked_reason=? where id=?",(state,"why" if state=="BLOCKED" else None,pid))
    result=pipeline.status_summary(pid)
    assert result["project"]["state"]==state
    assert result["project"]["blocked"]==(state=="BLOCKED")


def test_summary_bounds_adversarial_job_cardinality_and_excludes_stale_indicators(tmp_path):
    db,pipeline,pid=_planned(tmp_path,1); stamp="2026-01-02T03:04:05+00:00"
    for i in range(500):
        db.execute("insert into jobs(project_id,kind,state,created_at,updated_at,project_version) values(?,?,?,?,?,?)",
                   (pid,("K"*200)+str(i),"SUCCEEDED",stamp,stamp,1))
    for state in ("FAILED","BLOCKED"):
        db.execute("insert into jobs(project_id,kind,state,created_at,updated_at,project_version) values(?,?,?,?,?,?)",
                   (pid,"STALE_"+state,state,stamp,stamp,0))
    result=pipeline.status_summary(pid)
    assert len(result["current_jobs"]) == pipeline.STATUS_JOB_GROUP_LIMIT + 1
    assert result["current_jobs"][-1] == {"kind":"OTHER","state":"OTHER","count":493,"unknown_group_count":493}
    assert all(len(row["kind"]) <= pipeline.STATUS_TEXT_LIMITS["job_kind"] and len(row["state"]) <= pipeline.STATUS_TEXT_LIMITS["job_state"] for row in result["current_jobs"])
    assert result["stale_job_count"] == 2
    assert not {"JOB_FAILED","JOB_BLOCKED"} & {x["code"] for x in result["indicators"]}


def test_summary_current_failed_and_blocked_indicator_codes(tmp_path):
    db,pipeline,pid=_planned(tmp_path,1); stamp="2026-01-02T03:04:05+00:00"
    for state in ("FAILED","BLOCKED"):
        db.execute("insert into jobs(project_id,kind,state,created_at,updated_at,project_version) values(?,?,?,?,?,?)",
                   (pid,"CURRENT_"+state,state,stamp,stamp,1))
    assert {x["code"] for x in pipeline.status_summary(pid)["indicators"]} == {"JOB_FAILED","JOB_BLOCKED"}


def test_summary_gates_require_exact_current_evidence(tmp_path):
    db,pipeline,pid=_planned(tmp_path,2); stamp="2026-01-02T03:04:05+00:00"
    scenes=db.all("select * from scenes where project_id=? order by ord",(pid,))
    db.execute("update scenes set approval_state='APPROVED' where project_id=?",(pid,))
    for scene in scenes:
        evidence=pipeline._scene_evidence_hash(scene["id"])
        db.execute("insert into approvals(project_id,scene_id,gate,decision,actor,created_at,project_version,evidence_sha256) values(?,?,?,?,?,?,?,?)",(pid,scene["id"],"SCENE","APPROVED","reviewer",stamp,1,evidence))
    manifest=pipeline._manifest_hash(pid)
    for gate in ("POST_BATCH","FINAL"):
        db.execute("insert into approvals(project_id,gate,decision,actor,created_at,project_version,evidence_sha256) values(?,?,?,?,?,?,?)",(pid,gate,"APPROVED","reviewer",stamp,1,manifest))
    assert all(g["current"] for g in pipeline.status_summary(pid)["gates"].values())

    db.execute("update approvals set evidence_sha256=? where scene_id=?",("0"*64,scenes[0]["id"]))
    db.execute("update approvals set evidence_sha256=? where gate='POST_BATCH'",("1"*64,))
    db.execute("update approvals set project_version=0 where gate='FINAL'")
    gates=pipeline.status_summary(pid)["gates"]
    assert gates["pilot"]["current"] is False
    assert gates["post_batch"]["current"] is False
    assert gates["final"]["current"] is False


def test_summary_counts_null_project_version_job_as_stale(tmp_path):
    db,pipeline,pid=_planned(tmp_path,1); stamp="2026-01-02T03:04:05+00:00"
    db.execute("insert into jobs(project_id,kind,state,created_at,updated_at,project_version) values(?,?,?,?,?,NULL)",(pid,"LEGACY","FAILED",stamp,stamp))
    assert pipeline.status_summary(pid)["stale_job_count"] == 1
