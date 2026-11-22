#!/usr/bin/env python3
"""Publish a verified build as a GitHub Release and hand the manifest to the bucket.

Assets published on the release: the ZIP, provenance.json, the Scoop manifest, the
pointer file (latest.json) and SHA256SUMS. Two files are also committed to `main`
so a consumer only has to read one URL:

  latest.json                    machine-readable pointer to the current build
  bucket/hermes-desktop-light.json   the Scoop manifest for that build

A consumer can either poll `latest.json` (Scoop `checkver` + `autoupdate`) or listen
for the `repository_dispatch` event this script can raise in the bucket repository
when BUCKET_REPO and a token that can write there are configured.

`--dry-run` writes every generated file and prints the requests it would send
without touching GitHub.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import http.client
import json
import os
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from github_api import UPLOADS_ROOT, USER_AGENT, GitHub, GitHubError  # noqa: E402

APP = "hermes-desktop-light"
MANIFEST_PATH = f"bucket/{APP}.json"
POINTER_PATH = "latest.json"
DEFAULT_EVENT = "hermes-desktop-light-released"
SHORTCUT_NAME = "Hermes Desktop (Light, Unofficial)"


def asset_url(repository: str, release_tag: str, name: str) -> str:
    return f"https://github.com/{repository}/releases/download/{release_tag}/{name}"


def raw_url(repository: str, path: str, branch: str = "main") -> str:
    return f"https://raw.githubusercontent.com/{repository}/{branch}/{path}"


def scoop_manifest(provenance: dict, repository: str, branch: str = "main") -> dict:
    """Scoop manifest for the current build. Version ordering is measured, not guessed.

    Rebuilds of one upstream release get a numeric fourth field (`0.23.3.2`), which
    Scoop's own comparator orders above `0.23.3` and below `0.23.4`; a `-r<N>` suffix
    does not (`-r10` compares older than `-r9`). tests/version-order.ps1 asserts this.
    """
    version = provenance["version"]
    asset = provenance["asset"]
    upstream = provenance["upstream"]
    pointer = raw_url(repository, POINTER_PATH, branch)
    return {
        "$schema": "https://raw.githubusercontent.com/ScoopInstaller/Scoop/master/schema.json",
        "version": version,
        "description": (f"Hermes Desktop remote-only build {version} (unofficial, no local "
                        f"agent) from the official release {upstream['tag']}"),
        "homepage": f"https://github.com/{upstream['repository']}",
        "license": "MIT",
        "architecture": {
            "64bit": {
                "url": asset_url(repository, provenance["releaseTag"], asset),
                "hash": provenance["sha256"],
            }
        },
        "shortcuts": [["Hermes Light.exe", SHORTCUT_NAME]],
        "checkver": {"url": pointer, "jsonpath": "$.version"},
        "autoupdate": {
            "architecture": {
                "64bit": {
                    "url": asset_url(repository, "v$version",
                                      f"{APP}-$version-windows-x64.zip"),
                    "hash": {"url": pointer, "jsonpath": "$.sha256"},
                }
            }
        },
        "notes": ("Unofficial unsigned x64 build of Hermes Desktop with no local Python and no "
                  "local agent: connect it to a Hermes gateway you already run. Upstream does "
                  f"not produce or support this package (source release {upstream['tag']}).")
    }


def latest_pointer(provenance: dict, repository: str) -> dict:
    upstream = provenance["upstream"]
    return {
        "schema": 1,
        "app": APP,
        "version": provenance["version"],
        "releaseTag": provenance["releaseTag"],
        "asset": provenance["asset"],
        "url": asset_url(repository, provenance["releaseTag"], provenance["asset"]),
        "sha256": provenance["sha256"],
        "size": provenance["size"],
        "upstream": {
            "repository": upstream["repository"],
            "tag": upstream["tag"],
            "commit": upstream["commit"],
            "claimTag": upstream["claimTag"],
            "url": upstream["url"] or f"https://github.com/{upstream['repository']}/releases",
        },
        "provenance": asset_url(repository, provenance["releaseTag"], "provenance.json"),
        "source": f"https://github.com/{repository}",
        "builtAt": provenance["builtAt"],
        "builder": provenance.get("builder", {}),
    }


def sha256sums(provenance: dict) -> str:
    lines = [f"{provenance['sha256']}  {provenance['asset']}"]
    for name in ("provenance.json", MANIFEST_PATH.split("/")[-1], POINTER_PATH):
        lines.append(f"# {name} is published as a release asset")
    return "\n".join(lines) + "\n"


def release_notes(provenance: dict, repository: str) -> str:
    upstream = provenance["upstream"]
    builder = provenance.get("builder", {})
    return "\n".join([
        f"## Hermes Desktop Light {provenance['version']}",
        "",
        "Unofficial, unsigned, remote-only Windows x64 build prepared from the official",
        "stable source. No local Python runtime and no local agent are included.",
        "",
        "| | |",
        "| --- | --- |",
        f"| Upstream release | `{upstream['tag']}` |",
        f"| Upstream commit | `{upstream['commit']}` |",
        f"| Release claim | `{upstream['claimTag']}` (`{upstream['claimObject']}`) |",
        f"| Upstream license SHA256 | `{upstream.get('licenseSha256', '')}` |",
        f"| Built at | `{provenance['builtAt']}` |",
        f"| Built by | {builder.get('workflowRun') or repository} |",
        f"| Asset | `{provenance['asset']}` |",
        f"| SHA256 | `{provenance['sha256']}` |",
        "",
        "Assets: the ZIP, `provenance.json`, the Scoop manifest, `latest.json` and",
        "`SHA256SUMS`. `latest.json` in `main` always points at the newest verified build.",
        "",
        "Upstream (NousResearch) does not produce or support this package.",
    ])


# --------------------------------------------------------------------------- publish
def upload_headers(token: str, size: int) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": USER_AGENT,
        "Content-Type": "application/octet-stream",
        "Content-Length": str(size),
    }


def upload_url(uploads: str, repository: str, release_id: int | None, name: str) -> str:
    base = f"{uploads}/repos/{repository}/releases/"
    base += f"{release_id}/assets" if release_id else "assets"
    return base + "?" + urllib.parse.urlencode({"name": name})


def upload_file(url: str, token: str, path: Path, timeout: int = 1800) -> dict:
    """Stream one asset to GitHub's upload host without holding it in memory."""
    split = urllib.parse.urlsplit(url)
    connection = http.client.HTTPSConnection(split.netloc, timeout=timeout)
    try:
        connection.request("POST", split.path + "?" + split.query if split.query else split.path,
                           body=path.open("rb"), headers=upload_headers(token, path.stat().st_size))
        response = connection.getresponse()
        payload = response.read().decode("utf-8", "replace")
    finally:
        connection.close()
    if response.status >= 300:
        raise GitHubError(f"upload of {path.name} -> HTTP {response.status}: {payload[:300]}")
    return json.loads(payload)


