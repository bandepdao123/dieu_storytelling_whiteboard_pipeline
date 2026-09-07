"""Executable README metadata quickstart; media E2E remains public service API."""
import os
from pathlib import Path
import re
import subprocess
import sys


def test_readme_quickstart(tmp_path):
    repo=Path(__file__).resolve().parents[1]
    readme=(repo/'README.md').read_text()
    code=re.search(r"python - <<'PY'\n(.*?)\nPY",readme,re.S).group(1)
    # Faithful executable entry point wrapper for source test; installed-wheel
    # validation executes this same README block with the actual console script.
    wrapper=tmp_path/'du-pipeline'
    wrapper.write_text('#!'+sys.executable+'\nfrom du_pipeline.cli import main\nraise SystemExit(main())\n')
    wrapper.chmod(0o700)
    env=dict(os.environ, PATH=str(tmp_path)+os.pathsep+os.environ['PATH'],
             PYTHONPATH=str(repo/'src'), PYTHONDONTWRITEBYTECODE='1')
    result=subprocess.run([sys.executable,'-c',code],env=env,cwd=tmp_path,capture_output=True,text=True)
    assert result.returncode==0, result.stderr
    assert 'Demo' in result.stdout


def test_readme_assemble_positional_parser():
    from du_pipeline.cli import parser
    args=parser().parse_args(['--db','p.db','assemble','PROJECT_ID','OUTPUT_PATH','--dry-run'])
    assert args.output=='OUTPUT_PATH' and args.dry_run
