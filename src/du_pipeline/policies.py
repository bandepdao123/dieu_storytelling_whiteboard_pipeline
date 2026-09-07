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

def local_visual_frames(audio_ms, intervals):
    """Exact ms coverage, then cumulative nearest-frame rounding (ties to even)."""
    from fractions import Fraction
    recovery = ('; local assembly timing unsupported. Recovery: retain the original SRT/audio; '
                'use a separately approved continuous visual-coverage plan in an external editor '
                'with explicit gap/outro footage. This cue-based planner has no gap/outro API; '
                'do not close silence, extend stills or rewrite subtitle text. '
                'Retry here only with independently valid contiguous source timing.')
    previous = 0
    frames = []
    if type(audio_ms) is not int or audio_ms <= 0 or not intervals:
        raise TimingMismatch('missing/inexact integer-ms audio or visual intervals'+recovery)
    for i,(start,end) in enumerate(intervals,1):
        if type(start) is not int or type(end) is not int or start != previous or end <= start or end > audio_ms:
            raise TimingMismatch(f'interval {i}: expected start {previous} ms, got {start}..{end} ms'+recovery)
        count = round(Fraction(end*30,1000))-round(Fraction(start*30,1000))
        if count < 1: raise TimingMismatch(f'interval {i} rounds to zero frames'+recovery)
        frames.append(count); previous = end
    if previous != audio_ms:
        raise TimingMismatch(f'visual tail ends at {previous} ms, audio ends at {audio_ms} ms'+recovery)
    if sum(frames) != round(Fraction(audio_ms*30,1000)):
        raise TimingMismatch('frame allocation mismatch'+recovery)
    return frames

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
