#!/usr/bin/env python3
"""Release gate: is @alexgreensh/token-optimizer@<version> on npm?

Claude Code cloud sessions install Token Optimizer from the npm tarball, and
`measure.py cloud-setup-script` pins the version of the plugin that prints it.
A plugin release whose npm package was never published hands every cloud user
a setup script that 404s and silently installs nothing. This gate answers the
same question as

    npm view @alexgreensh/token-optimizer@<version> dist.integrity

(it reads the same registry document, without needing npm on PATH) and exits 1
when the answer is empty.

    python3 scripts/check_npm_published.py                      # plugin.json version
    python3 scripts/check_npm_published.py --tag v5.13.50       # a release tag
    python3 scripts/check_npm_published.py --expect-integrity sha512-...   # also match bytes
    python3 scripts/check_npm_published.py --tag v5.13.51 --match-tree     # npm == this checkout

Wired into the release in two places (docs: packaging/npm/RELEASING.md):
  * RELEASING.md step 4, by hand, right after `npm publish`, with
    --expect-integrity from the `build.py --pack` output;
  * the "Sign release" workflow, as its last step, so a GitHub release whose
    npm package is missing turns that workflow red. It runs AFTER the
    CHECKSUMS.sha256 upload on purpose: a missing npm package must never also
    break the script installer.

A tree with no packaging/npm (a release that predates the cloud package) is
skipped with exit 0. Exit codes: 0 published (or skipped), 1 missing or
mismatched, 2 usage error.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "skills" / "token-optimizer" / "scripts"))

import cloud_kit  # noqa: E402  (stdlib only; shares the setup script's registry reader)


def plugin_version(repo: Path = REPO) -> str:
    return json.loads((repo / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))["version"]


def check(version: str, fetch=cloud_kit.registry_dist, expect_integrity: str = "",
          attempts: int = 1, wait_seconds: float = 0.0, sleep=time.sleep) -> tuple:
    """(ok, message). ``fetch(version) -> {"integrity", "tarball"}`` raises
    cloud_kit.KitError when the version or its sha512 is missing. Retries
    cover registry propagation right after a publish."""
    last = ""
    for attempt in range(max(1, attempts)):
        try:
            dist = fetch(version)
        except cloud_kit.KitError as exc:
            last = str(exc)
            if attempt + 1 < attempts:
                sleep(wait_seconds)
            continue
        integrity = dist.get("integrity") or ""
        if not integrity.startswith("sha512-"):
            return False, "%s@%s has no sha512 dist.integrity on the registry" % (cloud_kit.PACKAGE, version)
        if expect_integrity and integrity != expect_integrity:
            return False, ("%s@%s on the registry has integrity %s, but the tarball built for this "
                           "release has %s. Never republish a version: fix forward with a new one."
                           % (cloud_kit.PACKAGE, version, integrity, expect_integrity))
        return True, "%s@%s published: %s" % (cloud_kit.PACKAGE, version, integrity)
    return False, ("%s@%s is not on npm (%s). Cloud setup scripts for this release would install "
                   "nothing. Publish it: packaging/npm/RELEASING.md." % (cloud_kit.PACKAGE, version, last))


def _staged_files(repo: Path) -> dict:
    """relpath -> sha256 of what packaging/npm/build.py ships from ``repo``
    (tracked files only, exactly as --pack stages them)."""
    sys.path.insert(0, str(repo / "packaging" / "npm"))
    try:
        import build
    finally:
        sys.path.pop(0)
    if Path(build.REPO).resolve() != repo.resolve():
        raise SystemExit("check_npm_published: packaging/npm/build.py belongs to %s, not %s"
                         % (build.REPO, repo))
    with tempfile.TemporaryDirectory() as tmp:
        pkg = build.stage(Path(tmp), include_untracked=False)
        return {p.relative_to(pkg).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in pkg.rglob("*") if p.is_file()}


def tree_mismatches(tgz: bytes, expected: dict) -> list:
    """Every way the published tarball differs from ``expected`` (relpath ->
    sha256): changed bytes, files only on npm, files only in the tree. Members
    are read in memory, never extracted."""
    found = {}
    with tarfile.open(fileobj=io.BytesIO(tgz), mode="r:gz") as tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            name = member.name[len("package/"):] if member.name.startswith("package/") else member.name
            found[name] = hashlib.sha256(tar.extractfile(member).read()).hexdigest()
    out = []
    for rel in sorted(set(found) | set(expected)):
        if rel not in found:
            out.append("missing from npm: %s" % rel)
        elif rel not in expected:
            out.append("only on npm:      %s" % rel)
        elif found[rel] != expected[rel]:
            out.append("different bytes:  %s" % rel)
    return out


def match_tree(version: str, repo: Path, fetch, download=None, attempts: int = 1,
               wait_seconds: float = 0.0, sleep=time.sleep) -> tuple:
    """(ok, message): the npm tarball for ``version`` is byte-for-byte what this
    checkout ships. The existence check alone passed 5.13.50, whose npm bytes
    came from an earlier commit than the git release.

    ``attempts``/``wait_seconds`` cover registry propagation of the tarball: right after a
    publish the metadata can resolve while the tarball URL still answers 404."""
    download = download or (lambda url: urllib.request.urlopen(url, timeout=60).read())  # noqa: S310
    last = ""
    for attempt in range(max(1, attempts)):
        try:
            dist = fetch(version)
            tgz = download(dist["tarball"])
            break
        except Exception as exc:  # noqa: BLE001 - any failure is a failed gate, said out loud
            last = str(exc)
            if attempt + 1 < attempts:
                sleep(wait_seconds)
    else:
        return False, "could not download %s@%s to compare it with the tree (%s)" % (
            cloud_kit.PACKAGE, version, last)
    diffs = tree_mismatches(tgz, _staged_files(repo))
    if diffs:
        return False, ("%s@%s on npm is not this release's tree (%d difference(s)); the npm and "
                       "git releases have drifted. Never republish a version: fix forward.\n  %s"
                       % (cloud_kit.PACKAGE, version, len(diffs), "\n  ".join(diffs[:40])))
    return True, "%s@%s on npm matches this tree file for file" % (cloud_kit.PACKAGE, version)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--version", help="package version (default: .claude-plugin/plugin.json)")
    ap.add_argument("--tag", help="release tag, e.g. v5.13.50 (sets --version)")
    ap.add_argument("--expect-integrity", default="", help="sha512-... that the registry must report")
    ap.add_argument("--registry", default=cloud_kit.REGISTRY)
    ap.add_argument("--attempts", type=int, default=1, help="tries, for registry propagation")
    ap.add_argument("--wait", type=float, default=20.0, help="seconds between tries")
    ap.add_argument("--match-tree", action="store_true",
                    help="also download the tarball and require it to equal this checkout's shipped files")
    ap.add_argument("--repo", type=Path, default=REPO, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    if not (args.repo / "packaging" / "npm" / "package.template.json").is_file():
        print("check_npm_published: this tree has no packaging/npm (predates the cloud package); skipped")
        return 0
    version = args.version or (args.tag[1:] if args.tag and args.tag[:1] in "vV" else args.tag) \
        or plugin_version(args.repo)
    if not cloud_kit._VERSION_RE.match(version or ""):
        print("check_npm_published: not a release version: %r" % version, file=sys.stderr)
        return 2
    ok, message = check(version, fetch=lambda v: cloud_kit.registry_dist(v, args.registry),
                        expect_integrity=args.expect_integrity, attempts=args.attempts,
                        wait_seconds=args.wait)
    if ok and args.match_tree:
        ok, message = match_tree(version, args.repo, fetch=lambda v: cloud_kit.registry_dist(v, args.registry),
                                 attempts=args.attempts, wait_seconds=args.wait)
    if ok:
        print("OK: " + message)
        return 0
    # ::error:: becomes a GitHub Actions annotation; harmless locally.
    print("::error::" + message, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
