"""Audit regressions: public approvals, no SQL acceptance shortcuts."""
import pytest
from test_final_assembly import fixture, SuccessfulFakeAssembler
from du_pipeline.media import AssemblyError


def test_same_input_same_destination_verified_reuse(tmp_path):
    db,p,pid,root,*_=fixture(tmp_path)
    engine=SuccessfulFakeAssembler(root)
    first=p.assemble(pid,root/'final.mp4',assembler=engine)
    second=p.assemble(pid,root/'final.mp4',assembler=engine)
    assert second['reused'] and second['artifact_id']==first['artifact_id']


def test_publishing_final_does_not_invalidate_batch_input_evidence(tmp_path):
    db,p,pid,root,*_=fixture(tmp_path)
    before=p._manifest_hash(pid)
    p.assemble(pid,root/'final.mp4',assembler=SuccessfulFakeAssembler(root))
    assert p._manifest_hash(pid)==before
    assert p.status_summary(pid)['gates']['post_batch']['current']


def test_input_identity_excludes_destination_and_dry_run_tool_observation(tmp_path):
    db,p,pid,root,*_=fixture(tmp_path)
    engine=SuccessfulFakeAssembler(root)
    dry=p.assemble(pid,root/'preview.mp4',dry_run=True,assembler=engine)
    actual=p.assemble(pid,root/'final.mp4',assembler=engine)
    assert dry['manifest']['input_evidence_sha256']==actual['manifest']['input_evidence_sha256']


def test_reuse_revalidates_actual_media_qa(tmp_path):
    db,p,pid,root,*_=fixture(tmp_path)
    engine=SuccessfulFakeAssembler(root)
    p.assemble(pid,root/'final.mp4',assembler=engine)
    def reject(path,expected):
        raise AssemblyError('fresh QA rejected')
    engine.validate_final=reject
    with pytest.raises(AssemblyError,match='fresh QA rejected'):
        p.assemble(pid,root/'final.mp4',assembler=engine)


def test_final_review_requires_persisted_subject_bound_qa(tmp_path):
    from du_pipeline.contracts import QAEvidence
    db,p,pid,root,*_=fixture(tmp_path)
    engine=SuccessfulFakeAssembler(root)
    result=p.assemble(pid,root/'final.mp4',assembler=engine)
    aid=result['artifact_id']
    with pytest.raises((PermissionError,ValueError),match='QA'):
        p.review_final(pid,aid,'APPROVED','reviewer',assembler=engine)
    receipt=p.record_final_qa(pid,aid,QAEvidence({'visual':True},1,'reviewer'),assembler=engine)
    assert receipt['subject']['artifact_id']==aid
    p.review_final(pid,aid,'APPROVED','human',assembler=engine)
    assert p.status_summary(pid)['gates']['final']['current']
    (root/'final.mp4').write_bytes(b'tampered')
    with pytest.raises((PermissionError,ValueError,AssemblyError)):
        p.review_final(pid,aid,'APPROVED','human',assembler=engine)


@pytest.mark.parametrize('mutation',['manifest','legacy','output','audio','image'])
def test_reuse_rejects_tampering_and_legacy(tmp_path,mutation):
    import json
    db,p,pid,root,sid,aid,audio=fixture(tmp_path)
    engine=SuccessfulFakeAssembler(root)
    result=p.assemble(pid,root/'final.mp4',assembler=engine)
    if mutation in ('manifest','legacy'):
        m=result['manifest'].copy()
        if mutation=='legacy': m.pop('input_identity')
        else: m['ffprobe']={'forged':True}
        db.execute('update final_assemblies set manifest_json=? where artifact_id=?',(json.dumps(m),result['artifact_id']))
    elif mutation=='output': (root/'final.mp4').write_bytes(b'bad')
    elif mutation=='audio': audio.write_bytes(b'bad')
    else:
        row=db.one("select uri from artifacts where scene_id=? and kind='IMAGE' and status='ACTIVE'",(sid,))
        from pathlib import Path
        Path(row['uri']).write_bytes(b'bad')
    with pytest.raises((PermissionError,AssemblyError,ValueError)):
        p.assemble(pid,root/'final.mp4',assembler=engine)


def test_batch_qa_provenance_is_persisted_and_bound_to_assembly(tmp_path):
    db,p,pid,root,*_=fixture(tmp_path)
    result=p.assemble(pid,root/'final.mp4',assembler=SuccessfulFakeAssembler(root))
    receipt=result['manifest']['input_identity']['batch_qa']
    assert receipt['checks']=={'all':True}
    assert receipt['subject']['artifact_id']
    assert receipt['subject']['sha256']
    assert db.one("select 1 from events where type='POST_BATCH_QA_RECORDED'")


