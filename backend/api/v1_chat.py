from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
import json
import logging
import time
import uuid
from typing import Any, Awaitable, Callable
from backend.adapter.standard_request import StandardRequest
from backend.core.config import resolve_model, settings
from backend.core.request_logging import new_request_id, request_context, update_request_context
from backend.runtime.stream_presenter import openai_chunk, openai_done
from backend.runtime.execution import (
    build_tool_directive,
    cleanup_runtime_resources,
    collect_completion_run,
    evaluate_retry_directive,
    retryable_usage_delta,
)
from backend.services.attachment_preprocessor import preprocess_attachments
from backend.services.auth_quota import resolve_auth_context
from backend.services.completion_bridge import run_retryable_completion_bridge
from backend.services.openai_stream_translator import OpenAIStreamTranslator
from backend.services.prompt_builder import messages_to_prompt
from backend.services.response_formatters import build_openai_completion_payload
from backend.services.qwen_client import QwenClient
from backend.toolcall.normalize import build_tool_name_registry

log = logging.getLogger("qwen2api.chat")
router = APIRouter()
OpenAIDeltaHandler = Callable[[dict[str, Any], str | None, list[dict[str, Any]] | None], Awaitable[None]]


def _build_standard_request(req_data: dict) -> StandardRequest:
    requested_model = req_data.get("model", "gpt-3.5-turbo")
    prompt_result = messages_to_prompt(req_data)
    prompt = prompt_result.prompt
    tools = prompt_result.tools
    tool_names = [tool_name for tool_name in (tool.get("name") for tool in tools) if isinstance(tool_name, str) and tool_name]
    return StandardRequest(
        prompt=prompt,
        response_model=requested_model,
        resolved_model=resolve_model(requested_model),
        surface="openai",
        stream=req_data.get("stream", False),
        tools=tools,
        tool_names=tool_names,
        tool_name_registry=build_tool_name_registry(tool_names),
        tool_enabled=prompt_result.tool_enabled,
    )


