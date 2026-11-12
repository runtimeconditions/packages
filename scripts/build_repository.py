"""Build signed apt and yum repositories from verified rc-cli release packages."""

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


SOURCE = "runtimeconditions/rc-cli"
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


def assets(tag):
    version = tag[1:]
    return [name for deb, rpm in ARCHITECTURES for name in (
        f"rc-cli_{version}-1_{deb}.deb", f"rc-cli-{version}-1.{rpm}.rpm")]


def stable(release):
    return TAG.fullmatch(release["tag_name"]) and not release["draft"] and not release["prerelease"]


def complete(release):
    names = {asset["name"] for asset in release["assets"]}
    return set(assets(release["tag_name"]) + ["SHA256SUMS"]) <= names


def releases(trigger):
    requested = None
    if trigger:
        if not TAG.fullmatch(trigger):
            raise ValueError("trigger tag must be vMAJOR.MINOR.PATCH")
        release = api(f"repos/{SOURCE}/releases/tags/{trigger}")
        if not stable(release) or not complete(release):
            raise ValueError("trigger release must be stable and contain all four Linux packages")
        requested = release
    all_releases, page = [], 1
    while True:
        batch = api(f"repos/{SOURCE}/releases?per_page=100&page={page}")
        all_releases.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    by_tag = {release["tag_name"]: release for release in all_releases}
    if requested:
        by_tag[trigger] = requested
    candidates = sorted((r for r in by_tag.values() if stable(r)),
                        key=lambda r: tuple(map(int, TAG.fullmatch(r["tag_name"]).groups())), reverse=True)
    if not candidates or not complete(candidates[0]):
        raise ValueError("newest stable release must contain all four Linux packages")
    return [release for release in candidates if complete(release)][:10]


def checksums(path):
    result = {}
    for line in path.read_text().splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  ([A-Za-z0-9._-]+)", line)
        if not match or match[2] in result:
            raise ValueError("invalid or duplicate SHA256SUMS entry")
        result[match[2]] = match[1]
    return result


def stage(release, output, temporary):
    tag = release["tag_name"]
    download = temporary / tag
    download.mkdir()
    for name in assets(tag) + ["SHA256SUMS"]:
        url = f"https://github.com/{SOURCE}/releases/download/{tag}/{name}"
        with urllib.request.urlopen(url, timeout=60) as response, (download / name).open("wb") as target:
            shutil.copyfileobj(response, target)
    manifest = checksums(download / "SHA256SUMS")
    for name in assets(tag):
        package = download / name
        with package.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if manifest.get(name) != digest:
            raise ValueError(f"missing or incorrect checksum for {tag}/{name}")
    for deb, rpm in ARCHITECTURES:
        debian, redhat = (download / name for name in (
            f"rc-cli_{tag[1:]}-1_{deb}.deb", f"rc-cli-{tag[1:]}-1.{rpm}.rpm"))
        for field, expected in (("Package", "rc-cli"), ("Version", tag[1:] + "-1"), ("Architecture", deb)):
            if run("dpkg-deb", "--field", str(debian), field).decode().strip() != expected:
                raise ValueError(f"incorrect {field} in {debian.name}")
        metadata = run("rpm", "-qp", "--queryformat", "%{NAME} %{VERSION}-%{RELEASE} %{ARCH}", str(redhat))
        if metadata.decode() != f"rc-cli {tag[1:]}-1 {rpm}":
            raise ValueError(f"incorrect package metadata in {redhat.name}")
        for package, directory in ((debian, output / "apt/pool" / deb), (redhat, output / "rpm" / rpm)):
            directory.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(package, directory / package.name)
            (directory / package.name).chmod(0o644)
    print(f"Verified and staged {tag}", flush=True)


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
    parser.add_argument("--tag", default="")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--key", required=True, help="full RSA signing-key fingerprint")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Fa-f0-9]{40}", args.key):
        raise ValueError("--key must be a full 40-character signing-key fingerprint")
    output = args.output.resolve()
    if output.exists() and any(output.iterdir()):
        raise ValueError("output directory must be new or empty")
    selected = releases(args.tag)
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="rc-packages-") as temporary:
        for release in selected:
            stage(release, output, Path(temporary))
    rpm_repository(output, args.key)
    apt_repository(output, args.key)
    client_files(output, args.key)
    print(f"Built signed repositories for {len(selected)} release(s)", flush=True)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        raise SystemExit(f"build_repository: {error}")
