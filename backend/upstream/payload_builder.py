import time
import uuid


CUSTOM_TOOL_COMPAT_FEATURE_CONFIG = {
    "thinking_enabled": True,
    "output_schema": "phase",
    "research_mode": "normal",
    "auto_thinking": True,
    "thinking_mode": "Auto",
    "thinking_format": "summary",
    "auto_search": False,
    "code_interpreter": False,
    "plugins_enabled": False,
}


def build_chat_payload(chat_id: str, model: str, content: str, has_custom_tools: bool = False) -> dict:
    ts = int(time.time())
    feature_config = {
        **CUSTOM_TOOL_COMPAT_FEATURE_CONFIG,
        "function_calling": has_custom_tools,
    }
    return {
        "stream": True,
        "version": "2.1",
        "incremental_output": True,
        "chat_id": chat_id,
        "chat_mode": "normal",
        "model": model,
        "parent_id": None,
        "messages": [
            {
                "fid": str(uuid.uuid4()),
                "parentId": None,
                "childrenIds": [str(uuid.uuid4())],
                "role": "user",
                "content": content,
                "user_action": "chat",
                "files": [],
                "timestamp": ts,
                "models": [model],
                "chat_type": "t2t",
                "feature_config": feature_config,
                "extra": {"meta": {"subChatType": "t2t"}},
                "sub_chat_type": "t2t",
                "parent_id": None,
            }
        ],
        "timestamp": ts,
    }
