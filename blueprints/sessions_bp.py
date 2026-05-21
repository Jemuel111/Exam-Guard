"""
ExamGuard — Sessions blueprint
/api/start_session, /api/analyze_frame, /api/tab_switch,
/api/audio_alert, /api/end_session, /api/status/<id>, /api/sessions
/api/archive_session/<id>

CHANGES:
- /api/sessions excludes archived sessions by default
- /api/archive_session/<id> soft-archives a session
"""
import json
import logging
import time
from datetime import datetime

from flask import Blueprint, request, jsonify, current_app

from auth import get_token_from_request, verify_token
from cv_engine import process_frame
from database import get_db
from session_model import ExamSession
from blueprints.push_bp import send_push_to_teachers, send_fcm_to_teachers
from crypto import encrypt, decrypt
from crypto import encrypt, decrypt

logger      = logging.getLogger(__name__)
bp_sessions = Blueprint('sessions', __name__)

exam_sessions: dict[str, ExamSession] = {}


def _get_session_config(app) -> dict:
    cfg = app.config
    return {
        'NO_FACE_THRESHOLD':    cfg.get('NO_FACE_THRESHOLD',    5),
        'LOOK_AWAY_THRESHOLD':  cfg.get('LOOK_AWAY_THRESHOLD',  3),
        'MULTI_FACE_THRESHOLD': cfg.get('MULTI_FACE_THRESHOLD', 2),
        'WEIGHT_FACE_ABSENCE':  cfg.get('WEIGHT_FACE_ABSENCE',  15),
        'WEIGHT_MULTI_FACE':    cfg.get('WEIGHT_MULTI_FACE',    25),
        'WEIGHT_LOOK_AWAY':     cfg.get('WEIGHT_LOOK_AWAY',     10),
        'WEIGHT_TAB_SWITCH':    cfg.get('WEIGHT_TAB_SWITCH',    20),
        'WEIGHT_AUDIO':         cfg.get('WEIGHT_AUDIO',         12),
        'RISK_LOW_CUTOFF':      cfg.get('RISK_LOW_CUTOFF',      5),
        'RISK_MEDIUM_CUTOFF':   cfg.get('RISK_MEDIUM_CUTOFF',   15),
    }


@bp_sessions.get('/api/sessions')
def get_sessions():
    db    = get_db()
    token = get_token_from_request()
    info  = verify_token(token)
    try:
        is_student = (info and info.get('role') == 'student'
                      and info.get('user_id') and info['user_id'] > 0)
        if is_student:
            rows = db.execute('''
                SELECT s.*, COUNT(v.id) AS vcount,
                       sub.score, sub.max_score, sub.percentage, sub.passed, sub.submitted_at
                FROM sessions s
                LEFT JOIN violations v     ON s.session_id=v.session_id
                LEFT JOIN exam_submissions sub ON sub.session_id=s.session_id
                WHERE s.student_id=? AND COALESCE(s.is_archived,0)=0
                GROUP BY s.session_id
                ORDER BY s.created_at DESC LIMIT 50
            ''', (info['user_id'],)).fetchall()
        else:
            rows = db.execute('''
                SELECT s.*, COUNT(v.id) AS vcount,
                       sub.score, sub.max_score, sub.percentage, sub.passed, sub.submitted_at
                FROM sessions s
                LEFT JOIN violations v     ON s.session_id=v.session_id
                LEFT JOIN exam_submissions sub ON sub.session_id=s.session_id
                WHERE COALESCE(s.is_archived,0)=0
                GROUP BY s.session_id
                ORDER BY s.created_at DESC LIMIT 50
            ''').fetchall()
        sessions = []
        for r in rows:
            s = dict(r)
            s['student_name'] = decrypt(s['student_name']) if s.get('student_name') else ''
            sessions.append(s)
        return jsonify(sessions)
    except Exception as e:
        logger.error('/api/sessions error: %s', e, exc_info=True)
        return jsonify([])


