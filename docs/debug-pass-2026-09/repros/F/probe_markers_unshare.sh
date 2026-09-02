#!/bin/sh
# Faithful repro: a mount namespace where /tmp, /var/tmp and /usr/tmp are READ-ONLY and TMPDIR/TEMP/TMP are unset,
# i.e. "no usable temporary directory" -- the locked-down-sandbox state core/markers.py's own docstring says it met.
set -e
BOX=$(mktemp -d /root/F-nosystmp-XXXXXX)
trap 'rm -rf "$BOX"' EXIT
mkdir -p "$BOX/home" "$BOX/user-repo"
cd "$BOX/user-repo" && git init -q . && echo "# my project" > README.md && git add -A && git -c user.email=f@x -c user.name=f commit -qm init
unshare -m sh -c '
  mount -t tmpfs -o ro,size=64k none /tmp
  mount -t tmpfs -o ro,size=64k none /var/tmp
  [ -d /usr/tmp ] && mount -t tmpfs -o ro,size=64k none /usr/tmp
  cd '"$BOX/user-repo"'
  env -u TMPDIR -u TEMP -u TMP HOME='"$BOX/home"' USERPROFILE='"$BOX/home"' python3 - <<PY
import os, sys, tempfile
sys.path.insert(0, "/home/user/cc-memory/cc_memory")
print("tempfile.gettempdir() ->", tempfile.gettempdir())
from core import markers
d = markers.marker_dir(); print("marker_dir() ->", d)
p = markers.marker_path("cc_mem_prompt_", markers.safe_id("sess-1"))
print("write_marker(prompt) ->", markers.write_marker(p, "please rotate the prod DB password to hunter2"))
print("read_marker ->", markers.read_marker(p))
os.system("git status --short")
PY
'
