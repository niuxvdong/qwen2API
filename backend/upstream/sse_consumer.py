import json


def parse_sse_chunk(chunk: str) -> list[dict]:
    events = []
    for line in chunk.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            obj = json.loads(data)
            events.append(obj)
        except Exception:
            continue

    parsed = []
    for evt in events:
        if evt.get("choices"):
            delta = evt["choices"][0].get("delta", {})
            parsed.append(
                {
                    "type": "delta",
                    "phase": delta.get("phase", "answer"),
                    "content": delta.get("content", ""),
                    "status": delta.get("status", ""),
                    "extra": delta.get("extra", {}),
                }
            )
    return parsed
