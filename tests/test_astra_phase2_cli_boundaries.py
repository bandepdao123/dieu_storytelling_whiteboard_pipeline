import json
import sqlite3
import pytest
from du_pipeline.db import Database, SchemaVersionError, SCHEMA_VERSION
from du_pipeline.service import Pipeline
from du_pipeline.cli import main
from du_pipeline.media import AssemblyError
from du_pipeline.integrations import IntegrationConfigurationError


def offline(path):
    c=sqlite3.connect(path)
    try: c.execute('PRAGMA journal_mode=DELETE')
    finally: c.close()


def test_wal_inspection_fails_without_sidecars(tmp_path,capsys):
    path=tmp_path/'db'
    with Database(path): pass
    before={p.name:p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}
    assert main(['--db',str(path),'status','p'])==2
    assert json.loads(capsys.readouterr().err)['error']['code']=='SCHEMA_UNSUPPORTED'
    assert before=={p.name:p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}


@pytest.mark.parametrize('command,method,exc', [('assemble','assemble',AssemblyError),('sync-sheet',None,IntegrationConfigurationError)])
def test_domain_errors_sanitized(tmp_path,capsys,monkeypatch,command,method,exc):
    path=tmp_path/'db'
    with Database(path): pass
    def fail(*a,**kw): raise exc('secret-provider-diagnostic')
    if method: monkeypatch.setattr(Pipeline,method,fail)
    else: monkeypatch.setattr('du_pipeline.cli.integration_client',fail)
    args=['--db',str(path),command,'p']+(['out'] if method else [])
    assert main(args)==2
    out=capsys.readouterr()
    assert 'secret' not in out.err
    assert json.loads(out.err)['error']['code'] in ('ASSEMBLY_REJECTED','INTEGRATION_ERROR')


def test_dry_assembly_uses_readonly_open(tmp_path,monkeypatch,capsys):
    path=tmp_path/'db'
    with Database(path): pass
    offline(path)
    def inspect(self,*args):
        assert self.db.readonly
        return {'dry_run':True}
    monkeypatch.setattr(Pipeline,'assemble',inspect)
    assert main(['--db',str(path),'assemble','p','out','--dry-run'])==0


def test_writer_open_no_migration_under_existing_writer(tmp_path):
    path=tmp_path/'db'
    with Database(path) as writer:
        with writer.transaction():
            with Database.open_existing(path) as other:
                assert other.one('PRAGMA user_version')[0]==SCHEMA_VERSION


def test_offline_inspection_coexists_with_reserved_writer(tmp_path):
    path=tmp_path/'db'
    with Database(path) as db: pid=Pipeline(db).init_project('x')
    offline(path)
    with Database.open_existing(path) as writer:
        with writer.transaction():
            with Database.open_existing(path,readonly=True) as reader:
                assert Pipeline(reader).status_summary(pid)['project']['id']==pid


def test_inspection_does_not_recreate_artifact_root(tmp_path,capsys):
    path=tmp_path/'db'
    with Database(path) as db:
        svc=Pipeline(db); pid=svc.init_project('x')
        root=svc._root_lookup(pid)
    if root.exists(): root.rmdir()
    offline(path)
    before=set(tmp_path.rglob('*'))
    for command in ('status','status-summary','report'):
        assert main(['--db',str(path),command,pid])==0
        capsys.readouterr()
    assert not root.exists()
    assert set(tmp_path.rglob('*'))==before


def test_explicit_migrate_and_missing_write(tmp_path,capsys):
    path=tmp_path/'db'
    assert main(['--db',str(path),'pause','p'])==2
    assert not path.exists()
    capsys.readouterr()
    assert main(['--db',str(path),'init','x'])==0
    capsys.readouterr()
    assert main(['--db',str(path),'migrate'])==0
    assert json.loads(capsys.readouterr().out)['schema_version']==SCHEMA_VERSION
