"""Local media boundaries. No network providers are called here."""
from dataclasses import dataclass
from pathlib import Path
import json, shutil, subprocess

@dataclass(frozen=True)
class ProbeContract:
    min_width:int=1920; min_height:int=1080; min_fps:float=30
    def validate(self, data):
        stream=next((x for x in data.get('streams',[]) if x.get('codec_type')=='video'),None)
        if not stream: raise ValueError('video stream required')
        rate=stream.get('avg_frame_rate','0/1').split('/'); fps=float(rate[0])/float(rate[1])
        if stream.get('width',0)<self.min_width or stream.get('height',0)<self.min_height or fps<self.min_fps or stream.get('pix_fmt')!='yuv420p' or stream.get('codec_name')!='h264': raise ValueError('media contract failed')
        return True

def probe(path, runner=subprocess.run):
    cmd=['ffprobe','-v','error','-show_streams','-of','json',str(path)]
    result=runner(cmd,capture_output=True,text=True,check=True); data=json.loads(result.stdout); ProbeContract().validate(data); return data

class FastWhiteboardAdapter:
    def command(self,image,output,duration,hold=1.5):
        if not 1<=hold<=2 or duration<=hold: raise ValueError('hold must fit inside scene duration')
        return ['ffmpeg','-y','-loop','1','-i',str(image),'-t',str(duration),'-vf','scale=1920:1080,format=yuv420p','-r','30','-c:v','libx264','-an',str(output)]
    def render(self,image,output,duration,*,dry_run=False,fake=False):
        cmd=self.command(image,output,duration)
        if dry_run:return cmd
        if fake: Path(output).write_bytes(b'fake-mp4'); return output
        subprocess.run(cmd,check=True); probe(output); return output

class FinalAssembler:
    def command(self,concat_file,output): return ['ffmpeg','-y','-f','concat','-safe','0','-i',str(concat_file),'-c:v','libx264','-pix_fmt','yuv420p','-r','30','-an',str(output)]
    def assemble(self,concat_file,output,*,dry_run=False,fake=False):
        cmd=self.command(concat_file,output)
        if dry_run:return cmd
        if fake: Path(output).write_bytes(b'fake-final'); return output
        subprocess.run(cmd,check=True); probe(output); return output
