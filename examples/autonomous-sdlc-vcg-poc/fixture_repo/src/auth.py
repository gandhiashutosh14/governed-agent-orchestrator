"""Tiny demonstration authentication component."""
ACTIVE = set()
REVOKED = set()

def issue_token(token):
    ACTIVE.add(token)
    REVOKED.discard(token)
    return token

def revoke_token(token):
    ACTIVE.discard(token)
    REVOKED.add(token)

def valid_token(token):
    return token in ACTIVE and token not in REVOKED
