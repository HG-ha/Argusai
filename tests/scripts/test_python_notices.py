from __future__ import annotations

import hashlib
import importlib.util
import json
from email.message import Message
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "python_notices", Path(__file__).resolve().parents[2] / "scripts/python_notices.py"
)
notices = importlib.util.module_from_spec(spec)
spec.loader.exec_module(notices)


class Distribution:
    def __init__(self, root, name="sample", version="1.0", files=None):
        self.root = root
        self.version = version
        self.files = files if files is not None else ["sample-1.0.dist-info/licenses/LICENSE"]
        self.metadata = Message()
        self.metadata["Name"] = name
        self.metadata["License"] = "MIT"

    def locate_file(self, path):
        return self.root / str(path)


@pytest.fixture
def environment(tmp_path, monkeypatch):
    site = tmp_path / "site"
    original = site / "sample-1.0.dist-info/licenses/LICENSE"
    original.parent.mkdir(parents=True)
    original.write_bytes(b"Copyright upstream\r\nComplete permission\r\nDisclaimer\xff\r\n")
    dist = Distribution(site)
    lock = tmp_path / "uv.lock"
    lock.write_text('''version = 1
[[package]]
name = "argusai"
source = { editable = "." }
dependencies = [{ name = "sample" }]
[[package]]
name = "sample"
version = "1.0"
source = { registry = "https://example.test/simple" }
''')
    fallback = tmp_path / "fallback.json"
    fallback.write_text('{"packages": {}, "binary_supplements": []}')
    python_license = tmp_path / "LICENSE_PYTHON.txt"
    python_license.write_bytes(b"Actual interpreter license\r\n")
    monkeypatch.setattr(notices, "LOCK_PATH", lock)
    monkeypatch.setattr(notices, "FALLBACK_MANIFEST", fallback)
    monkeypatch.setattr(notices.metadata, "distributions", lambda: [dist])
    monkeypatch.setattr(notices, "_python_license", lambda: python_license)
    return tmp_path, dist, original, lock, fallback


def test_preserves_original_bytes_and_nested_playwright_notices(environment):
    root, dist, original, _, _ = environment
    nested = dist.root / "playwright/driver/package/ThirdPartyNotices.txt"
    nested.parent.mkdir(parents=True)
    nested.write_bytes(b"Nested original notice\r\n")
    dist.files.append(nested.relative_to(dist.root).as_posix())
    output = root / "out"
    assert notices.generate_notices(output) == output / "THIRD_PARTY_NOTICES.txt"
    assert (output / "packages/sample/1.0/sample-1.0.dist-info/licenses/LICENSE").read_bytes() == original.read_bytes()
    assert (output / "packages/sample/1.0/playwright/driver/package/ThirdPartyNotices.txt").read_bytes() == nested.read_bytes()
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["packages"][0]["roles"] == ["runtime"]
    assert (output / manifest["python"]["document"]).read_bytes() == b"Actual interpreter license\r\n"
    notices.check_notices(output)


def test_version_drift_is_rejected_before_output(environment):
    root, dist, *_ = environment
    dist.version = "2.0"
    with pytest.raises(ValueError, match="does not match uv.lock"):
        notices.generate_notices(root / "out")
    assert not (root / "out").exists()


def test_pinned_fallback_exact_bytes_and_version_required(environment):
    root, dist, original, lock, fallback = environment
    dist.files = []
    raw = original.read_bytes()
    (root / "verified.txt").write_bytes(raw)
    record = {"kind": "upstream-license", "version": "1.0", "source": "https://example.test/source.tar.gz", "file": "verified.txt", "sha256": hashlib.sha256(raw).hexdigest()}
    fallback.write_text(json.dumps({"packages": {"sample": record}}))
    output = root / "out"
    notices.generate_notices(output)
    assert (output / "packages/sample/1.0/reviewed-upstream/verified.txt").read_bytes() == raw
    record["version"] = "0.9"
    fallback.write_text(json.dumps({"packages": {"sample": record}}))
    with pytest.raises(ValueError, match="no version-matched"):
        notices.generate_notices(root / "other")
    record["version"] = "1.0"
    record["sha256"] = "0" * 64
    fallback.write_text(json.dumps({"packages": {"sample": record}}))
    with pytest.raises(ValueError, match="checksum mismatch"):
        notices.generate_notices(root / "other")


@pytest.fixture
def reviewed_documents(environment):
    root, dist, _, lock, fallback = environment
    dist.files = []
    raw_files = {
        "COPYING.txt": b"Dual license summary\r\n",
        "COPYING-LGPL.txt": b"Complete LGPL original\r\n\xff",
        "COPYING-MPL.txt": b"Complete MPL original\n",
    }
    source = "https://example.test/sample-1.0.tar.gz"
    archive_sha = "a" * 64
    record = {
        "kind": "upstream-license", "version": "1.0", "source": source,
        "archive_sha256": archive_sha,
        "documents": [
            {"file": name, "sha256": hashlib.sha256(raw).hexdigest(),
             "archive_member": f"sample-1.0/{name}"}
            for name, raw in raw_files.items()
        ],
    }
    for name, raw in raw_files.items():
        (root / name).write_bytes(raw)
    lock.write_text(lock.read_text() + f'sdist = {{ url = "{source}", hash = "sha256:{archive_sha}" }}\n')
    fallback.write_text(json.dumps({"packages": {"sample": record}}))
    return environment, record, raw_files


