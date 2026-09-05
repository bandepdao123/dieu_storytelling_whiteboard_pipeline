import shlex
from dataclasses import dataclass
from enum import Enum
SHEET_TABS=("Projects","Jobs","Stages","Scenes","Attempts","Artifacts","Approvals","Events","Costs","Timings","Learnings")
DRIVE=("00_inputs","01_references","02_audio_srt","03_scenes","04_images","05_animation","06_qa","07_exports","99_failed_metadata")
def drive_folders(pid): return tuple(f"{pid}/{x}" for x in DRIVE)
class Role(str,Enum): OWNER="OWNER"; REVIEWER="REVIEWER"; OPERATOR="OPERATOR"
@dataclass(frozen=True)
class Command: name:str; args:tuple[str,...]
ALIASES={"phe-duyet":"approve","tu-choi":"reject","tam-dung":"pause","tiep-tuc":"resume","thu-lai":"retry","trang-thai":"status","chi-phi":"costs","loi":"errors","approve":"approve","reject":"reject","pause":"pause","resume":"resume","retry":"retry","status":"status"}
def parse_discord(text):
    parts=shlex.split(text.strip())
    if not parts or not parts[0].startswith("du-"): raise ValueError("command must begin du-")
    raw=parts[0][3:]; return Command(ALIASES.get(raw,raw),tuple(parts[1:]))
def authorize(role, command):
    if role==Role.OWNER:return True
    if role==Role.REVIEWER:return command in {"approve","reject","status","costs","errors"}
    return command in {"import","plan","batch","animate","pause","resume","retry","status","costs","errors"}
