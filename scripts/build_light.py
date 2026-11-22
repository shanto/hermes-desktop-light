#!/usr/bin/env python3
"""Build, verify and package the remote-only ("light") Windows payload.

Steps, all of them fail-closed:

  1. guards   the upstream checkout is exactly the admitted commit, the tree is
              clean, and the release tag + its release claim resolve to it
  2. build    upstream's own preparation with `--variant light`, which skips the
              agent payload, TUI, web and webapp workspaces entirely
  3. verify   the packaged stamp says payload=light / updateMechanism=external,
              records the admitted commit and tag, and no agent payload shipped
  4. smoke    two native launches with a restricted PATH (Playwright)
  5. package  deterministic ZIP + provenance.json

Nothing here talks to GitHub: the workflow resolves the inputs, this script only
touches the checkout it was given.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

FULL_SHA = re.compile(r"^[a-f0-9]{40}$")
PACK_RELATIVE = Path("apps/desktop/release/win-unpacked")
PREPARED_RELATIVE = Path(".build/desktop-job/prepared.json")
AGENT_PAYLOAD_RELATIVE = Path("resources/agent-payload")
STAMP_RELATIVE = Path("resources/install-stamp.json")
NOTICE_NAME = "UNOFFICIAL-BUILD.txt"
EXPECTED_STAMP = {
    "payload": "light",
    "distribution": "desktop-app",
    "updateMechanism": "external",
}
IDENTITY_SCRIPT = (
    "const identity = require(process.env.HERMES_PRODUCT_IDENTITY_PATH);"
    "process.stdout.write(String(identity.windowsExecutableName))"
)


class BuildError(RuntimeError):
    pass


def git(source: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(source), *args],
                            capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise BuildError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            hasher.update(chunk)
    return hasher.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------- guards
def assert_checkout(source: Path, commit: str) -> None:
    if not FULL_SHA.match(commit or ""):
        raise BuildError(f"admitted commit is not a full SHA: {commit!r}")
    head = git(source, "rev-parse", "HEAD")
    if head != commit:
        raise BuildError(f"checkout is at {head}, expected {commit}")
    dirty = git(source, "status", "--porcelain", "-uno")
    if dirty:
        raise BuildError("upstream checkout has modified tracked files:\n" + dirty)


def assert_release_refs(source: Path, release_tag: str, claim_tag: str,
                        claim_object: str, commit: str) -> None:
    resolved = git(source, "rev-parse", f"refs/tags/{release_tag}^{{commit}}")
    if resolved != commit:
        raise BuildError(f"{release_tag} points at {resolved}, expected {commit}")
    claimed = git(source, "rev-parse", f"refs/tags/{claim_tag}")
    if claimed != claim_object:
        raise BuildError(f"{claim_tag} resolves to {claimed}, expected {claim_object}")
    metadata = git(source, "cat-file", "-p", claim_object)
    if not re.search(r"^tagger .* \d+ [+-]\d{4}$", metadata, re.MULTILINE):
        raise BuildError(f"{claim_tag} has no immutable tagger timestamp")


def assert_pack(pack: Path, commit: str, tag: str) -> dict:
    stamp_file = pack / STAMP_RELATIVE
    if not stamp_file.is_file():
        raise BuildError(f"packaged build has no {STAMP_RELATIVE.as_posix()}: {pack}")
    stamp = json.loads(stamp_file.read_text(encoding="utf-8-sig"))
    for key, expected in EXPECTED_STAMP.items():
        if stamp.get(key) != expected:
            raise BuildError(f"stamp {key} is {stamp.get(key)!r}, expected {expected!r}")
    if stamp.get("commit") != commit:
        raise BuildError(f"stamp commit is {stamp.get('commit')!r}, expected {commit}")
    if stamp.get("tag") != tag:
        raise BuildError(f"stamp tag is {stamp.get('tag')!r}, expected {tag}")
    if stamp.get("dirty") is not False:
        raise BuildError("stamp says the source tree was dirty")
    if (pack / AGENT_PAYLOAD_RELATIVE).exists():
        raise BuildError("light build unexpectedly contains a local agent payload")
    if not (pack / "resources/app.asar").is_file():
        raise BuildError("packaged build has no resources/app.asar")
    return stamp


def identity_executable(source: Path, tag: str) -> str:
    """Resolve the expected executable from upstream's own identity module."""
    identity = source / "apps/desktop/product-identity.cjs"
    if not identity.is_file():
        raise BuildError(f"upstream Desktop identity module is missing: {identity}")
    node = prepared_node(source) or shutil.which("node")
    if not node:
        raise BuildError("no Node runtime available to resolve the product identity")
    env = {
        **os.environ,
        "HERMES_DESKTOP_VARIANT": "light",
        "HERMES_BUILD_COMMIT": "",
        "HERMES_PAYLOAD_TAG": tag,
        "HERMES_PRODUCT_IDENTITY_PATH": str(identity),
    }
    result = subprocess.run([node, "-e", IDENTITY_SCRIPT], capture_output=True,
                            text=True, check=False, env=env)
    if result.returncode != 0:
        raise BuildError(f"product identity lookup failed: {result.stderr.strip()}")
    name = result.stdout.strip()
    if not name:
        raise BuildError("product identity module returned an empty executable name")
    return f"{name}.exe"


