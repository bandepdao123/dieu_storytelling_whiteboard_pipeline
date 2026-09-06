"""Local media boundaries. No network providers are called here."""
from dataclasses import dataclass
from pathlib import Path
from fractions import Fraction
import json, shutil, subprocess

class AssemblyError(RuntimeError): pass

class LocalFinalAssembler:
    def __init__(self,managed_root,timeout=300,runner=subprocess.run): self.root=Path(managed_root).resolve(strict=True); self.timeout=timeout; self.runner=runner
    def normalize_command(self,source,output,duration,image=False,frames=None):
        frames=round(duration*30) if frames is None else frames
        if frames < 1: raise AssemblyError('scene must contain at least one frame')
        prefix=['ffmpeg','-y']+(['-loop','1'] if image else [])+['-i',str(source)]
        vf=f'scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30,tpad=stop_mode=clone:stop_duration={duration},trim=end_frame={frames},format=yuv420p'
        return prefix+['-map','0:v:0','-vf',vf,'-frames:v',str(frames),'-c:v','libx264','-pix_fmt','yuv420p','-r','30','-an',str(output)]
    def concat_command(self,list_file,output): return ['ffmpeg','-y','-f','concat','-safe','0','-i',str(list_file),'-map','0:v:0','-c','copy','-an',str(output)]
    def mux_command(self,video,audio,output): return ['ffmpeg','-y','-i',str(video),'-i',str(audio),'-map','0:v:0','-map','1:a:0','-c:v','copy','-c:a','aac','-b:a','192k',str(output)]
    def run(self,cmd):
        try:return self.runner(cmd,check=True,capture_output=True,text=True,timeout=self.timeout)
        except (subprocess.SubprocessError,OSError) as e: raise AssemblyError('media command failed: '+(getattr(e,'stderr','') or str(e))[-2000:]) from e
    def tool_versions(self):
        """Return exact media-tool banners for durable assembly evidence."""
        versions={}
        for tool in ('ffmpeg','ffprobe'):
            result=self.run([tool,'-version'])
            versions[tool]=(result.stdout or '').splitlines()[0].strip()
            if not versions[tool]: raise AssemblyError(f'{tool} did not report a version')
        return versions
    def probe_json(self,path):
        try:return json.loads(self.run(['ffprobe','-v','error','-show_streams','-show_format','-of','json',str(path)]).stdout)
        except json.JSONDecodeError as e: raise AssemblyError('invalid ffprobe output') from e
    @staticmethod
    def _number(value):
        try:
            number=float(value)
            return number if number >= 0 and number != float('inf') else None
        except (TypeError,ValueError,OverflowError): return None
    def _stream_duration(self,path,stream):
        direct=self._number(stream.get('duration'))
        if direct is not None: return direct
        ticks=self._number(stream.get('duration_ts'))
        try: base=float(Fraction(stream.get('time_base','')))
        except (ValueError,ZeroDivisionError): base=None
        if ticks is not None and base is not None and base > 0: return ticks*base
        tag=(stream.get('tags') or {}).get('DURATION')
        if isinstance(tag,str):
            try:
                h,m,s=tag.split(':'); tagged=int(h)*3600+int(m)*60+float(s)
                if tagged >= 0: return tagged
            except (ValueError,TypeError): pass
        try:
            payload=json.loads(self.run(['ffprobe','-v','error','-show_packets','-select_streams',str(stream['index']),'-show_entries','packet=pts_time,dts_time,duration_time','-of','json',str(path)]).stdout)
        except (KeyError,json.JSONDecodeError): return None
        starts=[]; ends=[]
        for packet in payload.get('packets',[]):
            start=self._number(packet.get('pts_time'))
            if start is None: start=self._number(packet.get('dts_time'))
            length=self._number(packet.get('duration_time'))
            if start is not None:
                starts.append(start)
                if length is not None: ends.append(start+length)
        return max(ends)-min(starts) if starts and ends and max(ends)>=min(starts) else None
    def validate_final(self,path,expected):
        data=self.probe_json(path); streams=data.get('streams',[]); videos=[s for s in streams if s.get('codec_type')=='video']; audios=[s for s in streams if s.get('codec_type')=='audio']
        if len(streams)!=2 or len(videos)!=1 or len(audios)!=1: raise AssemblyError('final requires exactly one video and one audio stream')
        v,a=videos[0],audios[0]
        try: fps=float(Fraction(v.get('avg_frame_rate','0/1')))
        except (ValueError,ZeroDivisionError): fps=0
        if (v.get('codec_name'),v.get('width'),v.get('height'),v.get('pix_fmt'))!=('h264',1920,1080,'yuv420p') or abs(fps-30)>.01 or a.get('codec_name')!='aac': raise AssemblyError('final stream contract failed')
        vd=self._stream_duration(path,v); ad=self._stream_duration(path,a); tolerance=1/30+.020
        if vd is None or ad is None: raise AssemblyError('final stream duration unavailable')
        if abs(vd-expected)>tolerance or abs(ad-expected)>tolerance or abs(vd-ad)>tolerance: raise AssemblyError('final duration mismatch')
        return data

