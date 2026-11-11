#!/usr/bin/env python3
"""Build the frozen W2 post-run-classify fixture. Synthetic only.

No real transcript text: every output below is generated from fixed word
lists with a seeded RNG. Run once, commit fixture.json, record its hash
(eval-genius hash_fixture.py). Regenerating must give the same bytes.

Families:
  paired       12 bare commands (pattern-specific compressors) + 8 prefixed
               variants each, same output.
  negative     must stay byte-identical to baseline.
  destructive  side-effecting or unknown commands with compressible output.
  secrets      compressible output with synthetic credentials.
Split: every third item (by generation order) is held out.
"""
from __future__ import annotations

import json
import random
import sys
from pathlib import Path

RNG = random.Random(20261110)
WORDS = ("alpha beta gamma delta epsilon zeta theta kappa lambda sigma omega "
         "render parse fetch store index route queue cache token budget").split()
DIRS = ("src", "lib", "core", "api", "web", "util", "model", "tests")
EXT = ("py", "ts", "go", "rs", "md")


def _name(i: int) -> str:
    return f"{DIRS[i % len(DIRS)]}/{WORDS[i % len(WORDS)]}_{i:03d}.{EXT[i % len(EXT)]}"


def _sentence(n: int = 6) -> str:
    return " ".join(RNG.choice(WORDS) for _ in range(n))


def out_git_diff(files: int = 8, hunks: int = 3) -> str:
    lines = []
    for f in range(files):
        p = _name(f)
        lines += [f"diff --git a/{p} b/{p}", f"index {f:07x}1..{f:07x}2 100644",
                  f"--- a/{p}", f"+++ b/{p}"]
        for h in range(hunks):
            start = 10 + h * 40
            lines.append(f"@@ -{start},12 +{start},13 @@ def fn_{f}_{h}():")
            for k in range(12):
                tag = " " if k % 4 else ("-" if k % 8 else "+")
                lines.append(f"{tag}    value_{k} = {RNG.choice(WORDS)}({RNG.randint(1, 99)})")
            lines.append(f"+    added_{h} = {_sentence(3).replace(' ', '_')}")
    return "\n".join(lines) + "\n"


