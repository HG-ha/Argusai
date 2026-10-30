from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from pathlib import Path
from urllib.request import Request

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("release_feed", ROOT / "scripts/release_feed.py")
feed = importlib.util.module_from_spec(spec)
spec.loader.exec_module(feed)
COMMIT = "a" * 40
NOTES = "# ArgusAI v0.1.3\n\n- Preserve files.\n"


@pytest.fixture
def evidence():
    release = {
        "tag_name": "v0.1.3", "html_url": f"{feed.RELEASE_ROOT}/tag/v0.1.3",
        "draft": False, "prerelease": False, "published_at": "2026-10-30T20:00:00Z",
        "body": NOTES, "assets": [],
    }
    checksums = {}
    for suffix in [*feed.PLATFORMS.values(), "source.tar.gz"]:
        name = f"ArgusAI-0.1.3-{suffix}"
        data = b"package bytes for " + name.encode()
        digest = hashlib.sha256(data).hexdigest()
        checksum = f"{digest}  {name}\n".encode()
        checksums[name + ".sha256"] = checksum
        for asset_name, content in ((name, data), (name + ".sha256", checksum)):
            release["assets"].append({
                "name": asset_name, "size": len(content), "state": "uploaded",
                "digest": "sha256:" + hashlib.sha256(content).hexdigest(),
                "browser_download_url": f"{feed.RELEASE_ROOT}/download/v0.1.3/{asset_name}",
            })
    return release, checksums


def build(evidence, **kwargs):
    return feed.build_manifest(*evidence, resolved_commit=COMMIT, expected_commit=COMMIT,
                               committed_notes=NOTES, **kwargs)


def test_manifest_contains_exactly_five_platforms_and_verified_assets(evidence):
    manifest = build(evidence)
    assert set(manifest["assets"]) == set(feed.PLATFORMS)
    assert manifest["notes"] == NOTES.strip()
    assert manifest["schema_version"] == 1
    assert manifest["assets"]["win-arm64"]["url"].endswith("win-arm64-setup.exe")


@pytest.mark.parametrize("mutation", [
    lambda r: r.update(draft=True),
    lambda r: r.update(prerelease=True),
    lambda r: r.update(tag_name="v0.1.3-rc1"),
    lambda r: r.update(tag_name="v00.1.3"),
    lambda r: r.update(body=NOTES + "Uncommitted note"),
    lambda r: r.update(published_at=None),
    lambda r: r["assets"].pop(),
    lambda r: r["assets"].append(r["assets"][0]),
    lambda r: r["assets"][0].update(size=0),
    lambda r: r["assets"][0].update(size=True),
    lambda r: r["assets"][0].update(state="new"),
    lambda r: r["assets"][0].update(digest=None),
    lambda r: r["assets"][0].update(browser_download_url="https://evil.example/installer.exe"),
    lambda r: r["assets"][0].update(browser_download_url=r["assets"][0]["browser_download_url"].replace("HG-ha/Argusai", "evil/Argusai")),
])
def test_incomplete_or_untrusted_release_cannot_publish(evidence, mutation):
    mutation(evidence[0])
    with pytest.raises(ValueError):
        build(evidence)


def test_changed_checksum_rejected_even_if_name_and_length_match(evidence):
    name = next(iter(evidence[1]))
    first = b"0" if evidence[1][name][:1] != b"0" else b"1"
    evidence[1][name] = first + evidence[1][name][1:]
    with pytest.raises(ValueError, match="digest mismatch"):
        build(evidence)


def test_mismatching_package_digest_rejected(evidence):
    evidence[0]["assets"][0]["digest"] = "sha256:" + "b" * 64
    with pytest.raises(ValueError, match="Package SHA-256 mismatch"):
        build(evidence)


def test_wrong_tag_commit_rejected(evidence):
    with pytest.raises(ValueError, match="expected source commit"):
        feed.build_manifest(*evidence, resolved_commit="b" * 40, expected_commit=COMMIT,
                            committed_notes=NOTES)


