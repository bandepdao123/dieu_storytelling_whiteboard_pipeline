import argparse,json,re,sys,os,importlib
from .db import Database,SchemaVersionError,LeaseUnavailable
import sqlite3
from pathlib import Path

class UsageError(ValueError): pass
class CLIParser(argparse.ArgumentParser):
 def error(self,message): raise UsageError('invalid command syntax; use --help')

def error_result(code,message):
 print(json.dumps({'error':{'code':code,'message':message}}),file=sys.stderr)
 return 2
from .service import Pipeline
from .media import AssemblyError
from .integrations import IntegrationConfigurationError,VerificationError

def srt(path):
 text=open(path,encoding='utf8').read().strip(); out=[]
 for block in re.split(r'\n\s*\n',text):
  lines=block.splitlines(); timing=next((x for x in lines if '-->' in x),None)
  if not timing: continue
  a,b=[x.strip() for x in timing.split('-->')]
  def ms(x):
   h,m,z=re.split('[:]',x); sec,milli=re.split('[,.]',z); return (int(h)*3600+int(m)*60+int(sec))*1000+int(milli)
  out.append((ms(a),ms(b),' '.join(lines[lines.index(timing)+1:])))
 return out
def external_qa(path):
 from .contracts import QAEvidence
 import math
 with open(path,encoding='utf8') as stream: payload=json.load(stream)
 if not isinstance(payload,dict) or set(payload)!={'checks','score','evaluator'}: raise ValueError('QA shape')
 checks,score,evaluator=payload['checks'],payload['score'],payload['evaluator']
 if not isinstance(checks,dict) or not checks or any(not k.strip() or type(v) is not bool for k,v in checks.items()): raise ValueError('QA checks')
 if type(score) not in (int,float) or not math.isfinite(score) or not 0<=score<=1: raise ValueError('QA score')
 if not isinstance(evaluator,str) or not evaluator.strip(): raise ValueError('QA evaluator')
 return QAEvidence(checks,score,'external:'+evaluator)

def checkpoint_data(path):
 def pairs(items):
  result={}
  for key,value in items:
   if key in result: raise ValueError('duplicate JSON key')
   result[key]=value
  return result
 def constant(value): raise ValueError('nonfinite JSON value')
 with open(path,encoding='utf8') as stream:
  data=json.load(stream,object_pairs_hook=pairs,parse_constant=constant)
 reserved={'snapshot_version','data','project','job','scenes','artifacts','snapshot_sha256','identity','plan_identity'}
 if not isinstance(data,dict) or reserved.intersection(data): raise ValueError('checkpoint data object required; reserved keys forbidden')
 # Reject overflowed JSON numeric literals as well as NaN/Infinity tokens.
 json.dumps(data,allow_nan=False)
 return data

