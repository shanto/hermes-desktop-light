# Hermes Desktop Light builder

Builds the **unofficial remote-only Windows build of Hermes Desktop** from the newest
plain (non-prerelease) release of [NousResearch/hermes-agent][upstream], verifies it,
and publishes it as a GitHub Release in this repository.

This is part one of two. Part two is the Scoop bucket, [shanto/Scoop][scoop]: this
repository builds and publishes the release, then commits the updated manifest into
that bucket. No manifest is stored here.

[scoop]: https://github.com/shanto/Scoop

[upstream]: https://github.com/NousResearch/hermes-agent

## Why this exists

Upstream Desktop releases are consumed through its own Desktop *channels* (a stable
feed and a canary feed). Those feeds lag the GitHub releases, so a bucket that follows
them falls behind. This repository ignores the feeds completely and tracks the GitHub
release instead:

* upstream release `vX.Y.Z` (`/releases/latest`, so never a prerelease or a draft)
* the commit that tag points at
* the **light** variant, built by upstream's own driver: no local Python, no local
  agent, no TUI/web/webapp workspaces

One upstream requirement cannot be dropped, only resolved: a stable build must be
handed the exact *release claim* it was published under
(`RELEASE_CLAIM_TAG` + `RELEASE_CLAIM_OBJECT`), because upstream derives the
reproducible packaging epoch from that annotated tag's tagger date. So the resolver
finds the single annotated `rc.<N>-v<X.Y.Z>` tag whose peeled commit equals the
release commit and passes it through. No channel, feed, or product-version parsing is
involved — a claim is provenance, not a channel.

A release is buildable exactly when it is a published, non-prerelease `v<X.Y.Z>` tag
*and* exactly one matching claim exists. Anything else (a canary tag, a date-styled
tag, a release whose commit has no claim, a version already published here) is
reported as a skip with its reason.

## What a run does

| Job | Runner | What it proves |
| --- | --- | --- |
| `checks` | ubuntu | unit checks pass, and the published version scheme orders correctly under Scoop's own comparator |
| `resolve` | ubuntu | which upstream release to build, and whether to build at all |
| `build` | windows-2025 | the checkout is the admitted commit, upstream's light build succeeds, the packaged stamp says `payload=light` / `updateMechanism=external`, no agent payload shipped, the application `LICENSE` (staged from the admitted commit) plus the Electron and Chromium notices are in the package, the app launches twice with a crippled PATH, the ZIP and provenance are produced |
| `publish` | ubuntu | every asset uploads and reads back with a matching digest, the release is published, `latest.json` is committed to `main`, and the manifest is committed to the bucket repository and read back from it |

The build guards are fail-closed: a mismatch in commit, tag, claim object, stamp,
executable name, or updater ownership stops the run before anything is published. A
failed run never changes what is currently installable.

## What it publishes

Release assets (tag `v<version>`):

| Asset | Contents |
| --- | --- |
| `hermes-desktop-light-<version>-windows-x64.zip` | the packaged app, plus an `UNOFFICIAL-BUILD.txt` notice recording the upstream tag, commit and claim |
| `provenance.json` | every input, hash and check result for that build |
| `hermes-desktop-light.json` | the Scoop manifest for that build |
| `latest.json` | machine-readable pointer to the current build |
| `SHA256SUMS` | checksums of the ZIP |

`latest.json` is also committed to `main` here, so a consumer needs no secrets to find
a build:

```
latest.json                          pointer: version, url, sha256, upstream identity
```

The manifest itself is committed to the bucket repository instead, as
`bucket/hermes-desktop-light.json` in [`shanto/Scoop`][scoop] (`master`), by the
`SCOOP_TOKEN` secret. Nothing else in the bucket is touched.

## Consuming it from the bucket

```
scoop bucket add shanto https://github.com/shanto/Scoop
scoop install shanto/hermes-desktop-light
```

The manifest's `checkver`/`autoupdate` poll this repository's pointer, so Scoop finds
new builds without any credential:

```json
"checkver": { "url": "https://raw.githubusercontent.com/<owner>/<this-repo>/main/latest.json", "jsonpath": "$.version" },
"autoupdate": {
  "architecture": { "64bit": {
    "url": "https://github.com/<owner>/<this-repo>/releases/download/v$version/hermes-desktop-light-$version-windows-x64.zip",
    "hash": { "url": "https://raw.githubusercontent.com/<owner>/<this-repo>/main/latest.json", "jsonpath": "$.sha256" }
  } }
}
```

