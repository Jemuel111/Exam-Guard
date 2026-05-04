"""
ExamGuard — Auth blueprint
POST /api/login, POST /api/logout, GET /api/me
POST /api/forgot_password, POST /api/reset_password
POST /api/validate_reset_token
"""
import logging
import secrets
import smtplib
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from flask import Blueprint, request, jsonify, current_app

from auth import (hash_password, verify_password, generate_token,
                  revoke_token, get_token_from_request, verify_token)
from database import get_db

logger  = logging.getLogger(__name__)
bp_auth = Blueprint('auth', __name__)


# ── Email helper ──────────────────────────────────────────────────────────────

def _send_reset_email(to_email: str, user_name: str, token: str, app) -> bool:
    """
    Send a password-reset email.
    Returns True on success, False if mail is not configured or sending fails.
    """
    mail_user = app.config.get('MAIL_USER', '').strip()
    mail_pass = app.config.get('MAIL_PASS', '').strip()
    mail_server = app.config.get('MAIL_SERVER', 'smtp.gmail.com')
    mail_port   = int(app.config.get('MAIL_PORT', 587))

    if not mail_user or not mail_pass:
        logger.warning('MAIL_USER / MAIL_PASS not configured — cannot send reset email')
        return False

    reset_link = f"http://localhost:5000/?token={token}"

    html_body = f"""
<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8"/>
  <style>
    body {{ font-family: 'Segoe UI', Arial, sans-serif; background: #f0f4fa; margin: 0; padding: 0; }}
    .wrapper {{ max-width: 520px; margin: 40px auto; background: #ffffff; border: 1px solid rgba(15,23,42,0.1); }}
    .header {{ background: #2563eb; padding: 28px 36px; }}
    .header h1 {{ color: #fff; font-size: 1.1rem; font-weight: 700; letter-spacing: 0.05em;
                   font-family: monospace; margin: 0; }}
    .body {{ padding: 36px; }}
    .body p {{ color: #475569; font-size: 0.9rem; line-height: 1.7; margin: 0 0 16px; }}
    .btn {{ display: inline-block; background: #2563eb; color: #ffffff !important;
             text-decoration: none; padding: 12px 28px; font-weight: 700;
             font-size: 0.85rem; letter-spacing: 0.04em; margin: 8px 0 20px; }}
    .token-box {{ background: #f0f4fa; border: 1px solid rgba(15,23,42,0.1);
                   padding: 14px 18px; font-family: monospace; font-size: 0.78rem;
                   color: #0f172a; word-break: break-all; margin: 12px 0 20px; }}
    .note {{ font-size: 0.78rem; color: #94a3b8; border-top: 1px solid rgba(15,23,42,0.08);
              padding-top: 16px; margin-top: 8px; }}
    .footer {{ padding: 16px 36px; background: #f0f4fa; font-family: monospace;
                font-size: 0.65rem; color: #94a3b8; letter-spacing: 0.08em; }}
  </style>
</head>
<body>
  <div class="wrapper">
    <div class="header"><h1>EXAMGUARD · PASSWORD RESET</h1></div>
    <div class="body">
      <p>Hi <strong>{user_name}</strong>,</p>
      <p>We received a request to reset your ExamGuard password. Click the button below to set a new password. This link expires in <strong>1 hour</strong>.</p>
      <a class="btn" href="{reset_link}">Reset My Password</a>
      <p style="color:#94a3b8;font-size:0.8rem;">If the button doesn't work, copy and paste this link into your browser:</p>
      <div class="token-box">{reset_link}</div>
      <p class="note">If you did not request a password reset, you can safely ignore this email. Your password will not be changed.</p>
    </div>
    <div class="footer">EXAMGUARD · SECURE EXAM PLATFORM · DO NOT REPLY TO THIS EMAIL</div>
  </div>
</body>
</html>
"""

    try:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = 'ExamGuard — Password Reset Request'
        msg['From']    = f'ExamGuard <{mail_user}>'
        msg['To']      = to_email
        msg.attach(MIMEText(html_body, 'html'))

        with smtplib.SMTP(mail_server, mail_port, timeout=10) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.login(mail_user, mail_pass)
            smtp.send_message(msg)

        logger.info('Password reset email sent to %s', to_email)
        return True
    except Exception as e:
        logger.error('Failed to send reset email to %s: %s', to_email, e)
        return False


# ── Login ─────────────────────────────────────────────────────────────────────

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
    Generate a password-reset token and email it to the user.
    Always returns the same success message to prevent email enumeration.
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
    generic_ok = jsonify({
        'success': True,
        'message': 'If that email is registered, a reset link has been sent.',
    })

    if not user:
        return generic_ok

    # Invalidate old tokens for this user
    db.execute('DELETE FROM password_reset_tokens WHERE user_id=?', (user['id'],))

    token      = secrets.token_urlsafe(32)
    expires_at = (datetime.now() + timedelta(hours=1)).strftime('%Y-%m-%d %H:%M:%S')

    db.execute(
        'INSERT INTO password_reset_tokens (user_id, token, expires_at) VALUES (?,?,?)',
        (user['id'], token, expires_at)
    )
    db.commit()

    logger.info('Password reset token generated for user %s (%s)', user['id'], email)

    # Try to send the real email
    mail_sent = _send_reset_email(email, user['name'], token, current_app)

    if mail_sent:
        return generic_ok

    # Mail not configured — inform admin/dev without leaking the token to the
    # browser. Log the reset link server-side so it can be used during local dev.
    reset_link = f"http://localhost:5000/?token={token}"
    logger.info(
        'EMAIL NOT CONFIGURED — reset link for %s: %s',
        email, reset_link
    )

    # Return a mail-not-configured flag so the frontend can show a helpful
    # fallback message. Never send the raw token in the JSON response.
    return jsonify({
        'success': True,
        'message': 'If that email is registered, a reset link has been sent.',
        'mail_not_configured': True,   # frontend uses this only to show a UI hint
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
    data         = request.get_json(silent=True) or {}
    token        = data.get('token', '').strip()
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