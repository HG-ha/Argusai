from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('wiki_docs', Path(__file__).resolve().parents[2] / 'scripts/wiki_docs.py')
wiki_docs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wiki_docs)


@pytest.fixture
def source(tmp_path):
    root = tmp_path / 'repo'
    for name, content in {
        'README.md': '# Project\n\n[Docs](wiki/Home.md)\n',
        'README.en.md': '# Project\n\n[Docs](wiki/Home.md)\n',
        'assets/brand/README.md': '# Brand\n',
        'wiki/Home.md': '# Home\n\n[Start](Quick-Start.md#first-task)\n',
        'wiki/Quick-Start.md': '# Quick start\n\n## First task\n',
        'wiki/_Sidebar.md': '[Home](Home.md)\n[Start](Quick-Start.md)\n',
        'wiki/_Footer.md': '[Home](Home.md)\n',
        'sample.py': 'print("sample")\n',
        'assets/brand/icon.svg': '<svg xmlns="http://www.w3.org/2000/svg"/>',
    }.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8')
    return root


def test_check_validates_files_and_heading_anchors(source):
    errors, count = wiki_docs.check_documents(source)
    assert errors == []
    assert count == 6


@pytest.mark.parametrize('link, message', [
    ('Missing.md', 'missing local target'),
    ('Quick-Start.md#absent', 'missing heading anchor'),
    ('../../outside.md', 'escapes repository'),
])
def test_check_reports_broken_links_with_source_location(source, link, message):
    (source / 'wiki/Home.md').write_text(f'# Home\n\n[Bad]({link})\n')
    errors, _ = wiki_docs.check_documents(source)
    assert any('wiki/Home.md:3:' in error and message in error for error in errors)


def test_instructional_code_is_not_checked_as_links(source):
    with (source / 'wiki/Home.md').open('a') as file:
        file.write('\n`[sample](not-a-file.md)`\n\n```md\n[example](also-not-a-file.md)\n```\n')
    assert wiki_docs.check_documents(source)[0] == []


def test_unclosed_fence_and_unlisted_page_are_reported(source):
    (source / 'wiki/Home.md').write_text('# Home\n\n```sh\nunclosed\n')
    (source / 'wiki/Unlisted.md').write_text('# Orphaned page\n')
    errors, _ = wiki_docs.check_documents(source)
    assert any('unclosed fenced code block' in error for error in errors)
    assert any('absent from navigation: Unlisted.md' in error for error in errors)


def test_export_rewrites_pages_and_source_links_but_preserves_examples(source, tmp_path):
    home = source / 'wiki/Home.md'
    with home.open('a', encoding='utf-8') as file:
        file.write('\n[Source](../sample.py)\n![Icon](../assets/brand/icon.svg)\n')
        file.write('[Directory](../assets/brand/)\n')
        file.write('<a href="Quick-Start.md">Start</a>\n')
        file.write('`[sample](Quick-Start.md)`\n\n```md\n[example](Quick-Start.md)\n```\n')
    before = home.read_bytes()
    output = tmp_path / 'export'
    count = wiki_docs.export_wiki(output, root=source, ref='v0.1.0')
    assert count == 4
    exported = (output / 'Home.md').read_text(encoding='utf-8')
    assert '[Start](Quick-Start#first-task)' in exported
    assert '[Source](https://github.com/HG-ha/Argusai/blob/v0.1.0/sample.py)' in exported
    assert '[Directory](https://github.com/HG-ha/Argusai/tree/v0.1.0/assets/brand)' in exported
    assert 'https://raw.githubusercontent.com/HG-ha/Argusai/v0.1.0/assets/brand/icon.svg' in exported
    assert '<a href="Quick-Start">' in exported
    assert '`[sample](Quick-Start.md)`' in exported
    assert '```md\n[example](Quick-Start.md)\n```' in exported
    assert home.read_bytes() == before


def test_export_never_overwrites_existing_directory(source, tmp_path):
    output = tmp_path / 'existing-wiki'
    output.mkdir()
    sentinel = output / 'manual.md'
    sentinel.write_text('Unpublished manual changes')
    with pytest.raises(ValueError, match='already exists'):
        wiki_docs.export_wiki(output, root=source)
    assert sentinel.read_text() == 'Unpublished manual changes'


def test_export_refuses_source_subdirectory(source):
    with pytest.raises(ValueError, match='inside the wiki source'):
        wiki_docs.export_wiki(source / 'wiki/export', root=source)
    assert not (source / 'wiki/export').exists()
