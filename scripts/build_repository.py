"""Build signed apt and yum repositories from verified release packages."""

import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import urllib.request


# Each package is built from the GitHub releases of its source repository, which attach
# <name>_<version>-1_<arch>.deb and <name>-<version>-1.<arch>.rpm files and a SHA256SUMS file.
PACKAGES = {
    "rc-cli": "runtimeconditions/rc-cli",
    "rc-profiler-typescript": "runtimeconditions/ts-rc-profiler",
    "rc-profiler-java": "runtimeconditions/java-rc-profiler",
}
# The repository is not built without a release of these. Another package is left out
# until one of its releases has packages attached.
REQUIRED = ("rc-cli",)
TAG = re.compile(r"^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
ARCHITECTURES = (("amd64", "x86_64"), ("arm64", "aarch64"))


def run(*args, cwd=None):
    return subprocess.check_output(args, cwd=cwd)


def api(path):
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "runtimeconditions-packages"}
    if token := os.environ.get("GH_TOKEN"):
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request("https://api.github.com/" + path, headers=headers)
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def assets(name, tag):
    version = tag[1:]
    return [file for deb, rpm in ARCHITECTURES for file in (
        f"{name}_{version}-1_{deb}.deb", f"{name}-{version}-1.{rpm}.rpm")]


def stable(release):
    return TAG.fullmatch(release["tag_name"]) and not release["draft"] and not release["prerelease"]


def complete(name, release):
    names = {asset["name"] for asset in release["assets"]}
    return set(assets(name, release["tag_name"]) + ["SHA256SUMS"]) <= names


def releases(name, trigger):
    """Return the newest releases of one package that carry all four Linux packages."""
    source = PACKAGES[name]
    requested = None
    if trigger:
        if not TAG.fullmatch(trigger):
            raise ValueError("trigger tag must be vMAJOR.MINOR.PATCH")
        release = api(f"repos/{source}/releases/tags/{trigger}")
        if not stable(release) or not complete(name, release):
            raise ValueError(f"trigger release must be stable and contain all four Linux packages of {name}")
        requested = release
    all_releases, page = [], 1
    while True:
        batch = api(f"repos/{source}/releases?per_page=100&page={page}")
        all_releases.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    by_tag = {release["tag_name"]: release for release in all_releases}
    if requested:
        by_tag[trigger] = requested
    candidates = sorted((r for r in by_tag.values() if stable(r)),
                        key=lambda r: tuple(map(int, TAG.fullmatch(r["tag_name"]).groups())), reverse=True)
    packaged = [release for release in candidates if complete(name, release)]
    if not packaged:
        return []
    if not complete(name, candidates[0]):
        raise ValueError(f"newest stable {name} release must contain all four Linux packages")
    return packaged[:10]


def select_releases(package, tag):
    """Choose the releases of every package. The tag, if any, belongs to the named package."""
    selected = {name: releases(name, tag if name == package else "") for name in PACKAGES}
    for name in REQUIRED:
        if not selected[name]:
            raise ValueError(f"no stable {name} release contains all four Linux packages")
    return selected


def checksums(path):
    result = {}
    for line in path.read_text().splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  ([A-Za-z0-9._-]+)", line)
        if not match or match[2] in result:
            raise ValueError("invalid or duplicate SHA256SUMS entry")
        result[match[2]] = match[1]
    return result


def stage(name, release, output, temporary):
    tag = release["tag_name"]
    download = temporary / name / tag
    download.mkdir(parents=True)
    for file in assets(name, tag) + ["SHA256SUMS"]:
        url = f"https://github.com/{PACKAGES[name]}/releases/download/{tag}/{file}"
        with urllib.request.urlopen(url, timeout=60) as response, (download / file).open("wb") as target:
            shutil.copyfileobj(response, target)
    manifest = checksums(download / "SHA256SUMS")
    for file in assets(name, tag):
        package = download / file
        with package.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if manifest.get(file) != digest:
            raise ValueError(f"missing or incorrect checksum for {name} {tag}/{file}")
    for deb, rpm in ARCHITECTURES:
        debian, redhat = (download / file for file in (
            f"{name}_{tag[1:]}-1_{deb}.deb", f"{name}-{tag[1:]}-1.{rpm}.rpm"))
        for field, expected in (("Package", name), ("Version", tag[1:] + "-1"), ("Architecture", deb)):
            if run("dpkg-deb", "--field", str(debian), field).decode().strip() != expected:
                raise ValueError(f"incorrect {field} in {debian.name}")
        metadata = run("rpm", "-qp", "--queryformat", "%{NAME} %{VERSION}-%{RELEASE} %{ARCH}", str(redhat))
        if metadata.decode() != f"{name} {tag[1:]}-1 {rpm}":
            raise ValueError(f"incorrect package metadata in {redhat.name}")
        for package, directory in ((debian, output / "apt/pool" / deb), (redhat, output / "rpm" / rpm)):
            directory.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(package, directory / package.name)
            (directory / package.name).chmod(0o644)
    print(f"Verified and staged {name} {tag}", flush=True)


