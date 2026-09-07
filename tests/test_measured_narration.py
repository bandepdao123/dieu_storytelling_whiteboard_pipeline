import hashlib
import os
import wave
from pathlib import Path
from contextlib import contextmanager
import pytest
from du_pipeline.db import Database
from du_pipeline.service import Pipeline


def wav(path,frames=16000):
    with wave.open(str(path),'wb') as out:
        out.setparams((1,2,8000,0,'NONE','not compressed'))
        out.writeframes(b'\0\0'*frames)
    return path.read_bytes()

@pytest.fixture
def setup(tmp_path):
    with Database(tmp_path/'db') as db:
        svc=Pipeline(db); pid=svc.init_project('measured',scene_range=(2,2))
        source=tmp_path/'external.wav'; original=wav(source)
        yield svc,pid,source,original

def test_cold_source_atime_is_not_a_content_change(setup):
    svc,pid,source,original=setup
    os.utime(source,(1,1))
    assert svc.import_narration(pid,source)['sha256']==hashlib.sha256(original).hexdigest()


def test_audio_reference_blocks_copy_compensation(setup,monkeypatch):
    svc,pid,source,original=setup
    copy=svc._managed_copy
    @contextmanager
    def referenced(*args):
        with copy(*args) as owned:
            with Database.open_existing(svc.db.path,readonly=False) as other:
                Pipeline(other).import_audio(pid,str(owned[0]),2000,hashlib.sha256(original).hexdigest())
            yield owned
    monkeypatch.setattr(svc,'_managed_copy',referenced)
    with pytest.raises(PermissionError): svc.import_narration(pid,source)
    row=svc.db.one('select uri from audio')
    assert Path(row['uri']).read_bytes()==original


@pytest.mark.parametrize('failure',['event','collision','missing'])
def test_failure_does_not_overwrite_or_commit(setup,monkeypatch,failure):
    svc,pid,source,original=setup
    if failure=='event':
        event=svc._event
        def fail(pid,kind,*args):
            event(pid,kind,*args)
            if kind=='NARRATION_MEASURED': raise KeyboardInterrupt()
        monkeypatch.setattr(svc,'_event',fail)
    elif failure=='collision':
        from types import SimpleNamespace
        import du_pipeline.service as module
        monkeypatch.setattr(module.uuid,'uuid4',lambda:SimpleNamespace(hex='collision'))
        (svc._root_lookup(pid)/'collision').write_bytes(b'unknown')
    else: source=source.with_name('missing.wav')
    with pytest.raises((KeyboardInterrupt,FileExistsError,FileNotFoundError)): svc.import_narration(pid,source)
    assert not svc.db.one('select * from audio')
    assert not svc.db.one('select * from artifacts')
    assert [p.read_bytes() for p in svc._root_lookup(pid).iterdir()]==([b'unknown'] if failure=='collision' else [])


def test_measured_owned_import(setup):
    svc,pid,source,original=setup
    receipt=svc.import_narration(pid,source)
    row=dict(svc.db.one('select * from audio where project_id=?',(pid,)))
    assert row['duration_ms']==2000
    assert row['sha256']==hashlib.sha256(original).hexdigest()==receipt['sha256']
    assert row['uri']!=str(source)
    assert Path(row['uri']).read_bytes()==source.read_bytes()==original
    assert svc.db.one('select uri from artifacts where id=?',(receipt['artifact_id'],))['uri']==row['uri']
    assert receipt['measurement']=='pcm-wav-owned-v1'

@pytest.mark.parametrize('bad',['garbage','truncated','empty','no-audio','partial-frame'])
def test_invalid_preserves_source_and_database(setup,bad):
    svc,pid,source,_=setup
    if bad=='garbage': source.write_bytes(b'not media')
    elif bad=='truncated': source.write_bytes(source.read_bytes()[:-20])
    elif bad=='empty': wav(source,0)
    elif bad=='partial-frame':
        import struct
        data=bytearray(source.read_bytes()); data.pop()
        struct.pack_into('<I',data,4,len(data)-8); struct.pack_into('<I',data,40,len(data)-44)
        source.write_bytes(data)
    else: source.write_bytes(b'P6\n1 1\n255\n\0\0\0')
    before=source.read_bytes(); dump=list(svc.db.conn.iterdump())
    with pytest.raises(ValueError): svc.import_narration(pid,source)
    assert source.read_bytes()==before
    assert list(svc.db.conn.iterdump())==dump
    assert list(svc._root_lookup(pid).iterdir())==[]

@pytest.mark.parametrize('alias',['symlink','parent','hardlink','managed'])
def test_alias_rejected(setup,alias,tmp_path):
    svc,pid,source,original=setup
    if alias=='symlink':
        link=tmp_path/'link.wav'; link.symlink_to(source); source=link
    elif alias=='parent':
        link=tmp_path/'link'; link.symlink_to(tmp_path,target_is_directory=True); source=link/source.name
    elif alias=='hardlink': os.link(source,tmp_path/'hard.wav')
    else:
        source=svc._root_lookup(pid)/'caller.wav'; source.write_bytes(original)
    with pytest.raises((PermissionError,OSError)): svc.import_narration(pid,source)
    assert source.read_bytes()==original
    assert not svc.db.one('select * from audio')

def test_nested_boundary_before_copy(setup):
    svc,pid,source,original=setup
    with svc.db.transaction():
        with pytest.raises(PermissionError): svc.import_narration(pid,source)
        assert list(svc._root_lookup(pid).iterdir())==[]
    assert source.read_bytes()==original

@pytest.mark.parametrize('change',['pause','revision','source','unknown','failure'])
def test_copy_window_fences_and_compensation(setup,monkeypatch,change):
    svc,pid,source,original=setup
    copy=svc._managed_copy
    @contextmanager
    def intercepted(*args):
        with copy(*args) as owned:
            if change in ('pause','revision'):
                with Database.open_existing(svc.db.path,readonly=False) as other:
                    peer=Pipeline(other)
                    if change=='pause': peer.pause(pid)
                    else: peer.configure_project(pid,'language','en')
            elif change=='source': source.write_bytes(b'changed source')
            elif change=='unknown': owned[0].write_bytes(b'unknown do not remove')
            else: raise RuntimeError('injected')
            yield owned
    monkeypatch.setattr(svc,'_managed_copy',intercepted)
    with pytest.raises((PermissionError,ValueError,RuntimeError)): svc.import_narration(pid,source)
    assert not svc.db.one('select * from audio')
    assert not svc.db.one('select * from artifacts')
    if change=='unknown': assert [p.read_bytes() for p in svc._root_lookup(pid).iterdir()]==[b'unknown do not remove']
    else: assert list(svc._root_lookup(pid).iterdir())==[]
    assert source.read_bytes()==(b'changed source' if change=='source' else original)
