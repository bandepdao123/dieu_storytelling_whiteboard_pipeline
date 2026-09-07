"""Executable local CLI workflow: real media, no SQL approval mutation."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import wave
import pytest


def cli(db, *args, ok=True):
    result = subprocess.run([sys.executable, '-m', 'du_pipeline.cli', '--db', str(db), *map(str,args)], capture_output=True, text=True)
    assert result.returncode == (0 if ok else 2), result.stderr
    return json.loads(result.stdout if ok else result.stderr)


def test_real_cli_media_workflow(tmp_path):
    db=tmp_path/'local.db'
    pid=cli(db,'init','CLI media','--min-scenes',2,'--max-scenes',2)['project_id']
    assert cli(db,'status',pid,ok=False)['error']['code']=='SCHEMA_UNSUPPORTED'
    status=cli(db,'--operational-wal','status',pid)
    root=Path(status['project']['artifact_root'])
    audio=root/'voice.wav'
    with wave.open(str(audio),'wb') as w:
        w.setparams((1,2,8000,0,'NONE','not compressed'))
        w.writeframes(b'\0\0'*16000)
    cli(db,'import-audio',pid,audio,2000,hashlib.sha256(audio.read_bytes()).hexdigest())
    cues=tmp_path/'input.srt'
    cues.write_text('1\n00:00:00,000 --> 00:00:01,000\nfirst\n\n2\n00:00:01,000 --> 00:00:02,000\nsecond\n')
    cli(db,'import-srt',pid,cues)
    scenes=cli(db,'plan',pid)['scenes']
    assert len(scenes)==2
    qa=tmp_path/'qa.json'
    qa.write_text(json.dumps({'checks':{'visual':True},'score':1,'evaluator':'fixture-human'}))
    ppm=tmp_path/'image.ppm'
    ppm.write_bytes(b'P6\n16 16\n255\n'+bytes([100,20,30])*256)
    for s in scenes:
        cli(db,'register-artifact',pid,'IMAGE',ppm,'--scene-id',s['id'])
        cli(db,'image-attempt',s['id'],'succeeded')
        assert cli(db,'approve',pid,s['code'],ok=False)['error']['code']=='NOT_ALLOWED'
        cli(db,'scene-qa',s['id'],qa)
        cli(db,'approve',pid,s['code'],'--actor','fixture-human')
    contact=cli(db,'post-batch',pid,ppm,qa,'--actor','fixture-human')
    assert Path(contact['uri']).read_bytes()==ppm.read_bytes()
    from du_pipeline.db import Database
    with Database.open_existing(db,readonly=True,operational_wal=True) as inspect:
        for row in inspect.all('select qa_json from scenes'):
            evidence=json.loads(row['qa_json'])
            assert evidence['evaluator']=='external:fixture-human'
            assert evidence['subject']['sha256']
        evidence=json.loads(inspect.one("select data_json from events where type='POST_BATCH_QA_RECORDED'")[0])
        assert evidence['evaluator']=='external:fixture-human'
        assert evidence['subject']=={'artifact_id':contact['id'],'sha256':contact['sha256']}
    cli(db,'--role','OPERATOR','post-batch',pid,ppm,qa,'--actor','operator',ok=False)
    output=root/'final.mp4'
    final=cli(db,'assemble',pid,output)
    assert output.stat().st_size>0
    assert cli(db,'assemble',pid,output)['reused']
    aid=final['artifact_id']
    cli(db,'final-review',pid,aid,'APPROVED','--actor','fixture-human',ok=False)
    receipt=cli(db,'final-qa',pid,aid,qa)
    assert receipt['subject']['sha256']
    assert receipt['evaluator']=='external:fixture-human'
    cli(db,'final-review',pid,aid,'APPROVED','--actor','fixture-human')
    assert cli(db,'--operational-wal','status-summary',pid)['gates']['final']['current']


@pytest.mark.parametrize('payload',[[],{}, {'checks':{'x':1},'score':1,'evaluator':'x'}, {'checks':{'x':True},'score':True,'evaluator':'x'}, {'checks':{'x':True},'score':1,'evaluator':3}, {'checks':{'x':True},'score':1,'evaluator':' '}, {'checks':{'x':True},'score':1,'evaluator':'x','subject':{}}])
def test_invalid_external_qa(tmp_path,payload):
    db=tmp_path/'local.db'
    cli(db,'init','validation')
    qa=tmp_path/'qa.json'; qa.write_text(json.dumps(payload))
    assert cli(db,'scene-qa',1,qa,ok=False)['error']['code']=='INVALID_REQUEST'
