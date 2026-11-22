#!/usr/bin/env python3
"""Local checks for the builder: release resolution, build guards, published shapes.

Everything here runs offline. The GitHub client is driven through an injected
transport, and the build guards are exercised against synthetic checkouts and
package directories, so the fail-closed paths are provable without a Windows
runner or a released build.

    python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_light  # noqa: E402
import publish_release  # noqa: E402
import resolve_release  # noqa: E402
from github_api import GitHub, GitHubError  # noqa: E402

COMMIT = "caafe5389ae585ef77387a962aee05b1bcf17029"
OTHER_COMMIT = "ae2a81ccc00e9b7c06301523e5bb09608397149c"
TAG_OBJECT = "2861fe3744f8134b782659438961a4f69ab5d495"
CLAIM_OBJECT = "8de0016e755d3cdb834656b75148ecdda8182be1"
UPSTREAM = "NousResearch/hermes-agent"
REPOSITORY = "shanto/hermes-desktop-light"
CLAIM_DATE = "2026-11-20T07:00:00Z"
CLAIM_EPOCH = str(int(datetime(2026, 11, 20, 7, 0, 0, tzinfo=timezone.utc).timestamp()))


def transport(routes: dict):
    """Stub transport. A route value is (status, payload) or a callable(query)."""
    def call(method, url, headers, body):
        parsed = urlsplit(url)
        route = routes.get(f"{method} {parsed.path}")
        if route is None:
            return 404, {}, b'{"message":"Not Found"}'
        status, payload = route
        if callable(payload):
            payload = payload(parse_qs(parsed.query))
        if not isinstance(payload, bytes):
            payload = json.dumps(payload).encode("utf-8")
        return status, {}, payload
    return call


def upstream_routes(tag: str = "v0.23.3", commit: str = COMMIT,
                    claim: str = "rc.1-v0.23.3", claim_commit: str | None = None,
                    claim_object: str = CLAIM_OBJECT) -> dict:
    routes = {
        f"GET /repos/{UPSTREAM}/releases/latest": (200, {
            "tag_name": tag, "draft": False, "prerelease": False,
            "published_at": "2026-11-20T07:02:11Z",
            "html_url": f"https://github.com/{UPSTREAM}/releases/tag/{tag}"}),
        f"GET /repos/{UPSTREAM}/git/ref/tags/{tag}": (
            200, {"object": {"type": "tag", "sha": TAG_OBJECT}}),
        f"GET /repos/{UPSTREAM}/git/tags/{TAG_OBJECT}": (
            200, {"object": {"type": "commit", "sha": commit},
                  "tagger": {"date": CLAIM_DATE}}),
        f"GET /repos/{UPSTREAM}/tags": (200, [
            {"name": "v0.23.1", "commit": {"sha": OTHER_COMMIT}},
            {"name": claim, "commit": {"sha": claim_commit or commit}}]),
        f"GET /repos/{UPSTREAM}/git/ref/tags/{claim}": (
            200, {"object": {"type": "tag", "sha": claim_object}}),
    }
    return routes


class PlanTest(unittest.TestCase):
    def plan(self, routes, *, revision=1, force=False):
        return resolve_release.plan(GitHub(transport=routes and transport(routes)),
                                    UPSTREAM, REPOSITORY, revision, force)

    def test_version_shape(self):
        self.assertEqual(resolve_release.version_for("0.23.3", 1), "0.23.3")
        self.assertEqual(resolve_release.version_for("0.23.3", 2), "0.23.3-r2")
        self.assertEqual(resolve_release.version_for("0.23.3", 11), "0.23.3-r11")
        for bad in (0, -1):
            with self.assertRaises(ValueError):
                resolve_release.version_for("0.23.3", bad)
        with self.assertRaises(ValueError):
            resolve_release.version_for("2026.6864.2132.0", 1)

    def test_happy_path(self):
        plan = self.plan(upstream_routes() | {
            f"GET /repos/{REPOSITORY}/releases/tags/v0.23.3": (404, {}),
        })
        self.assertEqual(plan["build"], "true")
        self.assertEqual(plan["version"], "0.23.3")
        self.assertEqual(plan["release_tag"], "v0.23.3")
        self.assertEqual(plan["upstream_commit"], COMMIT)
        self.assertEqual(plan["claim_tag"], "rc.1-v0.23.3")
        self.assertEqual(plan["claim_object"], CLAIM_OBJECT)
        self.assertEqual(plan["upstream_claim_epoch"], CLAIM_EPOCH)

    def test_rebuild_carries_a_revision_suffix(self):
        plan = self.plan(upstream_routes() | {
            f"GET /repos/{REPOSITORY}/releases/tags/v0.23.3-r2": (404, {}),
        }, revision=2)
        self.assertEqual(plan["build"], "true")
        self.assertEqual(plan["version"], "0.23.3-r2")
        self.assertEqual(plan["release_tag"], "v0.23.3-r2")

    def test_rejects_canary_style_tag_shapes(self):
        plan = self.plan(upstream_routes(tag="v0.23.3+canary.20261118T064620Z"))
        self.assertEqual(plan["build"], "false")
        self.assertEqual(plan["skip_reason"],
                         "unsupported-release-tag:v0.23.3+canary.20261118T064620Z")

    def test_date_tag_is_only_buildable_with_a_release_claim(self):
        plan = self.plan(upstream_routes(tag="v2026.9.24"))
        self.assertEqual(plan["build"], "false")
        self.assertEqual(plan["skip_reason"], "no-release-claim:v2026.9.24")

    def test_skips_when_no_claim_matches_the_release_commit(self):
        plan = self.plan(upstream_routes(claim_commit=OTHER_COMMIT))
        self.assertEqual(plan["build"], "false")
        self.assertTrue(plan["skip_reason"].startswith("no-release-claim:"))

    def test_refuses_ambiguous_claim(self):
        routes = upstream_routes()
        routes[f"GET /repos/{UPSTREAM}/tags"] = (200, [
            {"name": "rc.1-v0.23.3", "commit": {"sha": COMMIT}},
            {"name": "rc.2-v0.23.3", "commit": {"sha": COMMIT}}])
        with self.assertRaises(GitHubError):
            self.plan(routes)

    def test_skips_already_published_unless_forced(self):
        published = {f"GET /repos/{REPOSITORY}/releases/tags/v0.23.3": (200, {"id": 1})}
        plan = self.plan(upstream_routes() | published)
        self.assertEqual(plan["build"], "false")
        self.assertEqual(plan["skip_reason"], "already-published:v0.23.3")
        self.assertEqual(self.plan(upstream_routes() | published, force=True)["build"], "true")

    def test_rejects_non_annotated_claim(self):
        routes = upstream_routes()
        routes[f"GET /repos/{UPSTREAM}/git/ref/tags/rc.1-v0.23.3"] = (
            200, {"object": {"type": "commit", "sha": COMMIT}})
        with self.assertRaises(GitHubError):
            self.plan(routes)

    def test_rejects_release_tag_without_a_commit(self):
        routes = upstream_routes()
        routes[f"GET /repos/{UPSTREAM}/git/ref/tags/v0.23.3"] = (
            200, {"object": {"type": "commit", "sha": "not-a-sha"}})
        with self.assertRaises(GitHubError):
            self.plan(routes)

    def test_cli_reports_a_skip_as_data(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "out.txt"
            client = transport(upstream_routes(tag="v2026.9.24"))
            with mock.patch.object(resolve_release, "GitHub",
                                   lambda *a, **k: GitHub(transport=client)):
                code = resolve_release.main(["--repository", REPOSITORY,
                                             "--output", str(output)])
            self.assertEqual(code, 0)
            self.assertIn("build=false", output.read_text(encoding="utf-8"))


class GuardTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def make_repo(self) -> Path:
        repo = self.root / "upstream"
        repo.mkdir()
        for argv in (["init", "-q"],
                     ["config", "user.email", "builder@example.com"],
                     ["config", "user.name", "Builder"]):
            subprocess.run(["git", "-C", str(repo), *argv], check=True)
        (repo / "LICENSE").write_text("MIT License\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "LICENSE"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "initial"], check=True)
        return repo

    @staticmethod
    def head(repo: Path) -> str:
        return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                              capture_output=True, text=True, check=True).stdout.strip()

    def make_pack(self, name: str = "pack", commit: str = COMMIT,
                  tag: str = "v0.23.3", **overrides) -> Path:
        pack = self.root / name
        (pack / "resources").mkdir(parents=True, exist_ok=True)
        (pack / "resources" / "app.asar").write_bytes(b"asar")
        stamp = {"payload": "light", "distribution": "desktop-app",
                 "updateMechanism": "external", "commit": commit, "tag": tag,
                 "dirty": False}
        stamp.update(overrides)
        (pack / "resources" / "install-stamp.json").write_text(json.dumps(stamp),
                                                               encoding="utf-8")
        (pack / "Hermes Light.exe").write_bytes(b"exe")
        for licence in ("LICENSE", "LICENSE.electron.txt", "LICENSES.chromium.html"):
            (pack / licence).write_bytes(b"licence text\n")
        return pack

    # -- checkout and ref guards -------------------------------------------
    def test_checkout_guards(self):
        repo = self.make_repo()
        head = self.head(repo)
        build_light.assert_checkout(repo, head)
        for wrong in (OTHER_COMMIT, "abc"):
            with self.assertRaises(build_light.BuildError):
                build_light.assert_checkout(repo, wrong)
        (repo / "LICENSE").write_text("changed\n", encoding="utf-8")
        with self.assertRaises(build_light.BuildError):
            build_light.assert_checkout(repo, head)

    def test_release_ref_guards(self):
        repo = self.make_repo()
        head = self.head(repo)
        for tag, message in (("v0.23.3", "release"), ("rc.1-v0.23.3", "claim")):
            subprocess.run(["git", "-C", str(repo), "tag", "-a", tag, "-m", message],
                           check=True)
        claim_object = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "refs/tags/rc.1-v0.23.3"],
            capture_output=True, text=True, check=True).stdout.strip()
        build_light.assert_release_refs(repo, "v0.23.3", "rc.1-v0.23.3", claim_object, head)
        with self.assertRaises(build_light.BuildError):
            build_light.assert_release_refs(repo, "v0.23.3", "rc.1-v0.23.3",
                                            OTHER_COMMIT, head)
        with self.assertRaises(build_light.BuildError):
            build_light.assert_release_refs(repo, "v9.9.9", "rc.1-v0.23.3",
                                            claim_object, head)
        with self.assertRaises(build_light.BuildError):
            build_light.assert_release_refs(repo, "v0.23.3", "rc.1-v0.23.3",
                                            claim_object, OTHER_COMMIT)

    # -- package guards ----------------------------------------------------
    def test_pack_guard_accepts_a_light_package(self):
        stamp = build_light.assert_pack(self.make_pack(), COMMIT, "v0.23.3")
        self.assertEqual(stamp["payload"], "light")

    def test_pack_guard_rejections(self):
        cases = {
            "payload": {"payload": "bundled"},
            "distribution": {"distribution": "store"},
            "mechanism": {"updateMechanism": "electron-updater"},
            "commit": {"commit": OTHER_COMMIT},
            "tag": {"tag": "v0.23.2"},
            "dirty": {"dirty": True},
        }
        for label, overrides in cases.items():
            with self.subTest(label=label):
                pack = self.make_pack(name=f"pack-{label}", **overrides)
                with self.assertRaises(build_light.BuildError):
                    build_light.assert_pack(pack, COMMIT, "v0.23.3")

    def test_pack_guard_rejects_a_shipped_agent_payload(self):
        pack = self.make_pack()
        (pack / "resources" / "agent-payload").mkdir()
        with self.assertRaises(build_light.BuildError):
            build_light.assert_pack(pack, COMMIT, "v0.23.3")

    def test_pack_guard_rejects_a_missing_bundle(self):
        pack = self.make_pack()
        (pack / "resources" / "app.asar").unlink()
        with self.assertRaises(build_light.BuildError):
            build_light.assert_pack(pack, COMMIT, "v0.23.3")

    def test_pack_guard_rejects_a_missing_stamp(self):
        pack = self.make_pack()
        (pack / "resources" / "install-stamp.json").unlink()
        with self.assertRaises(build_light.BuildError):
            build_light.assert_pack(pack, COMMIT, "v0.23.3")

    def test_pack_guard_requires_the_licence_text(self):
        # The light variant's packager output carries no application LICENSE, and a
        # redistribution without it is not permitted, so its absence must fail.
        for missing in ("LICENSE", "LICENSE.electron.txt", "LICENSES.chromium.html"):
            with self.subTest(missing=missing):
                pack = self.make_pack(name=f"pack-{missing}")
                (pack / missing).unlink()
                with self.assertRaises(build_light.BuildError):
                    build_light.assert_pack(pack, COMMIT, "v0.23.3")

    def test_stage_licenses_copies_the_admitted_commit_bytes(self):
        repo = self.make_repo()
        pack = self.root / "stage-pack"
        pack.mkdir()
        for notice in build_light.PACKAGER_LICENSES:
            (pack / notice).write_bytes(b"packager notice\n")
        staged = build_light.stage_licenses(repo, pack)
        expected = subprocess.run(["git", "-C", str(repo), "show", "HEAD:LICENSE"],
                                  capture_output=True, check=True).stdout
        self.assertEqual((pack / "LICENSE").read_bytes(), expected)
        self.assertEqual(staged["LICENSE"]["sha256"], build_light.sha256_bytes(expected))
        self.assertEqual(staged["LICENSE"]["size"], len(expected))
        for notice in build_light.PACKAGER_LICENSES:
            self.assertIn(notice, staged)

    def test_stage_licenses_refuses_a_commit_without_a_licence(self):
        repo = self.root / "unlicensed"
        repo.mkdir()
        (repo / "README").write_text("no licence here\n", encoding="utf-8")
        for argv in (["init", "-q"], ["config", "user.email", "b@example.com"],
                     ["config", "user.name", "Builder"], ["add", "README"],
                     ["commit", "-qm", "no licence"]):
            subprocess.run(["git", "-C", str(repo), *argv], check=True)
        (self.root / "empty-pack").mkdir()
        with self.assertRaises(build_light.BuildError):
            build_light.stage_licenses(repo, self.root / "empty-pack")

    # -- packaging ---------------------------------------------------------
    def test_zip_is_deterministic_and_sorted(self):
        pack = self.root / "tree"
        (pack / "sub").mkdir(parents=True)
        (pack / "b.txt").write_text("beta", encoding="utf-8")
        (pack / "sub" / "a.txt").write_text("alpha", encoding="utf-8")
        first, second = self.root / "one.zip", self.root / "two.zip"
        build_light.zip_pack(pack, first)
        build_light.zip_pack(pack, second)
        self.assertEqual(build_light.sha256_file(first), build_light.sha256_file(second))
        with zipfile.ZipFile(first) as archive:
            names = archive.namelist()
            self.assertEqual(names, sorted(names))
            self.assertEqual(archive.getinfo("b.txt").date_time, (1980, 1, 1, 0, 0, 0))

    def test_notice_records_provenance(self):
        notice = build_light.write_notice(
            self.make_pack(), upstream=UPSTREAM, upstream_tag="v0.23.3", commit=COMMIT,
            claim_tag="rc.1-v0.23.3", version="0.23.3",
            version_url="https://example.invalid/release", built_at="2026-11-22T00:00:00Z")
        text = notice.read_text(encoding="utf-8")
        self.assertIn("UNOFFICIAL BUILD", text)
        self.assertIn(COMMIT, text)
        self.assertIn("rc.1-v0.23.3", text)

    def test_build_cli_fails_on_a_wrong_commit(self):
        repo = self.make_repo()
        code = build_light.main([
            "--source", str(repo), "--tag", "v0.23.3", "--expected-commit", OTHER_COMMIT,
            "--claim-tag", "rc.1-v0.23.3", "--claim-object", CLAIM_OBJECT,
            "--version", "0.23.3", "--release-tag", "v0.23.3",
            "--asset-name", "hermes-desktop-light-0.23.3-windows-x64.zip",
            "--out", str(self.root / "out")])
        self.assertEqual(code, 2)


class PublishShapeTest(unittest.TestCase):
    def provenance(self, **overrides) -> dict:
        data = {
            "schema": 1, "app": "hermes-desktop-light", "version": "0.23.3",
            "releaseTag": "v0.23.3",
            "asset": "hermes-desktop-light-0.23.3-windows-x64.zip",
            "sha256": "3e38de2c703f93d6c0595383c3409db5655f10117f86d046380d5bc9e5833e35",
            "size": 1234, "builtAt": "2026-11-22T00:00:00Z",
            "upstream": {"repository": UPSTREAM, "tag": "v0.23.3", "commit": COMMIT,
                         "claimTag": "rc.1-v0.23.3", "claimObject": CLAIM_OBJECT,
                         "publishedAt": "2026-11-20T07:02:11Z",
                         "url": f"https://github.com/{UPSTREAM}/releases/tag/v0.23.3",
                         "licenseSha256": "aa" * 32},
            "builder": {"repository": REPOSITORY, "commit": "bb" * 20,
                        "workflowRun": f"https://github.com/{REPOSITORY}/actions/runs/1"},
        }
        data.update(overrides)
        return data

    def test_manifest_matches_the_published_asset(self):
        manifest = publish_release.scoop_manifest(self.provenance(), REPOSITORY)
        self.assertEqual(manifest["version"], "0.23.3")
        entry = manifest["architecture"]["64bit"]
        self.assertTrue(entry["url"].endswith(
            f"/v0.23.3/hermes-desktop-light-0.23.3-windows-x64.zip"))
        self.assertIn(REPOSITORY, entry["url"])
        self.assertEqual(entry["hash"], self.provenance()["sha256"])
        self.assertEqual(manifest["checkver"]["jsonpath"], "$.version")
        self.assertIn("$version", manifest["autoupdate"]["architecture"]["64bit"]["url"])
        self.assertEqual(manifest["shortcuts"],
                         [["Hermes Light.exe", publish_release.SHORTCUT_NAME]])

    def test_rebuild_manifest_orders_above_the_base_release(self):
        manifest = publish_release.scoop_manifest(
            self.provenance(version="0.23.3-r2", releaseTag="v0.23.3-r2",
                            asset="hermes-desktop-light-0.23.3-r2-windows-x64.zip"),
            REPOSITORY)
        self.assertEqual(manifest["version"], "0.23.3-r2")
        self.assertIn("/v0.23.3-r2/hermes-desktop-light-0.23.3-r2-windows-x64.zip",
                      manifest["architecture"]["64bit"]["url"])

    def test_pointer_carries_everything_a_consumer_needs(self):
        pointer = publish_release.latest_pointer(self.provenance(), REPOSITORY)
        self.assertEqual(pointer["version"], "0.23.3")
        self.assertEqual(pointer["upstream"]["commit"], COMMIT)
        self.assertEqual(pointer["upstream"]["claimTag"], "rc.1-v0.23.3")
        self.assertTrue(pointer["url"].startswith("https://github.com/"))
        self.assertTrue(pointer["provenance"].endswith("/provenance.json"))
        self.assertEqual(pointer["builder"]["repository"], REPOSITORY)

    def test_pointer_version_is_discoverable_by_checkver(self):
        pointer = json.loads(json.dumps(publish_release.latest_pointer(
            self.provenance(), REPOSITORY)))
        self.assertEqual(pointer["version"], self.provenance()["version"])
        self.assertIn("sha256", pointer)

    def test_release_notes_name_the_unofficial_source(self):
        notes = publish_release.release_notes(self.provenance(), REPOSITORY)
        self.assertIn("v0.23.3", notes)
        self.assertIn(COMMIT, notes)
        self.assertIn("does not produce or support this package", notes)

    def test_dry_run_writes_the_published_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            asset = root / self.provenance()["asset"]
            asset.write_bytes(b"payload")
            provenance = self.provenance(sha256=build_light.sha256_bytes(b"payload"),
                                         size=len(b"payload"))
            provenance_file = root / "provenance.json"
            provenance_file.write_text(json.dumps(provenance), encoding="utf-8")
            out = root / "out"
            code = publish_release.main([
                "--repository", REPOSITORY, "--asset", str(asset),
                "--provenance", str(provenance_file), "--out-dir", str(out),
                "--dry-run", "--update-main", "--dispatch-repo", "shanto/bucket"])
            self.assertEqual(code, 0)
            manifest = json.loads(
                (out / "hermes-desktop-light.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["version"], "0.23.3")
            self.assertEqual(manifest["architecture"]["64bit"]["hash"],
                             provenance["sha256"])
            self.assertEqual(
                json.loads((out / "latest.json").read_text(encoding="utf-8"))["sha256"],
                provenance["sha256"])
            self.assertTrue((out / "SHA256SUMS").read_text(encoding="utf-8")
                            .startswith(provenance["sha256"]))

    def test_publish_refuses_a_mismatched_asset(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / self.provenance()["asset"]).write_bytes(b"payload")
            provenance_file = root / "provenance.json"
            provenance_file.write_text(json.dumps(self.provenance()), encoding="utf-8")
            code = publish_release.main([
                "--repository", REPOSITORY, "--asset", str(root / self.provenance()["asset"]),
                "--provenance", str(provenance_file), "--out-dir", str(root / "out"),
                "--dry-run"])
            self.assertEqual(code, 2)

    def test_upload_request_shape(self):
        url = publish_release.upload_url("https://uploads.github.com", REPOSITORY, 42, "a.zip")
        self.assertEqual(
            url,
            f"https://uploads.github.com/repos/{REPOSITORY}/releases/42/assets?name=a.zip")
        headers = publish_release.upload_headers("token", 7)
        self.assertEqual(headers["Content-Length"], "7")
        self.assertTrue(headers["Authorization"].startswith("Bearer "))


class SmokeScriptTest(unittest.TestCase):
    def test_smoke_driver_parses(self):
        script = ROOT / "scripts" / "smoke_light.cjs"
        result = subprocess.run(["node", "--check", str(script)],
                                capture_output=True, text=True, check=False)
        if result.returncode != 0 and "not found" in result.stderr.lower():
            self.skipTest("node is not available")
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
