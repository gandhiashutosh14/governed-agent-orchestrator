"""Tiny demonstration API handler."""
from src.auth import valid_token
from src.store import get_request_count

def handle_request(token, trace_id):
    if not valid_token(token):
        return {"status": 401, "trace_id": trace_id}
    return {"status": 200, "trace_id": trace_id, "count": get_request_count()}
