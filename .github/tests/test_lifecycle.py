import importlib.util
from pathlib import Path
import tarfile
import tempfile
import unittest
import zipfile
import io
import json
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location(
    "lifecycle", Path(__file__).parents[1] / "scripts" / "lifecycle.py")
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)


def release(tag, prerelease=False, draft=False):
    return {"tag_name": tag, "prerelease": prerelease, "draft": draft}


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

    def test_packages_must_match_requested_version(self):
        with tempfile.TemporaryDirectory() as directory:
            with zipfile.ZipFile(Path(directory) / "umd.whl", "w") as archive:
                archive.writestr("umd.dist-info/METADATA", "Name: umd\nVersion: 0.1.0b1\n")
            with tarfile.open(Path(directory) / "umd.tar.gz", "w:gz") as archive:
                content = b"Name: umd\nVersion: 0.1.0b1\n"
                info = tarfile.TarInfo("umd/PKG-INFO")
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
            self.assertEqual(len(policy.verify_artifacts(directory, "0.1.0b1")), 2)
            with self.assertRaises(ValueError):
                policy.verify_artifacts(directory, "0.1.0")

    def test_empty_build_cannot_be_published(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                policy.verify_artifacts(directory, "0.1.0")


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

    def test_stable_requires_beta_of_exact_commit(self):
        api = Mock()
        api.request.return_value = {"object": {"sha": "abc"}}
        api.release.return_value = None
        api.all_releases.return_value = [release("v0.1.0-beta.1", prerelease=True)]
        with patch.object(policy, "GitHub", return_value=api), \
                patch.object(policy, "read_version", return_value="0.1.0"), \
                patch.object(policy, "git", side_effect=["abc", "v0.1.0-beta.1", "older"]):
            with self.assertRaises(ValueError):
                policy.plan("stable", "abc")

    def test_publish_keeps_draft_if_upload_fails(self):
        api = Mock()
        api.request.side_effect = [
            {"object": {"sha": "abc"}}, {"body": "Notes"},
            {"id": 123, "upload_url": "https://uploads.github.com/example{?name}"},
            RuntimeError("upload failed"),
        ]
        plan_data = {"sha": "abc", "tag": "v0.1.0-beta.1", "channel": "beta",
                     "package_version": "0.1.0b1"}
        artifact = Mock()
        artifact.name = "umd.whl"
        artifact.read_bytes.return_value = b"wheel"
        with patch.object(policy, "GitHub", return_value=api), \
                patch.object(policy.Path, "read_text", return_value=json.dumps(plan_data)), \
                patch.object(policy, "verify_artifacts", return_value=[artifact]):
            with self.assertRaises(RuntimeError):
                policy.publish()
        self.assertTrue(api.request.call_args_list[2].args[2]["draft"])
        self.assertFalse(any(call.args[0] == "PATCH" for call in api.request.call_args_list))

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
