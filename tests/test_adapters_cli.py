import json
from du_pipeline.adapters import SHEET_TABS, drive_folders, parse_discord, authorize, Role
from du_pipeline.cli import main


def test_contracts_discord_rbac():
    assert len(SHEET_TABS) == 11
    assert len(drive_folders("p1")) == 9
    cmd = parse_discord("du-phe-duyet S001 --ly-do tot")
    assert cmd.name == "approve" and cmd.args[0] == "S001"
    assert authorize(Role.REVIEWER, cmd.name)
    assert not authorize(Role.OPERATOR, cmd.name)


def test_cli_end_to_end(tmp_path, capsys):
    db = str(tmp_path / "p.db")
    assert main(["--db", db, "init", "Demo", "--language", "vi"]) == 0
    pid = json.loads(capsys.readouterr().out)["project_id"]
    # Strict filesystem-readonly inspection uses an offline rollback-journal copy.
    import sqlite3
    connection = sqlite3.connect(db)
    connection.execute('PRAGMA journal_mode=DELETE'); connection.close()
    assert main(["--db", db, "status", pid]) == 0
    assert json.loads(capsys.readouterr().out)["project"]["name"] == "Demo"


def test_report_cli_forwards_role(tmp_path, monkeypatch, capsys):
    seen = {}
    def report(self, pid, role):
        seen.update(pid=pid, role=role); return {"ok": True}
    monkeypatch.setattr("du_pipeline.cli.Pipeline.report", report)
    from du_pipeline.db import Database
    import sqlite3
    path = tmp_path/'p.db'
    with Database(path): pass
    connection=sqlite3.connect(path)
    connection.execute('PRAGMA journal_mode=DELETE'); connection.close()
    assert main(["--db", str(path), "--role", "REVIEWER", "report", "p1"]) == 0
    assert seen == {"pid": "p1", "role": "REVIEWER"}