def prepared_node(source: Path) -> str | None:
    prepared = source / PREPARED_RELATIVE
    if not prepared.is_file():
        return None
    data = json.loads(prepared.read_text(encoding="utf-8-sig"))
    node = data.get("node")
    return str(node) if node else None


# --------------------------------------------------------------------------- build
def prepare_and_build(source: Path, tag: str, claim_tag: str, claim_object: str,
                      python: str) -> None:
    """Run upstream's own light-variant preparation and packaging."""
    driver = source / "scripts/bundles/desktop.py"
    if not driver.is_file():
        raise BuildError(f"upstream Desktop build driver is missing: {driver}")
    env = {
        **os.environ,
        "PYTHONUTF8": "1",
        # Upstream refuses a stable build without the exact claim it was
        # published under, and derives the reproducible packaging epoch from it.
        "RELEASE_CLAIM_TAG": claim_tag,
        "RELEASE_CLAIM_OBJECT": claim_object,
    }
    argv = [python, str(driver), "--tag", tag, "--variant", "light", "--", "--dir"]
    print("build: " + subprocess.list2cmdline(argv), flush=True)
    if subprocess.run(argv, cwd=str(source), env=env, check=False).returncode != 0:
        raise BuildError("upstream light build failed")


def smoke(source: Path, pack: Path, executable: str, out: Path, script: Path,
          node: str | None) -> dict:
    runtime = node or prepared_node(source) or shutil.which("node")
    if not runtime:
        raise BuildError("no Node runtime available for the native smoke test")
    if not script.is_file():
        raise BuildError(f"smoke driver is missing: {script}")
    exe = pack / executable
    if not exe.is_file():
        raise BuildError(f"expected executable is not in the package: {exe}")
    argv = [runtime, str(script), str(source), str(exe), str(out)]
    print("smoke: " + subprocess.list2cmdline(argv), flush=True)
    if subprocess.run(argv, check=False).returncode != 0:
        raise BuildError("native smoke test failed")
    evidence = out / "native-checks.json"
    if not evidence.is_file():
        raise BuildError("native smoke test produced no evidence")
    return json.loads(evidence.read_text(encoding="utf-8-sig"))


# --------------------------------------------------------------------------- package
def write_notice(pack: Path, *, upstream: str, upstream_tag: str, commit: str,
                 claim_tag: str, version: str, version_url: str, built_at: str) -> Path:
    notice = pack / NOTICE_NAME
    notice.write_text(
        "UNOFFICIAL BUILD - NOT PRODUCED OR SUPPORTED BY NOUS RESEARCH\n"
        "===========================================================\n\n"
        "This package is an unofficial, unsigned, remote-only build of the Hermes\n"
        "Desktop application. It contains no local Python runtime and no local\n"
        "agent: it connects to a Hermes gateway you already run.\n\n"
        f"Upstream project : https://github.com/{upstream}\n"
        f"Upstream release : {upstream_tag}\n"
        f"Upstream commit  : {commit}\n"
        f"Release claim    : {claim_tag}\n"
        f"Build version    : {version}\n"
        f"Build details    : {version_url}\n"
        f"Built at         : {built_at}\n\n"
        "The upstream MIT license text ships beside this file as LICENSE.\n"
        "Electron and Chromium license texts ship as LICENSE.electron.txt and\n"
        "LICENSES.chromium.html.\n",
        encoding="utf-8", newline="\n")
    return notice


def zip_pack(pack: Path, target: Path) -> None:
    """Deterministic ZIP: sorted entries, fixed timestamps, fixed modes."""
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(pack.rglob("*")):
            if path.is_dir():
                continue
            info = zipfile.ZipInfo(path.relative_to(pack).as_posix(),
                                   date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, path.read_bytes())


