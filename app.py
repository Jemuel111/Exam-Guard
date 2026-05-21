# -*- coding: utf-8 -*-
"""
ExamGuard v2.1 - Main application
Clean factory pattern, blueprints, SocketIO, download routes, report routes.
"""
import json
import logging
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from flask import Flask, render_template, jsonify, send_file, request, redirect

from config import config as app_configs
from database import get_db, close_db, init_db
from blueprints import bp_auth, bp_exams, bp_sessions, bp_admin, bp_archive
from auth import get_token_from_request, verify_token
from crypto import decrypt
from blueprints.push_bp import bp_push, init_push_table


# Logging
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

# App factory
app = Flask(__name__)
env = os.environ.get('FLASK_ENV', 'development')
app.config.from_object(app_configs.get(env, app_configs['default']))

from flask_socketio import SocketIO, join_room, emit
socketio = SocketIO(app, cors_allowed_origins='*', async_mode='threading')

# Register blueprints
app.register_blueprint(bp_auth)
app.register_blueprint(bp_exams)
app.register_blueprint(bp_sessions)
app.register_blueprint(bp_admin)
app.register_blueprint(bp_archive)
app.register_blueprint(bp_push)

# DB teardown
app.teardown_appcontext(close_db)


# Page routes

@app.get('/')
def index():
    return render_template('index.html')


@app.get('/exam')
def exam():
    token = get_token_from_request()
    info  = verify_token(token)
    if not info:
        return redirect('/')
    exam_id = request.args.get('exam_id')
    if exam_id and info.get('user_id') and info['user_id'] > 0:
        db = get_db()
        existing = db.execute(
            'SELECT id FROM exam_submissions WHERE exam_id=? AND student_id=?',
            (exam_id, info['user_id'])
        ).fetchone()
        if existing:
            return redirect('/submitted?session=already&name=' + (info.get('name') or 'Student'))
    return render_template('exam.html')


@app.get('/teacher/dashboard')
def teacher_dashboard():
    token = get_token_from_request()
    info  = verify_token(token)
    if not info or info.get('role') != 'teacher':
        return redirect('/')
    return render_template('teacher_dashboard.html')


@app.get('/student/dashboard')
def student_dashboard():
    token = get_token_from_request()
    info  = verify_token(token)
    if not info or info.get('role') != 'student':
        return redirect('/')
    return render_template('student_dashboard.html')


@app.get('/report/<session_id>')
def report(session_id):
    token = get_token_from_request()
    info  = verify_token(token)
    if not info:
        return redirect('/')

    from blueprints.sessions_bp import exam_sessions
    session = exam_sessions.get(session_id)
    if session:
        return render_template('report.html', report=session.generate_report())

    path = f'reports/{session_id}.json'
    if os.path.exists(path):
        with open(path) as f:
            return render_template('report.html', report=json.load(f))

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

        sub_data = None
        if sub:
            sub_data = dict(sub)
            try:
                sub_data['answers'] = json.loads(sub['answers'] or '{}')
            except (json.JSONDecodeError, TypeError):
                sub_data['answers'] = {}

        report_data = {
            'session_id':       row['session_id'],
            'student_name':     decrypt(row['student_name']) if row['student_name'] else '',
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
            'submission': sub_data,
        }
        return render_template('report.html', report=report_data)

    return 'Session not found', 404


@app.get('/reset-password')
def reset_password_page():
    return render_template('index.html')


@app.get('/archive')
def archive_page():
    token = get_token_from_request()
    info  = verify_token(token)
    if not info or info.get('role') != 'teacher':
        return redirect('/')
    return render_template('archive.html')


@app.get('/static/sw.js')
def service_worker():
    response = app.make_response(app.send_static_file('sw.js'))
    response.headers['Service-Worker-Allowed'] = '/'
    response.headers['Content-Type'] = 'application/javascript'
    return response


@app.get('/manifest.json')
def manifest():
    return app.send_static_file('manifest.json')


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

@app.get('/submitted')
def submitted():
    token = get_token_from_request()
    info  = verify_token(token)
    if not info:
        return redirect('/')
    return render_template('submitted.html')

@app.get('/offline.html')
def offline_page():
    return render_template('offline.html')

@app.get('/push-test')
def push_test():
    token = get_token_from_request()
    info  = verify_token(token)
    if not info or info.get('role') != 'teacher':
        return redirect('/')
    return render_template('push_test.html')


def send_report_email(to_email, student_name, session_id, report):
    if not app.config.get('MAIL_USER'):
        return
    try:
        risk = report['risk_assessment']
        msg  = MIMEMultipart('alternative')
        msg['Subject'] = f'ExamGuard Report - {student_name} - {risk["level"]} Risk'
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


if __name__ == '__main__':
    with app.app_context():
        init_db(app)
        init_push_table(app)
    logger.info('ExamGuard v2.1 starting at http://localhost:5000')
    socketio.run(app, debug=False,
             host='0.0.0.0', port=5000,
             allow_unsafe_werkzeug=True)