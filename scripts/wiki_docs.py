#!/usr/bin/env python3
"""Check public documentation or export the wiki for a separate GitHub Wiki checkout.

This tool never commits, pushes, deletes pages or contacts the network.
"""
from __future__ import annotations

import argparse
import re
import sys
from html import unescape
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "https://github.com/HG-ha/Argusai"
LINK = re.compile(r'(!?\[[^\]\n]*\]\(\s*)(<[^>]+>|[^\s)]+)(\s*(?:["\'][^\n]*?["\']\s*)?\))')
ATTRIBUTE = re.compile(r'\b(href|src)\s*=\s*(["\'])(.*?)\2', re.IGNORECASE)
INLINE_CODE = re.compile(r'(`+).*?\1')
FENCE = re.compile(r'^\s{0,3}(`{3,}|~{3,})(.*)$')


def prose_lines(text: str):
    """Yield line numbers and prose only; report an unclosed code fence."""
    fence = ""
    size = 0
    for number, line in enumerate(text.splitlines(keepends=True), 1):
        match = FENCE.match(line)
        if match:
            marker, rest = match.groups()
            if not fence:
                fence, size = marker[0], len(marker)
            elif marker[0] == fence and len(marker) >= size and not rest.strip():
                fence = ""
            continue
        if not fence:
            yield number, line
    if fence:
        raise ValueError("unclosed fenced code block")


def anchors(text: str) -> set[str]:
    found = set(re.findall(r'\b(?:id|name)=["\']([^"\']+)["\']', text))
    counts: dict[str, int] = {}
    for _, line in prose_lines(text):
        match = re.match(r'^ {0,3}#{1,6}\s+(.+?)\s*#*\s*$', line)
        if not match:
            continue
        title = re.sub(r'<[^>]*>', '', match[1])
        title = re.sub(r'!?\[([^\]]+)\]\([^)]*\)', r'\1', title)
        title = unescape(title).lower().replace('`', '')
        slug = re.sub(r'[^\w\- ]', '', title).replace(' ', '-')
        count = counts.get(slug, 0)
        counts[slug] = count + 1
        found.add(f"{slug}-{count}" if count else slug)
    return found


def local_target(source: Path, destination: str, root: Path) -> tuple[Path, str] | None:
    value = unescape(destination.strip('<>'))
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc:
        return None
    path = unquote(parsed.path)
    target = (root / path.lstrip('/') if path.startswith('/') else source.parent / path) if path else source
    target = target.resolve()
    if not target.is_relative_to(root.resolve()):
        raise ValueError(f"link escapes repository: {destination}")
    return target, unquote(parsed.fragment)


def destinations(line: str):
    line = INLINE_CODE.sub('', line)
    for match in LINK.finditer(line):
        yield match[2]
    for match in ATTRIBUTE.finditer(line):
        yield match[3]
    reference = re.match(r'^\s{0,3}\[[^\]]+\]:\s*(<[^>]+>|\S+)', line)
    if reference:
        yield reference[1]


def documentation_files(root: Path) -> list[Path]:
    files = [root / name for name in ('README.md', 'README.en.md', 'assets/brand/README.md')]
    if (root / 'THIRD_PARTY_NOTICES.md').is_file():
        files.append(root / 'THIRD_PARTY_NOTICES.md')
    return files


def check_documents(root: Path = ROOT) -> tuple[list[str], int]:
    files = documentation_files(root) + sorted((root / 'wiki').glob('*.md'))
    errors: list[str] = []
    count = 0
    anchor_cache: dict[Path, set[str]] = {}
    if not (root / 'wiki/Home.md').is_file():
        errors.append('wiki/Home.md: missing documentation entry point')
    for source in files:
        if not source.is_file():
            errors.append(f'{source.relative_to(root).as_posix()}: missing document')
            continue
        text = source.read_text(encoding='utf-8')
        text = re.sub(r'<!--[\s\S]*?-->', '', text)
        try:
            for number, line in prose_lines(text):
                for destination in destinations(line):
                    try:
                        resolved = local_target(source, destination, root)
                        if resolved is None:
                            continue
                        target, fragment = resolved
                        count += 1
                        if not target.exists():
                            raise ValueError(f'missing local target: {destination}')
                        if fragment and target.suffix == '.md':
                            if target not in anchor_cache:
                                anchor_cache[target] = anchors(target.read_text(encoding='utf-8'))
                            if fragment not in anchor_cache[target]:
                                raise ValueError(f'missing heading anchor: {destination}')
                    except ValueError as exc:
                        errors.append(f'{source.relative_to(root).as_posix()}:{number}: {exc}')
        except ValueError as exc:
            errors.append(f'{source.relative_to(root).as_posix()}: {exc}')
    sidebar = root / 'wiki/_Sidebar.md'
    if sidebar.is_file():
        linked: set[Path] = set()
        try:
            for _, line in prose_lines(sidebar.read_text(encoding='utf-8')):
                for link in destinations(line):
                    try:
                        resolved = local_target(sidebar, link, root)
                    except ValueError:
                        continue  # Already reported with a source location above.
                    if resolved is not None:
                        linked.add(resolved[0])
        except ValueError:
            pass  # The main document loop reports unclosed sidebar fences.
        for page in (root / 'wiki').glob('*.md'):
            if not page.name.startswith('_') and page.resolve() not in linked:
                errors.append(f'wiki/_Sidebar.md: page absent from navigation: {page.name}')
    else:
        errors.append('wiki/_Sidebar.md: missing sidebar')
    return errors, count


