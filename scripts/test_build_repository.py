"""Tests for how build_repository.py chooses releases. Run: python3 -m unittest scripts/test_build_repository.py"""

from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_repository as build  # noqa: E402


def release(name, tag, *, packaged=True, draft=False, prerelease=False):
    files = (build.assets(name, tag) + ["SHA256SUMS"]) if packaged else []
    return {"tag_name": tag, "draft": draft, "prerelease": prerelease, "assets": [{"name": file} for file in files]}


def github(**releases_by_package):
    """A stand-in for the GitHub API that answers with the given releases of each source repository."""
    by_source = {build.PACKAGES[name]: items for name, items in releases_by_package.items()}

    def api(path):
        parts = path.split("/")
        source = "/".join(parts[1:3])
        if parts[3:5] == ["releases", "tags"]:
            return next(item for item in by_source[source] if item["tag_name"] == parts[5])
        return by_source[source]
    return mock.patch.object(build, "api", api)


class AssetNameTests(unittest.TestCase):
    def test_names_follow_the_package_name(self):
        self.assertEqual(build.assets("rc-cli", "v1.2.3"), [
            "rc-cli_1.2.3-1_amd64.deb", "rc-cli-1.2.3-1.x86_64.rpm",
            "rc-cli_1.2.3-1_arm64.deb", "rc-cli-1.2.3-1.aarch64.rpm"])
        self.assertEqual(build.assets("rc-profiler-typescript", "v0.3.0")[0], "rc-profiler-typescript_0.3.0-1_amd64.deb")


class ReleaseSelectionTests(unittest.TestCase):
    def test_a_package_with_no_packaged_release_is_left_out(self):
        with github(**{"rc-profiler-typescript": [release("rc-profiler-typescript", "v0.2.0", packaged=False)]}):
            self.assertEqual(build.releases("rc-profiler-typescript", ""), [])

    def test_the_newest_packaged_releases_come_first(self):
        items = [release("rc-cli", tag) for tag in ("v0.1.2", "v0.2.0", "v0.1.10")]
        with github(**{"rc-cli": items}):
            self.assertEqual([r["tag_name"] for r in build.releases("rc-cli", "")], ["v0.2.0", "v0.1.10", "v0.1.2"])

    def test_drafts_and_prereleases_are_ignored(self):
        items = [release("rc-cli", "v0.1.0"), release("rc-cli", "v0.2.0", draft=True), release("rc-cli", "v0.3.0-rc1", prerelease=True)]
        with github(**{"rc-cli": items}):
            self.assertEqual([r["tag_name"] for r in build.releases("rc-cli", "")], ["v0.1.0"])

    def test_a_newest_release_without_packages_is_an_error_once_older_ones_have_them(self):
        items = [release("rc-profiler-typescript", "v0.3.0"), release("rc-profiler-typescript", "v0.3.1", packaged=False)]
        with github(**{"rc-profiler-typescript": items}):
            with self.assertRaisesRegex(ValueError, "newest stable rc-profiler-typescript release"):
                build.releases("rc-profiler-typescript", "")

    def test_a_trigger_release_must_be_stable_and_complete(self):
        items = [release("rc-cli", "v0.1.0"), release("rc-cli", "v0.2.0", packaged=False)]
        with github(**{"rc-cli": items}):
            with self.assertRaisesRegex(ValueError, "trigger release must be stable and contain all four"):
                build.releases("rc-cli", "v0.2.0")
            with self.assertRaisesRegex(ValueError, "vMAJOR.MINOR.PATCH"):
                build.releases("rc-cli", "main")


class SelectReleasesTests(unittest.TestCase):
    def both(self, cli, typescript):
        return github(**{"rc-cli": cli, "rc-profiler-typescript": typescript})

    def test_the_repository_is_built_with_rc_cli_alone_until_the_other_package_has_packages(self):
        with self.both([release("rc-cli", "v0.2.0")], [release("rc-profiler-typescript", "v0.2.0", packaged=False)]):
            selected = build.select_releases("rc-profiler-typescript", "")
        self.assertEqual([r["tag_name"] for r in selected["rc-cli"]], ["v0.2.0"])
        self.assertEqual(selected["rc-profiler-typescript"], [])

    def test_both_packages_are_included_when_both_have_packages(self):
        with self.both([release("rc-cli", "v0.2.0")], [release("rc-profiler-typescript", "v0.3.0")]):
            selected = build.select_releases("rc-cli", "")
        self.assertEqual({name: [r["tag_name"] for r in chosen] for name, chosen in selected.items()},
                         {"rc-cli": ["v0.2.0"], "rc-profiler-typescript": ["v0.3.0"]})

    def test_the_tag_is_checked_against_its_own_package_only(self):
        with self.both([release("rc-cli", "v0.2.0")], [release("rc-profiler-typescript", "v0.3.0"), release("rc-profiler-typescript", "v0.2.0", packaged=False)]):
            selected = build.select_releases("rc-profiler-typescript", "v0.3.0")
            self.assertEqual([r["tag_name"] for r in selected["rc-profiler-typescript"]], ["v0.3.0"])
            with self.assertRaisesRegex(ValueError, "trigger release must be stable and contain all four Linux packages of rc-profiler-typescript"):
                build.select_releases("rc-profiler-typescript", "v0.2.0")

    def test_rc_cli_is_required(self):
        with self.both([release("rc-cli", "v0.2.0", packaged=False)], [release("rc-profiler-typescript", "v0.3.0")]):
            with self.assertRaisesRegex(ValueError, "no stable rc-cli release"):
                build.select_releases("rc-cli", "")


if __name__ == "__main__":
    unittest.main()
