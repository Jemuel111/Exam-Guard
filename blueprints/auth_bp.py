"""
ExamGuard — Auth blueprint
POST /api/login, POST /api/logout, GET /api/me
"""
import logging
from flask import Blueprint, request, jsonify, g

from auth import (hash_password, verify_password, generate_token,
                  revoke_token, get_token_from_request, verify_token)
from database import get_db

logger  = logging.getLogger(__name__)
bp_auth = Blueprint('auth', __name__)


@bp_auth.post('/api/login')
def login():
    data     = request.get_json(silent=True) or {}
    email    = (data.get('email') or '').lower().strip()
    password = data.get('password', '')
    role     = data.get('role', 'student')
    is_reg   = bool(data.get('register', False))

    if not email or not password:
        return jsonify({'success': False, 'error': 'Email and password are required.'}), 400

    db = get_db()

    # ── Registration ──────────────────────────────────────────────────────────
    if is_reg:
        import sqlite3
        name     = data.get('name') or email.split('@')[0].title()
        sid      = data.get('student_id', '')
        initials = ''.join(p[0].upper() for p in name.split()[:2])
        try:
            db.execute(
                'INSERT INTO users (email,password_hash,role,name,student_id,avatar_initials) VALUES (?,?,?,?,?,?)',
                (email, hash_password(password), role, name, sid, initials)
            )
            db.commit()
        except sqlite3.IntegrityError:
            return jsonify({'success': False, 'error': 'Email already registered.'}), 409

        user  = db.execute('SELECT * FROM users WHERE email=?', (email,)).fetchone()
        token = generate_token(user['id'], role, name)
        return jsonify({'success': True, 'token': token, 'role': role, 'name': name})

    # ── Login ─────────────────────────────────────────────────────────────────
    user = db.execute(
        'SELECT * FROM users WHERE email=? AND role=? AND is_active=1',
        (email, role)
    ).fetchone()

    if user and verify_password(password, user['password_hash']):
        token = generate_token(user['id'], role, user['name'])
        db.execute(
            "UPDATE users SET last_login=datetime('now') WHERE id=?",
            (user['id'],)
        )
        db.execute(
            'INSERT INTO audit_log (user_id,action,ip) VALUES (?,?,?)',
            (user['id'], 'LOGIN', request.remote_addr)
        )
        db.commit()
        return jsonify({
            'success': True,
            'token':   token,
            'role':    role,
            'name':    user['name'],
            'user_id': user['id'],
            'avatar':  user['avatar_initials'] or user['name'][0].upper(),
        })

    # No match — never reveal whether email exists
    logger.warning('Failed login attempt for %s from %s', email, request.remote_addr)
    return jsonify({'success': False, 'error': 'Invalid credentials.'}), 401


@bp_auth.post('/api/logout')
def logout():
    token = get_token_from_request()
    revoke_token(token)
    return jsonify({'success': True})


@bp_auth.get('/api/me')
def me():
    token = get_token_from_request()
    info  = verify_token(token)
    if not info or not info.get('user_id'):
        return jsonify({'error': 'Unauthorized'}), 401
    db   = get_db()
    user = db.execute('SELECT * FROM users WHERE id=?', (info['user_id'],)).fetchone()
    if not user:
        return jsonify({'error': 'Not found'}), 404
    return jsonify({
        'id':         user['id'],
        'name':       user['name'],
        'email':      user['email'],
        'role':       user['role'],
        'student_id': user['student_id'],
        'avatar':     user['avatar_initials'],
    })