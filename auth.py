
import secrets
import logging
from datetime import datetime, timedelta, timezone
from functools import wraps

def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)

import bcrypt
from flask import request, jsonify, g

logger = logging.getLogger(__name__)

_active_tokens: dict = {}


# ── Password hashing ──────────────────────────────────────────────────────────

def hash_password(pw: str) -> str:
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()


def verify_password(pw: str, pw_hash: str) -> bool:
    try:
        return bcrypt.checkpw(pw.encode(), pw_hash.encode())
    except Exception:
        return False


# ── Token management ──────────────────────────────────────────────────────────

def generate_token(user_id: int, role: str, name: str, ttl_hours: int = 8) -> str:
    token = secrets.token_hex(32)
    _active_tokens[token] = {
        'user_id': user_id,
        'role':    role,
        'name':    name,
        'exp':     _utcnow() + timedelta(hours=ttl_hours),
    }
    _prune_tokens()
    return token


def verify_token(token: str) -> dict | None:
  
    if not token:
        return None
    info = _active_tokens.get(token)
    if not info:
        return None
    if _utcnow() > info['exp']:
        _active_tokens.pop(token, None)
        return None
    # Prune stale entries opportunistically on each successful verify
    _prune_tokens()
    return info


def revoke_token(token: str):
    _active_tokens.pop(token, None)


def _prune_tokens():

    now = _utcnow()
    expired = [t for t, v in list(_active_tokens.items()) if now > v['exp']]
    for t in expired:
        _active_tokens.pop(t, None)


def get_token_from_request() -> str:
   
    auth = request.headers.get('Authorization', '')
    if auth.startswith('Bearer '):
        return auth[7:].strip()
    # Query param (used in exam URL token= links)
    qp = request.args.get('token', '')
    if qp:
        return qp
    # Cookie fallback
    return request.cookies.get('eg_token', '')


# ── Decorators ────────────────────────────────────────────────────────────────

def login_required(role: str | None = None):
    

    
    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            token = get_token_from_request()
            info  = verify_token(token)

            if not info:
                # Demo mode: allow requests with X-Demo-Mode header
                if request.headers.get('X-Demo-Mode') == '1':
                    g.current_user = {
                        'user_id': -1,          # FIX: was None, caused TypeError on > 0 checks
                        'role':    role or 'student',
                        'name':    'Demo User',
                    }
                    return f(*args, **kwargs)
                return jsonify({'error': 'Unauthorized'}), 401

            if role and info['role'] != role:
                return jsonify({'error': 'Forbidden'}), 403

            g.current_user = info
            return f(*args, **kwargs)
        return wrapped
    return decorator


def get_current_user() -> dict | None:
    return getattr(g, 'current_user', None)