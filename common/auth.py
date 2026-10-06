def auth_headers(token: str) -> dict:
    return {'Authorization': f'Bearer {token}'}

def extract_bearer(value: str | None) -> str | None:
    if not value: return None
    if value.startswith('Bearer '): return value[7:]
    return value
