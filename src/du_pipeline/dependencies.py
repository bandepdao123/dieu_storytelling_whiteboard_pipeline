"""Explicit rerun contracts; artifact kinds are not execution stage names."""
from dataclasses import dataclass
from enum import Enum

class Stage(str, Enum):
    PLANNING='planning'
    IMAGE='image'
    QA='qa'
    ANIMATION='animation'
    ASSEMBLY='assembly'
    UPLOAD='upload'

class ArtifactKind(str, Enum):
    IMAGE='IMAGE'
    CONTACT_SHEET='CONTACT_SHEET'
    ANIMATION='ANIMATION'
    SCENE_VIDEO='SCENE_VIDEO'
    FINAL_VIDEO='FINAL_VIDEO'

@dataclass(frozen=True)
class Impact:
    artifacts: frozenset[ArtifactKind]
    approvals: frozenset[str]
    jobs: frozenset[str]
    reset_qa: bool=False
    new_epoch: bool=False

FINAL=frozenset({ArtifactKind.FINAL_VIDEO})
CLIPS=FINAL|{ArtifactKind.ANIMATION,ArtifactKind.SCENE_VIDEO}
QA=CLIPS|{ArtifactKind.CONTACT_SHEET}
IMAGES=QA|{ArtifactKind.IMAGE}
# Project-gate evidence retains exact global execution version. Therefore a
# media rerun also requires fresh POST_BATCH authorization, not copied approval.
PROJECT_GATES=frozenset({'POST_BATCH','BATCH','FINAL','UPLOAD'})
IMPACT={
    Stage.PLANNING:Impact(IMAGES,PROJECT_GATES|{'SCENE','PILOT'},frozenset({'PIPELINE','BATCH_IMAGE','RETRY_IMAGE','QA','ANIMATION','ASSEMBLY','UPLOAD'}),True,True),
    Stage.IMAGE:Impact(IMAGES,PROJECT_GATES|{'SCENE','PILOT'},frozenset({'BATCH_IMAGE','RETRY_IMAGE','QA','ANIMATION','ASSEMBLY','UPLOAD'}),True,True),
    Stage.QA:Impact(QA,PROJECT_GATES|{'SCENE','PILOT'},frozenset({'QA','ANIMATION','ASSEMBLY','UPLOAD'}),True),
    Stage.ANIMATION:Impact(CLIPS,PROJECT_GATES,frozenset({'ANIMATION','ASSEMBLY','UPLOAD'})),
    Stage.ASSEMBLY:Impact(FINAL,PROJECT_GATES,frozenset({'ASSEMBLY','UPLOAD'})),
    Stage.UPLOAD:Impact(frozenset(),frozenset({'UPLOAD'}),frozenset({'UPLOAD'})),
}

def normalize_stage(value):
    return Stage({'plan':'planning'}.get(value,value))

def job_stage(kind):
    if kind.startswith('RERUN:'):
        return {'planning':'PIPELINE','image':'BATCH_IMAGE'}.get(kind[6:],kind[6:].upper())
    return kind.split(':',1)[0]
