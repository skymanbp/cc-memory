"""D: /save-memories writes to <project>/memory/memory.db, not .ccm/ (v2.13.0 rename miss)."""
import sys, re, sqlite3, subprocess
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _h

sb = _h.make_sandbox()
try:
    proj = sb / "projA"
    db, pid, _ = _h.seed_project(proj, [
        {"content": "Existing fact: the API gateway times out after 30 seconds by default"}])
    print("seeded .ccm/memory.db, active memories:",
          db.get_stats(pid)["n_memories"])

    skill = (_h.REPO / "skills" / "save-memories" / "SKILL.md").read_text(encoding="utf-8")
    m = re.search(r"```bash\n(python3 -c \".*?\n\")\n```", skill, re.S)
    assert m, "could not extract the skill's bash block"
    block = m.group(1)
    assert "# ADD MEMORIES HERE" in block
    block = block.replace(
        "    # ADD MEMORIES HERE — see Step 2 for fields.",
        "    {'category': 'decision', 'content': 'Chose SQLite WAL mode for the memory "
        "store because it tolerates concurrent hook writers', 'importance': 4, "
        "'topic': 'storage'},")
    sh = sb / "skill.sh"
    sh.write_text(block + "\n", encoding="utf-8")

    # Run exactly as Claude would: bash, cwd = the project, plugin root set.
    rc, out, err = _h.run(["bash", str(sh)], cwd=proj,
                          extra_env={"CLAUDE_PLUGIN_ROOT": str(_h.REPO)})
    _h.show("skill run", rc, out, err)

    def count(dbfile):
        if not dbfile.exists():
            return "ABSENT"
        c = sqlite3.connect(f"file:{dbfile}?mode=ro", uri=True)
        try:
            return c.execute("SELECT COUNT(*) FROM memories WHERE is_active=1").fetchone()[0]
        finally:
            c.close()

    print("\nAFTER the skill:")
    print("  .ccm/memory.db    active memories:", count(proj / ".ccm" / "memory.db"))
    print("  memory/memory.db  active memories:", count(proj / "memory" / "memory.db"))
    print("  memory/.gitignore exists:", (proj / "memory" / ".gitignore").exists())
    print("  memory/MEMORY.md  exists:", (proj / "memory" / "MEMORY.md").exists())
    print("  .ccm/MEMORY.md mentions WAL:",
          "WAL" in ((proj / ".ccm" / "MEMORY.md").read_text(encoding="utf-8")
                    if (proj / ".ccm" / "MEMORY.md").exists() else ""))

    # What every other surface resolves to:
    from core.layout import memory_dir, find_db_path
    print("  core.layout.memory_dir(proj) ->", memory_dir(proj).name,
          "| find_db_path ->", find_db_path(proj).relative_to(proj))
    rc, out, err = _h.mem(["search", "WAL"], proj, cwd=proj)
    _h.show("/cc-mem search WAL (reads .ccm)", rc, out, err)
    rc, out, err = _h.mem(["status"], proj, cwd=proj)
    print("  /cc-mem status warns about memory/ ?",
          "memory/" in out or "Separate database" in out)
    print("\ngit view of the project root:")
    subprocess.run(["git", "init", "-q"], cwd=proj)
    rc, out, err = _h.run(["git", "status", "--porcelain", "--untracked-files=all"], cwd=proj)
    print(out)
finally:
    _h.destroy_sandbox()