def test_site_generation_is_idempotent_keeps_history_and_escapes_notes(evidence, tmp_path):
    manifest = build(evidence)
    manifest["notes"] = "# ArgusAI\n\n- <script>alert(1)</script>"
    changelog = tmp_path / "changelog.html"
    changelog.write_text("<header>Existing header</header>" + feed.BEGIN + "\n" + feed.END + "<p>Older release</p>", encoding="utf-8")
    assert len(feed.write_feed(tmp_path, manifest)) == 3
    assert feed.write_feed(tmp_path, manifest) == []
    page = changelog.read_text(encoding="utf-8")
    assert "Existing header" in page and "Older release" in page
    assert "&lt;script&gt;" in page and "<script>" not in page
    assert 'datetime="2026-10-31"' in page  # publication dates use Asia/Shanghai
    assert page.count('id="v0-1-3"') == 1
    assert json.loads((tmp_path / "updates/stable.json").read_text(encoding="utf-8")) == manifest


def test_older_release_cannot_replace_stable_or_mutate_published_version(evidence, tmp_path):
    manifest = build(evidence)
    (tmp_path / "changelog.html").write_text(feed.BEGIN + feed.END, encoding="utf-8")
    feed.write_feed(tmp_path, manifest)
    old = copy.deepcopy(manifest)
    old["version"] = "0.1.2"
    with pytest.raises(ValueError, match="downgrade"):
        feed.write_feed(tmp_path, old)
    changed = copy.deepcopy(manifest)
    changed["assets"]["win-x64"]["sha256"] = "f" * 64
    with pytest.raises(ValueError, match="immutable"):
        feed.write_feed(tmp_path, changed)
    assert json.loads((tmp_path / "updates/stable.json").read_text(encoding="utf-8")) == manifest


def test_missing_changelog_markers_do_not_publish_pointer(evidence, tmp_path):
    (tmp_path / "changelog.html").write_text("Unrelated page", encoding="utf-8")
    with pytest.raises(ValueError, match="markers"):
        feed.write_feed(tmp_path, build(evidence))
    assert not (tmp_path / "updates/stable.json").exists()


def test_live_feed_verification_does_not_accept_stale_or_corrupt_data(evidence, monkeypatch):
    manifest = build(evidence)
    stale = {**manifest, "version": "0.1.2"}
    monkeypatch.setattr(feed, "_read_url", lambda *a, **k: json.dumps(stale).encode())
    with pytest.raises(ValueError, match="not live"):
        feed.verify_live(manifest, attempts=1)
    monkeypatch.setattr(feed, "_read_url", lambda *a, **k: json.dumps(manifest).encode())
    feed.verify_live(manifest, attempts=1)


@pytest.mark.parametrize("target", ["http://release-assets.githubusercontent.com/a", "https://evil.example/a", "https://github.com.evil.example/a", "https://github.com:444/a", "https://user@github.com/a"])
def test_checksum_redirects_cannot_leave_https_github_hosts(target):
    with pytest.raises(ValueError, match="Unexpected redirect"):
        feed._ReleaseRedirect(False).redirect_request(Request("https://github.com/a"), None, 302, "Found", {}, target)


def test_api_credentials_never_follow_a_redirect():
    with pytest.raises(ValueError, match="Unexpected redirect"):
        feed._ReleaseRedirect(True).redirect_request(Request(feed.API + "/releases"), None, 302, "Found", {}, "https://github.com/a")


def test_release_feed_sync_waits_for_publish_and_preserves_main():
    import yaml
    workflow = yaml.load((ROOT / ".github/workflows/release-packages.yml").read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    job = workflow["jobs"]["update-website"]
    assert job["needs"] == ["validate", "publish"]
    assert job["steps"][0]["with"]["ref"] == "main"
    sync = next(step["run"] for step in job["steps"] if step.get("name", "").startswith("Generate metadata"))
    assert '"HEAD:main"' in sync and '"--force"' not in sync
    assert "--verify-live" in job["steps"][-1]["run"]


def test_native_update_regressions_run_after_tools_and_before_compilation():
    import yaml
    workflow = yaml.load((ROOT / ".github/workflows/release-packages.yml").read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    steps = workflow["jobs"]["build"]["steps"]
    names = [step.get("name") for step in steps]
    regression = "Test update verification, native installer helpers and GUI handoff"
    assert names.index("Ensure NSIS is available") < names.index(regression) < names.index("Build native package")
    dependencies = steps[names.index("Install locked Python dependencies")]["run"]
    assert "--group dev" in dependencies
    command = steps[names.index(regression)]["run"]
    assert "python -m pytest" in command
    for test in ("tests/core/test_updater.py", "tests/core/test_updater_native.py",
                 "tests/gui/test_updates.py", "tests/scripts/test_release_feed.py"):
        assert test in command
