import hashlib, json, os, shutil, subprocess

from pathlib import Path
import pytest
import du_pipeline.service as service_module
from du_pipeline.db import Database
from du_pipeline.service import Pipeline, now
from du_pipeline.media import AssemblyError, LocalFinalAssembler
from du_pipeline.contracts import QAEvidence
from qa_helpers import ready_qa


def sha(p): return hashlib.sha256(Path(p).read_bytes()).hexdigest()

def fixture(tmp_path):
    db=Database(tmp_path/'p.db'); p=Pipeline(db); pid=p.init_project('x',scene_range=(1,3)); root=p._root(pid)
    audio=root/'narration.wav'; audio.write_bytes(b'audio'); p.import_audio(pid,str(audio),2000,sha(audio))
    p.import_srt(pid,[(0,2000,'x')]); scene=p.plan_scenes(pid)[0]; sid=scene['id']
    ready_qa(p,scene)
    clip=root/'clip.mp4'; clip.write_bytes(b'clip'); aid=p.add_artifact(pid,'SCENE_VIDEO',clip,sid)
    p.decide_scene(pid,'S001','APPROVED','t')
    sheet=root/'sheet.ppm'; sheet.write_bytes(b'P6\n1 1\n255\n\x80\x40\x20')
    p.approve_post_batch(pid,QAEvidence({'all':True},1,'fixture'),sheet,'batch-reviewer')
    return db,p,pid,root,sid,aid,audio

def test_command_has_audio_and_no_shortest(tmp_path):
    a=LocalFinalAssembler(tmp_path)
    cmd=a.mux_command(tmp_path/'v.mp4',tmp_path/'a.wav',tmp_path/'o.mp4')
    assert '-map' in cmd and '1:a:0' in cmd and '-c:a' in cmd and 'aac' in cmd and '-shortest' not in cmd

def test_dry_run_order_and_no_artifact(tmp_path):
    db,p,pid,root,sid,aid,audio=fixture(tmp_path)
    result=p.assemble(pid,root/'final.mp4',dry_run=True)
    assert result['manifest']['scenes'][0]['source_artifact_id']==aid
    assert not db.one("select 1 from artifacts where kind='FINAL_VIDEO'")

def test_missing_duplicate_and_tamper_fail_closed(tmp_path):
    db,p,pid,root,sid,aid,audio=fixture(tmp_path)
    db.execute("update scenes set approval_state='NOT_REQUIRED' where id=?",(sid,))
    db.execute("update artifacts set status='SUPERSEDED' where scene_id=?",(sid,))
    with pytest.raises(AssemblyError,match='exactly one'): p.assemble(pid,root/'f.mp4',dry_run=True)
    db.execute("update artifacts set status='ACTIVE' where id=?",(aid,)); other=root/'other.mp4'; other.write_bytes(b'x'); p.add_artifact(pid,'ANIMATION',other,sid)
    with pytest.raises(AssemblyError,match='duplicate'): p.assemble(pid,root/'f.mp4',dry_run=True)
    db.execute("update artifacts set status='SUPERSEDED' where kind='ANIMATION'"); (root/'clip.mp4').write_bytes(b'tampered')
    with pytest.raises(AssemblyError,match='checksum'): p.assemble(pid,root/'f.mp4',dry_run=True)

def test_two_active_preferred_clips_fail_even_when_versions_differ(tmp_path):
    db,p,pid,root,sid,aid,audio=fixture(tmp_path)
    newer=root/'newer.mp4'; newer.write_bytes(b'newer')
    db.execute("insert into artifacts(project_id,scene_id,kind,uri,sha256,version,status,created_at,expires_at) values(?,?,?,?,?,2,'ACTIVE',?,?)",
               (pid,sid,'SCENE_VIDEO',str(newer),sha(newer),now(),now()))
    db.execute("update approvals set evidence_sha256=? where scene_id=?",(p._scene_evidence_hash(sid),sid))
    with pytest.raises(AssemblyError,match='duplicate'):
        p.assemble(pid,root/'f.mp4',dry_run=True)

def test_manifest_has_canonical_audio_then_visual_parent_lineage(tmp_path):
    db,p,pid,root,sid,aid,audio=fixture(tmp_path)
    manifest=p.assemble(pid,root/'f.mp4',dry_run=True)['manifest']
    assert manifest['parent_lineage'] == [
        {'type':'NARRATION_AUDIO','sha256':sha(audio)},
        {'type':'SCENE_VISUAL','ord':1,'artifact_id':aid,'sha256':sha(root/'clip.mp4')},
    ]

def test_output_escape_rejected(tmp_path):
    db,p,pid,root,*_=fixture(tmp_path)
    with pytest.raises((AssemblyError,PermissionError)): p.assemble(pid,tmp_path/'outside.mp4',dry_run=True)

