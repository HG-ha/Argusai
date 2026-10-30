#!/usr/bin/env python3
"""Publish a static update feed only from a complete, verified GitHub Release."""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone, timedelta
import hashlib
import html
import json
import os
from pathlib import Path
import re
import tempfile
import time
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

REPOSITORY = "HG-ha/Argusai"
API = f"https://api.github.com/repos/{REPOSITORY}"
RELEASE_ROOT = f"https://github.com/{REPOSITORY}/releases"
PLATFORMS = {
    "win-x64": "win-x64-setup.exe",
    "win-arm64": "win-arm64-setup.exe",
    "linux-x64": "linux-x64.tar.gz",
    "linux-arm64": "linux-arm64.tar.gz",
    "macos-arm64": "macos-arm64.dmg",
}
BEGIN = "<!-- BEGIN GENERATED RELEASES -->"
END = "<!-- END GENERATED RELEASES -->"
VERSION = re.compile(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\Z")
SHA = re.compile(r"[0-9a-f]{64}\Z")


def version_tuple(version: str) -> tuple[int, ...]:
    if not isinstance(version, str) or not VERSION.fullmatch(version):
        raise ValueError("Only stable major.minor.patch releases are supported")
    return tuple(map(int, version.split(".")))


def build_manifest(release: dict, checksums: dict[str, bytes], *,
                   resolved_commit: str, expected_commit: str,
                   committed_notes: str) -> dict:
    """Validate evidence before generating any client-visible update metadata."""
    if not re.fullmatch(r"[0-9a-f]{40}", expected_commit):
        raise ValueError("Expected source commit must be a full Git SHA")
    if resolved_commit != expected_commit:
        raise ValueError("Release tag does not point to the expected source commit")
    if release.get("draft") is not False or release.get("prerelease") is not False:
        raise ValueError("Drafts and prereleases cannot update the stable feed")
    tag = release.get("tag_name", "")
    version = tag.removeprefix("v")
    version_tuple(version)
    if tag != f"v{version}" or release.get("html_url") != f"{RELEASE_ROOT}/tag/{tag}":
        raise ValueError("Unexpected release tag or repository URL")
    published = release.get("published_at")
    if not isinstance(published, str) or not published.endswith("Z"):
        raise ValueError("A published UTC timestamp is required")
    datetime.fromisoformat(published.replace("Z", "+00:00"))
    notes = release.get("body")
    if not isinstance(notes, str) or not notes.strip() or len(notes.encode("utf-8")) > 64 * 1024:
        raise ValueError("Missing or oversized release notes")
    normalize = lambda text: text.replace("\r\n", "\n").strip()
    if normalize(notes) != normalize(committed_notes):
        raise ValueError("Release notes do not match the tagged source")
    suffixes = list(PLATFORMS.values()) + ["source.tar.gz"]
    names = [f"ArgusAI-{version}-{suffix}" for suffix in suffixes]
    expected = {name for package in names for name in (package, package + ".sha256")}
    assets = release.get("assets")
    if not isinstance(assets, list) or len(assets) != len(expected):
        raise ValueError("The release must contain exactly six packages and six checksums")
    indexed = {asset.get("name"): asset for asset in assets}
    if set(indexed) != expected:
        raise ValueError("Missing, duplicate or unexpected release assets")
    for name, asset in indexed.items():
        if asset.get("state") != "uploaded" or type(asset.get("size")) is not int or asset["size"] <= 0:
            raise ValueError(f"Release asset is not ready: {name}")
        if asset.get("browser_download_url") != f"{RELEASE_ROOT}/download/{tag}/{name}":
            raise ValueError(f"Unexpected asset download URL: {name}")
        if not isinstance(asset.get("digest"), str) or not SHA.fullmatch(asset["digest"].removeprefix("sha256:")) or not asset["digest"].startswith("sha256:"):
            raise ValueError(f"Missing GitHub SHA-256 digest: {name}")
    for name in names:
        content = checksums.get(name + ".sha256")
        if not isinstance(content, bytes) or len(content) > 512:
            raise ValueError(f"Missing or oversized checksum: {name}")
        if hashlib.sha256(content).hexdigest() != indexed[name + ".sha256"]["digest"][7:]:
            raise ValueError(f"Checksum file digest mismatch: {name}")
        if len(content) != indexed[name + ".sha256"]["size"]:
            raise ValueError(f"Checksum file size mismatch: {name}")
        digest = indexed[name]["digest"][7:]
        if content.decode("utf-8").strip() != f"{digest}  {name}":
            raise ValueError(f"Package SHA-256 mismatch: {name}")
    return {
        "schema_version": 1,
        "version": version,
        "published_at": published,
        "release_url": release["html_url"],
        "notes": normalize(notes),
        "assets": {
            platform: {
                "url": indexed[f"ArgusAI-{version}-{suffix}"]["browser_download_url"],
                "sha256": indexed[f"ArgusAI-{version}-{suffix}"]["digest"][7:],
                "size": indexed[f"ArgusAI-{version}-{suffix}"]["size"],
            } for platform, suffix in PLATFORMS.items()
        },
    }


class _ReleaseRedirect(HTTPRedirectHandler):
    def __init__(self, authenticated: bool):
        self.authenticated = authenticated

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urlsplit(newurl)
        if self.authenticated or parsed.scheme != "https" or parsed.hostname not in {
            "github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com",
        } or parsed.username or parsed.password or parsed.port not in (None, 443):
            raise ValueError("Unexpected redirect while reading release evidence")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _read_url(url: str, *, limit: int, token: str = "") -> bytes:
    headers = {"User-Agent": "ArgusAI-release-feed", "Accept": "application/vnd.github+json"}
    if token:
        if not url.startswith(API + "/"):
            raise ValueError("GitHub token may only be sent to this repository API")
        headers["Authorization"] = f"Bearer {token}"
    with build_opener(_ReleaseRedirect(bool(token))).open(Request(url, headers=headers), timeout=30) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Response exceeds the update metadata size limit")
    return data


def fetch_verified_release(tag: str, commit: str, token: str = "") -> dict:
    version_tuple(tag.removeprefix("v"))
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("Expected source commit must be a full Git SHA")
    if tag != "v" + tag.removeprefix("v"):
        raise ValueError("Expected a v-prefixed stable tag")
    get = lambda path: json.loads(_read_url(API + path, limit=2 * 1024 * 1024, token=token))
    release = get(f"/releases/tags/{tag}")
    ref = get(f"/git/ref/tags/{tag}")["object"]
    for _ in range(5):
        if ref["type"] == "commit":
            break
        if ref["type"] != "tag" or not re.fullmatch(r"[0-9a-f]{40}", ref["sha"]):
            raise ValueError("Invalid release tag target")
        ref = get(f"/git/tags/{ref['sha']}")["object"]
    else:
        raise ValueError("Release tag nesting exceeds the limit")
    if ref.get("type") != "commit" or ref.get("sha") != commit:
        raise ValueError("Release tag does not match the expected source commit")
    content = get(f"/contents/scripts/packaging/release-notes-{tag[1:]}.md?ref={commit}")
    if content.get("encoding") != "base64":
        raise ValueError("Cannot read the release notes from the source commit")
    notes = base64.b64decode(content["content"], validate=False).decode("utf-8")
    checksums = {}
    for asset in release.get("assets", []):
        name = asset.get("name", "")
        if name.endswith(".sha256"):
            expected = f"{RELEASE_ROOT}/download/{tag}/{name}"
            if asset.get("browser_download_url") != expected or not re.fullmatch(r"ArgusAI-[0-9.]+-[a-z0-9.-]+\.sha256", name):
                raise ValueError("Invalid checksum download URL")
            # Never forward the API token to an asset URL or its CDN redirect.
            checksums[name] = _read_url(expected, limit=512)
    return build_manifest(release, checksums, resolved_commit=ref["sha"],
                          expected_commit=commit, committed_notes=notes)


def _json(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def _atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".feed-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _article(manifest: dict, latest: bool) -> str:
    version = manifest["version"]
    date = datetime.fromisoformat(manifest["published_at"].replace("Z", "+00:00")).astimezone(timezone(timedelta(hours=8))).date()
    lines = [line.strip() for line in manifest["notes"].splitlines() if line.strip() and not line.startswith("#")]
    items = "".join("<li>" + html.escape(line.removeprefix("- ")) + "</li>" for line in lines)
    badge = '<span class="badge">最新记录</span>' if latest else ""
    return (f'<article class="release" id="v{version.replace(".", "-")}">'
            f'<div class="release-meta"><time datetime="{date}">{str(date).replace("-", ".")}</time>{badge}</div>'
            f'<div><p class="release-version">v{version} <span>更新</span></p>'
            f'<h2>ArgusAI v{version}</h2><ul class="release-notes">{items}</ul>'
            f'<a class="text-link" href="{html.escape(manifest["release_url"], quote=True)}">发布说明与下载 ↗</a></div></article>')


def write_feed(website: Path, manifest: dict) -> list[Path]:
    """Preserve per-version metadata and reject accidental stable downgrades."""
    version_tuple(manifest["version"])
    root = website / "updates"
    stable = root / "stable.json"
    if stable.exists():
        previous = json.loads(stable.read_text(encoding="utf-8"))
        if version_tuple(previous["version"]) > version_tuple(manifest["version"]):
            raise ValueError("Refusing to downgrade the stable update feed")
    archive = root / "releases" / (manifest["version"] + ".json")
    if archive.exists() and json.loads(archive.read_text(encoding="utf-8")) != manifest:
        raise ValueError("Published version metadata is immutable; publish a new version")
    changelog = website / "changelog.html"
    page = changelog.read_text(encoding="utf-8")
    if page.count(BEGIN) != 1 or page.count(END) != 1 or page.index(BEGIN) >= page.index(END):
        raise ValueError("Changelog is missing its generated release markers")
    history = {entry.stem: json.loads(entry.read_text(encoding="utf-8"))
               for entry in (root / "releases").glob("*.json")}
    history[manifest["version"]] = manifest
    ordered = sorted(history.values(), key=lambda item: version_tuple(item["version"]), reverse=True)
    rendered = "\n".join(_article(item, i == 0) for i, item in enumerate(ordered))
    before, rest = page.split(BEGIN, 1)
    _, after = rest.split(END, 1)
    page = before + BEGIN + "\n" + rendered + "\n" + END + after
    # Commit the client-visible pointer last, after both the archive and website exist.
    changed = []
    for path, text in ((archive, _json(manifest)), (changelog, page), (stable, _json(manifest))):
        if not path.exists() or path.read_text(encoding="utf-8") != text:
            _atomic_text(path, text)
            changed.append(path)
    return changed


def verify_live(manifest: dict, *, attempts: int = 24, interval: int = 15) -> None:
    for attempt in range(attempts):
        try:
            live = json.loads(_read_url("https://argusai.sh/updates/stable.json", limit=256 * 1024))
            if live == manifest:
                return
            if version_tuple(live["version"]) > version_tuple(manifest["version"]):
                return  # A later verified release won the deployment race.
        except (OSError, ValueError, KeyError):
            pass
        if attempt + 1 < attempts:
            time.sleep(interval)
    raise ValueError("Release is published, but the verified update feed is not live on argusai.sh")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--website", type=Path, default=Path("website"))
    parser.add_argument("--verify-live", action="store_true")
    args = parser.parse_args()
    manifest = fetch_verified_release(args.tag, args.commit, os.environ.get("GH_TOKEN", ""))
    if args.verify_live:
        verify_live(manifest)
        print(f"Verified live update feed for v{manifest['version']}")
    else:
        changed = write_feed(args.website, manifest)
        print(f"Verified v{manifest['version']}; updated {len(changed)} static files")


if __name__ == "__main__":
    main()
