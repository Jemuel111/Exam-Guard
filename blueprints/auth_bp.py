"""
ExamGuard — Auth blueprint
POST /api/login, POST /api/logout, GET /api/me
POST /api/forgot_password, POST /api/reset_password

ADDITIONS:
- /api/forgot_password: generates a reset token (in demo: returns it directly)
- /api/reset_password: validates token and sets new password
- /api/validate_reset_token: checks if a token is still valid
"""
import logging
import secrets
from datetime import datetime, timedelta

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


# ── Forgot password ───────────────────────────────────────────────────────────

@bp_auth.post('/api/forgot_password')
def forgot_password():
    """
    Generates a password reset token.
    In production: send via email. In demo mode: return token in response
    so it can be used directly (since there's no mail server configured).
    """
    data  = request.get_json(silent=True) or {}
    email = (data.get('email') or '').lower().strip()
    role  = data.get('role', 'student')

    if not email:
        return jsonify({'success': False, 'error': 'Email is required.'}), 400

    db   = get_db()
    user = db.execute(
        'SELECT * FROM users WHERE email=? AND role=? AND is_active=1',
        (email, role)
    ).fetchone()

    # Always return success to prevent email enumeration
    if not user:
        return jsonify({
            'success': True,
            'message': 'If that email exists, a reset link has been sent.',
            'demo_mode': True,
        })

    # Invalidate old tokens for this user
    db.execute('DELETE FROM password_reset_tokens WHERE user_id=?', (user['id'],))

    token      = secrets.token_urlsafe(32)
    expires_at = (datetime.now() + timedelta(hours=1)).strftime('%Y-%m-%d %H:%M:%S')

    db.execute(
        'INSERT INTO password_reset_tokens (user_id, token, expires_at) VALUES (?,?,?)',
        (user['id'], token, expires_at)
    )
    db.commit()

    logger.info('Password reset token generated for user %s', user['id'])

    # In production: send email with reset link
    # For demo: return the token directly so teacher/student can test the flow
    mail_configured = bool(db.execute("SELECT 1").fetchone())  # placeholder check
    return jsonify({
        'success':    True,
        'message':    'If that email exists, a reset link has been sent.',
        'demo_token': token,   # Remove this in production
        'demo_email': email,
        'demo_mode':  True,
    })


@bp_auth.post('/api/validate_reset_token')
def validate_reset_token():
    data  = request.get_json(silent=True) or {}
    token = data.get('token', '').strip()
    if not token:
        return jsonify({'valid': False, 'error': 'Token required'})

    db  = get_db()
    row = db.execute(
        '''SELECT prt.*, u.email, u.name FROM password_reset_tokens prt
           JOIN users u ON u.id=prt.user_id
           WHERE prt.token=? AND prt.used=0''',
        (token,)
    ).fetchone()

    if not row:
        return jsonify({'valid': False, 'error': 'Invalid or expired token'})

    if datetime.strptime(row['expires_at'], '%Y-%m-%d %H:%M:%S') < datetime.now():
        db.execute('DELETE FROM password_reset_tokens WHERE token=?', (token,))
        db.commit()
        return jsonify({'valid': False, 'error': 'Token has expired'})

    return jsonify({'valid': True, 'email': row['email'], 'name': row['name']})


@bp_auth.post('/api/reset_password')
def reset_password():
    data        = request.get_json(silent=True) or {}
    token       = data.get('token', '').strip()
    new_password = data.get('password', '').strip()

    if not token or not new_password:
        return jsonify({'success': False, 'error': 'Token and new password are required.'}), 400

    if len(new_password) < 6:
        return jsonify({'success': False, 'error': 'Password must be at least 6 characters.'}), 400

    db  = get_db()
    row = db.execute(
        'SELECT * FROM password_reset_tokens WHERE token=? AND used=0',
        (token,)
    ).fetchone()

    if not row:
        return jsonify({'success': False, 'error': 'Invalid or already used reset link.'}), 400

    if datetime.strptime(row['expires_at'], '%Y-%m-%d %H:%M:%S') < datetime.now():
        db.execute('DELETE FROM password_reset_tokens WHERE token=?', (token,))
        db.commit()
        return jsonify({'success': False, 'error': 'Reset link has expired. Please request a new one.'}), 400

    pw_hash = hash_password(new_password)
    db.execute('UPDATE users SET password_hash=? WHERE id=?', (pw_hash, row['user_id']))
    db.execute('UPDATE password_reset_tokens SET used=1 WHERE token=?', (token,))
    db.commit()

    logger.info('Password reset successful for user_id=%s', row['user_id'])
    return jsonify({'success': True, 'message': 'Password updated successfully. You can now log in.'})