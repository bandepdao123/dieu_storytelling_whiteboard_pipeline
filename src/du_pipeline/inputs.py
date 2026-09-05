import hashlib,re
from pathlib import Path
class RemoteDocumentAdapter:
 def fetch(self,kind:str,source:str): raise NotImplementedError
def normalize_text(text):
 if not isinstance(text,str): raise TypeError('text')
 value=' '.join(text.replace('\x00','').split())
 if not value: raise ValueError('empty text')
 return {'kind':'text','text':value}
def local_metadata(path,kind):
 p=Path(path).expanduser().resolve()
 if not p.is_file(): raise ValueError('local file does not exist')
 data=p.read_bytes(); return {'kind':kind,'path':str(p),'size':len(data),'sha256':hashlib.sha256(data).hexdigest()}
def normalize_document(kind,source,remote=None):
 if kind=='text': return normalize_text(source)
 if kind in ('audio','srt'): return local_metadata(source,kind)
 if kind=='docx':
  try: import docx
  except ImportError as e: raise RuntimeError('DOCX support requires python-docx') from e
  return normalize_text('\n'.join(p.text for p in docx.Document(source).paragraphs))
 if kind in ('xlsx','excel'):
  try: import openpyxl
  except ImportError as e: raise RuntimeError('XLSX support requires openpyxl') from e
  wb=openpyxl.load_workbook(source,read_only=True,data_only=True); return normalize_text('\n'.join(str(c) for ws in wb for row in ws.values for c in row if c is not None))
 if kind in ('gdocs','url'):
  if remote is None: raise RuntimeError('remote adapter required')
  return remote.fetch(kind,source)
 raise ValueError('unsupported input kind')
def narrator_metadata(name,*,human=True,count=1):
 if not human or count!=1 or not str(name).strip(): raise ValueError('exactly one human narrator required')
 return {'name':name.strip(),'human':True,'count':1}