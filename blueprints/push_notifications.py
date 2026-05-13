"""
ExamGuard — Push Notifications Blueprint
Handles Web Push subscriptions and sending notifications via VAPID.

Uses the `pywebpush` library (pip install pywebpush).

Setup:
1. pip install pywebpush
2. Generate VAPID keys once:
       python -c "from py_vapid import Vapid; v=Vapid(); v.generate_keys(); v.save_key('vapid_private.pem'); print(v.public_key)"
3. Add to your .env file:
       VAPID_PRIVATE_KEY=<contents of vapid_private.pem OR the raw base64 private key>
       VAPID_PUBLIC_KEY=<the printed public key>
       VAPID_CLAIMS_EMAIL=mailto:admin@yourdomain.com
"""
import json
import logging
import os

from flask import Blueprint, request, jsonify, current_app
from database import get_db

logger = logging.getLogger(__name__)
bp_push = Blueprint('push', __name__)


# ── Helper: get pywebpush application server key ──────────────────────────────

def _get_webpush():
    """Lazy-import webpush so the app still starts if pywebpush isn't installed."""
    try:
        from pywebpush import webpush, WebPushException
        return webpush, WebPushException
    except ImportError:
        logger.error('pywebpush not installed. Run: pip install pywebpush')
        return None, None


# ── Ensure subscriptions table exists ─────────────────────────────────────────

def init_push_table(app):
    """Call this from app.py after init_db()."""
    import sqlite3
    db_path = app.config['DATABASE']
    conn = sqlite3.connect(db_path)
    conn.execute('''
        CREATE TABLE IF NOT EXISTS push_subscriptions (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER,
            role       TEXT,
            endpoint   TEXT UNIQUE NOT NULL,
            p256dh     TEXT NOT NULL,
            auth       TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now'))
        )
    ''')
    conn.commit()
    conn.close()
    logger.info('Push subscriptions table ready')


# ── VAPID public key (sent to client so it can subscribe) ─────────────────────

@bp_push.get('/api/push/vapid-public-key')
def get_vapid_public_key():
    key = os.environ.get('VAPID_PUBLIC_KEY', '')
    if not key:
        return jsonify({'error': 'VAPID not configured'}), 503
    return jsonify({'publicKey': key})


# ── Subscribe ─────────────────────────────────────────────────────────────────

@bp_push.post('/api/push/subscribe')
def subscribe():
    data = request.get_json(silent=True) or {}
    subscription = data.get('subscription', {})
    user_id = data.get('user_id')
    role = data.get('role', 'student')

    endpoint = subscription.get('endpoint')
    keys = subscription.get('keys', {})
    p256dh = keys.get('p256dh')
    auth = keys.get('auth')

    if not endpoint or not p256dh or not auth:
        return jsonify({'success': False, 'error': 'Invalid subscription object'}), 400

    db = get_db()
    try:
        db.execute('''
            INSERT OR REPLACE INTO push_subscriptions (user_id, role, endpoint, p256dh, auth)
            VALUES (?, ?, ?, ?, ?)
        ''', (user_id, role, endpoint, p256dh, auth))
        db.commit()
        logger.info('Push subscription saved for user_id=%s role=%s', user_id, role)
        return jsonify({'success': True})
    except Exception as e:
        logger.error('Subscribe error: %s', e)
        return jsonify({'success': False, 'error': str(e)}), 500


# ── Unsubscribe ───────────────────────────────────────────────────────────────

@bp_push.post('/api/push/unsubscribe')
def unsubscribe():
    data = request.get_json(silent=True) or {}
    endpoint = (data.get('subscription') or {}).get('endpoint') or data.get('endpoint')
    if not endpoint:
        return jsonify({'success': False, 'error': 'No endpoint'}), 400

    db = get_db()
    db.execute('DELETE FROM push_subscriptions WHERE endpoint=?', (endpoint,))
    db.commit()
    return jsonify({'success': True})


# ── Send notification to a specific user ──────────────────────────────────────

def send_push_to_user(user_id, title, body, url='/', tag='examguard', require_interaction=False):
    """
    Call this from anywhere in the app to push a notification to a specific user.
    Returns (sent_count, error_count).
    """
    webpush, WebPushException = _get_webpush()
    if not webpush:
        return 0, 0

    vapid_private = os.environ.get('VAPID_PRIVATE_KEY', '')
    vapid_email   = os.environ.get('VAPID_CLAIMS_EMAIL', 'mailto:admin@examguard.local')

    if not vapid_private:
        logger.warning('VAPID_PRIVATE_KEY not set — push not sent')
        return 0, 0

    from flask import current_app
    with current_app.app_context():
        db = get_db()
        subs = db.execute(
            'SELECT * FROM push_subscriptions WHERE user_id=?', (user_id,)
        ).fetchall()

    payload = json.dumps({
        'title': title,
        'body': body,
        'url': url,
        'tag': tag,
        'requireInteraction': require_interaction,
        'icon': '/static/icons/icon-192.png',
    })

    sent = 0
    errors = 0
    for sub in subs:
        try:
            webpush(
                subscription_info={
                    'endpoint': sub['endpoint'],
                    'keys': {'p256dh': sub['p256dh'], 'auth': sub['auth']},
                },
                data=payload,
                vapid_private_key=vapid_private,
                vapid_claims={'sub': vapid_email},
            )
            sent += 1
        except WebPushException as e:
            logger.error('Push failed for sub %s: %s', sub['id'], e)
            # Remove expired/invalid subscriptions (410 Gone)
            if e.response and e.response.status_code in (404, 410):
                db = get_db()
                db.execute('DELETE FROM push_subscriptions WHERE id=?', (sub['id'],))
                db.commit()
            errors += 1

    return sent, errors