The bucket is updated in the same run, not by polling on its side: the `publish` job
checks the bucket out in a second `actions/checkout`
(`repository: ${{ github.repository_owner }}/Scoop`, `token: secrets.SCOOP_TOKEN`,
`path: scoop`), copies the manifest over `bucket/hermes-desktop-light.json`, lets
`stefanzweifel/git-auto-commit-action` commit it as
`hermes-desktop-light: Update to version <version>`, and then reads the file back out
of the pushed branch to confirm. `GITHUB_TOKEN` cannot do this — it is scoped to this
repository — so `SCOOP_TOKEN` must be a token with `contents: write` on the bucket
repository. A run without it fails at the push rather than silently shipping a stale
bucket.

## Version scheme

The first build of an upstream release carries upstream's version (`0.23.3`). A
hand-dispatched rebuild of the same release carries `-r<N>` (`0.23.3-r2`), because
upstream's own tag can never be republished under a different number.

`tests/version-order.ps1` asserts that ordering against Scoop's `Compare-Version` from
a pinned Scoop commit — the same function Scoop uses to decide whether to offer an
update — and passes on this machine:

```
0.23.3 < 0.23.3-r2 < 0.23.3-r11 < 0.23.4 < 0.23.4-r1 < 0.23.10    (all newer)
2026.6864.2132.0-r1 < 0.23.3                                       (a legacy manifest upgrades forward)
0.23.3-r01 vs 0.23.3-r1                                            (no defined order: never pad a revision)
```

## Running it

* Scheduled six-hourly at `:52` (00:52/06:52/12:52/18:52 UTC); the resolver exits in
  seconds when the newest release is already published here. A second schedule entry
  kicks the very first run off; delete that line once a release exists.
* Manual: `gh workflow run build.yml -f revision=1` for a first build, or
  `-f revision=2 -f force=true` to rebuild a version that is already published.
* Re-publishing an existing version is refused unless `force` is set, and a published
  release is never overwritten — a rebuild always gets a new revision.
* Concurrency is per upstream commit, not per workflow: `build` holds
  `build-<upstream commit>` with `cancel-in-progress: false`, so the same release is
  never built twice in parallel and a build already running is never cancelled — a
  duplicate run queues behind it, then skips itself after re-checking that the release
  does not exist yet. `publish` holds `publish-<release tag>` with
  `cancel-in-progress: false`, so a publish is serialized rather than cancelled and a
  duplicate ends green having published nothing. Workflow-level keying is impossible
  here: the commit is only known after `resolve` runs.

Repository setup: `contents: write` permission (already declared in the workflow), and
a `SCOOP_TOKEN` secret that can write to `shanto/Scoop`. The bucket repository and
branch are set as `SCOOP_REPO` / `SCOOP_BRANCH` in the workflow's publish job.

## Local checks

```bash
python3 -m unittest discover -s tests -v      # resolver, guards, published shapes (offline)
pwsh tests/version-order.ps1                  # version ordering under Scoop's comparator
python3 scripts/resolve_release.py --repository <owner>/<repo> --dump-json   # live resolve
python3 scripts/publish_release.py --asset <zip> --provenance output/provenance.json \
    --out-dir output --dry-run                # write every generated file, touch nothing
python3 scripts/build_light.py --help         # --pack verifies and re-packages an existing build
```

The unit tests drive the GitHub client through an injected transport and the build
guards against synthetic checkouts and package directories, so resolution, refusal and
packaging behaviour are all provable without a Windows runner or a released build.

## Deliberately not here

No upstream feed or channel tracking, no MSIX/Store packaging, no macOS or ARM64 leg,
no code signing, no Scoop install acceptance test (the bucket repo can run that), and no
full dependency licence audit beyond what the build asserts: upstream's own `LICENSE` is
staged from the admitted commit, and the Electron and Chromium notices must be present.
That last part is deliberate — the light variant's `--dir` packaging does not carry the
application's licence text on its own, and redistributing an MIT application without it
is not permitted. The build runs upstream's driver unmodified; if upstream changes the
light contract, the stamp and identity guards are what will fail first, loudly.

## Credit

The verification approach (native double-launch smoke with a restricted PATH, stamp
and updater-ownership assertions, provenance beside the artifact) was learned from
[takano536/scoop-bucket][takano], which builds the same light payload and is
Unlicense-licensed. This repository is a smaller two-part take on it: build and
publish here, bucket there.

[takano]: https://github.com/takano536/scoop-bucket

The packaged application is Hermes Desktop, an MIT-licensed product of Nous Research.
This repository is not affiliated with Nous Research, and this build is unofficial and
unsigned: upstream does not produce, support, or vouch for it.