@pytest.mark.parametrize("installed_summary", [False, True])
def test_complete_reviewed_set_is_preserved_even_when_record_has_summary(reviewed_documents, installed_summary):
    environment, record, raw_files = reviewed_documents
    root, dist, original, _, _ = environment
    if installed_summary:
        dist.files = [original.relative_to(dist.root).as_posix()]
    output = root / "out"
    notices.generate_notices(output)
    for name, raw in raw_files.items():
        assert (output / "packages/sample/1.0/reviewed-upstream" / name).read_bytes() == raw
    package = json.loads((output / "manifest.json").read_text())["packages"][0]
    assert package["reviewed_fallback"] == record
    assert not package["warnings"]
    for document in record["documents"]:
        entry = next(d for d in package["documents"] if d["path"].endswith("reviewed-upstream/" + document["file"]))
        assert entry["source"] == record["source"] + "#" + document["archive_member"]
    notices.check_notices(output)


@pytest.mark.parametrize("index", [0, 1, 2])
def test_each_reviewed_document_checksum_is_required_before_writing(reviewed_documents, index):
    environment, record, _ = reviewed_documents
    root, _, _, _, _ = environment
    target = root / record["documents"][index]["file"]
    target.write_bytes(target.read_bytes() + b"modified")
    with pytest.raises(ValueError, match="checksum mismatch"):
        notices.generate_notices(root / "out")
    assert not (root / "out").exists()


@pytest.mark.parametrize("invalid", [[], {}, None])
def test_reviewed_document_set_cannot_disable_missing_license_guard(reviewed_documents, invalid):
    environment, record, _ = reviewed_documents
    root, _, _, _, fallback = environment
    record["documents"] = invalid
    fallback.write_text(json.dumps({"packages": {"sample": record}}))
    with pytest.raises(ValueError, match="nonempty list"):
        notices.generate_notices(root / "out")
    assert not (root / "out").exists()


@pytest.mark.parametrize("field,value", [("archive_sha256", "b" * 64), ("source", "https://example.test/other.tar.gz")])
def test_reviewed_archive_must_match_locked_source(reviewed_documents, field, value):
    environment, record, _ = reviewed_documents
    root, _, _, _, fallback = environment
    record[field] = value
    fallback.write_text(json.dumps({"packages": {"sample": record}}))
    with pytest.raises(ValueError, match="archive does not match uv.lock"):
        notices.generate_notices(root / "out")
    assert not (root / "out").exists()


@pytest.mark.parametrize("form", ["single", "documents"])
@pytest.mark.parametrize("missing", ["archive_hash", "sdist", "sdist_url", "sdist_hash"])
def test_archive_fallback_requires_complete_locked_provenance(reviewed_documents, form, missing):
    environment, record, _ = reviewed_documents
    root, _, _, lock, fallback = environment
    if form == "single":
        original = record.pop("documents")[0]
        record.update(original)
    if missing == "archive_hash":
        del record["archive_sha256"]
    else:
        lines = [line for line in lock.read_text().splitlines() if not line.startswith("sdist =")]
        if missing == "sdist_url":
            lines.append('sdist = { hash = "sha256:' + record["archive_sha256"] + '" }')
        elif missing == "sdist_hash":
            lines.append('sdist = { url = "' + record["source"] + '" }')
        lock.write_text("\n".join(lines) + "\n")
    fallback.write_text(json.dumps({"packages": {"sample": record}}))
    with pytest.raises(ValueError, match="requires archive_sha256 and complete uv.lock sdist"):
        notices.generate_notices(root / "out")
    assert not (root / "out").exists()


def test_installed_summary_cannot_hide_outdated_reviewed_document_set(reviewed_documents):
    environment, record, _ = reviewed_documents
    root, dist, original, _, fallback = environment
    dist.files = [original.relative_to(dist.root).as_posix()]
    record["version"] = "0.9"
    fallback.write_text(json.dumps({"packages": {"sample": record}}))
    with pytest.raises(ValueError, match="no version-matched reviewed fallback"):
        notices.generate_notices(root / "out")
    assert not (root / "out").exists()


