import os, sys, tempfile, shutil
SB = tempfile.mkdtemp()
for k in ("HOME","USERPROFILE","TMPDIR","TEMP","TMP"):
    os.environ[k] = SB
os.environ.pop("ANTHROPIC_API_KEY", None)
sys.path.insert(0, "/home/user/cc-memory/cc_memory")
import pathlib
assert str(pathlib.Path.home()) == SB, (pathlib.Path.home(), SB)
def cleanup():
    shutil.rmtree(SB, ignore_errors=True)