def parser():
 p=CLIParser(); p.add_argument('--db',default='pipeline.db'); p.add_argument('--role',choices=['OWNER','REVIEWER','OPERATOR'],default='OWNER'); sub=p.add_subparsers(dest='cmd',required=True)
 q=sub.add_parser('init'); q.add_argument('name'); q.add_argument('--language',default='vi'); q.add_argument('--seed',type=int,default=0); q.add_argument('--style',default='{}'); q.add_argument('--references',default='[]')
 for name in ('status','status-summary','pause','resume','plan','report'): q=sub.add_parser(name); q.add_argument('project_id')
 q=sub.add_parser('import-narration',help='Measured owned import of external PCM RIFF/WAV; no generation'); q.add_argument('project_id'); q.add_argument('path')
 q=sub.add_parser('import-audio',help='Legacy metadata-only import: caller supplies duration/hash/managed URI'); q.add_argument('project_id'); q.add_argument('uri'); q.add_argument('duration_ms',type=int); q.add_argument('sha256')
 q=sub.add_parser('import-srt'); q.add_argument('project_id'); q.add_argument('path')
 q=sub.add_parser('import-script'); q.add_argument('project_id'); q.add_argument('kind',choices=['text','docx','gdocs','excel','url']); q.add_argument('source')
 q=sub.add_parser('retry'); q.add_argument('scene_id',type=int)
 for name in ('approve','reject'): q=sub.add_parser(name); q.add_argument('project_id'); q.add_argument('scene_code'); q.add_argument('--actor',default='cli-owner')
 for name in ('sync-sheet','ingest-sheet-commands'):
  q=sub.add_parser(name); q.add_argument('project_id'); q.add_argument('--dry-run',action='store_true')
 q=sub.add_parser('upload-drive'); q.add_argument('project_id'); q.add_argument('path'); q.add_argument('--folder',default='07_exports'); q.add_argument('--dry-run',action='store_true')
 q=sub.add_parser('discord-dispatch'); q.add_argument('message_id'); q.add_argument('user_id'); q.add_argument('text'); q.add_argument('--dry-run',action='store_true')
 q=sub.add_parser('discord-status'); q.add_argument('message_id')
 q=sub.add_parser('assemble-final',aliases=['assemble']); q.add_argument('project_id'); q.add_argument('output'); q.add_argument('--dry-run',action='store_true')
 p.add_argument('--operational-wal',action='store_true',help='Opt-in inspection allowing SQLite WAL/SHM sidecar writes; no journal conversion')
 q=sub.choices['init']; q.add_argument('--min-scenes',type=int,default=50); q.add_argument('--max-scenes',type=int,default=360)
 q=sub.add_parser('register-artifact'); q.add_argument('project_id'); q.add_argument('kind'); q.add_argument('uri'); q.add_argument('--scene-id',type=int); q.add_argument('--parent-id',type=int)
 q=sub.add_parser('image-attempt'); q.add_argument('scene_id',type=int); q.add_argument('outcome',choices=['succeeded','failed']); q.add_argument('--error'); q.add_argument('--scratch-receipt')
 q=sub.add_parser('scene-qa'); q.add_argument('scene_id',type=int); q.add_argument('qa_json')
 q=sub.add_parser('post-batch'); q.add_argument('project_id'); q.add_argument('contact_sheet'); q.add_argument('qa_json'); q.add_argument('--actor',required=True)
 q=sub.add_parser('final-qa'); q.add_argument('project_id'); q.add_argument('artifact_id',type=int); q.add_argument('qa_json')
 q=sub.add_parser('final-review'); q.add_argument('project_id'); q.add_argument('artifact_id',type=int); q.add_argument('decision',choices=['APPROVED','REJECTED']); q.add_argument('--actor',required=True)
 for name in ('checkpoint-create','restore-checkpoint','rerun-propose'):
  q=sub.add_parser(name); q.add_argument('project_id'); q.add_argument('stage')
  if name=='checkpoint-create': q.add_argument('data_json')
 q=sub.add_parser('recover-legacy-content',help='OWNER opt-in v3 content revalidation; see docs/legacy-content-recovery.md'); q.add_argument('project_id'); q.add_argument('stage'); q.add_argument('--expected-revision',type=int,required=True); q.add_argument('--checkpoint-sha256',required=True); q.add_argument('--allow-legacy-content',action='store_true')
 q=sub.add_parser('restore-artifact'); q.add_argument('artifact_id',type=int)
 for name in ('rerun-decide','rerun-reapprove','rerun-apply'):
  q=sub.add_parser(name); q.add_argument('proposal_id',type=int)
  if name=='rerun-decide': q.add_argument('decision',choices=['APPROVED','REJECTED'])
  if name!='rerun-apply': q.add_argument('--actor',required=True)
 sub.add_parser('recover-publications')
 sub.add_parser('migrate')
 return p

def integration_client(kind):
 from .integrations import require_oauth_path,IntegrationConfigurationError
 require_oauth_path()
 spec=os.environ.get('DU_INTEGRATION_CLIENT_FACTORY')
 if not spec: raise IntegrationConfigurationError('DU_INTEGRATION_CLIENT_FACTORY (module:callable) is required')
 try:
  module,name=spec.split(':',1); client=getattr(importlib.import_module(module),name)(kind)
 except Exception as exc: raise IntegrationConfigurationError(f'cannot load integration client factory {spec!r}: {exc}') from exc
 if client is None: raise IntegrationConfigurationError(f'factory did not provide a {kind} client')
 required={'sheets':('find_spreadsheet','create_spreadsheet','get_tabs','add_tabs','batch_upsert','read_rows'),'drive':('ensure_folder','begin_upload','upload_chunk','finish_upload','metadata')}
 missing=[n for n in required.get(kind,()) if not callable(getattr(client,n,None))]
 if missing: raise IntegrationConfigurationError(f'{kind} client missing required methods: {", ".join(missing)}')
 return client
