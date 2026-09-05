from dataclasses import dataclass,field,asdict
from typing import Protocol,Mapping,Any

def _nonempty(value,name):
 if not isinstance(value,str) or not value.strip(): raise ValueError(name)
 return value.strip()
@dataclass(frozen=True)
class StyleBible:
 visual_language:str; line_style:str; palette:tuple[str,...]; forbidden:tuple[str,...]=()
 def __post_init__(self):
  _nonempty(self.visual_language,'visual_language'); _nonempty(self.line_style,'line_style')
  if not self.palette: raise ValueError('palette required')
@dataclass(frozen=True)
class Character: name:str; appearance:str; reference_ids:tuple[str,...]
@dataclass(frozen=True)
class Environment: name:str; description:str; reference_ids:tuple[str,...]=()
@dataclass(frozen=True)
class Prop: name:str; description:str; reference_ids:tuple[str,...]=()
@dataclass(frozen=True)
class TimeState: label:str; description:str
@dataclass(frozen=True)
class Bible:
 style:StyleBible; characters:tuple[Character,...]; environments:tuple[Environment,...]; props:tuple[Prop,...]; time_states:tuple[TimeState,...]
 def validate_continuity(self,c):
  required=('character','environment','time_state')
  if any(not c.get(x) for x in required): raise ValueError('continuity requires character, environment and time_state')
  if c['character'] not in {x.name for x in self.characters} or c['environment'] not in {x.name for x in self.environments} or c['time_state'] not in {x.label for x in self.time_states}: raise ValueError('unknown continuity reference')
@dataclass(frozen=True)
class OutputConfig:
 width:int=1920; height:int=1080; fps:int=30; aspect_ratio:str='16:9'; codec:str='H264'; container:str='MP4'; pixel_format:str='yuv420p'; music:bool=False; subtitles:bool=False; srt:bool=False; logo:bool=False; final_hold_seconds:float=1.5; transition:str='hard_cut'
 def __post_init__(self):
  if (self.width,self.height,self.fps)!=(1920,1080,30) or not 1<=self.final_hold_seconds<=2: raise ValueError('invalid output contract')
class ResearchProvider(Protocol):
 def research(self,topic:str)->Mapping[str,Any]:...
class ScriptProvider(Protocol):
 def write(self,research:Mapping[str,Any],*,language:str)->str:...
def generation_request(scene,bible:Bible,seed:int):
 bible.validate_continuity(scene['continuity'])
 return {'scene':scene,'bible':asdict(bible),'references':sorted(set(sum((list(x.reference_ids) for x in bible.characters+bible.environments+bible.props),[]))),'seed':seed}