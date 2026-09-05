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
 p=argparse.ArgumentParser(); p.add_argument('--db',default='pipeline.db'); sub=p.add_subparsers(dest='cmd',required=True)
 q=sub.add_parser('init'); q.add_argument('name'); q.add_argument('--language',default='vi'); q.add_argument('--seed',type=int,default=0); q.add_argument('--style',default='{}'); q.add_argument('--references',default='[]')
 for name in ('status','pause','resume','plan','report'): q=sub.add_parser(name); q.add_argument('project_id')
 q=sub.add_parser('import-audio'); q.add_argument('project_id'); q.add_argument('uri'); q.add_argument('duration_ms',type=int); q.add_argument('sha256')
 q=sub.add_parser('import-srt'); q.add_argument('project_id'); q.add_argument('path')
 q=sub.add_parser('import-script'); q.add_argument('project_id'); q.add_argument('kind',choices=['text','docx','gdocs','excel','url']); q.add_argument('source')
 q=sub.add_parser('retry'); q.add_argument('scene_id',type=int)
 for name in ('approve','reject'): q=sub.add_parser(name); q.add_argument('project_id'); q.add_argument('scene_code'); q.add_argument('--actor',default='cli-owner')
 return p
def main(argv=None):
 a=parser().parse_args(argv); svc=Pipeline(Database(a.db)); result={}
 if a.cmd=='init': result={'project_id':svc.init_project(a.name,a.language,a.seed,json.loads(a.style),json.loads(a.references))}
 elif a.cmd=='status': result=svc.status(a.project_id)
 elif a.cmd=='pause': svc.pause(a.project_id); result={'state':'PAUSED'}
 elif a.cmd=='resume': svc.resume(a.project_id); result={'state':'ACTIVE'}
 elif a.cmd=='import-audio': svc.import_audio(a.project_id,a.uri,a.duration_ms,a.sha256); result={'imported':'audio'}
 elif a.cmd=='import-srt': svc.import_srt(a.project_id,srt(a.path)); result={'imported':'srt'}
 elif a.cmd=='import-script': svc.event(a.project_id,'SCRIPT_SOURCE_IMPORTED',{'kind':a.kind,'source':a.source}); result={'normalized_metadata':True}
 elif a.cmd=='plan': result={'scenes':svc.plan_scenes(a.project_id)}
 elif a.cmd=='retry': svc.record_image_attempt(a.scene_id,False,error='manual retry queued'); result={'scene_id':a.scene_id}
 elif a.cmd in ('approve','reject'): svc.decide_scene(a.project_id,a.scene_code,'APPROVED' if a.cmd=='approve' else 'REJECTED',a.actor); result={'decision':a.cmd}
 elif a.cmd=='report': result=svc.report(a.project_id)
 print(json.dumps(result,ensure_ascii=False)); return 0
if __name__=='__main__': raise SystemExit(main())
