"""Version policy and GitHub release operations. Uses only the Python standard library."""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
import urllib.error
import urllib.request
import zipfile
from email.parser import Parser


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


def plan(channel, commit):
    api = GitHub()
    version = read_version()
    sha = git("rev-parse", "HEAD")
    master = api.request("GET", "/git/ref/heads/master")
    if not master or master["object"]["sha"] != sha or (commit and commit != sha):
        raise ValueError("Publish only the current master commit; restart against current master")
    stable = api.release(f"v{version}")
    if stable:
        raise ValueError(f"v{version} already exists; finish it or advance VERSION after publication")
    tags = git("tag", "--list").splitlines()
    if f"v{version}" in tags:
        raise ValueError("Stable tag already exists; investigate before publishing again")
    releases = api.all_releases()
    if channel == "stable":
        candidates = [r for r in releases if r["prerelease"] and not r["draft"]
                      and re.fullmatch(rf"v{re.escape(version)}-beta\.[1-9][0-9]*", r["tag_name"])]
        if not any(git("rev-list", "-n", "1", r["tag_name"]) == sha for r in candidates):
            raise ValueError("Publish a successful beta of this master commit before stable")
        tag, package_version = f"v{version}", version
    else:
        number = beta_number(version, tags + [r["tag_name"] for r in releases])
        tag, package_version = f"v{version}-beta.{number}", f"{version}b{number}"
    result = {"tag": tag, "version": version, "package_version": package_version,
              "sha": sha, "channel": channel}
    Path(".ci-release.json").write_text(json.dumps(result), encoding="utf-8")
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        for key, value in result.items():
            output.write(f"{key}={value}\n")
    print(f"Planned {tag} at {sha}")


def verify_artifacts(directory, expected):
    files = sorted(Path(directory).glob("*"))
    wheels = [file for file in files if file.suffix == ".whl"]
    sources = [file for file in files if file.name.endswith(".tar.gz")]
    if not wheels or not sources:
        raise ValueError("Build must produce both a wheel and a source distribution")
    if len(files) != len(wheels) + len(sources):
        raise ValueError("Unexpected file in release artifact directory")
    for file in wheels + sources:
        if file.suffix == ".whl":
            with zipfile.ZipFile(file) as archive:
                names = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
                if len(names) != 1:
                    raise ValueError(f"Invalid wheel metadata: {file.name}")
                metadata = archive.read(names[0]).decode("utf-8")
        else:
            with tarfile.open(file) as archive:
                # Read only the top-level package metadata, never extract the archive.
                names = [m for m in archive.getmembers()
                         if len(m.name.split("/")) == 2 and m.name.endswith("/PKG-INFO")]
                if len(names) != 1:
                    raise ValueError(f"Invalid source metadata: {file.name}")
                metadata = archive.extractfile(names[0]).read().decode("utf-8")
        actual = Parser().parsestr(metadata)["Version"]
        if actual != expected:
            raise ValueError(f"{file.name}: package version {actual!r}, expected {expected!r}")
    return files


def publish():
    api = GitHub()
    plan_data = json.loads(Path(".ci-release.json").read_text(encoding="utf-8"))
    files = verify_artifacts("dist", plan_data["package_version"])
    master = api.request("GET", "/git/ref/heads/master")
    if not master or master["object"]["sha"] != plan_data["sha"]:
        raise ValueError("Master changed during build; publish again against the new master")
    notes = api.request("POST", "/releases/generate-notes", {
        "tag_name": plan_data["tag"], "target_commitish": plan_data["sha"]})
    # Keep the release a draft until every verified artifact is uploaded.
    release = api.request("POST", "/releases", {
        "tag_name": plan_data["tag"], "target_commitish": plan_data["sha"],
        "name": plan_data["tag"], "body": notes["body"], "draft": True,
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
    parser.add_argument("command", choices=["check", "plan", "verify", "publish", "cleanup"])
    parser.add_argument("--channel", choices=["beta", "stable"], default="beta")
    parser.add_argument("--commit", default="")
    parser.add_argument("--version", default="")
    args = parser.parse_args()
    if args.command == "check":
        check()
    elif args.command == "plan":
        plan(args.channel, args.commit)
    elif args.command == "verify":
        verify_artifacts("dist", args.version)
    elif args.command == "publish":
        publish()
    else:
        cleanup(args.version or read_version())