@pytest.mark.parametrize("package,expected_count", [("pycairo", 3), ("pygobject", 1)])
def test_locked_linux_sdist_fallbacks_cover_wheels_without_license_files(environment, monkeypatch, package, expected_count):
    root, dist, _, _, _ = environment
    project = Path(notices.__file__).resolve().parents[1]
    manifest = project / "scripts/packaging/python-licenses.json"
    reviewed = json.loads(manifest.read_text(encoding="utf-8"))["packages"][package]
    dist.metadata.replace_header("Name", package)
    dist.version = reviewed["version"]
    dist.files = []
    monkeypatch.setattr(notices, "FALLBACK_MANIFEST", manifest)
    monkeypatch.setattr(notices, "LOCK_PATH", project / "uv.lock")
    output = root / "out"
    notices.generate_notices(output)
    originals = reviewed.get("documents", [reviewed])
    assert len(originals) == expected_count
    for document in originals:
        target = output / f"packages/{package}/{dist.version}/reviewed-upstream" / document["file"]
        assert target.read_bytes() == (manifest.parent / document["file"]).read_bytes()
        assert hashlib.sha256(target.read_bytes()).hexdigest() == document["sha256"]
    report = json.loads((output / "manifest.json").read_text())
    assert not report["packages"][0]["warnings"]
    assert report["packages"][0]["reviewed_fallback"] == reviewed
    notices.check_notices(output)


@pytest.mark.parametrize("path", ["../LICENSE", "/LICENSE", "C:/LICENSE", "sample/../../NOTICE", "sample\\LICENSE"])
def test_record_document_path_escape_rejected(environment, path):
    root, dist, *_ = environment
    dist.files = [path]
    with pytest.raises(ValueError, match="Unsafe document path"):
        notices.generate_notices(root / "out")
    assert not (root / "out").exists()


def test_output_traversal_and_unowned_directory_rejected(environment):
    root, *_ = environment
    with pytest.raises(ValueError, match="Unsafe output"):
        notices.generate_notices(root / "out" / ".." / "outside")
    output = root / "out"
    output.mkdir()
    (output / "keep.txt").write_bytes(b"Keep original")
    with pytest.raises(ValueError, match="unowned nonempty"):
        notices.generate_notices(output)
    assert (output / "keep.txt").read_bytes() == b"Keep original"


def test_output_symlink_cannot_escape(environment):
    root, *_ = environment
    outside = root / "outside"
    outside.mkdir()
    link = root / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("OS does not allow creating symlinks")
    with pytest.raises(ValueError, match="Unsafe output"):
        notices.generate_notices(link / "out")
    assert not (outside / "out").exists()


def test_stale_output_source_and_lock_changes_detected_read_only(environment):
    root, _, original, lock, _ = environment
    output = root / "out"
    with pytest.raises(ValueError, match="missing, stale or modified"):
        notices.check_notices(output)
    assert not output.exists()
    notices.generate_notices(output)
    index = (output / notices.OUTPUT_NAME).read_bytes()
    original.write_bytes(original.read_bytes() + b"changed")
    with pytest.raises(ValueError, match="missing, stale or modified"):
        notices.check_notices(output)
    assert (output / notices.OUTPUT_NAME).read_bytes() == index
    notices.generate_notices(output)
    lock.write_bytes(lock.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="missing, stale or modified"):
        notices.check_notices(output)
    notices.generate_notices(output)
    (output / "unexpected.txt").write_bytes(b"old package")
    with pytest.raises(ValueError, match="Stale extra"):
        notices.generate_notices(output)


def test_reviewed_missing_text_warns_but_unknown_version_is_not_silent(environment):
    root, dist, _, _, fallback = environment
    dist.files = []
    with pytest.raises(ValueError, match="Missing original license"):
        notices.generate_notices(root / "out")
    fallback.write_text(json.dumps({"packages": {"sample": {"kind": "missing-original", "version": "1.0", "source": "https://example.test/", "limitation": "Original not found in reviewed source."}}}))
    with pytest.warns(notices.NoticeReviewWarning, match="ORIGINAL LICENSE TEXT MISSING"):
        notices.generate_notices(root / "out")
    assert "WARNING:" in (root / "out" / notices.OUTPUT_NAME).read_text()
    assert not list((root / "out").rglob("LICENSE"))


def test_binary_supplements_only_apply_to_matching_sha(environment):
    root, dist, _, _, fallback = environment
    binary = dist.root / "loader.dll"
    binary.write_bytes(b"verified binary")
    doc = root / "binary-license.txt"
    doc.write_bytes(b"Actual binary component license")
    fallback.write_text(json.dumps({"packages": {}, "binary_supplements": [{"package": "sample", "component": "fixture-component", "version": "3.0", "source": "https://example.test/official", "binaries": {"loader.dll": hashlib.sha256(binary.read_bytes()).hexdigest()}, "documents": [{"file": doc.name, "sha256": hashlib.sha256(doc.read_bytes()).hexdigest()}]}]}))
    notices.generate_notices(root / "good")
    assert (root / "good/packages/sample/1.0/reviewed-binary/binary-license.txt").read_bytes() == doc.read_bytes()
    binary.write_bytes(b"unreviewed version")
    with pytest.warns(notices.NoticeReviewWarning, match="NOT applied"):
        notices.generate_notices(root / "other")
    assert not (root / "other/packages/sample/1.0/reviewed-binary").exists()