def verify_assets(client: GitHub, repository: str, release: dict, expected: dict) -> None:
    """Read every asset back and compare digests (or at least sizes)."""
    live = client.get(f"/repos/{repository}/releases/{release['id']}")
    assets = {asset["name"]: asset for asset in live.get("assets", [])}
    for name, (path, digest) in expected.items():
        asset = assets.get(name)
        if asset is None:
            raise GitHubError(f"asset {name} is missing from release {release['tag_name']}")
        recorded = asset.get("digest")
        if recorded:
            if recorded != f"sha256:{digest}":
                raise GitHubError(f"asset {name} digest is {recorded}, expected sha256:{digest}")
        elif asset.get("size") != path.stat().st_size:
            raise GitHubError(f"asset {name} size is {asset.get('size')}, "
                              f"expected {path.stat().st_size}")


def put_file(client: GitHub, repository: str, path: str, data: bytes, message: str,
             branch: str = "main") -> str:
    existing = client.get(f"/repos/{repository}/contents/{path}",
                          params={"ref": branch}, allow_404=True)
    payload = {"message": message, "branch": branch,
               "content": base64.b64encode(data).decode("ascii")}
    if existing:
        payload["sha"] = existing["sha"]
    result = client.send("PUT", f"/repos/{repository}/contents/{path}", payload)
    return (result.get("commit") or {}).get("sha", "")


def sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            hasher.update(chunk)
    return hasher.hexdigest()


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--asset", required=True, help="path to the packaged ZIP")
    parser.add_argument("--provenance", required=True, help="path to provenance.json")
    parser.add_argument("--out-dir", default="output")
    parser.add_argument("--token", default=os.environ.get("GITHUB_TOKEN"))
    parser.add_argument("--branch", default="main")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--update-main", action="store_true",
                        help="commit latest.json and the manifest to the default branch")
    parser.add_argument("--dispatch-repo", default=os.environ.get("BUCKET_REPO", ""))
    parser.add_argument("--dispatch-event", default=DEFAULT_EVENT)
    parser.add_argument("--dispatch-token", default=os.environ.get("BUCKET_TOKEN"))
    parser.add_argument("--uploads-root", default=UPLOADS_ROOT)
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    if not args.repository:
        print("::error::--repository or GITHUB_REPOSITORY is required", file=sys.stderr)
        return 2

    asset = Path(args.asset).resolve()
    provenance_file = Path(args.provenance).resolve()
    out = Path(args.out_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    if not asset.is_file() or not provenance_file.is_file():
        print("::error::asset or provenance file is missing", file=sys.stderr)
        return 2

    provenance = json.loads(provenance_file.read_text(encoding="utf-8"))
    digest = sha256_file(asset)
    if provenance.get("sha256") != digest:
        print(f"::error::provenance sha256 {provenance.get('sha256')} does not match {digest}",
              file=sys.stderr)
        return 2
    if provenance.get("asset") != asset.name:
        print(f"::error::provenance asset {provenance.get('asset')} is not {asset.name}",
              file=sys.stderr)
        return 2

    manifest_file = out / f"{APP}.json"
    pointer_file = out / POINTER_PATH
    notes_file = out / "release-notes.md"
    sums_file = out / "SHA256SUMS"
    manifest_file.write_text(json.dumps(scoop_manifest(provenance, args.repository), indent=2)
                             + "\n", encoding="utf-8")
    pointer_file.write_text(json.dumps(latest_pointer(provenance, args.repository), indent=2)
                            + "\n", encoding="utf-8")
    notes_file.write_text(release_notes(provenance, args.repository) + "\n", encoding="utf-8")
    sums_file.write_text(sha256sums(provenance), encoding="utf-8")

    release_tag = provenance["releaseTag"]
    assets = {
        asset.name: asset,
        "provenance.json": provenance_file,
        f"{APP}.json": manifest_file,
        POINTER_PATH: pointer_file,
        "SHA256SUMS": sums_file,
    }
    if args.dry_run:
        print(f"dry-run: release tag {release_tag} on {args.repository}")
        for name, path in assets.items():
            print(f"  asset {name} ({path.stat().st_size} bytes)")
        print(f"  upload url {upload_url(args.uploads_root, args.repository, None, '…')}")
        print(f"  manifest {manifest_file}")
        print(f"  pointer  {pointer_file}")
        if args.update_main:
            print(f"  would commit {POINTER_PATH} and {MANIFEST_PATH} to {args.branch}")
        if args.dispatch_repo:
            print(f"  would dispatch {args.dispatch_event} to {args.dispatch_repo}")
        return 0

    if not args.token:
        print("::error::GITHUB_TOKEN is required to publish", file=sys.stderr)
        return 2

    client = GitHub(args.token)
    try:
        existing = client.get(f"/repos/{args.repository}/releases/tags/{release_tag}",
                              allow_404=True)
        if existing:
            print(f"::error::release {release_tag} already exists; bump --revision",
                  file=sys.stderr)
            return 2
        release = client.send("POST", f"/repos/{args.repository}/releases", {
            "tag_name": release_tag,
            "target_commitish": args.branch,
            "name": f"Hermes Desktop Light {provenance['version']}",
            "body": notes_file.read_text(encoding="utf-8"),
            "draft": True,
            "prerelease": False,
        })
        for name, path in assets.items():
            url = upload_url(args.uploads_root, args.repository, release["id"], name)
            upload_file(url, args.token, path)
        verify_assets(client, args.repository, release,
                      {name: (path, sha256_file(path)) for name, path in assets.items()})
        published = client.send("PATCH", f"/repos/{args.repository}/releases/{release['id']}", {
            "draft": False, "prerelease": False, "make_latest": "true",
        })
        print(f"release={published.get('html_url')}")

        if args.update_main:
            message = (f"chore: point {APP} at {provenance['version']} "
                       f"(upstream {provenance['upstream']['tag']})")
            put_file(client, args.repository, POINTER_PATH, pointer_file.read_bytes(),
                     message, args.branch)
            put_file(client, args.repository, MANIFEST_PATH, manifest_file.read_bytes(),
                     message, args.branch)
            print(f"committed {POINTER_PATH} and {MANIFEST_PATH} to {args.branch}")

        if args.dispatch_repo:
            if not args.dispatch_token:
                print("note: BUCKET_REPO is set but no BUCKET_TOKEN secret is available; "
                      "skipping repository_dispatch (the bucket can still poll latest.json)")
            else:
                GitHub(args.dispatch_token).send(
                    "POST", f"/repos/{args.dispatch_repo}/dispatches", {
                        "event_type": args.dispatch_event,
                        "client_payload": {
                            "app": APP,
                            "version": provenance["version"],
                            "release_tag": release_tag,
                            "asset": provenance["asset"],
                            "url": asset_url(args.repository, release_tag, provenance["asset"]),
                            "sha256": provenance["sha256"],
                            "manifest": raw_url(args.repository, MANIFEST_PATH, args.branch),
                            "pointer": raw_url(args.repository, POINTER_PATH, args.branch),
                            "upstream_tag": provenance["upstream"]["tag"],
                            "upstream_commit": provenance["upstream"]["commit"],
                            "provenance": raw_url(args.repository, "latest.json", args.branch),
                        },
                    })
                print(f"dispatched {args.dispatch_event} to {args.dispatch_repo}")
    except GitHubError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 2

    print(f"asset_url={asset_url(args.repository, release_tag, provenance['asset'])}")
    print(f"manifest={manifest_file}")
    print(f"pointer={pointer_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
