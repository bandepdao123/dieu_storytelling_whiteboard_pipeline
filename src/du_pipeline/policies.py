from dataclasses import dataclass

class TimingMismatch(ValueError): pass

@dataclass(frozen=True)
class DurationPolicy:
    early_until: float = 180
    early: tuple[int,int] = (5,8)
    late: tuple[int,int] = (10,20)
    def bounds_at(self, timeline_seconds: float): return self.early if timeline_seconds < self.early_until else self.late
    def choose(self, timeline_seconds: float, semantic_seconds: float | None=None):
        lo, hi = self.bounds_at(timeline_seconds)
        return max(lo, min(hi, semantic_seconds if semantic_seconds is not None else (lo+hi)/2))

def validate_timing(audio_ms: int, cues, tolerance_ms: int=500):
    if audio_ms <= 0 or not cues: raise TimingMismatch("missing audio/cues")
    previous = 0
    for start,end,text in cues:
        if start < previous or end <= start or end > audio_ms or not str(text).strip(): raise TimingMismatch("invalid/overlapping cue")
        previous=end
    if abs(audio_ms-cues[-1][1]) > tolerance_ms: raise TimingMismatch("SRT terminal drift")
    return True

def required_approval(code: str, special: bool): return special or int(code[1:]) <= 5

@dataclass(frozen=True)
class SchedulerConfig:
    cpu_limit:int=4; memory_mb:int=3072; image_workers:int=1; animation_workers:int=1; light_workers:int=2; queue_size:int=8

class ResourceScheduler:
    """Side-effect-free admission policy; executors own actual processes."""
    def __init__(self,config=SchedulerConfig()): self.config=config; self.running={"image":0,"animation":0,"light":0}; self.queued=[]
    def submit(self,kind,item):
        if kind not in self.running: raise ValueError('unknown resource class')
        if len(self.queued)>=self.config.queue_size: raise OverflowError('scheduler queue full')
        self.queued.append((kind,item))
    def acquire(self):
        limits={"image":self.config.image_workers,"animation":self.config.animation_workers,"light":self.config.light_workers}
        for i,(kind,item) in enumerate(self.queued):
            if self.running[kind]<limits[kind]: self.queued.pop(i); self.running[kind]+=1; return kind,item
        return None
    def release(self,kind):
        if self.running.get(kind,0)<=0: raise ValueError('resource not acquired')
        self.running[kind]-=1
