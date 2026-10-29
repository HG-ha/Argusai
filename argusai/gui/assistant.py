"""Assistant session lifecycle + streaming bridge methods."""

from __future__ import annotations

from .assistant_cron import AssistantCronMixin
from .assistant_explain import AssistantExplainMixin
from .assistant_run import AssistantRunMixin
from .assistant_sessions import AssistantSessionsMixin
from .assistant_stream import AssistantStreamMixin
from .assistant_tools import AssistantToolsMixin


class AssistantMixin(
    AssistantStreamMixin,
    AssistantExplainMixin,
    AssistantRunMixin,
    AssistantToolsMixin,
    AssistantCronMixin,
    AssistantSessionsMixin,
):
    """Bridge methods for the AI assistant subsystem."""
