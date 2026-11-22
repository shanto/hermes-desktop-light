#!/usr/bin/env python3
"""Resolve the newest plain (non-prerelease) Hermes Agent release into build inputs.

Upstream's own Desktop preparation refuses a stable build unless it is handed the
exact *claim* the release was built under (`RELEASE_CLAIM_TAG` +
`RELEASE_CLAIM_OBJECT`), because it derives the reproducible build epoch from the
claim tag's tagger date, and that epoch is what makes the packaged Windows
product version reproducible. So even a builder that ignores upstream's Desktop
*channels* still has to resolve one immutable claim tag per release:

    GitHub release v<X.Y.Z>  ->  tag object -> commit
                             ->  the single annotated `rc.<N>-v<X.Y.Z>` tag whose
                                 peeled commit equals that commit

Everything else comes from the plain GitHub release, so no feed, no channel
record, and no product-version parsing is involved.

A release is buildable exactly when it is a published, non-prerelease `v<X.Y.Z>`
tag *and* upstream published exactly one annotated `rc.<N>-v<X.Y.Z>` claim on that
same commit. Everything else — canary tags, date-styled tags, a release whose
commit carries no claim, one that was already published here — is reported as a
skip with its reason, so a run never has to guess.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent))

from github_api import GitHub, GitHubError  # noqa: E402

UPSTREAM = "NousResearch/hermes-agent"
RELEASE_TAG = re.compile(r"^v(?P<version>(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+))$")
CLAIM_TAG = re.compile(r"^rc\.(?P<round>[1-9]\d*)-v(?P<version>\d+\.\d+\.\d+)$")
FULL_SHA = re.compile(r"^[a-f0-9]{40}$")


def version_for(base_version: str, revision: int) -> str:
    """Scoop version for one build: upstream's `X.Y.Z`, plus `-r<N>` for a rebuild.

    Measured with Scoop's own comparator (lib/versions.ps1, pinned in
    tests/version-order.ps1), not assumed:

        0.23.3 < 0.23.3-r2 < 0.23.3-r11 < 0.23.4 < 0.23.4-r1 < 0.23.10
        revisions stay numeric at double digits (r11 outranks r2)
        zero padding has no defined order (-r01 vs -r1 compares to nothing)
        legacy manifests (2026.6864.2132.0-r1) upgrade forward to 0.23.3

    So the first build of an upstream release carries upstream's exact version, and
    rebuilds of that same release - dispatched by hand, revision >= 2 - carry `-r<N>`.
    """
    if revision < 1:
        raise ValueError("revision must be a positive integer")
    if not RELEASE_TAG.match(f"v{base_version}"):
        raise ValueError(f"base version is not X.Y.Z: {base_version}")
    return base_version if revision == 1 else f"{base_version}-r{revision}"


def tag_commit(client: GitHub, upstream: str, tag: str) -> dict:
    """Peel a release tag to its commit, keeping the annotated tag object."""
    ref = client.get(f"/repos/{upstream}/git/ref/tags/{quote(tag, safe='')}")
    target = ref.get("object") or {}
    if target.get("type") == "tag":
        annotated = client.get(f"/repos/{upstream}/git/tags/{target['sha']}")
        inner = annotated.get("object") or {}
        commit = inner.get("sha", "")
        epoch = None
        tagger = (annotated.get("tagger") or {}).get("date")
        if tagger:
            epoch = int(datetime.strptime(tagger, "%Y-%m-%dT%H:%M:%SZ")
                        .replace(tzinfo=timezone.utc).timestamp())
        tag_object = target["sha"]
    else:
        commit, epoch, tag_object = target.get("sha", ""), None, None
    if not FULL_SHA.match(commit or ""):
        raise GitHubError(f"release tag {tag} does not resolve to a commit")
    return {"release_tag": tag, "commit": commit, "tag_object": tag_object, "epoch": epoch}


def accepted_claim(client: GitHub, upstream: str, base_version: str, commit: str,
                   max_pages: int = 8) -> dict | None:
    """The one annotated `rc.<N>-v<X.Y.Z>` claim whose commit is the release commit.

    Zero matches is an upstream condition the caller reports as a skip; more than
    one is ambiguous and refuses to build.
    """
    matches = []
    for entry in client.paginate(f"/repos/{upstream}/tags", max_pages=max_pages):
        name = entry.get("name") or ""
        parsed = CLAIM_TAG.match(name)
        if not parsed or parsed.group("version") != base_version:
            continue
        if (entry.get("commit") or {}).get("sha") == commit:
            matches.append(name)
    if not matches:
        return None
    if len(matches) > 1:
        raise GitHubError(
            f"ambiguous release claim for v{base_version} at {commit[:12]}: {sorted(matches)}")
    claim = matches[0]
    ref = client.get(f"/repos/{upstream}/git/ref/tags/{quote(claim, safe='')}")
    target = ref.get("object") or {}
    if target.get("type") != "tag" or not FULL_SHA.match(target.get("sha", "")):
        raise GitHubError(f"release claim {claim} is not an annotated tag object")
    return {"claim_tag": claim, "claim_object": target["sha"]}


def existing_release(client: GitHub, repository: str, release_tag: str) -> dict | None:
    return client.get(f"/repos/{repository}/releases/tags/{quote(release_tag, safe='')}",
                      allow_404=True)


def plan(client: GitHub, upstream: str, repository: str, revision: int,
         force: bool) -> dict:
    """Decide what this run should do. Skips are data; failures raise."""
    release = client.get(f"/repos/{upstream}/releases/latest")
    if release.get("draft") or release.get("prerelease"):
        raise GitHubError("/releases/latest returned a draft or prerelease")
    upstream_tag = release.get("tag_name") or ""
    upstream_url = release.get("html_url") or f"https://github.com/{upstream}/releases"
    common = {
        "upstream_repo": upstream,
        "upstream_tag": upstream_tag,
        "upstream_url": upstream_url,
        "upstream_published_at": release.get("published_at") or "",
        "revision": str(revision),
    }

    match = RELEASE_TAG.match(upstream_tag)
    if not match:
        return {**common, "build": "false", "version": "",
                "skip_reason": f"unsupported-release-tag:{upstream_tag}"}

    base_version = match.group("version")
    version = version_for(base_version, revision)
    release_tag = f"v{version}"
    tags = tag_commit(client, upstream, upstream_tag)
    claim = accepted_claim(client, upstream, base_version, tags["commit"])
    if claim is None:
        return {**common, "build": "false", "version": version,
                "upstream_commit": tags["commit"],
                "skip_reason": f"no-release-claim:{upstream_tag}"}

    if not force and existing_release(client, repository, release_tag) is not None:
        return {**common, "build": "false", "version": version,
                "release_tag": release_tag, "upstream_commit": tags["commit"],
                "claim_tag": claim["claim_tag"], "claim_object": claim["claim_object"],
                "skip_reason": f"already-published:{release_tag}"}

    return {
        **common,
        "build": "true",
        "skip_reason": "",
        "version": version,
        "release_tag": release_tag,
        "upstream_commit": tags["commit"],
        "upstream_claim_epoch": str(tags["epoch"] or ""),
        **claim,
    }


def output(values: dict, target: str | None) -> None:
    lines = "".join(f"{key}={value}\n" for key, value in values.items())
    for key, value in values.items():
        print(f"{key}={value}")
    path = target or os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as stream:
            stream.write(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream", default=UPSTREAM)
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--revision", type=int, default=1)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--token", default=os.environ.get("GITHUB_TOKEN"))
    parser.add_argument("--output", default=None)
    parser.add_argument("--dump-json", action="store_true",
                        help="print the whole plan as JSON instead of key=value")
    args = parser.parse_args(argv)

    if not args.repository:
        parser.error("--repository or GITHUB_REPOSITORY is required")
    try:
        values = plan(GitHub(args.token), args.upstream, args.repository,
                      args.revision, args.force)
    except (GitHubError, ValueError) as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 2

    if args.dump_json:
        print(json.dumps(values, indent=2, sort_keys=True))
    else:
        output(values, args.output)
    if values.get("build") != "true":
        print(f"nothing to build: {values.get('skip_reason')}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
