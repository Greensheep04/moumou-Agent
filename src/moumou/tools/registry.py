"""Tool registry.

The registry is the only place that knows the mapping name -> tool. It also
produces the function-calling schemas handed to the model.
"""

from __future__ import annotations

from moumou.contracts import Tool


def flatten_schema(schema: dict) -> dict:
    """Inline ``$defs``/``$ref`` so the schema survives function-calling APIs.

    Pydantic emits a ``$defs`` block for nested models; several providers reject
    or silently ignore references. Inlining is cheap and removes that risk.
    """
    defs = schema.get("$defs", {})

    def walk(node):
        if isinstance(node, dict):
            if "$ref" in node:
                ref = node["$ref"].rsplit("/", 1)[-1]
                target = dict(defs.get(ref, {}))
                target.update({k: v for k, v in node.items() if k != "$ref"})
                return walk(target)
            return {k: walk(v) for k, v in node.items() if k != "$defs"}
        if isinstance(node, list):
            return [walk(x) for x in node]
        return node

    return walk(schema)


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool name: {tool.name}")
        self._tools[tool.name] = tool

    def register_all(self, *tools: Tool) -> None:
        for t in tools:
            self.register(t)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def names(self) -> set[str]:
        return set(self._tools)

    def tool_specs(self) -> list[dict]:
        """OpenAI-compatible function definitions."""
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": flatten_schema(t.params_schema),
                },
            }
            for t in self._tools.values()
        ]

    def subset(self, names: list[str]) -> list[dict]:
        """Schemas restricted to the tools a given role is allowed to use."""
        allowed = set(names)
        return [s for s in self.tool_specs() if s["function"]["name"] in allowed]