@dataclass(frozen=True)
class ProbeContract:
    min_width:int=1920; min_height:int=1080; min_fps:float=30
    def validate(self, data):
        stream=next((x for x in data.get('streams',[]) if x.get('codec_type')=='video'),None)
        if not stream: raise ValueError('video stream required')
        rate=stream.get('avg_frame_rate','0/1').split('/'); fps=float(rate[0])/float(rate[1])
        if stream.get('width',0)<self.min_width or stream.get('height',0)<self.min_height or fps<self.min_fps or stream.get('pix_fmt')!='yuv420p' or stream.get('codec_name')!='h264': raise ValueError('media contract failed')
        return True

def probe(path, runner=subprocess.run, *, managed_root=None, timeout=30):
    if managed_root is None: raise ValueError('managed_root required')
    root=Path(managed_root).resolve(strict=True); path=Path(path).resolve(strict=True); path.relative_to(root)
    cmd=['ffprobe','-v','error','-show_streams','-of','json',str(path)]
    result=runner(cmd,capture_output=True,text=True,check=True,timeout=timeout); data=json.loads(result.stdout); ProbeContract().validate(data); return data

class FastWhiteboardAdapter:
    def command(self,image,output,duration,hold=1.5):
        if not 1<=hold<=2 or duration<=hold: raise ValueError('hold must fit inside scene duration')
        return ['ffmpeg','-y','-loop','1','-i',str(image),'-t',str(duration),'-vf','scale=1920:1080,format=yuv420p','-r','30','-c:v','libx264','-an',str(output)]
    def render(self,image,output,duration,*,managed_root=None,timeout=300,dry_run=False,fake=False,runner=subprocess.run):
        cmd=self.command(image,output,duration)
        if dry_run:return cmd
        if managed_root is None: raise ValueError('managed_root required')
        root=Path(managed_root).resolve(strict=True); image=Path(image).resolve(strict=True); output=Path(output).resolve(); image.relative_to(root); output.relative_to(root)
        if fake: Path(output).write_bytes(b'fake-mp4'); return output
        runner(cmd,check=True,timeout=timeout); probe(output,managed_root=root,timeout=timeout); return output

class FinalAssembler:
    def __init__(self,managed_root=None,timeout=300): self.managed_root=Path(managed_root).resolve() if managed_root else None; self.timeout=timeout
    def command(self,concat_file,output): return ['ffmpeg','-y','-f','concat','-safe','1','-i',str(concat_file),'-c:v','libx264','-pix_fmt','yuv420p','-r','30','-an',str(output)]
    def _validate(self,concat_file):
        if not self.managed_root: raise ValueError('managed_root required')
        cf=Path(concat_file).resolve(strict=True); cf.relative_to(self.managed_root)
        for line in cf.read_text().splitlines():
            if line.strip().startswith('file '):
                value=line.strip()[5:].strip().strip("'\""); p=(cf.parent/value).resolve(strict=True); p.relative_to(self.managed_root)
    def assemble(self,concat_file,output,*,dry_run=False,fake=False,runner=subprocess.run):
        self._validate(concat_file)
        if not self.managed_root: raise ValueError('managed_root required')
        out=Path(output).resolve(); out.relative_to(self.managed_root)
        cmd=self.command(concat_file,out)
        if dry_run:return cmd
        if fake: Path(output).write_bytes(b'fake-final'); return output
        runner(cmd,check=True,capture_output=True,text=True,timeout=self.timeout); probe(out,managed_root=self.managed_root,timeout=self.timeout); return output
