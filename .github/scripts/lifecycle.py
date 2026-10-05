"""Version policy and GitHub release operations. Uses only the Python standard library."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import struct
import urllib.error
import urllib.request
import zipfile


VERSION_RE = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")


def version_tuple(version):
    if not VERSION_RE.fullmatch(version):
        raise ValueError("VERSION must be X.Y.Z, with no leading zeros or suffixes")
    return tuple(map(int, version.split(".")))


def read_version():
    version = Path("VERSION").read_text(encoding="utf-8").strip()
    version_tuple(version)
    return version


def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


class GitHub:
    def __init__(self):
        self.repo = os.environ["GITHUB_REPOSITORY"]
        self.base = f"https://api.github.com/repos/{self.repo}"
        self.token = os.environ["GH_TOKEN"]

    def request(self, method, path, data=None, binary=None):
        url = path if path.startswith("https://uploads.github.com/") else self.base + path
        headers = {"Authorization": f"Bearer {self.token}",
                   "Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28"}
        body = json.dumps(data).encode() if data is not None else binary
        if data is not None:
            headers["Content-Type"] = "application/json"
        elif binary is not None:
            headers["Content-Type"] = "application/octet-stream"
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                payload = response.read()
                return json.loads(payload) if payload else None
        except urllib.error.HTTPError as error:
            if error.code == 404 and method == "GET":
                return None
            raise RuntimeError(f"GitHub {method} request failed with HTTP {error.code}") from None

    def release(self, tag):
        return self.request("GET", f"/releases/tags/{tag}")

    def all_releases(self):
        releases = []
        page = 1
        while True:
            batch = self.request("GET", f"/releases?per_page=100&page={page}")
            if batch is None:
                raise RuntimeError("Unable to list releases")
            releases.extend(batch)
            if len(batch) < 100:
                return releases
            page += 1

    def asset(self, asset):
        """Download small verification metadata through the authenticated asset API."""
        request = urllib.request.Request(self.base + f"/releases/assets/{asset['id']}", headers={
            "Authorization": f"Bearer {self.token}", "Accept": "application/octet-stream",
            "X-GitHub-Api-Version": "2022-11-28"})
        with urllib.request.urlopen(request, timeout=60) as response:
            data = response.read(1024 * 1024 + 1)
        if len(data) > 1024 * 1024:
            raise ValueError("Release metadata is unexpectedly large")
        return data


def is_stable(release):
    return bool(release and not release["draft"] and not release["prerelease"])


def validate_transition(previous, current, previous_release):
    version_tuple(current)
    if previous is None or previous == current:
        return
    if version_tuple(current) <= version_tuple(previous):
        raise ValueError("VERSION can only increase")
    if not is_stable(previous_release):
        raise ValueError(f"Publish stable v{previous} before advancing VERSION")


def beta_number(version, tags):
    pattern = re.compile(rf"v{re.escape(version)}-beta\.([1-9][0-9]*)")
    numbers = [int(match[1]) for tag in tags if (match := pattern.fullmatch(tag))]
    return max(numbers, default=0) + 1


def cleanup_candidates(version, stable, releases):
    if not is_stable(stable) or stable["tag_name"] != f"v{version}":
        raise ValueError(f"Published stable v{version} is required before cleanup")
    pattern = re.compile(rf"v{re.escape(version)}-beta\.[1-9][0-9]*")
    return [release for release in releases
            if release["prerelease"] and pattern.fullmatch(release["tag_name"])]


def check():
    current = read_version()
    # Inspect every integration change, including changes hidden behind later
    # unchanged commits. For a PR checkout, the synthetic merge's first parent
    # is the target branch, just as for a real master merge.
    commits = git("log", "--first-parent", "--format=%H", "--", "VERSION").splitlines()
    versions = [git("show", f"{commit}:VERSION") for commit in reversed(commits)]
    if not versions or versions[-1] != current:
        versions.append(current)
    previous = None
    for version in versions:
        prior_release = GitHub().release(f"v{previous}") if previous and previous != version else None
        validate_transition(previous, version, prior_release)
        previous = version
    print(f"Version policy OK: {current}")


def branch_sha(api, branch):
    ref = api.request("GET", f"/git/ref/heads/{branch}")
    if not ref:
        raise ValueError(f"Required branch {branch} does not exist")
    return ref["object"]["sha"]


def latest_beta(version, releases):
    pattern = re.compile(rf"v{re.escape(version)}-beta\.([1-9][0-9]*)")
    candidates = [(int(match[1]), release) for release in releases
                  if release["prerelease"] and not release["draft"]
                  and (match := pattern.fullmatch(release["tag_name"]))]
    if not candidates:
        raise ValueError(f"Publish a successful Windows beta of {version} before stable")
    return max(candidates, key=lambda item: item[0])[1]


def require_latest_beta(api, version, sha, expected_tag=""):
    beta = latest_beta(version, api.all_releases())
    tag = beta["tag_name"]
    if expected_tag and tag != expected_tag:
        raise ValueError("Latest published beta changed; restart the stable release")
    if git("rev-list", "-n", "1", tag) != sha:
        raise ValueError("The latest published beta must match current master exactly")
    assets = {asset["name"]: asset for asset in beta.get("assets", [])}
    zip_name = f"UMD-{tag}-windows-x64.zip"
    required = [zip_name, "build-manifest.json", "SHA256SUMS.txt"]
    if any(name not in assets or assets[name].get("size", 0) <= 0 for name in required):
        raise ValueError("Latest beta is missing a verified Windows x64 application build")
    manifest = json.loads(api.asset(assets["build-manifest.json"]).decode("utf-8"))
    number = tag.rsplit(".", 1)[1]
    validate_manifest(manifest, f"{version}b{number}", tag, sha)
    checksum = api.asset(assets["SHA256SUMS.txt"]).decode("utf-8").strip()
    if checksum != f"{manifest['sha256']}  {zip_name}":
        raise ValueError("Latest beta checksum metadata is inconsistent")
    digest = assets[zip_name].get("digest")
    if digest and digest != "sha256:" + manifest["sha256"]:
        raise ValueError("Latest beta ZIP checksum does not match GitHub's asset digest")
    return beta


def output_values(result):
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        for key, value in result.items():
            output.write(f"{key}={value}\n")


def prepare_stable():
    if os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch":
        raise ValueError("Stable promotion requires an explicit manual workflow command")
    api = GitHub()
    version = read_version()
    master = branch_sha(api, "master")
    main = branch_sha(api, "main")
    if git("rev-parse", "HEAD") != master:
        raise ValueError("Master changed; restart the stable release")
    if api.release(f"v{version}") or f"v{version}" in git("tag", "--list").splitlines():
        raise ValueError("Stable release or tag already exists; investigate before retrying")
    beta = require_latest_beta(api, version, master)
    # Predict the merge before changing main: no untested main-only changes may enter a release.
    merged_tree = git("merge-tree", "--write-tree", main, master).splitlines()[0]
    master_tree = git("rev-parse", f"{master}^{{tree}}")
    if merged_tree != master_tree:
        raise ValueError("Merging main would change the tested beta tree; reconcile branches first")
    # GITHUB_TOKEN cannot write workflow files. Mirror reviewed workflow changes to main first.
    if git("ls-tree", main, ".github/workflows") != git("ls-tree", master, ".github/workflows"):
        raise ValueError("Mirror workflow changes to main before stable promotion (token limitation)")
    if branch_sha(api, "master") != master or branch_sha(api, "main") != main:
        raise ValueError("Branches changed before promotion; restart the stable release")
    merged = api.request("POST", "/merges", {
        "base": "main", "head": master, "commit_message": f"Release {version} from {beta['tag_name']}"})
    sha = merged["sha"] if merged else branch_sha(api, "main")
    tree = merged["commit"]["tree"]["sha"] if merged else git("rev-parse", f"{sha}^{{tree}}")
    if tree != master_tree:
        raise ValueError("Main merge differs from the tested beta; stable publication stopped")
    output_values({"sha": sha, "beta_commit": master, "beta_tag": beta["tag_name"]})
    print(f"Promoted {beta['tag_name']} to main at {sha}")


def plan(channel, commit, beta_commit="", beta_tag=""):
    api = GitHub()
    version = read_version()
    sha = git("rev-parse", "HEAD")
    branch = "main" if channel == "stable" else "master"
    if branch_sha(api, branch) != sha or (commit and commit != sha):
        raise ValueError(f"Publish only the current {branch} commit; restart the release")
    stable = api.release(f"v{version}")
    if stable:
        raise ValueError(f"v{version} already exists; finish it or advance VERSION after publication")
    tags = git("tag", "--list").splitlines()
    if f"v{version}" in tags:
        raise ValueError("Stable tag already exists; investigate before publishing again")
    if channel == "stable":
        if not beta_commit or not beta_tag or branch_sha(api, "master") != beta_commit:
            raise ValueError("Stable requires a pinned manual promotion of the latest master beta")
        require_latest_beta(api, version, beta_commit, beta_tag)
        if git("rev-parse", "HEAD^{tree}") != git("rev-parse", f"{beta_commit}^{{tree}}"):
            raise ValueError("Stable source tree must equal the latest beta source tree")
        tag, package_version = f"v{version}", version
    else:
        releases = api.all_releases()
        number = beta_number(version, tags + [r["tag_name"] for r in releases])
        tag, package_version = f"v{version}-beta.{number}", f"{version}b{number}"
    result = {"tag": tag, "version": version, "package_version": package_version,
              "sha": sha, "channel": channel, "branch": branch,
              "beta_commit": beta_commit, "beta_tag": beta_tag}
    Path(".ci-release.json").write_text(json.dumps(result), encoding="utf-8")
    output_values(result)
    print(f"Planned {tag} at {sha}")


def validate_manifest(manifest, expected, tag, commit):
    required = {"version": expected, "tag": tag, "platform": "windows-x64",
                "commit": commit, "executable": "UMD/UMD.exe", "ui": "qt-widgets",
                "console_executable": "UMD/UMD-console.exe"}
    if any(manifest.get(key) != value for key, value in required.items()):
        raise ValueError("Application manifest does not match requested version, tag, commit or platform")
    if not re.fullmatch(r"[0-9a-f]{64}", manifest.get("sha256", "")):
        raise ValueError("Application manifest must contain a ZIP SHA256 checksum")
    if any(manifest.get("checks", {}).get(key) is not True
           for key in ["version", "self_test", "environment", "unit_tests", "gui", "media_tools", "ssl_runtime"]):
        raise ValueError("Application build must pass executable smoke checks and unit tests")
    if not {"--version", "--self-test", "--check-environment", "--gui-smoke"}.issubset(manifest.get("smoke_tests", [])):
        raise ValueError("Required executable smoke tests were not recorded")


def verify_artifacts(directory, expected, tag="", commit=""):
    root = Path(directory)
    files = sorted(root.glob("*"))
    manifest_file = root / "build-manifest.json"
    if not manifest_file.is_file():
        raise ValueError("Build must produce a tested Windows x64 portable application and manifest")
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    # CLI verification always uses the checked-out commit, never an untrusted manifest's commit.
    tag = tag or manifest.get("tag", "")
    commit = commit or git("rev-parse", "HEAD")
    validate_manifest(manifest, expected, tag, commit)
    zip_name = f"UMD-{tag}-windows-x64.zip"
    if {file.name for file in files} != {zip_name, "build-manifest.json", "SHA256SUMS.txt"}:
        raise ValueError("Release directory must contain only the application ZIP, manifest and checksums")
    application = root / zip_name
    with application.open("rb") as stream:
        actual_hash = hashlib.file_digest(stream, "sha256").hexdigest()
    if actual_hash != manifest["sha256"]:
        raise ValueError("Windows application ZIP checksum does not match the build manifest")
    if (root / "SHA256SUMS.txt").read_text(encoding="utf-8").strip() != f"{actual_hash}  {zip_name}":
        raise ValueError("SHA256SUMS.txt does not match the application ZIP")
    with zipfile.ZipFile(application) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)) or any(".." in name.split("/") or name.startswith("/")
                                              or "\\" in name or ":" in name for name in names):
            raise ValueError("Unsafe or duplicated portable ZIP entries")
        if "UMD/UMD.exe" not in names:
            raise ValueError("Portable ZIP must contain UMD/UMD.exe")
        for name, subsystem in [("UMD/UMD.exe", 2), ("UMD/UMD-console.exe", 3)]:
            if name not in names:
                raise ValueError(f"Portable ZIP is missing {name}")
            with archive.open(name) as executable:
                header = executable.read(4096)
            try:
                offset = struct.unpack_from("<I", header, 60)[0]
                machine = struct.unpack_from("<H", header, offset + 4)[0]
                magic = struct.unpack_from("<H", header, offset + 24)[0]
                actual_subsystem = struct.unpack_from("<H", header, offset + 24 + 68)[0]
                valid = (header[:2] == b"MZ" and header[offset:offset + 4] == b"PE\x00\x00"
                         and machine == 0x8664 and magic == 0x20B and actual_subsystem == subsystem)
            except struct.error:
                valid = False
            if not valid:
                raise ValueError(f"{name} must be a Windows x64 executable with subsystem {subsystem}")
        for tool in ["yt-dlp.exe", "deno.exe", "chrome.exe", "ffmpeg.exe", "ffprobe.exe"]:
            if not any(name.endswith("/" + tool) for name in names):
                raise ValueError(f"Portable ZIP is missing bundled {tool}")
        if not any(name.endswith("START_HERE.txt") for name in names):
            raise ValueError("Portable ZIP must include getting-started instructions")
    return files


def release_notes(version):
    text = Path(f"release-notes/{version}.md").read_text(encoding="utf-8").strip()
    if not text or len(text.splitlines()) > 8 or len(text) > 2000:
        raise ValueError("Release notes must be meaningful and concise (maximum 8 lines / 2000 characters)")
    return text


def publish():
    api = GitHub()
    plan_data = json.loads(Path(".ci-release.json").read_text(encoding="utf-8"))
    files = verify_artifacts("release-dist", plan_data["package_version"], plan_data["tag"], plan_data["sha"])
    if branch_sha(api, plan_data["branch"]) != plan_data["sha"]:
        raise ValueError("Release branch changed during build; restart the release")
    if git("rev-parse", "HEAD") != plan_data["sha"]:
        raise ValueError("Built checkout differs from the planned release commit")
    if plan_data["channel"] == "stable":
        if branch_sha(api, "master") != plan_data["beta_commit"]:
            raise ValueError("Master changed during stable build; publish its beta before stable")
        require_latest_beta(api, plan_data["version"], plan_data["beta_commit"], plan_data["beta_tag"])
    notes = release_notes(plan_data["version"])
    # Keep the release a draft until every verified artifact is uploaded.
    release = api.request("POST", "/releases", {
        "tag_name": plan_data["tag"], "target_commitish": plan_data["sha"],
        "name": plan_data["tag"], "body": notes, "draft": True,
        "prerelease": plan_data["channel"] == "beta"})
    from urllib.parse import quote
    for file in files:
        url = release["upload_url"].split("{")[0] + "?name=" + quote(file.name)
        api.request("POST", url, binary=file.read_bytes())
    release = api.request("PATCH", f"/releases/{release['id']}", {
        "draft": False, "make_latest": "true" if plan_data["channel"] == "stable" else "false"})
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        output.write(f"url={release['html_url']}\n")
    print(f"Published {release['html_url']}")


def cleanup(version):
    version_tuple(version)
    api = GitHub()
    releases = api.all_releases()
    candidates = cleanup_candidates(version, api.release(f"v{version}"), releases)
    for release in candidates:
        api.request("DELETE", f"/releases/{release['id']}")
    # Include orphan beta tags and support retry after partial release deletion.
    pattern = re.compile(rf"v{re.escape(version)}-beta\.[1-9][0-9]*")
    protected_tags = {release["tag_name"] for release in releases if not release["prerelease"]}
    for tag in git("tag", "--list").splitlines():
        if pattern.fullmatch(tag) and tag not in protected_tags:
            api.request("DELETE", f"/git/refs/tags/{tag}")
    print(f"Removed beta releases and tags for {version}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["check", "prepare-stable", "plan", "verify", "publish", "cleanup"])
    parser.add_argument("--channel", choices=["beta", "stable"], default="beta")
    parser.add_argument("--commit", default="")
    parser.add_argument("--version", default="")
    parser.add_argument("--tag", default="")
    parser.add_argument("--beta-commit", default="")
    parser.add_argument("--beta-tag", default="")
    args = parser.parse_args()
    if args.command == "check":
        check()
    elif args.command == "plan":
        plan(args.channel, args.commit, args.beta_commit, args.beta_tag)
    elif args.command == "prepare-stable":
        prepare_stable()
    elif args.command == "verify":
        verify_artifacts("release-dist", args.version, args.tag, args.commit)
    elif args.command == "publish":
        publish()
    else:
        cleanup(args.version or read_version())
