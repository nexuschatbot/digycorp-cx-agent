"""Agent loop (AD-H): a LangGraph StateGraph over the existing tool REGISTRY.

Public surface:
- build_graph(checkpointer)       — compile the agent <-> tools graph
- build_config(db, session_id)    — the RunnableConfig carrying trusted context
- new_sqlite_checkpointer(path)   — SqliteSaver, thread_id = session_id
- SYSTEM_PROMPT                    — minimal Stage-1 prompt (rules 1/2/4/9)

The tool node (audit choke point + trusted-context injection + correlation id)
lives in app/agent/tool_node.
"""

from __future__ import annotations

from app.agent.graph import (
    AGENT_DEADLINE_SECONDS,
    FALLBACK_TEXT,
    MAX_ITERATIONS,
    SYSTEM_PROMPT,
    AgentState,
    StreamAborted,
    build_config,
    build_graph,
    new_sqlite_checkpointer,
)

__all__ = [
    "AgentState",
    "build_graph",
    "build_config",
    "new_sqlite_checkpointer",
    "StreamAborted",
    "SYSTEM_PROMPT",
    "MAX_ITERATIONS",
    "AGENT_DEADLINE_SECONDS",
    "FALLBACK_TEXT",
]