@pytest.mark.parametrize('mutation',['revoke','input_bytes'])
def test_reuse_rechecks_authorization_and_inputs_after_qa(tmp_path,mutation):
    db,p,pid,root,sid,aid,audio=fixture(tmp_path)
    engine=SuccessfulFakeAssembler(root)
    p.assemble(pid,root/'final.mp4',assembler=engine)
    original=engine.validate_final
    def mutate(path,expected):
        if mutation=='revoke':
            db.execute("update approvals set revoked_at='injected' where gate='POST_BATCH'")
        else: audio.write_bytes(b'changed during QA')
        return original(path,expected)
    engine.validate_final=mutate
    with pytest.raises((PermissionError,AssemblyError)):
        p.assemble(pid,root/'final.mp4',assembler=engine)


def test_final_qa_rechecks_input_bytes_after_output_validation(tmp_path):
    from du_pipeline.contracts import QAEvidence
    db,p,pid,root,sid,aid,audio=fixture(tmp_path)
    engine=SuccessfulFakeAssembler(root)
    result=p.assemble(pid,root/'final.mp4',assembler=engine)
    original=engine.validate_final
    def mutate(path,expected):
        audio.write_bytes(b'changed during final QA')
        return original(path,expected)
    engine.validate_final=mutate
    with pytest.raises((PermissionError,AssemblyError)):
        p.record_final_qa(pid,result['artifact_id'],QAEvidence({'ok':True},1,'test'),assembler=engine)
    assert not db.one("select 1 from events where type='FINAL_QA_RECORDED'")


@pytest.mark.parametrize('gate',['FINAL','PILOT','BATCH'])
def test_generic_gate_commands_reject_unsupported_success(tmp_path,gate):
    db,p,pid,root,*_=fixture(tmp_path)
    with pytest.raises(PermissionError):
        p.dispatch_discord('du-'+gate.lower()+'-approve '+pid,'REVIEWER')


@pytest.mark.parametrize('target',['audio','scene','hardlink'])
def test_reuse_never_accepts_input_alias(tmp_path,target):
    import os
    db,p,pid,root,sid,aid,audio=fixture(tmp_path)
    engine=SuccessfulFakeAssembler(root)
    p.assemble(pid,root/'final.mp4',assembler=engine)
    destination=audio if target=='audio' else root/'clip.mp4'
    if target=='hardlink':
        destination=root/'alias.mp4'
        os.link(audio,destination)
    before=destination.read_bytes()
    with pytest.raises(AssemblyError,match='aliases'):
        p.assemble(pid,destination,assembler=engine)
    assert destination.read_bytes()==before


@pytest.mark.parametrize('mutation',['legacy','wrong_project','failed_qa','revoked_batch','stale_config'])
def test_final_review_fail_closed(tmp_path,mutation):
    import json
    from du_pipeline.contracts import QAEvidence
    db,p,pid,root,*_=fixture(tmp_path)
    engine=SuccessfulFakeAssembler(root)
    result=p.assemble(pid,root/'final.mp4',assembler=engine)
    aid=result['artifact_id']
    p.record_final_qa(pid,aid,QAEvidence({'ok':True},1,'test'),assembler=engine)
    if mutation=='legacy':
        db.execute("update final_assemblies set manifest_json='{}' where artifact_id=?",(aid,))
    elif mutation=='wrong_project': pid=p.init_project('other',scene_range=(1,3))
    elif mutation=='failed_qa': p.record_final_qa(pid,aid,QAEvidence({'ok':False},0,'test'),assembler=engine)
    elif mutation=='revoked_batch': db.execute("update approvals set revoked_at='injected' where gate='POST_BATCH'")
    else: p.configure_project(pid,'transition','changed')
    with pytest.raises((PermissionError,AssemblyError,ValueError)):
        p.review_final(pid,aid,'APPROVED','human',assembler=engine)
    assert not p.status_summary(pid)['gates']['final']['current']


def test_symlink_to_registered_final_is_not_reuse(tmp_path):
    db,p,pid,root,*_=fixture(tmp_path)
    engine=SuccessfulFakeAssembler(root)
    p.assemble(pid,root/'final.mp4',assembler=engine)
    alias=root/'symlink.mp4'
    alias.symlink_to(root/'final.mp4')
    with pytest.raises((PermissionError,AssemblyError)):
        p.assemble(pid,alias,assembler=engine)


def test_unrelated_project_output_history_does_not_change_input_identity(tmp_path):
    db,p,pid,root,*_=fixture(tmp_path)
    engine=SuccessfulFakeAssembler(root)
    first=p.assemble(pid,root/'final.mp4',assembler=engine)
    history=root/'report.txt'; history.write_bytes(b'unrelated')
    p.add_artifact(pid,'REPORT',history)
    assert p.assemble(pid,root/'final.mp4',assembler=engine)['artifact_id']==first['artifact_id']


def test_final_without_output_and_persisted_qa_is_rejected(tmp_path):
    db,p,pid,root,*_=fixture(tmp_path)
    with pytest.raises((PermissionError,ValueError),match='final|FINAL'):
        p.decide_gate(pid,'FINAL','APPROVED','reviewer')
    assert not db.one("select 1 from approvals where gate='FINAL'")
