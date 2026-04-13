from fastapi import HTTPException, Request

from backend.core.config import API_KEYS, settings


class AuthContext:
    def __init__(self, token: str, user: dict | None):
        self.token = token
        self.user = user


async def resolve_auth_context(request: Request, users_db) -> AuthContext:
    auth_header = request.headers.get("Authorization", "")
    token = auth_header[7:].strip() if auth_header.startswith("Bearer ") else ""
    if not token:
        token = request.headers.get("x-api-key", "").strip()
    if not token:
        token = request.query_params.get("key", "").strip() or request.query_params.get("api_key", "").strip()

    admin_key = settings.ADMIN_KEY
    if API_KEYS and token != admin_key and token not in API_KEYS:
        raise HTTPException(status_code=401, detail="Invalid API Key")

    users = await users_db.get()
    user = next((u for u in users if u["id"] == token), None)
    if user and user.get("quota", 0) <= user.get("used_tokens", 0):
        raise HTTPException(status_code=402, detail="Quota Exceeded")

    return AuthContext(token=token, user=user)
