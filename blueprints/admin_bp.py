"""
ExamGuard — Students & Analytics blueprint

CHANGES:
- DELETE /api/students/<id> now soft-archives instead of hard-deletes
- Analytics queries exclude archived items
"""
import sqlite3
import logging
from flask import Blueprint, request, jsonify

from auth import hash_password
from database import get_db

logger   = logging.getLogger(__name__)
bp_admin = Blueprint('admin', __name__)


# ── Students ──────────────────────────────────────────────────────────────────

@bp_admin.get('/api/students')
def get_students():
    db   = get_db()
    rows = db.execute(
        "SELECT id,name,email,student_id,avatar_initials,created_at,last_login FROM users WHERE role='student' AND is_active=1 AND COALESCE(is_archived,0)=0"
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@bp_admin.post('/api/students')
def add_student():
    data = request.get_json(silent=True) or {}
    name  = (data.get('name') or '').strip()
    email = (data.get('email') or '').strip().lower()
    if not name or not email:
        return jsonify({'success': False, 'error': 'Name and email are required'}), 400

    db       = get_db()
    initials = ''.join(p[0].upper() for p in name.split()[:2])
    try:
        db.execute('''INSERT INTO users (email,password_hash,role,name,student_id,avatar_initials)
                      VALUES (?,?,?,?,?,?)''',
                   (email, hash_password(data.get('password', 'student123')),
                    'student', name, data.get('student_id', ''), initials))
        db.commit()
        return jsonify({'success': True})
    except sqlite3.IntegrityError:
        return jsonify({'success': False, 'error': 'Email already exists'}), 409


@bp_admin.delete('/api/students/<int:sid>')
def delete_student(sid):
    """Soft-delete: move to archive instead of permanent deletion."""
    db = get_db()
    db.execute(
        "UPDATE users SET is_archived=1, is_active=0, archived_at=datetime('now') WHERE id=? AND role='student'",
        (sid,)
    )
    db.commit()
    return jsonify({'success': True, 'archived': True})


# ── Analytics ─────────────────────────────────────────────────────────────────

@bp_admin.get('/api/dashboard_stats')
def dashboard_stats():
    from blueprints.sessions_bp import exam_sessions
    db     = get_db()
    active = len([s for s in exam_sessions.values() if s.is_active])
    try:
        total_subs = db.execute('SELECT COUNT(*) FROM exam_submissions').fetchone()[0]
        passed     = db.execute('SELECT COUNT(*) FROM exam_submissions WHERE passed=1').fetchone()[0]
        pass_rate  = round(passed / total_subs * 100, 1) if total_subs else 0
        return jsonify({
            'active_sessions':   active,
            'total_sessions':    db.execute("SELECT COUNT(*) FROM sessions WHERE COALESCE(is_archived,0)=0").fetchone()[0],
            'total_violations':  db.execute('SELECT COUNT(*) FROM violations').fetchone()[0],
            'high_risk':         db.execute("SELECT COUNT(*) FROM sessions WHERE risk_level='High' AND COALESCE(is_archived,0)=0").fetchone()[0],
            'total_students':    db.execute("SELECT COUNT(*) FROM users WHERE role='student' AND is_active=1 AND COALESCE(is_archived,0)=0").fetchone()[0],
            'total_exams':       db.execute("SELECT COUNT(*) FROM exams WHERE COALESCE(is_archived,0)=0").fetchone()[0],
            'total_submissions': total_subs,
            'pass_rate':         pass_rate,
        })
    except Exception as e:
        logger.error('dashboard_stats error: %s', e)
        return jsonify({'active_sessions': active, 'total_sessions': 0,
                        'total_violations': 0, 'high_risk': 0,
                        'total_students': 0, 'total_exams': 0,
                        'total_submissions': 0, 'pass_rate': 0})


@bp_admin.get('/api/analytics/violations_by_type')
def violations_by_type():
    db   = get_db()
    rows = db.execute('SELECT type, COUNT(*) as count FROM violations GROUP BY type ORDER BY count DESC').fetchall()
    return jsonify([dict(r) for r in rows])


@bp_admin.get('/api/analytics/risk_distribution')
def risk_distribution():
    db   = get_db()
    rows = db.execute(
        "SELECT risk_level as level, COUNT(*) as count FROM sessions WHERE is_active=0 AND COALESCE(is_archived,0)=0 GROUP BY risk_level"
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@bp_admin.get('/api/analytics/daily_sessions')
def daily_sessions():
    db   = get_db()
    rows = db.execute('''
        SELECT DATE(created_at) as day, COUNT(*) as count
        FROM sessions WHERE COALESCE(is_archived,0)=0
        GROUP BY day ORDER BY day DESC LIMIT 14
    ''').fetchall()
    return jsonify([dict(r) for r in rows])


@bp_admin.get('/api/notifications')
def get_notifications():
    db   = get_db()
    rows = db.execute("""
        SELECT 'HIGH_RISK_SESSION' as type,
               student_name || ' — High Risk (score: ' || risk_score || ')' as message,
               created_at
        FROM sessions WHERE risk_level='High' AND COALESCE(is_archived,0)=0
        ORDER BY created_at DESC LIMIT 10
    """).fetchall()
    return jsonify([dict(r) for r in rows])