media_tools=pytest.mark.skipif(not shutil.which('ffmpeg') or not shutil.which('ffprobe'),reason='ffmpeg and ffprobe binaries are required')

@media_tools
def test_real_ordered_scenes_and_narration_produce_conformant_mp4(tmp_path):
    db=Database(tmp_path/'real.db'); p=Pipeline(db); pid=p.init_project('real',scene_range=(2,2)); root=p._root(pid)
    audio=root/'narration.wav'; subprocess.run(['ffmpeg','-y','-f','lavfi','-i','sine=frequency=440:duration=2',str(audio)],check=True,capture_output=True); p.import_audio(pid,str(audio),2000,sha(audio))
    image=root/'second.png'; subprocess.run(['ffmpeg','-y','-f','lavfi','-i','color=c=blue:s=64x64','-frames:v','1',str(image)],check=True,capture_output=True)
    clip=root/'first.mp4'; subprocess.run(['ffmpeg','-y','-f','lavfi','-i','color=c=red:s=64x64:d=1','-c:v','libx264','-pix_fmt','yuv420p',str(clip)],check=True,capture_output=True)
    p.import_srt(pid,[(0,1000,'S001'),(1000,2000,'S002')]); scenes=p.plan_scenes(pid); source_ids=[]
    for code,order,start,end,path,kind in [('S001',1,0,1000,clip,'SCENE_VIDEO'),('S002',2,1000,2000,image,'IMAGE')]:
        scene=scenes[order-1]; sid=scene['id']
        if kind=='SCENE_VIDEO': ready_qa(p,scene)
        aid=p.add_artifact(pid,kind,path,sid); source_ids.append(aid)
        if kind=='IMAGE':
            p.record_image_attempt(sid,True)
            p.record_scene_qa(sid,QAEvidence({'visual':True},1,'test'))
        p.decide_scene(pid,code,'APPROVED','test')
    p.approve_post_batch(pid,QAEvidence({'all':True},1,'test'),image,'batch-reviewer')
    output=root/'ordered-final.mp4'; result=p.assemble(pid,output)
    probe=json.loads(subprocess.run(['ffprobe','-v','error','-show_streams','-show_format','-of','json',str(output)],check=True,capture_output=True,text=True).stdout)
    video=next(s for s in probe['streams'] if s['codec_type']=='video'); audio_stream=next(s for s in probe['streams'] if s['codec_type']=='audio')
    assert video['codec_name']=='h264' and audio_stream['codec_name']=='aac'
    assert float(probe['format']['duration'])==pytest.approx(2.0,abs=.06)
    assert [s['source_artifact_id'] for s in result['manifest']['scenes']]==source_ids
    assert result['manifest']['tool_versions']['ffmpeg'].startswith('ffmpeg version')
    assert result['manifest']['tool_versions']['ffprobe'].startswith('ffprobe version')

class SuccessfulFakeAssembler(LocalFinalAssembler):
    def tool_versions(self): return {'ffmpeg':'ffmpeg version test','ffprobe':'ffprobe version test'}
    def run(self,cmd): Path(cmd[-1]).write_bytes(b'generated')
    def validate_final(self,path,expected): return {'format':{'duration':str(expected)},'streams':[]}

def test_nested_absent_output_parent_is_safely_created_and_published(tmp_path):
    db,p,pid,root,*_=fixture(tmp_path)
    output=root/'exports'/'review'/'final.mp4'
    result=p.assemble(pid,output,assembler=SuccessfulFakeAssembler(root))
    assert output.read_bytes()==b'generated'
    assert result['output']==str(output)


def test_reconciliation_closes_parent_fds_across_rows_and_branches(tmp_path):
    db,p,pid,root,*_=fixture(tmp_path)
    (root/'.publications').mkdir()
    before=len(os.listdir('/proc/self/fd'))
    # Repeated missing-byte rows exercise the early-return recovery branch.
    for i in range(80):
        token=f'leak-{i}'; final=root/'nested'/str(i)/'final.mp4'
        db.execute("insert into publication_journal values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
          (token,pid,db.one('select version from projects where id=?',(pid,))['version'],f'e-{i}',p._manifest_hash(pid),str(root/'.publications'/f'{token}.mp4'),str(final),'0'*64,1,'{}','PREPARED',None,now(),now(),None))
    p.reconcile_publications()
    assert len(os.listdir('/proc/self/fd')) <= before + 2
    assert db.one("select count(*) n from publication_journal where state='ABORTED'")['n']==80


def test_registration_failure_removes_promoted_untracked_output(tmp_path):
    db,p,pid,root,*_=fixture(tmp_path); output=root/'final.mp4'
    db.execute("create trigger reject_final before insert on final_assemblies begin select raise(abort,'injected registration failure'); end")
    with pytest.raises(Exception,match='injected registration failure'): p.assemble(pid,output,assembler=SuccessfulFakeAssembler(root))
    assert not output.exists()
    assert not db.one("select 1 from artifacts where kind='FINAL_VIDEO'")
    assert not db.one('select 1 from final_assemblies')


