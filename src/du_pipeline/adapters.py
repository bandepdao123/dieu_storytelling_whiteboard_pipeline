import shlex
from dataclasses import dataclass
from enum import Enum
SHEET_TABS=("Projects","Jobs","Stages","Scenes","Attempts","Artifacts","Approvals","Events","Costs","Timings","Learnings")
DRIVE=("00_inputs","01_references","02_audio_srt","03_scenes","04_images","05_animation","06_qa","07_exports","99_failed_metadata")
def drive_folders(pid): return tuple(f"{pid}/{x}" for x in DRIVE)
class Role(str,Enum): OWNER="OWNER"; REVIEWER="REVIEWER"; OPERATOR="OPERATOR"
@dataclass(frozen=True)
class Command: name:str; args:tuple[str,...]
class CommandError(ValueError):
    """Safe user-facing command error (never leaks parser/index errors)."""
    def __init__(self,code,message): self.code=code; super().__init__(message)

ALIASES={
 "tao-du-an":"project-create","trang-thai":"project-status","cau-hinh":"project-config",
 "bat-dau":"project-start","tam-dung":"project-pause","tiep-tuc":"project-resume","huy":"project-cancel",
 "nhap":"import","lap-ke-hoach":"plan","duyet-pilot":"pilot-approve","tu-choi-pilot":"pilot-reject",
 "duyet-lo":"batch-approve","tu-choi-lo":"batch-reject","canh":"scene-status","thu-lai-canh":"scene-retry",
 "duyet-canh":"scene-approve","tu-choi-canh":"scene-reject","thay-canh":"scene-replace",
 "chay-cong-doan":"stage-run","chay-lai-cong-doan":"stage-rerun","danh-sach-checkpoint":"checkpoint-list",
 "khoi-phuc-checkpoint":"checkpoint-restore","de-xuat-phu-thuoc":"dependency-propose",
 "duyet-phu-thuoc":"dependency-approve","ap-dung-phu-thuoc":"dependency-apply",
 "chon-nha-cung-cap":"provider-select","chon-preset":"preset-select","chi-phi":"cost-report","loi":"error-report",
 "duyet-cuoi":"final-approve",
 # backwards compatible spellings
 "phe-duyet":"approve","tu-choi":"reject","pause":"pause","resume":"resume",
 "retry":"retry","status":"status"}
def parse_discord(text):
    try: parts=shlex.split(text.strip())
    except ValueError as exc: raise CommandError('INVALID_ARGUMENTS','malformed quoting') from exc
    if not parts or not parts[0].startswith("du-"): raise CommandError("INVALID_COMMAND","command must begin du-")
    raw=parts[0][3:]
    if raw not in ALIASES and raw not in set(ALIASES.values()): raise CommandError("UNKNOWN_COMMAND",raw)
    return Command(ALIASES.get(raw,raw),tuple(parts[1:]))
def authorize(role, command):
    if role==Role.OWNER:return True
    if role==Role.REVIEWER:return command in {"approve","reject","status","project-status","scene-status","pilot-approve","pilot-reject","batch-approve","batch-reject","scene-approve","scene-reject","cost-report","error-report","final-approve"}
    return command in {"pause","resume","retry","status","project-status","project-start","project-pause","project-resume","import","plan","batch","animate","scene-status","scene-retry","scene-replace","stage-run","checkpoint-list","provider-select","preset-select","cost-report","error-report","costs","errors","dependency-propose"}
