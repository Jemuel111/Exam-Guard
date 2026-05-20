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
from crypto import encrypt, decrypt, USER_FIELDS
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

    reset_link = f"https://examguardapp.duckdns.org/?token={token}"

    html_body = f"""
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>ExamGuard — Password Reset</title>
</head>
<body style="margin:0;padding:0;background-color:#f4f4f5;font-family:-apple-system,BlinkMacSystemFont,'Inter','Segoe UI',Arial,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f4f4f5;padding:48px 20px;">
    <tr>
      <td align="center">
        <table width="540" cellpadding="0" cellspacing="0" style="max-width:540px;width:100%;">

          <!-- Header -->
          <tr>
            <td style="background:#ffffff;border:1px solid #e4e4e7;border-bottom:none;border-radius:12px 12px 0 0;padding:24px 32px;">
              <table cellpadding="0" cellspacing="0">
                <tr>
                  <td style="background:#2563eb;width:30px;height:30px;border-radius:7px;text-align:center;vertical-align:middle;">
                    <span style="color:#fff;font-size:14px;font-weight:700;line-height:30px;display:block;">G</span>
                  </td>
                  <td style="padding-left:10px;">
                    <span style="font-size:15px;font-weight:600;letter-spacing:-0.02em;color:#09090b;">
                      Exam<span style="color:#2563eb;">Guard</span>
                    </span>
                  </td>
                </tr>
              </table>
            </td>
          </tr>

          <!-- Blue accent bar -->
          <tr>
            <td style="background:#2563eb;height:2px;font-size:0;line-height:0;">&nbsp;</td>
          </tr>

          <!-- Body -->
          <tr>
            <td style="background:#ffffff;border:1px solid #e4e4e7;border-top:none;border-bottom:none;padding:40px 32px;">

              <!-- Eyebrow -->
              <p style="margin:0 0 6px;font-size:11px;letter-spacing:0.1em;text-transform:uppercase;color:#2563eb;font-weight:500;">
                Account Recovery
              </p>

              <!-- Title -->
              <h1 style="margin:0 0 16px;font-size:22px;font-weight:700;letter-spacing:-0.03em;color:#09090b;line-height:1.2;">
                Reset your password
              </h1>

              <!-- Body text -->
              <p style="margin:0 0 32px;font-size:14px;line-height:1.75;color:#52525b;">
                Hi <strong style="color:#09090b;">{user_name}</strong>,<br><br>
                We received a request to reset your ExamGuard password. Click the button below to set a new password. This link expires in <strong style="color:#09090b;">1 hour</strong>.
              </p>

              <!-- CTA Button -->
              <table cellpadding="0" cellspacing="0" style="margin-bottom:36px;">
                <tr>
                  <td style="background:#2563eb;border-radius:8px;">
                    <a href="{reset_link}"
                       style="display:inline-block;padding:12px 28px;font-size:14px;font-weight:600;letter-spacing:-0.01em;color:#ffffff;text-decoration:none;">
                      Reset My Password &rarr;
                    </a>
                  </td>
                </tr>
              </table>

              <!-- Divider -->
              <table cellpadding="0" cellspacing="0" width="100%" style="margin-bottom:24px;">
                <tr>
                  <td style="border-top:1px solid #e4e4e7;font-size:0;">&nbsp;</td>
                </tr>
              </table>

              <!-- Fallback link -->
              <p style="margin:0 0 6px;font-size:12px;color:#a1a1aa;">
                If the button above does not work, copy and paste this link into your browser:
              </p>
              <p style="margin:0 0 28px;font-family:'Courier New',monospace;font-size:11px;color:#2563eb;word-break:break-all;line-height:1.6;">
                {reset_link}
              </p>

              <!-- Warning box -->
              <table cellpadding="0" cellspacing="0" width="100%">
                <tr>
                  <td style="background:#f4f4f5;border:1px solid #e4e4e7;border-left:3px solid #2563eb;border-radius:0 6px 6px 0;padding:14px 16px;">
                    <p style="margin:0;font-size:13px;line-height:1.65;color:#52525b;">
                      <strong style="color:#09090b;">Didn't request this?</strong><br>
                      You can safely ignore this email. Your password will not change unless you click the link above.
                    </p>
                  </td>
                </tr>
              </table>

            </td>
          </tr>

          <!-- Footer -->
          <tr>
            <td style="background:#fafafa;border:1px solid #e4e4e7;border-top:none;border-radius:0 0 12px 12px;padding:18px 32px;">
              <table cellpadding="0" cellspacing="0" width="100%">
                <tr>
                  <td>
                    <p style="margin:0;font-size:11px;letter-spacing:0.04em;color:#a1a1aa;text-transform:uppercase;">
                      ExamGuard &middot; Secure Exam Platform
                    </p>
                  </td>
                  <td align="right">
                    <p style="margin:0;font-size:11px;color:#a1a1aa;">
                      Do not reply to this email
                    </p>
                  </td>
                </tr>
              </table>
            </td>
          </tr>

        </table>
      </td>
    </tr>
  </table>
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


import hashlib

def _email_hash(email: str) -> str:
    """One-way SHA-256 hash for indexed lookup of encrypted email."""
    return hashlib.sha256(email.lower().strip().encode()).hexdigest()


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

    db         = get_db()
    email_hash = _email_hash(email)

    # ── Registration ──────────────────────────────────────────────────────────
    if is_reg:
        import sqlite3
        name     = data.get('name') or email.split('@')[0].title()
        sid      = data.get('student_id', '')
        initials = ''.join(p[0].upper() for p in name.split()[:2])
        try:
            db.execute(
                'INSERT INTO users (email,email_hash,password_hash,role,name,student_id,avatar_initials) VALUES (?,?,?,?,?,?,?)',
                (encrypt(email), email_hash, hash_password(password), role, encrypt(name), encrypt(sid), initials)
            )
            db.commit()
        except sqlite3.IntegrityError:
            return jsonify({'success': False, 'error': 'Email already registered.'}), 409

        user  = db.execute('SELECT * FROM users WHERE email_hash=?', (email_hash,)).fetchone()
        token = generate_token(user['id'], role, name)
        return jsonify({'success': True, 'token': token, 'role': role, 'name': name, 'user_id': user['id']})

    # ── Login ─────────────────────────────────────────────────────────────────
    user = db.execute(
        'SELECT * FROM users WHERE email_hash=? AND role=? AND is_active=1',
        (email_hash, role)
    ).fetchone()

    if user and verify_password(password, user['password_hash']):
        decrypted_name = decrypt(user['name'])
        token = generate_token(user['id'], role, decrypted_name)
        db.execute(
            "UPDATE users SET last_login=datetime(\'now\') WHERE id=?",
            (user['id'],)
        )
        db.execute(
            'INSERT INTO audit_log (user_id,action,ip) VALUES (?,?,?)',
            (user['id'], 'LOGIN', encrypt(request.remote_addr))
        )
        db.commit()
        return jsonify({
            'success': True,
            'token':   token,
            'role':    role,
            'name':    decrypted_name,
            'user_id': user['id'],
            'avatar':  user['avatar_initials'] or decrypted_name[0].upper(),
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
        'name':       decrypt(user['name']),
        'email':      decrypt(user['email']),
        'role':       user['role'],
        'student_id': decrypt(user['student_id']) if user['student_id'] else '',
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
        'SELECT * FROM users WHERE email_hash=? AND is_active=1',
        (_email_hash(email),)
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

    # Decrypt name before sending email
    decrypted_name = decrypt(user['name'])

    # Try to send the real email
    mail_sent = _send_reset_email(email, decrypted_name, token, current_app)

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