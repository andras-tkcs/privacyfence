"""Connector abstraction: the unit of extension for new data sources.

Adding a new service (Salesforce, Calendar, …) means:
  1. Create src/loopline/connectors/<name>.py implementing Connector.
  2. Register it in daemon_main.py.
  3. No changes needed to the bridge or the IPC layer.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolParam:
    """Describes one parameter of a connector tool."""

    name: str
    annotation: str  # Python type name: "str", "int", "bool", "float"
    required: bool = True
    default: Any = None
    description: str = ""


@dataclass
class ToolSpec:
    """Full description of one tool exposed by a connector."""

    name: str
    description: str
    params: list[ToolParam] = field(default_factory=list)
    read_only: bool = False

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "params": [
                {
                    "name": p.name,
                    "annotation": p.annotation,
                    "required": p.required,
                    "default": p.default,
                    "description": p.description,
                }
                for p in self.params
            ],
            "read_only": self.read_only,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ToolSpec":
        return cls(
            name=d["name"],
            description=d["description"],
            params=[
                ToolParam(
                    name=p["name"],
                    annotation=p["annotation"],
                    required=p["required"],
                    default=p.get("default"),
                    description=p.get("description", ""),
                )
                for p in d.get("params", [])
            ],
            read_only=d.get("read_only", False),
        )


class Connector(ABC):
    """Base class for every data-source connector.

    Subclasses live in loopline/connectors/ and are registered in daemon_main.
    The bridge discovers them at startup via the IPC manifest call.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Short, unique identifier: "gmail", "drive", "slack", "salesforce", …"""

    @abstractmethod
    def tool_specs(self) -> list[ToolSpec]:
        """Return the tool definitions this connector exposes."""

    @abstractmethod
    async def call(self, tool: str, args: dict[str, Any]) -> Any:
        """Execute a tool call and return the result (possibly after user review)."""
