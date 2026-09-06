import json
import threading
from datetime import datetime, timedelta, timezone

from du_pipeline.db import Database
from du_pipeline.integrations import DiscordBridge
from du_pipeline.service import Pipeline


def _expire(db, artifact_id):
    db.execute("update artifacts set expires_at=? where id=?", ((datetime.now(timezone.utc) - timedelta(days=1)).isoformat(), artifact_id))


def test_shared_uri_cleanup_and_reconcile_are_reference_counted(tmp_path):
    with Database(tmp_path / "db.sqlite") as db:
        pipeline = Pipeline(db)
        pid = pipeline.init_project("shared")
        path = pipeline._root(pid) / "shared.bin"
        path.write_bytes(b"shared")
        old_id = pipeline.add_artifact(pid, "IMAGE", path)
        row = db.one("select * from artifacts where id=?", (old_id,))
        live_id = db.execute("insert into artifacts(project_id,scene_id,kind,uri,sha256,version,parent_id,status,created_at,expires_at) values(?,?,?,?,?,?,?,?,?,?)", (pid, None, "ANIMATION", str(path), row["sha256"], 7, None, "SUPERSEDED", row["created_at"], "2999-01-01T00:00:00+00:00")).lastrowid
        _expire(db, old_id)
        assert pipeline.cleanup() == 1
        assert db.one("select status from artifacts where id=?", (old_id,))["status"] == "DELETED"
        assert path.read_bytes() == b"shared"

        # Rename crash with mixed statuses: every row, not an arbitrary row, decides.
        staged = path.with_name(path.name + ".deleting-crash")
        path.replace(staged)
        pipeline.reconcile_deletions()
        assert path.read_bytes() == b"shared" and not staged.exists()
        db.execute("update artifacts set status='DELETED',deleted_at=? where id=?", (datetime.now(timezone.utc).isoformat(), live_id))
        path.replace(staged)
        pipeline.reconcile_deletions()
        assert not staged.exists() and not path.exists()


def test_concurrent_shared_uri_cleanup_serializes_transition_and_delete(tmp_path):
    db_path = tmp_path / "concurrent.sqlite"
    with Database(db_path) as setup:
        pipeline = Pipeline(setup)
        pid = pipeline.init_project("concurrent-shared")
        path = pipeline._root(pid) / "shared.bin"
        path.write_bytes(b"shared")
        first = pipeline.add_artifact(pid, "IMAGE", path)
        row = setup.one("select * from artifacts where id=?", (first,))
        second = setup.execute("insert into artifacts(project_id,scene_id,kind,uri,sha256,version,parent_id,status,created_at,expires_at) values(?,?,?,?,?,?,?,?,?,?)", (pid, None, "ANIMATION", str(path), row["sha256"], 2, None, "SUPERSEDED", row["created_at"], row["expires_at"])).lastrowid
        _expire(setup, first); _expire(setup, second)
    results, errors = [], []
    def worker():
        try:
            with Database(db_path) as db:
                results.append(Pipeline(db).cleanup())
        except Exception as exc: errors.append(exc)
    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads: thread.start()
    for thread in threads: thread.join(timeout=10)
    assert not any(thread.is_alive() for thread in threads)
    # The SQLite reconciliation lease deliberately fails closed: one invocation
    # owns all filesystem work and a racing invocation is rejected.
    from du_pipeline.db import LeaseUnavailable
    assert sum(results) == 2
    # Depending on scheduling, the second pass either starts after release or
    # overlaps and fails closed; neither path duplicates filesystem work.
    assert len(errors) <= 1 and all(isinstance(e, LeaseUnavailable) for e in errors)
    with Database(db_path) as check:
        assert [r["status"] for r in check.all("select status from artifacts order by id")] == ["DELETED", "DELETED"]
    assert not path.exists()
    assert list(path.parent.glob(path.name + ".deleting-*")) == []


def test_reconcile_multiple_staged_files_validates_content(tmp_path):
    with Database(tmp_path / "multiple.sqlite") as db:
        pipeline = Pipeline(db)
        pid = pipeline.init_project("multiple-staged")
        original = pipeline._root(pid) / "asset.bin"
        original.write_bytes(b"tracked")
        artifact = pipeline.add_artifact(pid, "IMAGE", original)
        original.unlink()
        good_a = original.with_name(original.name + ".deleting-a")
        good_b = original.with_name(original.name + ".deleting-b")
        bad = original.with_name(original.name + ".deleting-c")
        good_a.write_bytes(b"tracked"); good_b.write_bytes(b"tracked"); bad.write_bytes(b"other")
        pipeline.reconcile_deletions()
        assert original.read_bytes() == b"tracked"
        assert not good_a.exists() and not good_b.exists()
        assert bad.read_bytes() == b"other"
        db.execute("update artifacts set status='DELETED',deleted_at=? where id=?", (datetime.now(timezone.utc).isoformat(), artifact))
        original.replace(good_a); good_b.write_bytes(b"tracked")
        pipeline.reconcile_deletions()
        assert not original.exists()
        assert not good_a.exists() and not good_b.exists() and not bad.exists()
        pipeline.reconcile_deletions()


