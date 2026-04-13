from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class StandardRequest:
    prompt: str
    response_model: str
    resolved_model: str
    surface: str
    requested_model: str | None = None
    content: str | None = None
    stream: bool = False
    tools: list[dict[str, Any]] = field(default_factory=list)
    tool_names: list[str] = field(default_factory=list)
    tool_name_registry: dict[str, str] = field(default_factory=dict)
    tool_enabled: bool = False
