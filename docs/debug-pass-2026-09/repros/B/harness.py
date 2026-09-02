"""Shared sandbox harness for reviewer B. Import-free of the package until sandbox() ran."""
import json, os, shutil, subprocess, sys, tempfile, time
from pathlib import Path

REPO = Path("/home/user/cc-memory")
PKG = REPO / "cc_memory"
HOOKS = PKG / "hooks"

class Sandbox:
    def __init__(self):
        self.root = Path(tempfile.mkdtemp(prefix="ccm-B-"))
        self.home = self.root / "home"; self.home.mkdir()
        self.tmp = self.root / "tmp"; self.tmp.mkdir()
        self.proj = self.root / "proj"; self.proj.mkdir()
        self.hookcwd = self.root / "hookcwd"; self.hookcwd.mkdir()   # process cwd for hooks
        self.env = dict(os.environ)
        for k in ("HOME", "USERPROFILE"):
            self.env[k] = str(self.home)
        for k in ("TMPDIR", "TEMP", "TMP"):
            self.env[k] = str(self.tmp)
        for k in ("ANTHROPIC_API_KEY", "CLAUDE_PLUGIN_ROOT", "CC_MEMORY_PLAN_ENFORCE"):
            self.env.pop(k, None)
        self.env["PYTHONDONTWRITEBYTECODE"] = "1"
        # also apply to THIS process so in-process imports land in the sandbox
        os.environ.update({k: self.env[k] for k in ("HOME","USERPROFILE","TMPDIR","TEMP","TMP")})
        for k in ("ANTHROPIC_API_KEY", "CLAUDE_PLUGIN_ROOT", "CC_MEMORY_PLAN_ENFORCE"):
            os.environ.pop(k, None)
        tempfile.tempdir = None
        assert Path.home() == self.home, Path.home()
        assert Path(tempfile.gettempdir()) == self.tmp, tempfile.gettempdir()

    def run_hook(self, name, payload, stdin_bytes=None, env_extra=None, timeout=60, cwd=None, args=()):
        env = dict(self.env)
        if env_extra: env.update(env_extra)
        data = stdin_bytes if stdin_bytes is not None else json.dumps(payload).encode("utf-8")
        t0 = time.monotonic()
        p = subprocess.run([sys.executable, str(HOOKS / f"{name}.py"), *args], input=data,
                           capture_output=True, env=env, cwd=str(cwd or self.hookcwd), timeout=timeout)
        return dict(rc=p.returncode, out=p.stdout.decode("utf-8", "replace"),
                    err=p.stderr.decode("utf-8", "replace"), secs=time.monotonic() - t0)

    def planted(self):
        """Any .ccm/ or memory/ created in the hook's own dir or the hooks source dir."""
        hits = []
        for base in (self.hookcwd, HOOKS, PKG, REPO):
            for n in (".ccm", "memory"):
                if (base / n).exists() and base != REPO:
                    hits.append(str(base / n))
        return hits

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)

def add_pkg_path():
    if str(PKG) not in sys.path:
        sys.path.insert(0, str(PKG))