def test_add_artifact_gives_each_row_exclusive_managed_uri(tmp_path):
    with Database(tmp_path / "db.sqlite") as db:
        pipeline = Pipeline(db)
        pid = pipeline.init_project("registration")
        source = pipeline._root(pid) / "same.bin"
        source.write_bytes(b"same")
        first = pipeline.add_artifact(pid, "IMAGE", source)
        second = pipeline.add_artifact(pid, "VIDEO", source)
        one = db.one("select uri from artifacts where id=?", (first,))["uri"]
        two = db.one("select uri from artifacts where id=?", (second,))["uri"]
        assert one != two
        assert open(one, "rb").read() == open(two, "rb").read() == b"same"


def test_scheduled_cleanup_reconciles_nested_staging_without_touching_active(tmp_path):
    with Database(tmp_path / "db.sqlite") as db:
        pipeline = Pipeline(db)
        pid = pipeline.init_project("nested")
        root = pipeline._root(pid)
        nested = root / "scenes" / "S001"
        nested.mkdir(parents=True)
        deleted = nested / "old.png"
        active = nested / "active.png"
        deleted.write_bytes(b"old")
        active.write_bytes(b"active")
        deleted_id = pipeline.add_artifact(pid, "IMAGE", deleted)
        pipeline.add_artifact(pid, "IMAGE", active)
        db.execute("update artifacts set status='DELETED',deleted_at=? where id=?", (datetime.now(timezone.utc).isoformat(), deleted_id))
        staged = deleted.with_name(deleted.name + ".deleting-crash")
        deleted.replace(staged)

        handler = pipeline.cleanup_schedule()["handler"]
        assert handler() == 0
        assert not staged.exists()
        assert active.read_bytes() == b"active"
        assert handler() == 0  # idempotent


def test_nested_reconciliation_restores_valid_active_and_rejects_symlink(tmp_path):
    with Database(tmp_path / "db.sqlite") as db:
        pipeline = Pipeline(db)
        pid = pipeline.init_project("nested")
        root = pipeline._root(pid)
        nested = root / "a" / "b"
        nested.mkdir(parents=True)
        original = nested / "active.bin"
        original.write_bytes(b"valid")
        pipeline.add_artifact(pid, "IMAGE", original)
        staged = original.with_name(original.name + ".deleting-crash")
        original.replace(staged)
        outside = tmp_path / "outside"
        outside.mkdir()
        escaped = outside / "victim.deleting-x"
        escaped.write_bytes(b"outside")
        (root / "linked").symlink_to(outside, target_is_directory=True)

        pipeline.reconcile_deletions()
        assert original.read_bytes() == b"valid"
        assert not staged.exists()
        assert escaped.read_bytes() == b"outside"


def test_v7_migration_blocks_unknown_legacy_discord_processing(tmp_path):
    path = tmp_path / "legacy.sqlite"
    db = Database(path)
    pipeline = Pipeline(db)
    pipeline.init_project("migration")
    db.execute("insert into discord_messages(message_id,user_id,response_json,created_at,state,lease_owner,lease_expires_at) values(?,?,NULL,?,'PROCESSING','old-worker','2000-01-01T00:00:00+00:00')", ("legacy", "user", datetime.now(timezone.utc).isoformat()))
    db.execute("pragma user_version=6")
    db.close()

    with Database(path) as migrated:
        row = migrated.one("select * from discord_messages where message_id='legacy'")
        assert row["state"] == "MANUAL_REVIEW"
        assert row["error"] == "V7_LEGACY_PROCESSING_SIDE_EFFECT_UNKNOWN"
        assert row["lease_owner"] is None and row["lease_expires_at"] is None
        pipe = Pipeline(migrated)
        bridge = DiscordBridge(pipe, {"user": "owner"})
        pipe.dispatch_discord = lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not replay"))
        result = bridge.dispatch("legacy", "user", "/project-create replay")
        assert result["state"] == "MANUAL_REVIEW"
        assert migrated.one("select outcome from integration_attempts order by id desc")["outcome"] == "MANUAL_REVIEW"