def sign(path, key, *, clear=False):
    destination = path.with_name("InRelease") if clear else Path(str(path) + ".asc")
    run("gpg", "--batch", "--yes", "--local-user", key, "--digest-algo", "SHA256",
        "--armor", "--output", str(destination), "--clearsign" if clear else "--detach-sign", str(path))
    if clear:
        run("gpg", "--batch", "--verify", str(destination))
    else:
        run("gpg", "--batch", "--verify", str(destination), str(path))


def apt_repository(output, key):
    apt = output / "apt"
    release_dir = apt / "dists/stable"
    for arch, _ in ARCHITECTURES:
        directory = release_dir / "main" / f"binary-{arch}"
        directory.mkdir(parents=True)
        index = run("apt-ftparchive", "packages", f"pool/{arch}", cwd=apt)
        (directory / "Packages").write_bytes(index)
        (directory / "Packages.gz").write_bytes(gzip.compress(index, mtime=0))
    command = ["apt-ftparchive"]
    for field, value in {"Origin": "Runtime Conditions", "Label": "Runtime Conditions",
                         "Suite": "stable", "Codename": "stable", "Architectures": "amd64 arm64",
                         "Components": "main"}.items():
        command.extend(["-o", f"APT::FTPArchive::Release::{field}={value}"])
    release = release_dir / "Release"
    release.write_bytes(run(*command, "release", "dists/stable", cwd=apt))
    sign(release, key, clear=True)


def rpm_repository(output, key):
    for _, arch in ARCHITECTURES:
        directory = output / "rpm" / arch
        for package in sorted(directory.glob("*.rpm")):
            run("rpmsign", "--addsign", "--define", f"_gpg_name {key}",
                "--define", "__gpg /usr/bin/gpg", "--define", "_gpg_digest_algo sha256", str(package))
        run("createrepo_c", "--database", "--general-compress-type", "gz",
            "--compress-type", "gz", str(directory))
        sign(directory / "repodata/repomd.xml", key)


def client_files(output, key):
    keys = output / "keys"
    keys.mkdir()
    (keys / "runtimeconditions.gpg").write_bytes(run("gpg", "--batch", "--export", key))
    (keys / "runtimeconditions.asc").write_bytes(run("gpg", "--batch", "--armor", "--export", key))
    (keys / "fingerprint.txt").write_text(key.upper() + "\n")
    setup = Path(__file__).resolve().parents[1] / "setup.sh"
    shutil.copyfile(setup, output / "setup.sh")
    (output / "setup.sh").chmod(0o644)
    (output / "index.html").write_text(
        '<!doctype html><html lang="en"><meta charset="utf-8"><title>Runtime Conditions packages</title>'
        '<h1>Runtime Conditions packages</h1><p><a href="https://github.com/runtimeconditions/packages#readme">'
        'Installation instructions for apt and yum</a></p></html>\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", default="", help="release tag to validate; blank rebuilds from current releases")
    parser.add_argument("--package", default="rc-cli", choices=sorted(PACKAGES), help="the package that --tag belongs to")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--key", required=True, help="full RSA signing-key fingerprint")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Fa-f0-9]{40}", args.key):
        raise ValueError("--key must be a full 40-character signing-key fingerprint")
    output = args.output.resolve()
    if output.exists() and any(output.iterdir()):
        raise ValueError("output directory must be new or empty")
    selected = select_releases(args.package, args.tag)
    for name, chosen in selected.items():
        if not chosen:
            print(f"{name} has no release with packages yet; leaving it out", flush=True)
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="rc-packages-") as temporary:
        for name, chosen in selected.items():
            for release in chosen:
                stage(name, release, output, Path(temporary))
    rpm_repository(output, args.key)
    apt_repository(output, args.key)
    client_files(output, args.key)
    print("Built signed repositories for " + ", ".join(
        f"{name} ({len(chosen)} release(s))" for name, chosen in selected.items() if chosen), flush=True)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        raise SystemExit(f"build_repository: {error}")
