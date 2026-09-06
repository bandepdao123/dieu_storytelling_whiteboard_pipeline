"""Canonical evidence projections shared by workflow gates and polling.

Schema v1 deliberately preserves the existing service digest payload. Projection
schema identity is documented here, not added to previously persisted payloads.
The F02/F04 predicates below additionally validate v2 subject-bound QA and
content-addressed local revisions; global execution fencing remains separate.
"""
import hashlib
import json

SCHEMA_VERSION = 1
PROJECT_FIELDS = (
    'language', 'style_json', 'references_json', 'bible_json', 'image_provider',
    'whiteboard_mode', 'seed', 'transition', 'min_scenes', 'max_scenes',
    'output_json', 'version',
)
SCENE_FIELDS = (
    'id', 'project_id', 'code', 'ord', 'start_ms', 'end_ms', 'text', 'special',
    'state', 'qa_state', 'qa_json', 'duration_exception', 'continuity_json',
    'checkpoint_json',
)
MANIFEST_SCENE_FIELDS = tuple(k for k in SCENE_FIELDS if k != 'project_id') + ('approval_state',)
ARTIFACT_FIELDS = ('id', 'kind', 'sha256', 'version', 'parent_id', 'status')
MANIFEST_ARTIFACT_FIELDS = ARTIFACT_FIELDS + ('scene_id',)


def digest(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def project_fields(row, fields):
    return {key: row[key] for key in fields}


def scene_evidence_hash(scene, artifacts):
    """Accept full rows; discard projection-only fields and inactive history."""
    current = [a for a in artifacts if a['scene_id'] == scene['id'] and a['status'] == 'ACTIVE']
    return digest({
        'scene': project_fields(scene, SCENE_FIELDS),
        'artifacts': [project_fields(a, ARTIFACT_FIELDS) for a in sorted(current, key=lambda a: a['id'])],
    })


def scene_dependency_revision(project, scene):
    """Content-addressed local revision, independent of global execution fencing.

    Inputs/replanning allocate new scene identities. Configuration and scene
    content are explicit dependencies; QA and human decisions are not inputs.
    """
    return digest({'schema_version': 2,
        'project': project_fields(project, tuple(k for k in PROJECT_FIELDS if k != 'version')),
        'scene': project_fields(scene, tuple(k for k in SCENE_FIELDS
            if k not in ('qa_state', 'qa_json', 'state'))),
    })


def image_subject(scene, artifacts):
    images = [a for a in artifacts if a['scene_id'] == scene['id']
              and a['kind'] == 'IMAGE' and a['status'] == 'ACTIVE']
    if len(images) != 1:
        return None
    return {'artifact_id': images[0]['id'], 'sha256': images[0]['sha256']}


def scene_qa_current(project, scene, artifacts):
    """Legacy or malformed QA never acquires a subject by inference."""
    try:
        qa = json.loads(scene['qa_json'])
        from .contracts import QAEvidence
        typed = QAEvidence(qa['checks'], qa['score'], qa['evaluator'])
        subject = image_subject(scene, artifacts)
        return bool(scene['qa_state'] == 'PASS' and typed.passed and subject
            and qa['schema_version'] == 2 and qa['subject'] == subject
            and qa['dependency_revision'] == scene_dependency_revision(project, scene))
    except (KeyError, TypeError, ValueError, AttributeError):
        return False


def scene_approval_current(project, scene, artifacts, approvals):
    evidence = scene_evidence_hash(scene, artifacts)
    return bool(scene['approval_state'] == 'APPROVED'
        and scene['state'] in ('IMAGE_READY', 'ANIMATED')
        and scene_qa_current(project, scene, artifacts)
        and any(a['gate'] == 'SCENE' and a['scene_id'] == scene['id']
            and a['decision'] == 'APPROVED' and a['revoked_at'] is None
            and a['project_version'] is not None
            and 0 < a['project_version'] <= project['version']
            and a['evidence_sha256'] == evidence for a in approvals))


def manifest_evidence_hash(project, scenes, artifacts):
    """V2 input inventory: active scene dependencies/contact sheets plus ancestors.

    Generated project outputs and unrelated retired history cannot invalidate
    their own inputs. Parent lineage remains a dependency even when retired.
    Global version remains an independent execution fence in this gate digest.
    """
    by_id = {a['id']: a for a in artifacts}
    ids = {a['id'] for a in artifacts if a['status'] == 'ACTIVE'
           and (a['scene_id'] is not None or a['kind'] == 'CONTACT_SHEET')}
    pending = list(ids)
    while pending:
        parent = by_id[pending.pop()]['parent_id']
        if parent in by_id and parent not in ids:
            ids.add(parent)
            pending.append(parent)
    return digest({
        'schema_version': 2,
        'project': project_fields(project, PROJECT_FIELDS),
        'scenes': [project_fields(s, MANIFEST_SCENE_FIELDS) for s in sorted(scenes, key=lambda s: s['ord'])],
        'artifacts': [project_fields(by_id[i], MANIFEST_ARTIFACT_FIELDS) for i in sorted(ids)],
    })
