from __future__ import annotations

from backend.adapter.standard_request import StandardRequest
from backend.core.config import resolve_model
from backend.services.prompt_builder import messages_to_prompt
from backend.toolcall.normalize import build_tool_name_registry


def build_chat_standard_request(req_data: dict, *, default_model: str, surface: str) -> StandardRequest:
    requested_model = req_data.get("model", default_model)
    prompt_result = messages_to_prompt(req_data)
    tools = prompt_result.tools
    tool_names = [tool_name for tool_name in (tool.get("name") for tool in tools) if isinstance(tool_name, str) and tool_name]
    return StandardRequest(
        prompt=prompt_result.prompt,
        response_model=requested_model,
        resolved_model=resolve_model(requested_model),
        surface=surface,
        stream=req_data.get("stream", False),
        tools=tools,
        tool_names=tool_names,
        tool_name_registry=build_tool_name_registry(tool_names),
        tool_enabled=prompt_result.tool_enabled,
    )
