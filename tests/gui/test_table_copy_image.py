from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
VUE = REPO / "argusai" / "webui" / "src" / "components" / "assistant" / "MarkdownText.vue"
MINDMAP = REPO / "argusai" / "webui" / "src" / "components" / "assistant" / "MindmapView.vue"
HELPER = REPO / "argusai" / "webui" / "src" / "components" / "assistant" / "tableCopyImage.js"
I18N = REPO / "argusai" / "webui" / "src" / "i18nMessages.js"


def test_table_copy_image_is_wired():
    vue = VUE.read_text(encoding="utf-8")
    i18n = I18N.read_text(encoding="utf-8")
    assert "data-copy-table-image" in vue
    assert "copyTableAsImage" in vue
    assert "copyExpandedView" in vue
    assert "assistant.copyTableImage" in vue
    mindmap = MINDMAP.read_text(encoding="utf-8")
    assert "copyMindmapAsImage" in mindmap
    assert "copyAsImage" in mindmap
    for key in (
        "copyTableImage:",
        "tableCopiedImage:",
        "tableDownloadedImage:",
        "tableCopyImageFail:",
    ):
        assert i18n.count(key) >= 2


@pytest.mark.skipif(not shutil.which("node"), reason="node not available")
def test_wrap_table_text_breaks_ascii_and_cjk():
    script = f"""
import {{ wrapTableText }} from {HELPER.resolve().as_uri()!r};
const ctx = {{ measureText: (s) => ({{ width: [...String(s)].length * 10 }}) }};
const ascii = wrapTableText(ctx, 'abcdefghij', 25);
if (JSON.stringify(ascii) !== JSON.stringify(['ab', 'cd', 'ef', 'gh', 'ij'])) {{
  console.error(ascii);
  process.exit(1);
}}
const cjk = wrapTableText(ctx, '中文表格', 25);
if (JSON.stringify(cjk) !== JSON.stringify(['中文', '表格'])) {{
  console.error(cjk);
  process.exit(1);
}}
const lines = wrapTableText(ctx, 'ab\\ncd', 100);
if (JSON.stringify(lines) !== JSON.stringify(['ab', 'cd'])) {{
  console.error(lines);
  process.exit(1);
}}
"""
    subprocess.run(
        [shutil.which("node"), "--input-type=module", "-e", script],
        check=True,
        cwd=REPO,
    )
