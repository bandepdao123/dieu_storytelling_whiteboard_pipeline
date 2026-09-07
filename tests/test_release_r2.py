"""R2 public compatibility regression; fixtures never touch production media."""
from pathlib import Path
from du_pipeline.db import Database
from du_pipeline.service import Pipeline


def test_idempotent_discord_replacement_public(tmp_path):
    with Database(tmp_path/'test.db') as db:
        p=Pipeline(db)
        pid=p.init_project('R2',scene_range=(1,10))
        p.import_audio(pid,'metadata',6000,'a'*64)
        p.import_srt(pid,[(0,6000,'text')])
        sid=p.plan_scenes(pid)[0]['id']
        source=tmp_path/'source.image'
        source.write_bytes(b'image')
        command=f'du-scene-replace {sid} {source}'
        result=p.dispatch_discord(command,'OWNER',idempotency_key='review')
        assert result=={'artifact_id':1}
        assert p.dispatch_discord(command,'OWNER',idempotency_key='review')==result
        assert len(db.all('select * from artifacts'))==1
        assert len(db.all('select * from command_receipts'))==1
        assert source.read_bytes()==b'image'
        assert Path(db.one('select uri from artifacts')['uri']).read_bytes()==b'image'