def main(argv=None):
 try:
  a=parser().parse_args(argv)
  readonly=a.cmd in ('status','status-summary','report','discord-status') or getattr(a,'dry_run',False)
  if a.cmd=='init': db=Database(a.db)
  elif a.cmd=='migrate':
   if not Path(a.db).is_file(): raise FileNotFoundError('database unavailable')
   db=Database(a.db)
  else: db=Database.open_existing(a.db,readonly=readonly,operational_wal=a.operational_wal)
  with db:
   return dispatch(a,Pipeline(db))
 except UsageError: return error_result('USAGE','Invalid command syntax; use --help.')
 except FileNotFoundError: return error_result('DATABASE_UNAVAILABLE','Database or input file unavailable; verify paths and initialize explicitly.')
 except SchemaVersionError: return error_result('SCHEMA_UNSUPPORTED','Current schema and offline rollback-journal inspection copy required. Back up before explicit migrate; see docs/cli-inspection.md.')
 except PermissionError: return error_result('NOT_ALLOWED','Role, evidence, lifecycle or ownership precondition rejected; inspect project state and command requirements.')
 except (ValueError,KeyError): return error_result('INVALID_REQUEST','Invalid input or project/evidence state; verify identifiers, typed inputs and prerequisites.')
 except AssemblyError: return error_result('ASSEMBLY_REJECTED','Assembly evidence, media or output contract rejected; see docs/final-assembly.md.')
 except (IntegrationConfigurationError,VerificationError): return error_result('INTEGRATION_ERROR','Integration configuration or verification failed; no diagnostic payload is exposed.')
 except LeaseUnavailable: return error_result('BUSY','Reconciliation lease unavailable; retry after the current operation.')
 except sqlite3.DatabaseError: return error_result('DATABASE_ERROR','Database operation failed; verify schema, integrity and contention on a backup.')
 except OSError: return error_result('IO_ERROR','Local I/O operation failed; verify files, permissions and available storage.')

