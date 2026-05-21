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

# ── Firebase Admin (FCM) ──────────────────────────────────────────────────────

def _get_fcm():
    """Lazy-init Firebase Admin SDK for FCM push to mobile devices."""
    try:
        import firebase_admin
        from firebase_admin import credentials, messaging
        if not firebase_admin._apps:
            import os
            key_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'serviceAccountKey.json')
            if os.path.exists(key_path):
                cred = credentials.Certificate(key_path)
                firebase_admin.initialize_app(cred)
                logger.info('Firebase Admin SDK initialized')
            else:
                logger.warning('serviceAccountKey.json not found — FCM disabled')
                return None, None
        return firebase_admin, messaging
    except ImportError:
        logger.warning('firebase-admin not installed — FCM disabled')
        return None, None


def send_fcm_to_user(user_id, title, body, data=None):
    db = get_db()
    try:
        tokens = db.execute('SELECT fcm_token FROM fcm_tokens WHERE user_id=?', (user_id,)).fetchall()
    except RuntimeError:
        from flask import current_app
        with current_app.app_context():
            import sqlite3
            conn = sqlite3.connect(current_app.config['DATABASE'])
            conn.row_factory = sqlite3.Row
            tokens = conn.execute('SELECT fcm_token FROM fcm_tokens WHERE user_id=?', (user_id,)).fetchall()
            conn.close()
    sent = 0
    for row in tokens:
        token = row['fcm_token']
        if token.startswith('ExponentPushToken'):
            if send_expo_push(token, title, body, data):
                sent += 1
    return sent, 0


def send_fcm_to_teachers(title, body, data=None):
    db = get_db()
    try:
        tokens = db.execute("SELECT fcm_token FROM fcm_tokens WHERE role='teacher'").fetchall()
    except RuntimeError:
        from flask import current_app
        with current_app.app_context():
            import sqlite3
            conn = sqlite3.connect(current_app.config['DATABASE'])
            conn.row_factory = sqlite3.Row
            tokens = conn.execute("SELECT fcm_token FROM fcm_tokens WHERE role='teacher'").fetchall()
            conn.close()
    sent = 0
    for row in tokens:
        token = row['fcm_token']
        if token.startswith('ExponentPushToken'):
            if send_expo_push(token, title, body, data):
                sent += 1
    return sent, 0

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
    conn.execute('''
        CREATE TABLE IF NOT EXISTS fcm_tokens (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL,
            role       TEXT NOT NULL,
            fcm_token  TEXT UNIQUE NOT NULL,
            created_at TEXT DEFAULT (datetime('now')),
            UNIQUE(user_id, fcm_token)
        )
    ''')
    conn.commit()
    conn.close()
    logger.info('Push subscriptions table ready')


# ── FCM token registration (mobile app) ──────────────────────────────────────

@bp_push.post('/api/push/fcm-token')
def register_fcm_token():
    """Mobile app registers its FCM token after login."""
    data      = request.get_json(silent=True) or {}
    token     = data.get('fcm_token')
    user_id   = data.get('user_id')
    role      = data.get('role', 'student')

    if not token or not user_id:
        return jsonify({'success': False, 'error': 'fcm_token and user_id required'}), 400

    db = get_db()
    db.execute('''
        INSERT OR REPLACE INTO fcm_tokens (user_id, role, fcm_token)
        VALUES (?, ?, ?)
    ''', (user_id, role, token))
    db.commit()
    logger.info('FCM token saved for user_id=%s role=%s', user_id, role)
    return jsonify({'success': True})


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

def send_push_to_all_students(title, body, url='/student/dashboard', tag='examguard-student', require_interaction=False):
    """Broadcast a push notification to ALL subscribed students."""
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
            "SELECT * FROM push_subscriptions WHERE role='student'"
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

    # Also send via FCM/Expo to all students
    try:
        from flask import current_app
        with current_app.app_context():
            db = get_db()
            fcm_tokens = db.execute(
                "SELECT fcm_token FROM fcm_tokens WHERE role='student'"
            ).fetchall()
        for row in fcm_tokens:
            token = row['fcm_token']
            if token.startswith('ExponentPushToken'):
                if send_expo_push(token, title, body):
                    sent += 1
    except Exception as e:
        logger.warning('FCM broadcast to students failed: %s', e)

    logger.info('Broadcast to all students: sent=%d errors=%d', sent, errors)
    return sent, errors


def send_expo_push(token, title, body, data=None):
    """Send push via Expo Push API — works with Expo Go during development."""
    import urllib.request, json as _json
    payload = _json.dumps({
        'to': token,
        'title': title,
        'body': body,
        'data': data or {},
        'sound': 'default',
    }).encode()
    req = urllib.request.Request(
        'https://exp.host/--/api/v2/push/send',
        data=payload,
        headers={'Content-Type': 'application/json'},
        method='POST'
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as res:
            result = _json.loads(res.read())
            logger.info('Expo push sent: %s', result)
            return True
    except Exception as e:
        logger.error('Expo push error: %s', e)
        return False
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