def test_assembly_closes_publication_fd_when_work_fd_open_fails(tmp_path, monkeypatch):
    db,p,pid,root,*_=fixture(tmp_path); output=root/'final.mp4'
    real_open=os.open
    def fail_work_open(path, flags, *args, **kwargs):
        if Path(path).name.startswith('.assembly-') and flags & os.O_DIRECTORY:
            raise OSError('injected work directory open failure')
        return real_open(path, flags, *args, **kwargs)
    monkeypatch.setattr(service_module.os, 'open', fail_work_open)
    before=len(os.listdir('/proc/self/fd'))
    with pytest.raises(OSError, match='injected work directory open failure'):
        p.assemble(pid,output,assembler=SuccessfulFakeAssembler(root))
    assert len(os.listdir('/proc/self/fd')) <= before


def test_rollback_closes_final_parent_fd_when_staging_parent_open_fails(tmp_path, monkeypatch):
    db,p,pid,root,*_=fixture(tmp_path); output=root/'final.mp4'
    db.execute("create trigger reject_final before insert on final_assemblies begin select raise(abort,'injected registration failure'); end")
    real_parent=p._publication_parent_fd; rollback_final_opened=False
    def fail_rollback_staging(pid_arg, path, create=False):
        nonlocal rollback_final_opened
        if Path(path) == output:
            rollback_final_opened=True
        elif rollback_final_opened and Path(path).name.endswith('.staged.mp4'):
            raise OSError('injected staging parent open failure')
        return real_parent(pid_arg,path,create=create)
    monkeypatch.setattr(p, '_publication_parent_fd', fail_rollback_staging)
    before=len(os.listdir('/proc/self/fd'))
    with pytest.raises(Exception, match='injected registration failure'):
        p.assemble(pid,output,assembler=SuccessfulFakeAssembler(root))
    assert rollback_final_opened
    assert len(os.listdir('/proc/self/fd')) <= before


def test_atomic_publication_collision_never_overwrites_or_registers(tmp_path, monkeypatch):
    db,p,pid,root,*_=fixture(tmp_path); output=root/'final.mp4'
    real = service_module.rename_noreplace_at
    def collide(source_fd, source, destination_fd, destination):
        if destination == output.name:
            fd=__import__('os').open(destination,__import__('os').O_WRONLY|__import__('os').O_CREAT|__import__('os').O_EXCL,0o600,dir_fd=destination_fd)
            __import__('os').write(fd,b'concurrent-owner'); __import__('os').close(fd)
        return real(source_fd,source,destination_fd,destination)
    monkeypatch.setattr(service_module, 'rename_noreplace_at', collide)
    with pytest.raises(AssemblyError, match='DESTINATION_COLLISION'):
        p.assemble(pid, output, assembler=SuccessfulFakeAssembler(root))
    assert output.read_bytes() == b'concurrent-owner'
    assert not db.one("select 1 from artifacts where kind='FINAL_VIDEO'")
    row=db.one('select state,error,staging_path from publication_journal')
    assert row['state']=='ABORTED' and 'atomic no-replace' in row['error']
    assert Path(row['staging_path']).read_bytes()==b'generated'


def test_recovery_atomic_collision_never_overwrites_or_registers(tmp_path, monkeypatch):
    db,p,pid,root,*_=fixture(tmp_path); output=root/'recovered.mp4'
    def crash(point):
        if point == 'after_prepared_commit': raise RuntimeError('simulated crash')
    p._publication_crash=crash
    with pytest.raises(RuntimeError, match='simulated crash'):
        p.assemble(pid, output, assembler=SuccessfulFakeAssembler(root))
    p._publication_crash=lambda point: None
    real = service_module.rename_noreplace_at
    def collide(source_fd, source, destination_fd, destination):
        fd=__import__('os').open(destination,__import__('os').O_WRONLY|__import__('os').O_CREAT|__import__('os').O_EXCL,0o600,dir_fd=destination_fd)
        __import__('os').write(fd,b'recovery-racer'); __import__('os').close(fd)
        return real(source_fd,source,destination_fd,destination)
    monkeypatch.setattr(service_module, 'rename_noreplace_at', collide)
    p.reconcile_publications()
    assert output.read_bytes()==b'recovery-racer'
    assert not db.one("select 1 from artifacts where kind='FINAL_VIDEO'")
    row=db.one('select state,error,staging_path from publication_journal')
    assert row['state']=='ABORTED' and 'DESTINATION_COLLISION' in row['error']
    assert Path(row['staging_path']).read_bytes()==b'generated'
