import hashlib
import importlib.util
import json
import os
import struct
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile

spec = importlib.util.spec_from_file_location(
    "lifecycle", Path(__file__).parents[1] / "scripts" / "lifecycle.py")
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)


def release(tag, prerelease=False, draft=False):
    return {"tag_name": tag, "prerelease": prerelease, "draft": draft}


def manifest(version="0.1.0b1", tag="v0.1.0-beta.1", sha="abc"):
    return {"version": version, "tag": tag, "platform": "windows-x64", "commit": sha,
            "executable": "UMD/UMD.exe", "sha256": "a" * 64,
            "console_executable": "UMD/UMD-console.exe", "ui": "qt-widgets",
            "checks": dict.fromkeys(["version", "self_test", "environment", "unit_tests", "gui", "media_tools", "ssl_runtime"], True),
            "smoke_tests": ["--version", "--self-test", "--check-environment", "--gui-smoke"]}


def windows_executable(subsystem=2):
    data = bytearray(512)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 60, 128)
    data[128:132] = b"PE\x00\x00"
    struct.pack_into("<H", data, 132, 0x8664)
    struct.pack_into("<H", data, 152, 0x20B)
    struct.pack_into("<H", data, 220, subsystem)
    return bytes(data)


def portable(directory, entries=None):
    root = Path(directory)
    info = manifest()
    zip_name = "UMD-v0.1.0-beta.1-windows-x64.zip"
    entries = entries if entries is not None else {
        "UMD/UMD.exe": windows_executable(), "UMD/UMD-console.exe": windows_executable(3),
        "UMD/tools/yt-dlp.exe": b"MZ", "UMD/tools/deno.exe": b"MZ",
        "UMD/tools/ffmpeg.exe": b"MZ", "UMD/tools/ffprobe.exe": b"MZ",
        "UMD/browser/chrome.exe": b"MZ", "UMD/START_HERE.txt": b"Start UMD.exe"}
    with zipfile.ZipFile(root / zip_name, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    info["sha256"] = hashlib.sha256((root / zip_name).read_bytes()).hexdigest()
    (root / "build-manifest.json").write_text(json.dumps(info), encoding="utf-8")
    (root / "SHA256SUMS.txt").write_text(f"{info['sha256']}  {zip_name}\n", encoding="utf-8")
    return info


class VersionPolicyTests(unittest.TestCase):
    def test_same_version_across_sprints(self):
        policy.validate_transition("0.1.0", "0.1.0", None)

    def test_version_increase_requires_published_stable(self):
        for previous in [None, release("v0.1.0", prerelease=True), release("v0.1.0", draft=True)]:
            with self.assertRaises(ValueError):
                policy.validate_transition("0.1.0", "0.2.0", previous)
        policy.validate_transition("0.1.0", "0.2.0", release("v0.1.0"))

    def test_no_version_rollback_or_ambiguous_version(self):
        with self.assertRaises(ValueError):
            policy.validate_transition("0.2.0", "0.1.0", release("v0.2.0"))
        for version in ["01.2.3", "1.2", "v1.2.3", "1.2.3-beta.1", "1.2.3\n"]:
            with self.assertRaises(ValueError):
                policy.version_tuple(version)

    def test_beta_numbers_are_monotonic_and_version_scoped(self):
        tags = ["v0.1.0-beta.1", "v0.1.0-beta.9", "v0.2.0-beta.100", "v0.1.0-beta.02"]
        self.assertEqual(policy.beta_number("0.1.0", tags), 10)

    def test_cleanup_does_not_touch_other_versions_or_stable(self):
        stable = release("v0.1.0")
        beta = release("v0.1.0-beta.1", prerelease=True)
        entries = [stable, beta, release("v0.2.0-beta.1", prerelease=True),
                   release("v0.1.0-beta.2"), release("v0.1.0-beta.1-extra", prerelease=True)]
        self.assertEqual(policy.cleanup_candidates("0.1.0", stable, entries), [beta])

    def test_cleanup_requires_the_exact_published_stable(self):
        for stable in [None, release("v0.2.0"), release("v0.1.0", draft=True),
                       release("v0.1.0", prerelease=True)]:
            with self.assertRaises(ValueError):
                policy.cleanup_candidates("0.1.0", stable, [])


class ArtifactTests(unittest.TestCase):
    def test_portable_build_matches_version_and_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            portable(directory)
            self.assertEqual(len(policy.verify_artifacts(directory, "0.1.0b1", "v0.1.0-beta.1", "abc")), 3)
            for version, tag, sha in [("0.1.0", "v0.1.0-beta.1", "abc"),
                                      ("0.1.0b1", "v0.1.0", "abc"),
                                      ("0.1.0b1", "v0.1.0-beta.1", "wrong")]:
                with self.assertRaises(ValueError):
                    policy.verify_artifacts(directory, version, tag, sha)

    def test_missing_bundled_tools_or_fake_executable_rejected(self):
        for entries in [{"UMD/UMD.exe": b"MZ"}, {"UMD/UMD.exe": b"fake"},
                        {"../UMD.exe": b"MZ"}]:
            with tempfile.TemporaryDirectory() as directory:
                portable(directory, entries)
                with self.assertRaises(ValueError):
                    policy.verify_artifacts(directory, "0.1.0b1", "v0.1.0-beta.1", "abc")

    def test_archive_tampering_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            portable(directory)
            with (Path(directory) / "UMD-v0.1.0-beta.1-windows-x64.zip").open("ab") as stream:
                stream.write(b"tampering")
            with self.assertRaises(ValueError):
                policy.verify_artifacts(directory, "0.1.0b1", "v0.1.0-beta.1", "abc")

    def test_failed_smoke_checks_rejected(self):
        info = manifest()
        info["checks"]["environment"] = False
        with self.assertRaises(ValueError):
            policy.validate_manifest(info, "0.1.0b1", "v0.1.0-beta.1", "abc")

    def test_console_executable_cannot_replace_graphical_launcher(self):
        with tempfile.TemporaryDirectory() as directory:
            portable(directory)
            zip_file = Path(directory) / "UMD-v0.1.0-beta.1-windows-x64.zip"
            with zipfile.ZipFile(zip_file) as archive:
                entries = {name: archive.read(name) for name in archive.namelist()}
            entries["UMD/UMD.exe"] = windows_executable(3)
            portable(directory, entries)
            with self.assertRaises(ValueError):
                policy.verify_artifacts(directory, "0.1.0b1", "v0.1.0-beta.1", "abc")

    def test_missing_gui_or_media_tool_check_blocks_release(self):
        for key in ["gui", "media_tools", "ssl_runtime"]:
            info = manifest()
            info["checks"].pop(key)
            with self.assertRaises(ValueError):
                policy.validate_manifest(info, "0.1.0b1", "v0.1.0-beta.1", "abc")

    def test_empty_or_python_package_only_build_cannot_be_published(self):
        with tempfile.TemporaryDirectory() as directory:
            for name in [None, "umd.whl"]:
                if name:
                    (Path(directory) / name).write_bytes(b"wheel")
                with self.assertRaises(ValueError):
                    policy.verify_artifacts(directory, "0.1.0b1", "v0.1.0-beta.1", "abc")

    def test_release_notes_are_short_and_required(self):
        with patch.object(policy.Path, "read_text", return_value="- Windows x64 app.\n- Metadata exports."):
            self.assertIn("Metadata", policy.release_notes("0.1.0"))
        for text in ["", "\n".join(["- feature"] * 9), "a" * 2001]:
            with patch.object(policy.Path, "read_text", return_value=text), self.assertRaises(ValueError):
                policy.release_notes("0.1.0")


class ReleaseOperationsTests(unittest.TestCase):
    def test_no_beta_after_stable(self):
        api = Mock()
        api.request.return_value = {"object": {"sha": "abc"}}
        api.release.return_value = release("v0.1.0")
        with patch.object(policy, "GitHub", return_value=api), \
                patch.object(policy, "read_version", return_value="0.1.0"), \
                patch.object(policy, "git", return_value="abc"):
            with self.assertRaises(ValueError):
                policy.plan("beta", "abc")
        api.all_releases.assert_not_called()

    def test_latest_beta_uses_highest_number_and_excludes_drafts(self):
        entries = [release("v0.1.0-beta.2", True), release("v0.1.0-beta.10", True),
                   release("v0.1.0-beta.20", True, True), release("v0.2.0-beta.100", True)]
        self.assertEqual(policy.latest_beta("0.1.0", entries)["tag_name"], "v0.1.0-beta.10")

    def test_latest_beta_must_match_current_master_even_if_older_beta_matches(self):
        api = Mock()
        api.all_releases.return_value = [release("v0.1.0-beta.1", True), release("v0.1.0-beta.2", True)]
        with patch.object(policy, "git", return_value="newer-beta-other-commit"), self.assertRaises(ValueError):
            policy.require_latest_beta(api, "0.1.0", "master")
        api.asset.assert_not_called()

    def test_published_beta_requires_windows_assets(self):
        api = Mock()
        api.all_releases.return_value = [release("v0.1.0-beta.1", True)]
        with patch.object(policy, "git", return_value="abc"), self.assertRaises(ValueError):
            policy.require_latest_beta(api, "0.1.0", "abc")

    def test_verified_latest_beta_metadata(self):
        api = Mock()
        beta = release("v0.1.0-beta.1", True)
        names = ["UMD-v0.1.0-beta.1-windows-x64.zip", "build-manifest.json", "SHA256SUMS.txt"]
        beta["assets"] = [{"name": name, "size": 100, "id": index} for index, name in enumerate(names)]
        api.all_releases.return_value = [beta]
        api.asset.side_effect = [json.dumps(manifest()).encode(),
                                ("a" * 64 + "  " + names[0] + "\n").encode()]
        with patch.object(policy, "git", return_value="abc"):
            self.assertEqual(policy.require_latest_beta(api, "0.1.0", "abc"), beta)

    def test_stable_requires_manual_promotion(self):
        with patch.dict(os.environ, {"GITHUB_EVENT_NAME": "push"}), self.assertRaises(ValueError):
            policy.prepare_stable()

    def test_stable_requires_main_not_master(self):
        api = Mock()
        api.request.return_value = {"object": {"sha": "main"}}
        with patch.object(policy, "GitHub", return_value=api), \
                patch.object(policy, "read_version", return_value="0.1.0"), \
                patch.object(policy, "git", return_value="master"), self.assertRaises(ValueError):
            policy.plan("stable", "master")

    def test_manual_promotion_merges_pinned_beta_to_main(self):
        api = Mock()
        api.release.return_value = None
        api.request.return_value = {"sha": "merge", "commit": {"tree": {"sha": "tree"}}}
        with patch.dict(os.environ, {"GITHUB_EVENT_NAME": "workflow_dispatch"}), \
                patch.object(policy, "GitHub", return_value=api), \
                patch.object(policy, "read_version", return_value="0.1.0"), \
                patch.object(policy, "branch_sha", side_effect=["master", "main", "master", "main"]), \
                patch.object(policy, "git", side_effect=["master", "", "tree", "tree", "workflows", "workflows"]), \
                patch.object(policy, "require_latest_beta", return_value=release("v0.1.0-beta.3", True)), \
                patch.object(policy, "output_values") as outputs:
            policy.prepare_stable()
        api.request.assert_called_once_with("POST", "/merges", {
            "base": "main", "head": "master", "commit_message": "Release 0.1.0 from v0.1.0-beta.3"})
        outputs.assert_called_once_with({"sha": "merge", "beta_commit": "master", "beta_tag": "v0.1.0-beta.3"})

    def test_promotion_rejects_untested_main_changes_before_merge(self):
        api = Mock()
        api.release.return_value = None
        with patch.dict(os.environ, {"GITHUB_EVENT_NAME": "workflow_dispatch"}), \
                patch.object(policy, "GitHub", return_value=api), \
                patch.object(policy, "read_version", return_value="0.1.0"), \
                patch.object(policy, "branch_sha", side_effect=["master", "main"]), \
                patch.object(policy, "git", side_effect=["master", "", "untested-tree", "beta-tree"]), \
                patch.object(policy, "require_latest_beta", return_value=release("v0.1.0-beta.3", True)), \
                self.assertRaises(ValueError):
            policy.prepare_stable()
        api.request.assert_not_called()

    def test_publish_keeps_draft_if_upload_fails(self):
        api = Mock()
        api.request.side_effect = [
            {"object": {"sha": "abc"}},
            {"id": 123, "upload_url": "https://uploads.github.com/example{?name}"},
            RuntimeError("upload failed"),
        ]
        plan_data = {"sha": "abc", "tag": "v0.1.0-beta.1", "channel": "beta", "branch": "master",
                     "package_version": "0.1.0b1", "version": "0.1.0"}
        artifact = Mock()
        artifact.name = "UMD-v0.1.0-beta.1-windows-x64.zip"
        artifact.read_bytes.return_value = b"zip"
        with patch.object(policy, "GitHub", return_value=api), \
                patch.object(policy.Path, "read_text", return_value=json.dumps(plan_data)), \
                patch.object(policy, "git", return_value="abc"), \
                patch.object(policy, "release_notes", return_value="- Windows app."), \
                patch.object(policy, "verify_artifacts", return_value=[artifact]):
            with self.assertRaises(RuntimeError):
                policy.publish()
        self.assertTrue(api.request.call_args_list[1].args[2]["draft"])
        self.assertFalse(any(call.args[0] == "PATCH" for call in api.request.call_args_list))

    def test_stale_branch_cannot_publish(self):
        plan_data = {"sha": "abc", "tag": "v0.1.0-beta.1", "channel": "beta", "branch": "master",
                     "package_version": "0.1.0b1", "version": "0.1.0"}
        api = Mock()
        api.request.return_value = {"object": {"sha": "changed"}}
        with patch.object(policy, "GitHub", return_value=api), \
                patch.object(policy.Path, "read_text", return_value=json.dumps(plan_data)), \
                patch.object(policy, "verify_artifacts", return_value=[]), self.assertRaises(ValueError):
            policy.publish()
        self.assertFalse(any(call.args[0] == "POST" for call in api.request.call_args_list))

    def test_cleanup_protects_release_marked_stable_even_with_beta_name(self):
        api = Mock()
        api.release.return_value = release("v0.1.0")
        beta = dict(release("v0.1.0-beta.1", prerelease=True), id=1)
        api.all_releases.return_value = [beta, release("v0.1.0-beta.2")]
        with patch.object(policy, "GitHub", return_value=api), \
                patch.object(policy, "git", return_value="v0.1.0-beta.1\nv0.1.0-beta.2\nv0.2.0-beta.1\nv0.1.0"):
            policy.cleanup("0.1.0")
        self.assertEqual([call.args for call in api.request.call_args_list], [
            ("DELETE", "/releases/1"), ("DELETE", "/git/refs/tags/v0.1.0-beta.1")])


if __name__ == "__main__":
    unittest.main()