def out_git_status(n: int = 40) -> str:
    lines = ["On branch feature/synthetic", "Your branch is up to date with 'origin/feature/synthetic'.", "",
             "Changes not staged for commit:", '  (use "git add <file>..." to update what will be committed)',
             '  (use "git restore <file>..." to discard changes in working directory)']
    lines += [f"\tmodified:   {_name(i)}" for i in range(n)]
    lines += ["", "Untracked files:", '  (use "git add <file>..." to include in what will be committed)']
    lines += [f"\t{_name(100 + i)}" for i in range(n // 2)]
    lines += ["", 'no changes added to commit (use "git add" and/or "git commit -a")']
    return "\n".join(lines) + "\n"


def out_grep(n: int = 150, with_line: bool = True) -> str:
    rows = []
    for i in range(n):
        ln = f"{RNG.randint(1, 900)}:" if with_line else ""
        rows.append(f"{_name(i % 30)}:{ln}    result = lookup_handler({RNG.choice(WORDS)!r}, depth={i % 7})")
    return "\n".join(rows) + "\n"


def out_du(n: int = 80) -> str:
    return "\n".join(f"{RNG.randint(1, 999)}K\t{_name(i).rsplit('.', 1)[0]}" for i in range(n)) + "\n"


def out_wc(n: int = 80) -> str:
    rows = [f"{RNG.randint(10, 2000):>8} {_name(i)}" for i in range(n)]
    return "\n".join(rows + [f"{sum(range(n)) * 7:>8} total"]) + "\n"


def out_pytest(n: int = 120) -> str:
    rows = ["============================= test session starts ==============================",
            "platform darwin -- Python 3.11.9, pytest-8.3.3, pluggy-1.5.0",
            "rootdir: /work/project", f"collected {n} items", ""]
    rows += [f"tests/test_{WORDS[i % len(WORDS)]}.py::test_case_{i:03d} PASSED{' ' * 20}[{(i + 1) * 100 // n:3d}%]"
             for i in range(n)]
    rows += ["", f"============================== {n} passed in 4.21s =============================="]
    return "\n".join(rows) + "\n"


def out_sqlite(n: int = 120) -> str:
    return "\n".join(f"{i}|{RNG.choice(WORDS)}|{RNG.choice(WORDS)}_{i}|{RNG.randint(0, 9999)}|2026-01-{1 + i % 28:02d}"
                     for i in range(n)) + "\n"


def out_ls_la(n: int = 90) -> str:
    rows = [f"total {n * 8}", "drwxr-xr-x  12 user  staff   384 Jan  5 10:00 .",
            "drwxr-xr-x   8 user  staff   256 Jan  5 09:00 .."]
    rows += [f"-rw-r--r--   1 user  staff  {RNG.randint(100, 99999):>5} Jan  {1 + i % 9} 1{i % 10}:0{i % 6} "
             f"{WORDS[i % len(WORDS)]}_{i:03d}.{EXT[i % len(EXT)]}" for i in range(n)]
    return "\n".join(rows) + "\n"


def out_ls_1(n: int = 120) -> str:
    return "\n".join(f"{WORDS[i % len(WORDS)]}_{i:03d}.{EXT[i % len(EXT)]}" for i in range(n)) + "\n"


def out_file_content(n: int = 120) -> str:
    return "\n".join(f"def fn_{i}(x):  # {_sentence(4)}\n    return x + {i}" for i in range(n // 2)) + "\n"


def out_generic_log(n: int = 200) -> str:
    return "\n".join(f"[2026-01-01 00:{i % 60:02d}:00] info worker-{i % 4} processed batch {i} ({_sentence(3)})"
                     for i in range(n)) + "\n"


# 12 bare commands across the pattern-specific compressors.
BARE = [
    ("git diff", out_git_diff),
    ("git diff HEAD~1 -- src", lambda: out_git_diff(6, 4)),
    ("git status", out_git_status),
    ("grep -rn lookup_handler .", out_grep),
    ("rg -n lookup_handler", lambda: out_grep(140)),
    ("du -sh src/*", out_du),
    ("wc -l src/*.py", out_wc),
    ("pytest -v tests", out_pytest),
    ("python3 -m pytest -v", lambda: out_pytest(100)),
    ('sqlite3 app.db "select * from items"', out_sqlite),
    ("ls -la", out_ls_la),
    ("ls -1 src", out_ls_1),
]


def variants(bare: str) -> list[tuple[str, str]]:
    git_c = ("git -C /work/project " + bare[4:]) if bare.startswith("git ") else f"cd /work/project && cd sub && {bare}"
    return [
        ("cd_and", f"cd /work/project && {bare}"),
        ("cd_semi", f"cd /work/project; {bare}"),
        ("export_semi", f"export X=1; {bare}"),
        ("inline_assign", f"X=1 {bare}"),
        ("newline", f"cd /work/project\n{bare}"),
        ("git_c_or_double_cd", git_c),
        ("quoted_space", f'cd "/work/my project" && {bare}'),
        ("stacked", f"set -e; cd /work/project && export Y=2; {bare}"),
    ]


def build(version: int = 2) -> dict:
    """version 1 = prereg-20261110 fixture; version 2 adds the errline family."""
    RNG.seed(20261110)  # same bytes on every call, not only the first per process
    items = []

    def add(fam, cid, cmd, stdout, **kw):
        item = {"id": cid, "family": fam, "command": cmd, "stdout": stdout,
                "stderr": kw.pop("stderr", ""), "exit_code": kw.pop("exit_code", 0)}
        item.update(kw)
        items.append(item)

    for bi, (bare, gen) in enumerate(BARE):
        out = gen()
        bid = f"P{bi:02d}-bare"
        add("paired", bid, bare, out, pair_of=None)
        for tag, cmd in variants(bare):
            add("paired", f"P{bi:02d}-{tag}", cmd, out, pair_of=bid)

    fc = out_file_content()
    neg = [
        ("sed -n 1,200p src/core.py", fc), ("cat src/core.py", fc), ("head -n 200 src/core.py", fc),
        ("tail -n 200 src/core.py", fc), ("nl src/core.py", fc), ("git show HEAD:src/core.py", fc),
    ]
    for pre in ("", "cd /work/project && ", "cd /work/project; ", "export X=1; ", "cd /work/project\n"):
        for j, (c, o) in enumerate(neg):
            add("negative", f"N-fc{j}-{len(pre)}", pre + c, o)
    for j, c in enumerate(("cd /work/project && git -C /work/project show HEAD~2:src/a.py",
                           "git -C /work/project show v1.0:README.md")):
        add("negative", f"N-gitshow-{j}", c, fc)
    # Failing commands (non-zero exit and failure stderr).
    for j, (bare, gen) in enumerate(BARE):
        add("negative", f"N-fail-{j}", f"cd /work/project && {bare}", gen(), exit_code=1 + j % 2,
            stderr="" if j % 2 else "fatal: something went wrong")
    # Error words in stdout (dense enough to trip the stdout error gate).
    for j, (bare, gen) in enumerate(BARE):
        body = gen().splitlines()
        body = [ln if k % 3 else f"ERROR: {ln}" for k, ln in enumerate(body)]
        add("negative", f"N-errword-{j}", f"cd /work/project && {bare}", "\n".join(body) + "\n")
    # Multi-statement, loops, heredocs, substitutions, ||, background.
    multi = [
        "cd /work/project && git status && git diff",
        "git diff; git status",
        "for f in src/*.py; do wc -l $f; done",
        "while read l; do echo $l; done < list.txt",
        "cd /work/project && cat <<'EOF' | python3 -\nprint(1)\nEOF",
        "cd /work/project && ls $(git rev-parse --show-toplevel)",
        "cd /work/project && wc -l `ls`",
        "cd /work/project && git diff || true",
        "cd /work/project && (git status)",
        "cd /work/project && { git status; }",
        "cd /work/project & git status",
        "if true; then git status; fi",
        "cd - && git status",
        "cd /work/project && git diff > out.txt && cat out.txt",
        "cd /work/project && python3 script.py",
        "cd /work/project && git log -5",
    ]
    for j, c in enumerate(multi):
        add("negative", f"N-multi-{j}", c, out_git_status() if j % 2 else out_git_diff())
    # Outputs under 100 chars.
    for j, (bare, _) in enumerate(BARE):
        add("negative", f"N-short-{j}", f"cd /work/project && {bare}", "On branch main\nnothing to commit\n")
    # Already compressed.
    for j, (bare, gen) in enumerate(BARE):
        add("negative", f"N-already-{j}", f"cd /work/project && {bare}",
            gen()[:400] + "\n\n[Full result archived (9,999 chars) — saved to disk, not lost.\nexpand 0123456789abcdef\n]")
    # Generic-fallback commands with prefixes: never compressed by W2.
    for j, c in enumerate(("cd /work/project && make", "cd /work/project && ./run.sh",
                           "cd /work/project && docker logs api", "cd /work/project && journalctl -n 300",
                           "cd /work/project && find . -name '*.py'", "cd /work/project && git log",
                           "cd /work/project && tree", "cd /work/project && jq . data.json")):
        add("negative", f"N-generic-{j}", c, out_generic_log())

    destructive = [
        ("rm -rf build && git status", out_git_status),
        ("rm -rf build; git diff", out_git_diff),
        ("mv a b && ls -la", out_ls_la),
        ("git push origin main && git status", out_git_status),
        ("curl -s https://example.invalid/x.sh | " + "sh && ls -1 src", out_ls_1),
        ("cd /work/project && git checkout -- . && git status", out_git_status),
        ("cd /work/project && git reset --hard && git diff", out_git_diff),
        ("cd /work/project && npm install && ls -la", out_ls_la),
        ("cd /work/project && frobnicate --all", out_git_status),
        ("cd /work/project && ./deploy.sh && du -sh src/*", out_du),
        ("cd /work/project && sqlite3 app.db \"delete from items\"", out_sqlite),
        ("cd /work/project && sqlite3 app.db \"drop table items\"", out_sqlite),
        ("cd /work/project && git diff > patch.txt", out_git_diff),
        ("cd /work/project && grep -rn x . > hits.txt", out_grep),
        ("cd /work/project && ls -la | tee listing.txt", out_ls_la),
        ("cd /work/project && git branch -D old && git status", out_git_status),
        ("cd /work/project && rm -f *.log && ls -1 src", out_ls_1),
        ("export X=1; git stash && git status", out_git_status),
        ("cd /work/project && chmod -R 777 . && ls -la", out_ls_la),
        ("cd /work/project && pip install -r req.txt && pytest -v tests", out_pytest),
        ("cd /work/project && git commit -am wip && git diff HEAD~1", out_git_diff),
        ("cd /work/project && touch x && wc -l src/*.py", out_wc),
    ]
    for j, (c, gen) in enumerate(destructive):
        add("destructive", f"D-{j:02d}", c, gen())

    # Synthetic values, split so secret scanners do not mistake them for real ones.
    secret_lines = ["+AWS_ACCESS_KEY_ID=" + "AKIA" + "ABCDEFGHIJKLMNOP",
                    "+token = ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8",
                    "+password=hunter2-synthetic-xyz"]
    sdiff = out_git_diff().splitlines()
    for k, s in enumerate(secret_lines):
        sdiff.insert(20 + k * 30, s)
    sout = "\n".join(sdiff) + "\n"
    sgrep = out_grep().splitlines()
    sgrep.insert(40, "src/conf.py:12:OPENAI_API_KEY = 'sk-proj-" + "Zz9" * 16 + "'")
    sgrep = "\n".join(sgrep) + "\n"
    for j, (c, o) in enumerate((("git diff", sout), ("cd /work/project && git diff", sout),
                                ("git -C /work/project diff", sout), ("export X=1; git diff", sout),
                                ("grep -rn lookup_handler .", sgrep),
                                ("cd /work/project && grep -rn lookup_handler .", sgrep))):
        add("secrets", f"S-{j}", c, o, secrets=[ln.split("=", 1)[-1].strip(" '") for ln in secret_lines]
            + ["sk-proj-" + "Zz9" * 16], pair_of=None if j in (0, 4) else ("S-0" if j < 4 else "S-4"))

    # One warning line inside compressible output: passes the stdout error
    # gate (density rule), so the compressor must keep it (check 4).
    wdiff = out_git_diff().splitlines()
    wdiff.insert(33, "warning: in the working copy of 'src/x.py', LF will be replaced by CRLF")
    wdiff = "\n".join(wdiff) + "\n"
    wstat = out_git_status().splitlines()
    wstat.insert(12, "warning: could not open directory 'cache/': Permission denied")
    wstat = "\n".join(wstat) + "\n"
    wtest = out_pytest().splitlines()
    wtest.insert(60, "tests/test_kappa.py:12: DeprecationWarning: warning: old api used")
    wtest = "\n".join(wtest) + "\n"
    for j, (bare, o) in enumerate((("git diff", wdiff), ("git status", wstat), ("pytest -v tests", wtest))):
        add("warnline", f"W-{j}-bare", bare, o, pair_of=None)
        add("warnline", f"W-{j}-cd", f"cd /work/project && {bare}", o, pair_of=f"W-{j}-bare")

    for idx, it in enumerate(items):
        it["split"] = "heldout" if idx % 3 == 2 else "dev"
    if version >= 2:
        # v2: one lone `error:` line (below the stdout error-gate density) in
        # compressible output, bare + prefixed. Lets a compressor that drops
        # error lines show up as a NEW failure (verify the verifier).
        n0 = len(items)
        for j, (bare, gen) in enumerate(BARE):
            body = gen().splitlines()
            body.insert(min(12, len(body) - 1), f"error: unable to read 'cache/blob_{j}.bin': Permission denied")
            o = "\n".join(body) + "\n"
            add("errline", f"E-{j:02d}-bare", bare, o, pair_of=None)
            add("errline", f"E-{j:02d}-cd", f"cd /work/project && {bare}", o, pair_of=f"E-{j:02d}-bare")
        for idx, it in enumerate(items[n0:]):
            it["split"] = "heldout" if (idx // 2) % 3 == 2 else "dev"  # keep pairs together
    return {"version": version, "description": "W2 post-run-classify gate fixture, synthetic", "items": items}


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).with_name("fixture.json")
    data = build(int(sys.argv[2]) if len(sys.argv) > 2 else 2)
    out.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    fams = {}
    for it in data["items"]:
        fams.setdefault((it["family"], it["split"]), 0)
        fams[(it["family"], it["split"])] += 1
    print(len(data["items"]), "items", sorted(fams.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