def dispatch(a,svc):
 result={}
 if a.cmd=='init': result={'project_id':svc.init_project(a.name,a.language,a.seed,json.loads(a.style),json.loads(a.references),scene_range=(a.min_scenes,a.max_scenes),role=a.role)}
 elif a.cmd=='status': result=svc.status(a.project_id,a.role)
 elif a.cmd=='status-summary': result=svc.status_summary(a.project_id,a.role)
 elif a.cmd=='pause': svc.pause(a.project_id,a.role); result={'state':'PAUSED'}
 elif a.cmd=='resume': svc.resume(a.project_id,a.role); result={'state':'ACTIVE'}
 elif a.cmd=='import-narration': result=svc.import_narration(a.project_id,a.path,a.role)
 elif a.cmd=='import-audio': svc.import_audio(a.project_id,a.uri,a.duration_ms,a.sha256,a.role); result={'imported':'audio','measurement':'legacy-metadata-only'}
 elif a.cmd=='import-srt': svc.import_srt(a.project_id,srt(a.path),a.role); result={'imported':'srt'}
 elif a.cmd=='import-script': result={'normalized':svc.import_document(a.project_id,a.kind,a.source,a.role)}
 elif a.cmd=='plan': result={'scenes':svc.plan_scenes(a.project_id,role=a.role)}
 elif a.cmd=='retry': svc.queue_retry(a.scene_id,a.role); result={'scene_id':a.scene_id,'state':'RETRY_QUEUED'}
 elif a.cmd in ('approve','reject'): svc.decide_scene(a.project_id,a.scene_code,'APPROVED' if a.cmd=='approve' else 'REJECTED',a.actor,a.role); result={'decision':a.cmd}
 elif a.cmd=='register-artifact': result={'artifact_id':svc.add_artifact(a.project_id,a.kind,a.uri,scene_id=a.scene_id,parent_id=a.parent_id,role=a.role)}
 elif a.cmd=='image-attempt': svc.record_image_attempt(a.scene_id,a.outcome=='succeeded',error=a.error,scratch_receipt=a.scratch_receipt,role=a.role); result={'scene_id':a.scene_id,'outcome':a.outcome}
 elif a.cmd=='scene-qa': svc.record_scene_qa(a.scene_id,external_qa(a.qa_json),role=a.role); result={'scene_id':a.scene_id,'qa_source':'external'}
 elif a.cmd=='post-batch':
  from dataclasses import asdict
  if not a.actor.strip(): raise ValueError('actor required')
  result=asdict(svc.approve_post_batch(a.project_id,external_qa(a.qa_json),a.contact_sheet,a.actor,role=a.role))
 elif a.cmd=='final-qa': result=svc.record_final_qa(a.project_id,a.artifact_id,external_qa(a.qa_json),role=a.role)
 elif a.cmd=='final-review': result={'approval_id':svc.review_final(a.project_id,a.artifact_id,a.decision,a.actor,role=a.role)}
 elif a.cmd in ('checkpoint-create','restore-checkpoint','rerun-propose'):
  svc.project(a.project_id)
  if not a.stage.strip(): raise ValueError('stage required')
  if a.cmd=='checkpoint-create':
   svc.checkpoint(a.project_id,a.stage,checkpoint_data(a.data_json),role=a.role)
   result={'project_id':a.project_id,'stage':a.stage,'checkpoint_created':True}
  elif a.cmd=='restore-checkpoint': result=svc.restore_latest_checkpoint(a.project_id,a.stage,role=a.role)
  else:
   from .dependencies import normalize_stage
   result={'proposal_id':svc.propose_rerun(a.project_id,normalize_stage(a.stage).value,role=a.role)}
 elif a.cmd=='recover-legacy-content': result=svc.recover_legacy_checkpoint_content(a.project_id,a.stage,expected_revision=a.expected_revision,checkpoint_sha256=a.checkpoint_sha256,allow_legacy_content=a.allow_legacy_content,role=a.role)
 elif a.cmd=='restore-artifact': result=svc.restore_artifact(a.artifact_id,role=a.role)
 elif a.cmd in ('rerun-decide','rerun-reapprove','rerun-apply'):
  if not svc.db.one('select id from proposals where id=?',(a.proposal_id,)): raise ValueError('proposal not found')
  if a.cmd=='rerun-apply': result={'proposal_id':a.proposal_id,'job_id':svc.apply_rerun(a.proposal_id,role=a.role)}
  else:
   approved=a.cmd=='rerun-reapprove' or a.decision=='APPROVED'
   svc.decide_rerun(a.proposal_id,approved,a.actor,role=a.role,reapprove=a.cmd=='rerun-reapprove')
   result={'proposal_id':a.proposal_id,'decision':'APPROVED' if approved else 'REJECTED'}
 elif a.cmd=='report': result=svc.report(a.project_id,a.role)
 elif a.cmd in ('sync-sheet','ingest-sheet-commands'):
  from .integrations import SheetsAdapter
  adapter=SheetsAdapter(integration_client('sheets'),svc.db) if not a.dry_run else SheetsAdapter(type('Dry',(),{'find_spreadsheet':lambda s,p:None})(),svc.db)
  result=adapter.sync(a.project_id,a.dry_run) if a.cmd=='sync-sheet' else adapter.ingest_commands(a.project_id,a.dry_run)
 elif a.cmd=='upload-drive':
  from .integrations import DriveAdapter
  result=DriveAdapter(integration_client('drive') if not a.dry_run else None).upload(a.project_id,a.path,a.folder,dry_run=a.dry_run)
 elif a.cmd=='discord-dispatch':
  from .integrations import DiscordBridge
  allow=json.loads(__import__('os').environ.get('DU_DISCORD_ALLOWLIST_JSON','{}'))
  result=DiscordBridge(svc,allow).dispatch(a.message_id,a.user_id,a.text,a.dry_run)
 elif a.cmd=='discord-status':
  from .integrations import DiscordBridge
  result=DiscordBridge(svc,{}).status(a.message_id)
 elif a.cmd in ('assemble','assemble-final'): result=svc.assemble(a.project_id,a.output,a.dry_run,a.role)
 elif a.cmd=='recover-publications': result={'publications':svc.reconcile_publications()}
 elif a.cmd=='migrate': result={'schema_version':svc.db.one('PRAGMA user_version')[0]}
 print(json.dumps(result,ensure_ascii=False)); return 0
if __name__=='__main__': raise SystemExit(main())