def wiki_destination(source: Path, destination: str, root: Path, ref: str, *, image: bool = False) -> str:
    resolved = local_target(source, destination, root)
    if resolved is None:
        return destination
    target, fragment = resolved
    wiki = (root / 'wiki').resolve()
    suffix = '#' + quote(fragment, safe='-_') if fragment else ''
    if target.parent == wiki and target.suffix == '.md':
        return quote(target.stem, safe='-_') + suffix
    relative = quote(target.relative_to(root.resolve()).as_posix(), safe='/')
    revision = quote(ref, safe='')
    if image:
        return f'https://raw.githubusercontent.com/HG-ha/Argusai/{revision}/{relative}{suffix}'
    view = 'tree' if target.is_dir() else 'blob'
    return f'{REPOSITORY}/{view}/{revision}/{relative}{suffix}'


def export_page(source: Path, root: Path, ref: str) -> str:
    text = source.read_text(encoding='utf-8')
    active_lines = {number for number, _ in prose_lines(text)}
    result = []
    for number, line in enumerate(text.splitlines(keepends=True), 1):
        if number not in active_lines:
            result.append(line)
            continue
        # Inline code is instructional text, not a link to rewrite.
        pieces = re.split(r'(`+[^`]*`+)', line)
        for index in range(0, len(pieces), 2):
            def markdown_link(match):
                url = wiki_destination(source, match[2], root, ref, image=match[1].startswith('!'))
                return match[1] + url + match[3]
            value = LINK.sub(markdown_link, pieces[index])
            def html_link(match):
                url = wiki_destination(source, match[3], root, ref, image=match[1].lower() == 'src')
                return match[1] + '=' + match[2] + url + match[2]
            value = ATTRIBUTE.sub(html_link, value)
            # Markdown reference destinations use the same page conversion.
            value = re.sub(r'^(\s{0,3}\[[^\]]+\]:\s*)(<[^>]+>|\S+)',
                           lambda m: m[1] + wiki_destination(source, m[2], root, ref), value)
            pieces[index] = value
        result.append(''.join(pieces))
    return ''.join(result)


def export_wiki(output: Path, *, root: Path = ROOT, ref: str = 'main') -> int:
    errors, _ = check_documents(root)
    if errors:
        raise ValueError('Documentation check failed:\n' + '\n'.join(errors))
    if not ref.strip() or any(character in ref for character in '\r\n'):
        raise ValueError('Invalid source ref')
    output = output.resolve()
    if output.exists():
        raise ValueError(f'Export directory already exists: {output}')
    if output.is_relative_to((root / 'wiki').resolve()):
        raise ValueError('Export must not be written inside the wiki source directory')
    output.mkdir(parents=True, exist_ok=False)
    pages = sorted((root / 'wiki').glob('*.md'))
    for page in pages:
        (output / page.name).write_text(export_page(page, root, ref), encoding='utf-8', newline='\n')
    return len(pages)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('check', help='Check local links, heading anchors, code fences and navigation')
    export = commands.add_parser('export', help='Export pages for a separate Wiki checkout; never push')
    export.add_argument('--output-dir', required=True, type=Path)
    export.add_argument('--ref', default='main', help='Source ref used for links to files in the main repository')
    args = parser.parse_args(argv)
    try:
        if args.command == 'check':
            errors, count = check_documents()
            if errors:
                print('\n'.join(errors), file=sys.stderr)
                return 1
            print(f'Documentation verified: {count} local links, anchors and image references')
        else:
            output = args.output_dir if args.output_dir.is_absolute() else ROOT / args.output_dir
            count = export_wiki(output, ref=args.ref)
            print(f'Exported {count} Wiki pages to {output}. No commit or push performed.')
    except (ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
