"""Static frontend contracts for long history and tab context editing."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_long_history_load_uses_a_realistic_local_bridge_timeout() -> None:
    panel = (ROOT / "argusai/webui/src/components/AssistantPanel.vue").read_text(encoding="utf-8")

    assert "const HISTORY_LOAD_TIMEOUT_MS = 30_000" in panel
    assert "api.getAssistantSession(tab.sessionId), HISTORY_LOAD_TIMEOUT_MS" in panel


def test_agent_tabs_expose_the_context_editor_on_right_click() -> None:
    panel = (ROOT / "argusai/webui/src/components/AssistantPanel.vue").read_text(encoding="utf-8")
    api = (ROOT / "argusai/webui/src/api.js").read_text(encoding="utf-8")

    assert '@contextmenu.prevent.stop="openTabContextMenu($event, tab)"' in panel
    assert "id: 'edit-context'" in panel
    assert "id: 'import-context'" in panel
    assert "id: 'export-context'" in panel
    assert 'api.updateAssistantSession(id, { context_notes: notes })' in panel
    assert "api.importAssistantContext(id)" in panel
    assert "api.exportAssistantContext(id)" in panel
    assert 'call("import_assistant_context"' in api
    assert 'call("export_assistant_context"' in api
    assert ':open="contextEditorOpen"' in panel
