"""
ExamGuard v3.0 — Main application
Clean factory pattern, blueprints, SocketIO, download routes, report routes.
"""
import json
import logging
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from flask import Flask, render_template, jsonify, send_file, request
from flask_socketio import SocketIO, join_room, emit

from config import config as app_configs
from database import get_db, close_db, init_db
from blueprints import bp_auth, bp_exams, bp_sessions, bp_admin

# ── Logging ───────────────────────────────────────────────────────────────────
os.makedirs('logs',    exist_ok=True)
os.makedirs('reports', exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler('logs/examguard.log'),
        logging.StreamHandler(),
    ]
)
logger = logging.getLogger(__name__)

# ── App factory ───────────────────────────────────────────────────────────────
app = Flask(__name__)
env = os.environ.get('FLASK_ENV', 'development')
app.config.from_object(app_configs.get(env, app_configs['default']))

socketio = SocketIO(app, cors_allowed_origins='*', async_mode='threading')

# Register blueprints
app.register_blueprint(bp_auth)
app.register_blueprint(bp_exams)
app.register_blueprint(bp_sessions)
app.register_blueprint(bp_admin)

# DB teardown
app.teardown_appcontext(close_db)


# ── Page routes ───────────────────────────────────────────────────────────────

@app.get('/')
def index():
    return render_template('index.html')


@app.get('/exam')
def exam():
    return render_template('exam.html')


@app.get('/teacher/dashboard')
def teacher_dashboard():
    return render_template('teacher_dashboard.html')


@app.get('/student/dashboard')
def student_dashboard():
    return render_template('student_dashboard.html')


@app.get('/report/<session_id>')
def report(session_id):
    from blueprints.sessions_bp import exam_sessions
    # Check in-memory first
    session = exam_sessions.get(session_id)
    if session:
        return render_template('report.html', report=session.generate_report())

    # Check saved JSON
    path = f'reports/{session_id}.json'
    if os.path.exists(path):
        with open(path) as f:
            return render_template('report.html', report=json.load(f))

    # DB fallback
    db  = get_db()
    row = db.execute('SELECT * FROM sessions WHERE session_id=?', (session_id,)).fetchone()
    if row:
        viols = db.execute(
            'SELECT * FROM violations WHERE session_id=? ORDER BY elapsed_seconds',
            (session_id,)
        ).fetchall()
        sub = db.execute(
            'SELECT * FROM exam_submissions WHERE session_id=?', (session_id,)
        ).fetchone()
        report_data = {
            'session_id':       row['session_id'],
            'student_name':     row['student_name'],
            'exam_date':        row['created_at'][:10],
            'start_time':       row['start_time'],
            'end_time':         row['end_time'],
            'duration_minutes': row['duration_minutes'],
            'total_violations': row['total_violations'],
            'violations':       [dict(v) for v in viols],
            'stats':            json.loads(row['stats'] or '{}'),
            'risk_assessment': {
                'level': row['risk_level'],
                'score': row['risk_score'],
                'color': ('#22c55e' if row['risk_level'] == 'Low'
                          else '#f59e0b' if row['risk_level'] == 'Medium'
                          else '#ef4444'),
            },
            'submission': dict(sub) if sub else None,
        }
        return render_template('report.html', report=report_data)

    return 'Session not found', 404


# ── Static assets ─────────────────────────────────────────────────────────────

@app.get('/static/sw.js')
def service_worker():
    return app.send_static_file('sw.js')


@app.get('/manifest.json')
def manifest():
    return app.send_static_file('manifest.json')


# ── Downloads ─────────────────────────────────────────────────────────────────

@app.get('/api/download_report/<session_id>')
def download_report(session_id):
    from blueprints.sessions_bp import exam_sessions
    path = f'reports/{session_id}.json'
    if not os.path.exists(path):
        s = exam_sessions.get(session_id)
        if s:
            s.save_reports()
        else:
            return jsonify({'error': 'not found'}), 404
    return send_file(path, as_attachment=True,
                     download_name=f'{session_id}_report.json')


@app.get('/api/download_csv/<session_id>')
def download_csv(session_id):
    from blueprints.sessions_bp import exam_sessions
    path = f'reports/{session_id}_violations.csv'
    if not os.path.exists(path):
        s = exam_sessions.get(session_id)
        if s:
            s.save_reports()
        else:
            return jsonify({'error': 'not found'}), 404
    return send_file(path, as_attachment=True,
                     download_name=f'{session_id}_violations.csv')


# ── Email helper ──────────────────────────────────────────────────────────────

def send_report_email(to_email: str, student_name: str, session_id: str, report: dict):
    if not app.config.get('MAIL_USER'):
        return
    try:
        risk = report['risk_assessment']
        msg  = MIMEMultipart('alternative')
        msg['Subject'] = f'ExamGuard Report — {student_name} — {risk["level"]} Risk'
        msg['From']    = app.config['MAIL_USER']
        msg['To']      = to_email
        body = f"""
        <h2>ExamGuard Session Report</h2>
        <p><strong>Student:</strong> {student_name}</p>
        <p><strong>Session:</strong> {session_id}</p>
        <p><strong>Risk Level:</strong> {risk['level']} (score: {risk['score']})</p>
        <p><strong>Total Flags:</strong> {report['total_violations']}</p>
        <p>Review the full report in your ExamGuard portal.</p>
        <hr><small>This is a decision-support tool. Human review required.</small>
        """
        msg.attach(MIMEText(body, 'html'))
        with smtplib.SMTP(app.config['MAIL_SERVER'], app.config['MAIL_PORT']) as s:
            s.starttls()
            s.login(app.config['MAIL_USER'], app.config['MAIL_PASS'])
            s.send_message(msg)
    except Exception as e:
        logger.warning('Email send failed: %s', e)


# ── SocketIO ──────────────────────────────────────────────────────────────────

@socketio.on('join')
def on_join(data):
    room = data.get('session_id') or data.get('room')
    if room:
        join_room(room)
        emit('joined', {'room': room})


@socketio.on('join_teacher')
def on_join_teacher(data):
    join_room('teachers')
    emit('joined', {'room': 'teachers'})


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == '__main__':
    with app.app_context():
        init_db(app)
    logger.info('ExamGuard v3.0 starting at http://localhost:5000')
    socketio.run(app, debug=app.config.get('DEBUG', True),
                 host='0.0.0.0', port=5000)