# --------------------------------------------------------------------------- main
def build(args) -> dict:
    source = Path(args.source).resolve()
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)

    assert_checkout(source, args.expected_commit)
    assert_release_refs(source, args.tag, args.claim_tag, args.claim_object,
                        args.expected_commit)

    pack = Path(args.pack).resolve() if args.pack else (source / PACK_RELATIVE)
    if not args.pack:
        prepare_and_build(source, args.tag, args.claim_tag, args.claim_object, args.python)
    if not pack.is_dir():
        raise BuildError(f"packaged build directory is missing: {pack}")

    assert_checkout(source, args.expected_commit)
    stamp = assert_pack(pack, args.expected_commit, args.tag)
    executable = identity_executable(source, args.tag)
    if not (pack / executable).is_file():
        raise BuildError(f"packaged build does not contain {executable}")

    license_sha = sha256_bytes(
        subprocess.run(["git", "-C", str(source), "show", "HEAD:LICENSE"],
                       capture_output=True, check=True).stdout)

    checks = None
    if not args.skip_smoke:
        checks = smoke(source, pack, executable, out, Path(args.smoke_script),
                       args.node)

    built_at = os.environ.get("BUILD_TIMESTAMP") or _utc_now()
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    notice = write_notice(
        pack,
        upstream=args.upstream,
        upstream_tag=args.tag,
        commit=args.expected_commit,
        claim_tag=args.claim_tag,
        version=args.version,
        version_url=f"https://github.com/{repository}/releases/tag/{args.release_tag}",
        built_at=built_at,
    )

    asset = out / args.asset_name
    zip_pack(pack, asset)
    digest = sha256_file(asset)
    provenance = {
        "schema": 1,
        "app": "hermes-desktop-light",
        "version": args.version,
        "releaseTag": args.release_tag,
        "asset": args.asset_name,
        "sha256": digest,
        "size": asset.stat().st_size,
        "builtAt": built_at,
        "upstream": {
            "repository": args.upstream,
            "tag": args.tag,
            "commit": args.expected_commit,
            "claimTag": args.claim_tag,
            "claimObject": args.claim_object,
            "publishedAt": os.environ.get("UPSTREAM_PUBLISHED_AT", ""),
            "url": os.environ.get("UPSTREAM_URL", ""),
            "licenseSha256": license_sha,
        },
        "builder": {
            "repository": repository,
            "commit": os.environ.get("GITHUB_SHA", ""),
            "workflowRun": f"https://github.com/{repository}/actions/runs/{run_id}" if run_id else "",
        },
        "package": {
            "executable": executable,
            "distribution": stamp.get("distribution"),
            "payload": stamp.get("payload"),
            "updateMechanism": stamp.get("updateMechanism"),
            "displayVersion": stamp.get("displayVersion"),
            "baseVersion": stamp.get("baseVersion"),
            "stampSource": stamp.get("source"),
            "notice": {"name": NOTICE_NAME, "sha256": sha256_file(notice)},
        },
        "nativeChecks": checks,
        "signing": "unsigned unofficial build",
    }
    provenance_file = out / "provenance.json"
    provenance_file.write_text(json.dumps(provenance, indent=2) + "\n", encoding="utf-8")

    print(f"asset={asset}")
    print(f"sha256={digest}")
    print(f"size={provenance['size']}")
    print(f"provenance={provenance_file}")
    print(f"pack={pack}")
    print(f"executable={executable}")
    return provenance


def _utc_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, help="upstream checkout")
    parser.add_argument("--tag", required=True, help="upstream release tag (v<X.Y.Z>)")
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--claim-tag", required=True)
    parser.add_argument("--claim-object", required=True)
    parser.add_argument("--version", required=True, help="published build version")
    parser.add_argument("--release-tag", required=True, help="this repository's release tag")
    parser.add_argument("--asset-name", required=True)
    parser.add_argument("--upstream", default="NousResearch/hermes-agent")
    parser.add_argument("--out", default="output")
    parser.add_argument("--pack", default=None,
                        help="verify and package an existing win-unpacked directory "
                             "instead of building (re-verification)")
    parser.add_argument("--skip-smoke", action="store_true")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--node", default=None)
    parser.add_argument("--smoke-script",
                        default=str(Path(__file__).with_name("smoke_light.cjs")))
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        build(args)
    except (BuildError, subprocess.CalledProcessError, OSError, ValueError) as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
