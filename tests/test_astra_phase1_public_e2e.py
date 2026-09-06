"""Public API real-media acceptance; no SQL approval shortcuts or media skips."""
import pytest
from test_astra_f02_f04 import workflow, approve, batch, image, QA
from du_pipeline.media import AssemblyError


def test_real_rerun_assembly_new_output_then_image_epoch(workflow):
    from test_astra_f07 import rerun
    p,pid,root,scenes,ids=workflow
    approve(p,pid,scenes); batch(p,pid,root)
    first=p.assemble(pid,root/'before.mp4')
    p.record_final_qa(pid,first['artifact_id'],QA)
    p.review_final(pid,first['artifact_id'],'APPROVED','human')
    rerun(p,pid,'upload')
    assert p.status_summary(pid)['gates']['final']['current']
    assert p.assemble(pid,root/'before.mp4')['reused']
    rerun(p,pid,'assembly')
    assert p.status_summary(pid)['gates']['pilot']['current']
    assert not p.status_summary(pid)['gates']['final']['current']
    with pytest.raises((PermissionError,AssemblyError)): p.assemble(pid,root/'before.mp4')
    batch(p,pid,root)
    second=p.assemble(pid,root/'after.mp4')
    assert not second['reused'] and second['artifact_id']!=first['artifact_id']
    assert p.assemble(pid,root/'after.mp4')['reused']
    p.record_final_qa(pid,second['artifact_id'],QA)
    p.review_final(pid,second['artifact_id'],'APPROVED','human')
    rerun(p,pid,'image')
    assert not p.status_summary(pid)['gates']['pilot']['current']
    for s in scenes:
        receipt=p.allocate_attempt_scratch(s['id'],b'dummy failure')
        p.record_image_attempt(s['id'],False,scratch_receipt=receipt['token'])
        p.replace_scene_artifact(s['id'],image(root/(s['code']+'-new.ppm'),100))
        p.record_image_attempt(s['id'],True)
        p.record_scene_qa(s['id'],QA)
    approve(p,pid,scenes); batch(p,pid,root)
    third=p.assemble(pid,root/'regenerated.mp4')
    assert not third['reused']
    p.record_final_qa(pid,third['artifact_id'],QA)
    p.review_final(pid,third['artifact_id'],'APPROVED','human')
    assert p.status_summary(pid)['gates']['final']['current']


def test_real_multiscene_reuse_persisted_qa_final_review(workflow):
    p,pid,root,scenes,ids=workflow
    approve(p,pid,scenes)
    batch(p,pid,root)
    dry=p.assemble(pid,root/'final.mp4',dry_run=True)
    result=p.assemble(pid,root/'final.mp4')
    assert dry['manifest']['input_evidence_sha256']==result['manifest']['input_evidence_sha256']
    reused=p.assemble(pid,root/'final.mp4')
    assert reused['reused'] and reused['artifact_id']==result['artifact_id']
    receipt=p.record_final_qa(pid,result['artifact_id'],QA)
    assert receipt['subject']['sha256']
    p.review_final(pid,result['artifact_id'],'APPROVED','human')
    assert p.status_summary(pid)['gates']['final']['current']
    assert p.assemble(pid,root/'final.mp4')['reused']
    p.replace_scene_artifact(scenes[0]['id'],image(root/'replacement.ppm',200))
    assert not p.status_summary(pid)['gates']['final']['current']
    with pytest.raises((PermissionError,AssemblyError)):
        p.review_final(pid,result['artifact_id'],'APPROVED','human')
    with pytest.raises((PermissionError,AssemblyError)):
        p.assemble(pid,root/'final.mp4')
