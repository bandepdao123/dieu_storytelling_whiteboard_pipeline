from pathlib import Path
import hashlib
import pytest
from test_cli_media_workflow import cli
import test_cli_media_workflow as workflow


def test_external_measured_full_media_subprocess(tmp_path,monkeypatch):
    original_cli=workflow.cli
    sources=[]
    def measured(db,*args,**kwargs):
        if args[0]=='import-audio':
            source=tmp_path/'external-narration.wav'
            data=Path(args[2]).read_bytes(); source.write_bytes(data)
            receipt=original_cli(db,'import-narration',args[1],source)
            assert receipt['sha256']==hashlib.sha256(data).hexdigest()
            assert receipt['duration_ms']==2000
            assert Path(receipt['uri']).read_bytes()==data
            sources.append((source,data))
            return receipt
        return original_cli(db,*args,**kwargs)
    monkeypatch.setattr(workflow,'cli',measured)
    workflow.test_real_cli_media_workflow(tmp_path)
    assert sources and all(p.read_bytes()==data for p,data in sources)

@pytest.mark.parametrize('case',['missing','invalid','unknown','role'])
def test_measured_cli_sanitized(tmp_path,case):
    db=tmp_path/'db'; pid=cli(db,'init','inputs')['project_id']
    source=tmp_path/'secret-file.wav'
    if case!='missing': source.write_bytes(b'invalid')
    args=['import-narration','unknown' if case=='unknown' else pid,source]
    if case=='role': args=['--role','REVIEWER',*args]
    result=cli(db,*args,ok=False)
    assert 'secret-file' not in str(result)
    assert result['error']['code'] in ('DATABASE_UNAVAILABLE','INVALID_REQUEST','NOT_ALLOWED')
