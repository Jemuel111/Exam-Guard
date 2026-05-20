"""
ExamGuard — Archive blueprint
Soft-delete management for exams, students, and sessions.
Items go to archive instead of permanent deletion.
Teachers can restore or permanently delete from archive.
"""
import logging
from flask import Blueprint, request, jsonify
from database import get_db
from crypto import decrypt

logger = logging.getLogger(__name__)
bp_archive = Blueprint('archive', __name__)


# ── Archive list ──────────────────────────────────────────────────────────────

@bp_archive.get('/api/archive')
def get_archive():
    db = get_db()
    try:
        exams = db.execute(
            "SELECT id, title, subject, duration_minutes, status, created_at, archived_at, 'exam' as item_type FROM exams WHERE is_archived=1 ORDER BY archived_at DESC"
        ).fetchall()
        students_raw = db.execute(
            "SELECT id, name, email, student_id, created_at, archived_at, 'student' as item_type FROM users WHERE role='student' AND is_archived=1 ORDER BY archived_at DESC"
        ).fetchall()
        sessions_raw = db.execute(
            "SELECT session_id as id, student_name as name, start_time as created_at, archived_at, 'session' as item_type, risk_level, total_violations FROM sessions WHERE is_archived=1 ORDER BY archived_at DESC"
        ).fetchall()
        # Decrypt student fields
        students = []
        for r in students_raw:
            s = dict(r)
            s['name']       = decrypt(s['name'])
            s['email']      = decrypt(s['email'])
            s['student_id'] = decrypt(s['student_id']) if s['student_id'] else ''
            students.append(s)
        # Decrypt session student_name
        sessions = []
        for r in sessions_raw:
            s = dict(r)
            s['name'] = decrypt(s['name'])
            sessions.append(s)
        return jsonify({
            'exams': [dict(r) for r in exams],
            'students': students,
            'sessions': sessions,
        })
    except Exception as e:
        logger.error('get_archive error: %s', e)
        return jsonify({'exams': [], 'students': [], 'sessions': []})


# ── Restore ───────────────────────────────────────────────────────────────────

@bp_archive.post('/api/archive/restore')
def restore_item():
    data = request.get_json(silent=True) or {}
    item_type = data.get('type')
    item_id = data.get('id')
    db = get_db()
    try:
        if item_type == 'exam':
            db.execute("UPDATE exams SET is_archived=0, archived_at=NULL WHERE id=?", (item_id,))
        elif item_type == 'student':
            db.execute("UPDATE users SET is_archived=0, archived_at=NULL, is_active=1 WHERE id=?", (item_id,))
        elif item_type == 'session':
            db.execute("UPDATE sessions SET is_archived=0, archived_at=NULL WHERE session_id=?", (item_id,))
        db.commit()
        return jsonify({'success': True})
    except Exception as e:
        logger.error('restore_item error: %s', e)
        return jsonify({'success': False, 'error': str(e)}), 500


# ── Permanent delete ──────────────────────────────────────────────────────────

@bp_archive.delete('/api/archive/<item_type>/<item_id>')
def permanent_delete(item_type, item_id):
    db = get_db()
    try:
        if item_type == 'exam':
            db.execute("DELETE FROM exams WHERE id=? AND is_archived=1", (item_id,))
        elif item_type == 'student':
            db.execute("DELETE FROM users WHERE id=? AND is_archived=1 AND role='student'", (item_id,))
        elif item_type == 'session':
            db.execute("DELETE FROM sessions WHERE session_id=? AND is_archived=1", (item_id,))
        db.commit()
        return jsonify({'success': True})
    except Exception as e:
        logger.error('permanent_delete error: %s', e)
        return jsonify({'success': False, 'error': str(e)}), 500


# ── Archive count (for badge) ─────────────────────────────────────────────────

@bp_archive.get('/api/archive/count')
def archive_count():
    db = get_db()
    try:
        exams = db.execute("SELECT COUNT(*) FROM exams WHERE is_archived=1").fetchone()[0]
        students = db.execute("SELECT COUNT(*) FROM users WHERE role='student' AND is_archived=1").fetchone()[0]
        sessions = db.execute("SELECT COUNT(*) FROM sessions WHERE is_archived=1").fetchone()[0]
        return jsonify({'total': exams + students + sessions, 'exams': exams, 'students': students, 'sessions': sessions})
    except Exception as e:
        return jsonify({'total': 0, 'exams': 0, 'students': 0, 'sessions': 0})