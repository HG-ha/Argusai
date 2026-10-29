"""Local assistant runtime registry and ACP integration."""

from .acp_runtime import ACPAgentProcess, ACPClientCallbacks, test_acp_runtime
from .acp_turn import (
    ACPTurnResult,
    ActiveACPTurn,
    acp_workspace_instruction,
    fetch_acp_config_options,
    is_synthetic_agent_chunk,
    run_acp_turn,
    strip_synthetic_agent_text,
)
from .catalog import BUILTIN_AGENT_RUNTIMES, NATIVE_RUNTIME_ID, builtin_agent_runtimes
from .config import (
    normalize_agent_runtimes,
    normalize_session_runtime_binding,
    preserve_session_project_dir,
    runtime_configs,
)
from .discovery import discover_agent_runtimes, probe_agent_runtime, public_runtime_config
from .models import AgentRuntimeConfig, AgentRuntimeProbe, SessionRuntimeBinding
from .tool_broker import ArgusAIToolBroker

__all__ = [
    "AgentRuntimeConfig",
    "AgentRuntimeProbe",
    "ArgusAIToolBroker",
    "ACPAgentProcess",
    "ACPClientCallbacks",
    "ACPTurnResult",
    "ActiveACPTurn",
    "BUILTIN_AGENT_RUNTIMES",
    "NATIVE_RUNTIME_ID",
    "SessionRuntimeBinding",
    "builtin_agent_runtimes",
    "discover_agent_runtimes",
    "fetch_acp_config_options",
    "is_synthetic_agent_chunk",
    "acp_workspace_instruction",
    "normalize_agent_runtimes",
    "normalize_session_runtime_binding",
    "preserve_session_project_dir",
    "probe_agent_runtime",
    "public_runtime_config",
    "runtime_configs",
    "run_acp_turn",
    "strip_synthetic_agent_text",
    "test_acp_runtime",
]
