import asyncio
import json
import logging
import time

from backend.core.config import settings
from backend.core.request_logging import update_request_context
from backend.services.auth_resolver import AuthResolver
from backend.upstream.payload_builder import build_chat_payload
from backend.upstream.sse_consumer import parse_sse_chunk

log = logging.getLogger("qwen2api.executor")


class QwenExecutor:
    def __init__(self, engine, account_pool):
        self.engine = engine
        self.account_pool = account_pool
        self.auth_resolver = AuthResolver(account_pool) if account_pool is not None else None

    async def create_chat(self, token: str, model: str, chat_type: str = "t2t") -> str:
        request_fn = getattr(self.engine, "_request_json", None) or getattr(self.engine, "api_call", None)
        if request_fn is None:
            raise Exception("request transport unavailable")

        ts = int(time.time())
        body = {
            "title": f"api_{ts}",
            "models": [model],
            "chat_mode": "normal",
            "chat_type": chat_type,
            "timestamp": ts,
        }

        if getattr(self.engine, "_request_json", None) is not None:
            r = await request_fn("POST", "/api/v2/chats/new", token, body, timeout=30.0)
        else:
            r = await request_fn("POST", "/api/v2/chats/new", token, body)
        body_text = r.get("body", "")
        if r["status"] != 200:
            body_lower = body_text.lower()
            if (
                r["status"] in (401, 403)
                or "unauthorized" in body_lower
                or "forbidden" in body_lower
                or "token" in body_lower
                or "login" in body_lower
                or "401" in body_text
                or "403" in body_text
            ):
                raise Exception(f"unauthorized: create_chat HTTP {r['status']}: {body_text[:100]}")
            if r["status"] == 429:
                raise Exception("429 Too Many Requests")
            raise Exception(f"create_chat HTTP {r['status']}: {body_text[:100]}")

        try:
            data = json.loads(body_text)
            if not data.get("success") or "id" not in data.get("data", {}):
                raise Exception("Qwen API returned error or missing id")
            return data["data"]["id"]
        except Exception as e:
            body_lower = body_text.lower()
            if any(
                kw in body_lower
                for kw in (
                    "html",
                    "login",
                    "unauthorized",
                    "activation",
                    "pending",
                    "forbidden",
                    "token",
                    "expired",
                    "invalid",
                )
            ):
                raise Exception(f"unauthorized: account issue: {body_text[:200]}")
            raise Exception(f"create_chat parse error: {e}, body={body_text[:200]}")

    async def stream(
        self,
        token: str,
        chat_id: str,
        model: str,
        content: str,
        has_custom_tools: bool = False,
    ):
        stream_fn = getattr(self.engine, "stream_chat_once", None) or getattr(self.engine, "fetch_chat", None)
        if stream_fn is None:
            raise Exception("stream transport unavailable")

        payload = build_chat_payload(chat_id, model, content, has_custom_tools)
        buffer = ""
        started_at = time.perf_counter()
        first_event_logged = False

        log.info(f"[Executor] stream start chat_id={chat_id} model={model} has_custom_tools={has_custom_tools}")

        async for chunk_result in stream_fn(token, chat_id, payload):
            if chunk_result.get("status") not in (None, 200, "streamed"):
                body = chunk_result.get("body", b"")
                if isinstance(body, bytes):
                    body = body.decode("utf-8", errors="ignore")
                raise Exception(f"HTTP {chunk_result['status']}: {str(body)[:100]}")

            if "chunk" in chunk_result:
                buffer += chunk_result["chunk"]
                while "\n\n" in buffer:
                    msg, buffer = buffer.split("\n\n", 1)
                    for evt in parse_sse_chunk(msg):
                        if not first_event_logged:
                            first_event_logged = True
                            log.info(
                                f"[Executor] first parsed event after {(time.perf_counter() - started_at):.3f}s chat_id={chat_id}"
                            )
                        yield evt

        if buffer:
            for evt in parse_sse_chunk(buffer):
                if not first_event_logged:
                    first_event_logged = True
                    log.info(
                        f"[Executor] first parsed event after {(time.perf_counter() - started_at):.3f}s chat_id={chat_id}"
                    )
                yield evt

        log.info(f"[Executor] stream finish chat_id={chat_id} total={(time.perf_counter() - started_at):.3f}s")

    async def chat_stream_events_with_retry(self, model: str, content: str, has_custom_tools: bool = False):
        exclude = set()
        for attempt in range(settings.MAX_RETRIES):
            update_request_context(upstream_attempt=attempt + 1)
            acc = await self.account_pool.acquire_wait(timeout=60, exclude=exclude)
            if not acc:
                raise Exception("No available accounts in pool (all busy or rate limited)")

            try:
                log.info(f"[Executor] acquired account={acc.email} model={model} attempt={attempt + 1}")
                chat_id = await self.create_chat(acc.token, model)
                update_request_context(chat_id=chat_id)
                log.info(f"[Executor] created chat_id={chat_id} account={acc.email}")
                yield {"type": "meta", "chat_id": chat_id, "acc": acc}

                async for evt in self.stream(acc.token, chat_id, model, content, has_custom_tools):
                    yield {"type": "event", "event": evt}
                return

            except Exception as e:
                err_msg = str(e).lower()
                if "429" in err_msg or "rate limit" in err_msg or "too many" in err_msg:
                    self.account_pool.mark_rate_limited(acc)
                    exclude.add(acc.email)
                elif "unauthorized" in err_msg or "401" in err_msg or "403" in err_msg:
                    self.account_pool.mark_invalid(acc)
                    exclude.add(acc.email)
                    if "activation" in err_msg or "pending" in err_msg:
                        acc.activation_pending = True
                    if self.auth_resolver is not None:
                        asyncio.create_task(self.auth_resolver.auto_heal_account(acc))
                else:
                    exclude.add(acc.email)

                self.account_pool.release(acc)
                log.warning(
                    f"[Executor] retry attempt={attempt + 1}/{settings.MAX_RETRIES} account={acc.email} error={e}"
                )

        raise Exception(f"All {settings.MAX_RETRIES} attempts failed. Please check upstream accounts.")