# ── Send notification to all teachers ─────────────────────────────────────────

def send_push_to_teachers(title, body, url='/teacher/dashboard', tag='examguard-teacher', require_interaction=False):
    """Broadcast a push to all subscribed teachers."""
    webpush, WebPushException = _get_webpush()
    if not webpush:
        return 0, 0

    vapid_private = os.environ.get('VAPID_PRIVATE_KEY', '')
    vapid_email   = os.environ.get('VAPID_CLAIMS_EMAIL', 'mailto:admin@examguard.local')

    if not vapid_private:
        logger.warning('VAPID_PRIVATE_KEY not set — push not sent')
        return 0, 0

    from flask import current_app
    with current_app.app_context():
        db = get_db()
        subs = db.execute(
            "SELECT * FROM push_subscriptions WHERE role='teacher'"
        ).fetchall()

    payload = json.dumps({
        'title': title,
        'body': body,
        'url': url,
        'tag': tag,
        'requireInteraction': require_interaction,
        'icon': '/static/icons/icon-192.png',
    })

    sent = 0
    errors = 0
    for sub in subs:
        try:
            webpush(
                subscription_info={
                    'endpoint': sub['endpoint'],
                    'keys': {'p256dh': sub['p256dh'], 'auth': sub['auth']},
                },
                data=payload,
                vapid_private_key=vapid_private,
                vapid_claims={'sub': vapid_email},
            )
            sent += 1
        except WebPushException as e:
            logger.error('Push failed for sub %s: %s', sub['id'], e)
            if e.response and e.response.status_code in (404, 410):
                db = get_db()
                db.execute('DELETE FROM push_subscriptions WHERE id=?', (sub['id'],))
                db.commit()
            errors += 1

    return sent, errors


# ── Manual send endpoint (teacher dashboard → test panel) ─────────────────────

@bp_push.post('/api/push/send')
def send_notification():
    """
    Teacher-triggered push. Body: { title, body, url, role }
    role = 'all' | 'teacher' | 'student'
    """
    data  = request.get_json(silent=True) or {}
    title = data.get('title', 'ExamGuard')
    body  = data.get('body', 'New notification')
    url   = data.get('url', '/')
    role  = data.get('role', 'teacher')
    tag   = data.get('tag', 'examguard-manual')

    webpush, WebPushException = _get_webpush()
    if not webpush:
        return jsonify({'success': False, 'error': 'pywebpush not installed'}), 503

    vapid_private = os.environ.get('VAPID_PRIVATE_KEY', '')
    vapid_email   = os.environ.get('VAPID_CLAIMS_EMAIL', 'mailto:admin@examguard.local')

    if not vapid_private:
        return jsonify({'success': False, 'error': 'VAPID_PRIVATE_KEY not configured in .env'}), 503

    db = get_db()
    if role == 'all':
        subs = db.execute('SELECT * FROM push_subscriptions').fetchall()
    else:
        subs = db.execute(
            'SELECT * FROM push_subscriptions WHERE role=?', (role,)
        ).fetchall()

    if not subs:
        return jsonify({'success': False, 'error': f'No subscriptions found for role: {role}'}), 404

    payload = json.dumps({
        'title': title, 'body': body, 'url': url,
        'tag': tag, 'icon': '/static/icons/icon-192.png',
    })

    sent = errors = 0
    for sub in subs:
        try:
            webpush(
                subscription_info={
                    'endpoint': sub['endpoint'],
                    'keys': {'p256dh': sub['p256dh'], 'auth': sub['auth']},
                },
                data=payload,
                vapid_private_key=vapid_private,
                vapid_claims={'sub': vapid_email},
            )
            sent += 1
        except WebPushException as e:
            logger.error('Push send error: %s', e)
            if e.response and e.response.status_code in (404, 410):
                db.execute('DELETE FROM push_subscriptions WHERE id=?', (sub['id'],))
                db.commit()
            errors += 1

    return jsonify({'success': True, 'sent': sent, 'errors': errors, 'total': len(subs)})


# ── Subscription count (for dashboard badge) ──────────────────────────────────

@bp_push.get('/api/push/subscription-count')
def subscription_count():
    db = get_db()
    try:
        total    = db.execute('SELECT COUNT(*) FROM push_subscriptions').fetchone()[0]
        teachers = db.execute("SELECT COUNT(*) FROM push_subscriptions WHERE role='teacher'").fetchone()[0]
        students = db.execute("SELECT COUNT(*) FROM push_subscriptions WHERE role='student'").fetchone()[0]
        return jsonify({'total': total, 'teachers': teachers, 'students': students})
    except Exception as e:
        return jsonify({'total': 0, 'teachers': 0, 'students': 0})