@router.post("/chat/completions")
@router.post("/v1/chat/completions")
async def chat_completions(request: Request):
    app = request.app
    users_db = app.state.users_db
    client: QwenClient = app.state.qwen_client

    auth = await resolve_auth_context(request, users_db)
    token = auth.token

    try:
        req_data = await request.json()
    except Exception:
        raise HTTPException(400, {"error": {"message": "Invalid JSON body", "type": "invalid_request_error"}})

    standard_request = _build_standard_request(req_data)
    model_name = standard_request.response_model
    qwen_model = standard_request.resolved_model
    prompt = standard_request.prompt
    history_messages = req_data.get("messages", [])

    completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = int(time.time())

    with request_context(req_id=new_request_id(), surface="openai", requested_model=model_name, resolved_model=qwen_model):
        log.info(f"[OAI] model={qwen_model}, stream={standard_request.stream}, tool_enabled={standard_request.tool_enabled}, tools={[t.get('name') for t in standard_request.tools]}, prompt_len={len(prompt)}")

        if standard_request.stream:
            async def generate():
                current_prompt = prompt
                max_attempts = settings.MAX_RETRIES + (1 if standard_request.tools else 0)
                for stream_attempt in range(max_attempts):
                    try:
                        update_request_context(stream_attempt=stream_attempt + 1)
                        role_chunk_sent = False

                        async def on_delta(evt, text_chunk, _):
                            nonlocal role_chunk_sent
                            if not role_chunk_sent:
                                pending_chunks.append(openai_chunk(completion_id, created, model_name, {"role": "assistant"}))
                                role_chunk_sent = True

                            if text_chunk and evt.get("phase") in ("think", "thinking_summary"):
                                pending_chunks.append(
                                    openai_chunk(
                                        completion_id,
                                        created,
                                        model_name,
                                        {"reasoning_content": text_chunk},
                                    )
                                )
                            elif text_chunk and evt.get("phase") == "answer":
                                pending_chunks.append(
                                    openai_chunk(
                                        completion_id,
                                        created,
                                        model_name,
                                        {"content": text_chunk},
                                    )
                                )

                        pending_chunks: list[str] = []
                        delta_handler: OpenAIDeltaHandler = on_delta
                        execution = await collect_completion_run(
                            client,
                            standard_request,
                            current_prompt,
                            capture_events=False,
                            on_delta=delta_handler,
                        )

                        retry = evaluate_retry_directive(
                            request=standard_request,
                            current_prompt=current_prompt,
                            history_messages=history_messages,
                            attempt_index=stream_attempt,
                            max_attempts=max_attempts,
                            state=execution.state,
                            allow_after_visible_output=False,
                        )
                        if retry.retry:
                            await cleanup_runtime_resources(client, execution.acc, execution.chat_id)
                            current_prompt = retry.next_prompt
                            continue

                        directive = build_tool_directive(standard_request, execution.state)
                        if directive.stop_reason == "tool_use":
                            tool_blocks = [b for b in directive.tool_blocks if b.get("type") == "tool_use"]
                            for idx, block in enumerate(tool_blocks):
                                pending_chunks.append(
                                    openai_chunk(
                                        completion_id,
                                        created,
                                        model_name,
                                        {
                                            "tool_calls": [{
                                                "index": idx,
                                                "id": block["id"],
                                                "type": "function",
                                                "function": {
                                                    "name": block["name"],
                                                    "arguments": json.dumps(block.get("input", {}), ensure_ascii=False),
                                                },
                                            }]
                                        },
                                    )
                                )
                            final_finish_reason = "tool_calls"
                        else:
                            final_finish_reason = "stop"

                        for chunk in pending_chunks:
                            yield chunk
                        yield openai_chunk(completion_id, created, model_name, {}, final_finish_reason)
                        yield openai_done()

                        users = await users_db.get()
                        for u in users:
                            if u["id"] == token:
                                u["used_tokens"] += retryable_usage_delta(prompt)(execution.state, None)
                                break
                        await users_db.save(users)

                        await cleanup_runtime_resources(client, execution.acc, execution.chat_id)
                        return
                    except HTTPException as he:
                        yield f"data: {json.dumps({'error': he.detail})}\n\n"
                        return
                    except Exception as e:
                        yield f"data: {json.dumps({'error': str(e)})}\n\n"
                        return

            return StreamingResponse(
                generate(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        current_prompt = prompt
        max_attempts = settings.MAX_RETRIES + (1 if standard_request.tools else 0)
        for stream_attempt in range(max_attempts):
            try:
                update_request_context(stream_attempt=stream_attempt + 1)
                execution = await collect_completion_run(client, standard_request, current_prompt)
                retry = evaluate_retry_directive(
                    request=standard_request,
                    current_prompt=current_prompt,
                    history_messages=history_messages,
                    attempt_index=stream_attempt,
                    max_attempts=max_attempts,
                    state=execution.state,
                    allow_after_visible_output=False,
                )
                if retry.retry:
                    await cleanup_runtime_resources(client, execution.acc, execution.chat_id)
                    current_prompt = retry.next_prompt
                    continue

                directive = build_tool_directive(standard_request, execution.state)
                if directive.stop_reason == "tool_use":
                    oai_tool_calls = [{
                        "id": block["id"],
                        "type": "function",
                        "function": {
                            "name": block["name"],
                            "arguments": json.dumps(block.get("input", {}), ensure_ascii=False),
                        },
                    } for block in directive.tool_blocks if block.get("type") == "tool_use"]
                    msg = {"role": "assistant", "content": None, "tool_calls": oai_tool_calls}
                    finish_reason = "tool_calls"
                else:
                    msg = {"role": "assistant", "content": execution.state.answer_text}
                    if execution.state.reasoning_text:
                        msg["reasoning_content"] = execution.state.reasoning_text
                    finish_reason = "stop"

                users = await users_db.get()
                for u in users:
                    if u["id"] == token:
                        u["used_tokens"] += retryable_usage_delta(prompt)(execution.state)
                        break
                await users_db.save(users)

                await cleanup_runtime_resources(client, execution.acc, execution.chat_id)

                return JSONResponse({
                    "id": completion_id,
                    "object": "chat.completion",
                    "created": created,
                    "model": model_name,
                    "choices": [{"index": 0, "message": msg, "finish_reason": finish_reason}],
                    "usage": {
                        "prompt_tokens": len(prompt),
                        "completion_tokens": len(execution.state.answer_text),
                        "total_tokens": len(prompt) + len(execution.state.answer_text),
                    },
                })
            except Exception as e:
                if stream_attempt == max_attempts - 1:
                    raise HTTPException(status_code=500, detail=str(e))
