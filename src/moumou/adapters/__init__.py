"""Reference adapters for the four ports.

These are deliberately thin: their job is to let the framework run end to end
before the three teammates deliver their real implementations.
"""

from .bus import InMemoryBus
from .human import AutoApproveHuman, ScriptedHuman
from .llm import DeepSeekClient, FakeLLM, LLMClient, LLMResponse, ToolCall
from .workspace import LocalDirs

__all__ = [
    "AutoApproveHuman",
    "DeepSeekClient",
    "FakeLLM",
    "InMemoryBus",
    "LLMClient",
    "LLMResponse",
    "LocalDirs",
    "ScriptedHuman",
    "ToolCall",
]
