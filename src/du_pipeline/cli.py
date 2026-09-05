import argparse,json,re,sys
from .db import Database
from .service import Pipeline

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
def parser():
 p=argparse.ArgumentParser(); p.add_argument('--db',default='pipeline.db'); p.add_argument('--role',choices=['OWNER','REVIEWER','OPERATOR'],default='OWNER'); sub=p.add_subparsers(dest='cmd',required=True)
 q=sub.add_parser('init'); q.add_argument('name'); q.add_argument('--language',default='vi'); q.add_argument('--seed',type=int,default=0); q.add_argument('--style',default='{}'); q.add_argument('--references',default='[]')
 for name in ('status','pause','resume','plan','report'): q=sub.add_parser(name); q.add_argument('project_id')
 q=sub.add_parser('import-audio'); q.add_argument('project_id'); q.add_argument('uri'); q.add_argument('duration_ms',type=int); q.add_argument('sha256')
 q=sub.add_parser('import-srt'); q.add_argument('project_id'); q.add_argument('path')
 q=sub.add_parser('import-script'); q.add_argument('project_id'); q.add_argument('kind',choices=['text','docx','gdocs','excel','url']); q.add_argument('source')
 q=sub.add_parser('retry'); q.add_argument('scene_id',type=int)
 for name in ('approve','reject'): q=sub.add_parser(name); q.add_argument('project_id'); q.add_argument('scene_code'); q.add_argument('--actor',default='cli-owner')
 for name in ('sync-sheet','ingest-sheet-commands'):
  q=sub.add_parser(name); q.add_argument('project_id'); q.add_argument('--dry-run',action='store_true')
 q=sub.add_parser('upload-drive'); q.add_argument('project_id'); q.add_argument('path'); q.add_argument('--folder',default='07_exports'); q.add_argument('--dry-run',action='store_true')
 q=sub.add_parser('discord-dispatch'); q.add_argument('message_id'); q.add_argument('user_id'); q.add_argument('text'); q.add_argument('--dry-run',action='store_true')
 q=sub.add_parser('discord-status'); q.add_argument('message_id')
 return p

def integration_client(kind):
 from .integrations import require_oauth_path,IntegrationConfigurationError
 require_oauth_path()
 raise IntegrationConfigurationError(f'{kind} client factory must be configured by the deployment plugin')
def main(argv=None):
 a=parser().parse_args(argv); svc=Pipeline(Database(a.db)); result={}
 if a.cmd=='init': result={'project_id':svc.init_project(a.name,a.language,a.seed,json.loads(a.style),json.loads(a.references),role=a.role)}
 elif a.cmd=='status': result=svc.status(a.project_id,a.role)
 elif a.cmd=='pause': svc.pause(a.project_id,a.role); result={'state':'PAUSED'}
 elif a.cmd=='resume': svc.resume(a.project_id,a.role); result={'state':'ACTIVE'}
 elif a.cmd=='import-audio': svc.import_audio(a.project_id,a.uri,a.duration_ms,a.sha256,a.role); result={'imported':'audio'}
 elif a.cmd=='import-srt': svc.import_srt(a.project_id,srt(a.path),a.role); result={'imported':'srt'}
 elif a.cmd=='import-script': result={'normalized':svc.import_document(a.project_id,a.kind,a.source,a.role)}
 elif a.cmd=='plan': result={'scenes':svc.plan_scenes(a.project_id,role=a.role)}
 elif a.cmd=='retry': svc.queue_retry(a.scene_id,a.role); result={'scene_id':a.scene_id,'state':'RETRY_QUEUED'}
 elif a.cmd in ('approve','reject'): svc.decide_scene(a.project_id,a.scene_code,'APPROVED' if a.cmd=='approve' else 'REJECTED',a.actor,a.role); result={'decision':a.cmd}
 elif a.cmd=='report': result=svc.report(a.project_id)
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
  row=svc.db.one('select response_json from discord_messages where message_id=?',(a.message_id,)); result=json.loads(row[0]) if row else {'type':'KHONG_TIM_THAY','ok':False,'message':'Không tìm thấy tin nhắn.'}
 print(json.dumps(result,ensure_ascii=False)); return 0
if __name__=='__main__': raise SystemExit(main())
