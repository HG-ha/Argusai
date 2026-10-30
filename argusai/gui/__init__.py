"""pywebview-based desktop GUI. Task-based, multi-concurrent, i18n-ready.

The ``Api`` class is exposed to the WebView via pywebview as
``window.pywebview.api.*``. Implementation is split across one base class
and a handful of single-responsibility mixins; the file you're looking
at is just the assembly point.

External contract (must remain stable):

* Every method whose name appears in ``argusai/webui/src/api.js``.
* The ``settings_mgr`` attribute (read by ``argusai/core/_tools.py``).
* The ``_window`` attribute (written by :py:func:`argusai.gui.launch.main`
  right after the pywebview window is created).

The ``main()`` re-export below keeps the historical entry point
``argusai-gui = "argusai.gui:main"`` (see ``pyproject.toml``) working.
"""

from __future__ import annotations

from ._helpers import WEBUI_DIR, debug_log, resource_root
from .agent_runtimes import AgentRuntimesMixin
from .ai_endpoints import AIEndpointsMixin
from .assistant import AssistantMixin
from .base import _ApiBase
from .browser import BrowserMixin
from .channels import ChannelsMixin
from .files import FilesMixin
from .host_service import HostServiceMixin
from .knowledge import KnowledgeMixin
from .proxy import ProxyMixin
from .reverse import ReverseMixin
from .settings import SettingsMixin
from .teams import TeamsMixin
from .ui_context import UiContextMixin
from .user_todos import UserTodosMixin
from .tasks import TasksMixin
from .window import WindowMixin
from .updates import UpdatesMixin


class Api(
    # Mixin order ≈ "frontend feature panel" order: tasks first, AI assistant
    # last. Python's MRO is left-to-right depth-first, so an earlier mixin
    # would shadow a later one if they ever defined the same method — which
    # they don't, on purpose — see local architecture notes / Api mixin layout.
    TasksMixin,
    FilesMixin,
    ProxyMixin,
    SettingsMixin,
    UiContextMixin,
    UserTodosMixin,
    KnowledgeMixin,
    ReverseMixin,
    BrowserMixin,
    AIEndpointsMixin,
    AgentRuntimesMixin,
    ChannelsMixin,
    HostServiceMixin,
    WindowMixin,
    UpdatesMixin,
    TeamsMixin,
    AssistantMixin,
    _ApiBase,
):
    """The single object pywebview exposes to JavaScript as ``window.pywebview.api``.

    See module docstring for the external contract; see ``base.py`` for
    the construction logic; see each mixin for the method group it owns.
    """

    @classmethod
    def for_headless(cls) -> "Api":
        """Build an Api host without a desktop window (channels / CLI)."""
        return cls(host_kind="headless")

    @classmethod
    def for_cli(cls) -> "Api":
        """Build an Api host for interactive CLI chat / doctor helpers."""
        return cls(host_kind="cli")


def main() -> None:
    """Bootstrap the GUI. Lazy-imported to keep ``Api`` importable in tests."""
    from .launch import main as _main

    _main()


__all__ = [
    "Api",
    "WEBUI_DIR",
    "debug_log",
    "main",
    "resource_root",
]