@bp_sessions.post('/api/start_session')
def start_session():
    data         = request.get_json(silent=True) or {}
    student_name = (data.get('student_name') or 'Unknown').strip()
    duration     = int(data.get('duration', 60))
    exam_id      = data.get('exam_id') or None
    student_id   = data.get('student_id') or None

    if not student_id:
        token = get_token_from_request()
        info  = verify_token(token)
        if info and info.get('user_id') and info['user_id'] > 0:
            student_id = info['user_id']

    session_id = f'EG-{int(time.time())}'
    cfg        = _get_session_config(current_app)
    session    = ExamSession(session_id, student_name, duration, exam_id, student_id, cfg)
    session.is_active = True
    exam_sessions[session_id] = session

    db = get_db()
    db.execute('''
        INSERT INTO sessions
        (session_id,student_id,exam_id,student_name,start_time,is_active,stats)
        VALUES (?,?,?,?,?,1,?)
    ''', (session_id, student_id, exam_id, encrypt(student_name),
          datetime.now().strftime('%H:%M:%S'), json.dumps(session.stats)))
    db.commit()
    logger.info('Session started: %s for %s (student_id=%s)', session_id, student_name, student_id)
    return jsonify({'session_id': session_id, 'student_name': student_name})


@bp_sessions.post('/api/analyze_frame')
def analyze():
    data       = request.get_json(silent=True) or {}
    session_id = data.get('session_id')
    session    = exam_sessions.get(session_id)
    if not session:
        return jsonify({'status': 'error', 'message': 'Invalid session'}), 404
    if not session.is_active:
        return jsonify({'status': 'ended'})
    frame = data.get('frame')
    if not frame:
        return jsonify({'status': 'error', 'message': 'No frame'}), 400

    from app import socketio

    # Rebroadcast frame to teacher dashboard for live camera feed
    socketio.emit('live_frame', {
        'session_id':   session_id,
        'student_name': session.student_name,
        'frame':        frame,
    }, room='teachers')

    return jsonify(process_frame(session, frame, socketio))


@bp_sessions.post('/api/tab_switch')
def tab_switch():
    data    = request.get_json(silent=True) or {}
    session = exam_sessions.get(data.get('session_id'))
    if not session:
        return jsonify({'error': 'not found'}), 404

    if session.ended:
        return jsonify({'logged': False, 'reason': 'session ended'})

    from app import socketio
    session.stats['tab_switches'] += 1
    v = session.log_violation('TAB_SWITCH', 'Student switched browser tab or window', 'high', socketio)
    return jsonify({'logged': True, 'violation': v})


@bp_sessions.post('/api/audio_alert')
def audio_alert():
    data    = request.get_json(silent=True) or {}
    session = exam_sessions.get(data.get('session_id'))
    if not session:
        return jsonify({'error': 'not found'}), 404

    if session.ended:
        return jsonify({'logged': False, 'reason': 'session ended'})

    from app import socketio
    level = data.get('level', 0)
    session.stats['audio_alerts'] += 1
    v = session.log_violation('AUDIO_ANOMALY', f'Suspicious audio: {level} dB', 'medium', socketio)
    return jsonify({'logged': True, 'violation': v})


@bp_sessions.post('/api/end_session')
def end_session():
    session_id = (request.get_json(silent=True) or {}).get('session_id')
    session    = exam_sessions.get(session_id)

    if not session:
        return jsonify({'error': 'not found'}), 404

    session.is_active = False
    session.ended     = True
    session.end_time  = datetime.now()
    session.save_reports()

    db = get_db()
    session.save_to_db(db)

    # Auto push to teachers if high risk
    risk = session.compute_risk()

    if risk['level'] == 'High':
        from blueprints.push_bp import send_push_to_teachers

        send_push_to_teachers(
            title='⚠ High Risk Session Detected',
            body=f'{session.student_name} — Risk Score {risk["score"]} · {len(session.violations)} flags',
            url=f'/report/{session_id}',
            tag=f'session-{session_id}',
            require_interaction=True,
        )
        send_fcm_to_teachers(
            title='⚠ High Risk Session Detected',
            body=f'{session.student_name} — Risk Score {risk["score"]} · {len(session.violations)} flags',
            data={'url': f'/report/{session_id}', 'session_id': session_id},
        )

    report = session.generate_report()
    logger.info('Session ended: %s, flags: %d', session_id, len(session.violations))

    return jsonify(report)


@bp_sessions.get('/api/status/<session_id>')
def session_status(session_id):
    session = exam_sessions.get(session_id)
    if not session:
        db  = get_db()
        row = db.execute('SELECT * FROM sessions WHERE session_id=?', (session_id,)).fetchone()
        if row:
            return jsonify({'session_id': session_id, 'is_active': False,
                            'risk_level': row['risk_level'], 'total_violations': row['total_violations']})
        return jsonify({'error': 'not found'}), 404
    return jsonify({
        'session_id':      session_id,
        'is_active':       session.is_active,
        'elapsed':         round(session.get_elapsed()),
        'violation_count': len(session.violations),
        'stats':           session.stats,
        'risk':            session.compute_risk(),
    })