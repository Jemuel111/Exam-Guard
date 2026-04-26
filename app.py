"""
ExamGuard - AI-Powered Online Exam Monitoring System
Backend: Flask + OpenCV + SQLite (persistent storage)
PWA-enabled with service worker support
"""

import cv2
import numpy as np
import json
import time
import base64
import os
import csv
import logging
import sqlite3
from datetime import datetime
from flask import Flask, render_template, request, jsonify, send_file, g
from flask_socketio import SocketIO, emit, join_room

# ─── Logging ──────────────────────────────────────────────────────────────────
os.makedirs('logs', exist_ok=True)
os.makedirs('reports', exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler('logs/examguard.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ─── Flask Setup ──────────────────────────────────────────────────────────────
app = Flask(__name__)
app.config['SECRET_KEY'] = 'examguard-secret-2024'
app.config['DATABASE'] = 'instance/examguard.db'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

# ─── OpenCV Setup ─────────────────────────────────────────────────────────────
face_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_frontalface_default.xml')
eye_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_eye.xml')

# ─── In-memory active sessions ────────────────────────────────────────────────
exam_sessions = {}


# ─── Database ─────────────────────────────────────────────────────────────────
def get_db():
    if 'db' not in g:
        os.makedirs('instance', exist_ok=True)
        g.db = sqlite3.connect(
            app.config['DATABASE'],
            detect_types=sqlite3.PARSE_DECLTYPES
        )
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(e=None):
    db = g.pop('db', None)
    if db is not None:
        db.close()


def init_db():
    os.makedirs('instance', exist_ok=True)
    conn = sqlite3.connect(app.config['DATABASE'])
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    c.executescript('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('teacher','student')),
            name TEXT NOT NULL,
            student_id TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS exams (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            subject TEXT,
            duration_minutes INTEGER DEFAULT 60,
            passing_score INTEGER DEFAULT 75,
            instructions TEXT,
            status TEXT DEFAULT 'draft' CHECK(status IN ('draft','active','ended')),
            created_by INTEGER REFERENCES users(id),
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS questions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            exam_id INTEGER REFERENCES exams(id) ON DELETE CASCADE,
            question_text TEXT NOT NULL,
            question_type TEXT DEFAULT 'mc' CHECK(question_type IN ('mc','tf','essay')),
            choices TEXT,
            correct_answer INTEGER,
            order_num INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS exam_enrollments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            exam_id INTEGER REFERENCES exams(id),
            student_id INTEGER REFERENCES users(id),
            assigned_at TEXT DEFAULT (datetime('now')),
            UNIQUE(exam_id, student_id)
        );

        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT UNIQUE NOT NULL,
            student_id INTEGER REFERENCES users(id),
            exam_id INTEGER REFERENCES exams(id),
            student_name TEXT,
            start_time TEXT,
            end_time TEXT,
            duration_minutes REAL DEFAULT 0,
            total_violations INTEGER DEFAULT 0,
            risk_level TEXT DEFAULT 'Low',
            risk_score REAL DEFAULT 0,
            stats TEXT DEFAULT '{}',
            is_active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS violations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT REFERENCES sessions(session_id),
            timestamp TEXT,
            elapsed_seconds REAL,
            type TEXT,
            details TEXT,
            severity TEXT
        );
    ''')

    # Seed demo users
    demo_users = [
        ('teacher@school.edu', 'teacher123', 'teacher', 'Ms. Santos', None),
        ('student@school.edu', 'student123', 'student', 'Juan dela Cruz', 'STU-001'),
        ('maria@school.edu', 'student123', 'student', 'Maria Santos', 'STU-002'),
        ('pedro@school.edu', 'student123', 'student', 'Pedro Reyes', 'STU-003'),
        ('ana@school.edu', 'student123', 'student', 'Ana Garcia', 'STU-004'),
    ]
    for u in demo_users:
        try:
            c.execute('INSERT INTO users (email,password,role,name,student_id) VALUES (?,?,?,?,?)', u)
        except sqlite3.IntegrityError:
            pass

    # Seed demo exam
    c.execute('SELECT id FROM exams WHERE title=?', ('Algebra Finals',))
    if not c.fetchone():
        c.execute('''INSERT INTO exams (title,subject,duration_minutes,passing_score,instructions,status,created_by)
                     VALUES (?,?,?,?,?,?,1)''',
                  ('Algebra Finals', 'Mathematics', 60, 75,
                   'Answer all questions carefully. You have 60 minutes.', 'active'))
        exam_id = c.lastrowid

        questions_data = [
            (exam_id, 'What is the quadratic formula?', 'mc',
             json.dumps(['x = (-b ± √(b²-4ac)) / 2a', 'x = -b/2a', 'x = b²-4ac', 'x = 2a/b']), 0, 1),
            (exam_id, 'Simplify: 3x + 2x', 'mc',
             json.dumps(['5x', '6x', 'x⁵', '5x²']), 0, 2),
            (exam_id, 'What is the slope-intercept form?', 'mc',
             json.dumps(['y = mx + b', 'y = ax² + bx + c', 'y = x/m + b', 'y = m/x']), 0, 3),
            (exam_id, 'The sum of angles in a triangle is 180°.', 'tf',
             json.dumps(['True', 'False']), 0, 4),
            (exam_id, 'Explain the Pythagorean theorem in your own words.', 'essay', None, None, 5),
        ]
        c.executemany(
            'INSERT INTO questions (exam_id,question_text,question_type,choices,correct_answer,order_num) VALUES (?,?,?,?,?,?)',
            questions_data
        )

        # Enroll demo students
        for sid in [2, 3, 4, 5]:
            try:
                c.execute('INSERT INTO exam_enrollments (exam_id,student_id) VALUES (?,?)', (exam_id, sid))
            except sqlite3.IntegrityError:
                pass

    conn.commit()
    conn.close()
    logger.info("Database initialized")


# ─── Exam Session Class ───────────────────────────────────────────────────────
class ExamSession:
    def __init__(self, session_id, student_name, exam_duration=60):
        self.session_id = session_id
        self.student_name = student_name
        self.exam_duration = exam_duration
        self.start_time = datetime.now()
        self.end_time = None
        self.violations = []
        self.is_active = False
        self.no_face_start = None
        self.look_away_start = None
        self.multi_face_start = None
        self._no_face_logged = False
        self._look_away_logged = False
        self._multi_face_logged = False
        self.stats = {
            'total_frames': 0,
            'no_face_frames': 0,
            'multiple_face_frames': 0,
            'look_away_frames': 0,
            'tab_switches': 0,
            'face_absence_events': 0,
            'multiple_face_events': 0,
            'look_away_events': 0,
        }
        self.NO_FACE_THRESHOLD = 5
        self.LOOK_AWAY_THRESHOLD = 3
        self.MULTI_FACE_THRESHOLD = 2

    def log_violation(self, violation_type, details="", severity="medium"):
        elapsed = self.get_elapsed()
        entry = {
            'timestamp': datetime.now().strftime('%H:%M:%S'),
            'elapsed_seconds': round(elapsed, 1),
            'type': violation_type,
            'details': details,
            'severity': severity
        }
        self.violations.append(entry)
        logger.warning(f"[{self.session_id}] VIOLATION: {violation_type} — {details}")
        return entry

    def get_elapsed(self):
        return (datetime.now() - self.start_time).total_seconds()

    def compute_risk(self):
        s = self.stats
        duration_min = max(self.get_elapsed() / 60, 1)
        raw = (
            s['face_absence_events'] * 15 +
            s['multiple_face_events'] * 25 +
            s['look_away_events'] * 10 +
            s['tab_switches'] * 20
        )
        score = raw / duration_min
        if score < 5:
            return {'level': 'Low', 'score': round(score, 1), 'color': '#22c55e'}
        elif score < 15:
            return {'level': 'Medium', 'score': round(score, 1), 'color': '#f59e0b'}
        else:
            return {'level': 'High', 'score': round(score, 1), 'color': '#ef4444'}

    def generate_report(self):
        end = self.end_time or datetime.now()
        duration_sec = (end - self.start_time).total_seconds()
        return {
            'session_id': self.session_id,
            'student_name': self.student_name,
            'exam_date': self.start_time.strftime('%Y-%m-%d'),
            'start_time': self.start_time.strftime('%H:%M:%S'),
            'end_time': end.strftime('%H:%M:%S'),
            'duration_minutes': round(duration_sec / 60, 2),
            'total_violations': len(self.violations),
            'violations': self.violations,
            'stats': self.stats,
            'risk_assessment': self.compute_risk()
        }

    def save_reports(self):
        report = self.generate_report()
        sid = self.session_id
        json_path = f'reports/{sid}.json'
        with open(json_path, 'w') as f:
            json.dump(report, f, indent=2)
        csv_path = f'reports/{sid}_violations.csv'
        with open(csv_path, 'w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=['timestamp', 'elapsed_seconds', 'type', 'details', 'severity'])
            writer.writeheader()
            writer.writerows(self.violations)
        logger.info(f"[{sid}] Reports saved")
        return json_path, csv_path

    def save_to_db(self, db):
        report = self.generate_report()
        risk = report['risk_assessment']
        db.execute('''
            INSERT OR REPLACE INTO sessions
            (session_id, student_name, start_time, end_time, duration_minutes,
             total_violations, risk_level, risk_score, stats, is_active)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        ''', (
            self.session_id, self.student_name,
            report['start_time'], report['end_time'],
            report['duration_minutes'], len(self.violations),
            risk['level'], risk['score'],
            json.dumps(self.stats), 0
        ))
        for v in self.violations:
            db.execute('''
                INSERT OR IGNORE INTO violations
                (session_id, timestamp, elapsed_seconds, type, details, severity)
                VALUES (?,?,?,?,?,?)
            ''', (self.session_id, v['timestamp'], v['elapsed_seconds'],
                  v['type'], v['details'], v['severity']))
        db.commit()


# ─── CV Helpers ───────────────────────────────────────────────────────────────
def check_lighting(frame):
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    brightness = np.mean(gray)
    contrast = np.std(gray)
    return brightness > 40 and contrast > 10, round(float(brightness), 1), round(float(contrast), 1)


def analyze_gaze(frame, face_rect):
    try:
        x, y, w, h = face_rect
        frame_h, frame_w = frame.shape[:2]
        face_center_x = x + w // 2
        face_center_y = y + h // 2
        h_offset = abs(face_center_x - frame_w // 2) / frame_w
        v_offset = (face_center_y - frame_h // 2) / frame_h
        if h_offset > 0.35 or v_offset < -0.25:
            return True
        face_roi_gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)[y:y+h, x:x+w]
        eyes = eye_cascade.detectMultiScale(face_roi_gray, scaleFactor=1.1, minNeighbors=5, minSize=(20, 20))
        if len(eyes) == 0:
            return True
        return False
    except Exception:
        return False


def detect_faces(frame):
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    faces = face_cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(80, 80))
    return faces if len(faces) > 0 else []


def process_frame(session: ExamSession, frame_b64: str):
    try:
        header, encoded = frame_b64.split(',', 1)
        img_data = base64.b64decode(encoded)
        nparr = np.frombuffer(img_data, np.uint8)
        frame = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        if frame is None:
            return {'status': 'error', 'message': 'Could not decode frame'}

        alerts = []
        session.stats['total_frames'] += 1
        now = time.time()

        ok_light, brightness, contrast = check_lighting(frame)
        if not ok_light:
            alerts.append({'type': 'POOR_LIGHTING', 'brightness': brightness, 'contrast': contrast})

        faces = detect_faces(frame)
        face_count = len(faces)

        if face_count == 0:
            session.stats['no_face_frames'] += 1
            if session.no_face_start is None:
                session.no_face_start = now
            elapsed_no_face = now - session.no_face_start
            if elapsed_no_face >= session.NO_FACE_THRESHOLD and not session._no_face_logged:
                v = session.log_violation('NO_FACE', f'No face detected for {round(elapsed_no_face)}s', 'high')
                session.stats['face_absence_events'] += 1
                session._no_face_logged = True
                alerts.append({'type': 'NO_FACE', 'violation': v})
            else:
                alerts.append({'type': 'NO_FACE'})
        else:
            session.no_face_start = None
            session._no_face_logged = False

        if face_count > 1:
            session.stats['multiple_face_frames'] += 1
            if session.multi_face_start is None:
                session.multi_face_start = now
            elapsed_multi = now - session.multi_face_start
            if elapsed_multi >= session.MULTI_FACE_THRESHOLD and not session._multi_face_logged:
                v = session.log_violation('MULTIPLE_FACES', f'{face_count} faces detected', 'critical')
                session.stats['multiple_face_events'] += 1
                session._multi_face_logged = True
                alerts.append({'type': 'MULTIPLE_FACES', 'count': face_count, 'violation': v})
            else:
                alerts.append({'type': 'MULTIPLE_FACES', 'count': face_count})
        else:
            session.multi_face_start = None
            session._multi_face_logged = False

        if face_count == 1:
            looking_away = analyze_gaze(frame, faces[0])
            if looking_away:
                session.stats['look_away_frames'] += 1
                if session.look_away_start is None:
                    session.look_away_start = now
                elapsed_look = now - session.look_away_start
                if elapsed_look >= session.LOOK_AWAY_THRESHOLD and not session._look_away_logged:
                    v = session.log_violation('LOOK_AWAY', f'Looking away for {round(elapsed_look)}s', 'medium')
                    session.stats['look_away_events'] += 1
                    session._look_away_logged = True
                    alerts.append({'type': 'LOOK_AWAY', 'violation': v})
                else:
                    alerts.append({'type': 'LOOK_AWAY'})
            else:
                session.look_away_start = None
                session._look_away_logged = False

        return {
            'status': 'ok',
            'alerts': alerts,
            'violation_count': len(session.violations),
            'stats': session.stats,
            'risk': session.compute_risk(),
            'elapsed': round(session.get_elapsed())
        }
    except Exception as e:
        logger.error(f"Frame processing error: {e}")
        return {'status': 'error', 'message': str(e)}


# ─── Routes ───────────────────────────────────────────────────────────────────
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/exam')
def exam():
    return render_template('exam.html')

@app.route('/report/<session_id>')
def report(session_id):
    session = exam_sessions.get(session_id)
    if session:
        return render_template('report.html', report=session.generate_report())
    json_path = f'reports/{session_id}.json'
    if os.path.exists(json_path):
        with open(json_path) as f:
            return render_template('report.html', report=json.load(f))
    # Try DB
    db = get_db()
    row = db.execute('SELECT * FROM sessions WHERE session_id=?', (session_id,)).fetchone()
    if row:
        viols = db.execute('SELECT * FROM violations WHERE session_id=? ORDER BY elapsed_seconds',
                           (session_id,)).fetchall()
        report_data = {
            'session_id': row['session_id'],
            'student_name': row['student_name'],
            'exam_date': row['created_at'][:10],
            'start_time': row['start_time'],
            'end_time': row['end_time'],
            'duration_minutes': row['duration_minutes'],
            'total_violations': row['total_violations'],
            'violations': [dict(v) for v in viols],
            'stats': json.loads(row['stats'] or '{}'),
            'risk_assessment': {'level': row['risk_level'], 'score': row['risk_score'],
                                'color': '#22c55e' if row['risk_level'] == 'Low' else '#f59e0b' if row['risk_level'] == 'Medium' else '#ef4444'}
        }
        return render_template('report.html', report=report_data)
    return "Session not found", 404

@app.route('/teacher/dashboard')
def teacher_dashboard():
    return render_template('teacher_dashboard.html')

@app.route('/student/dashboard')
def student_dashboard():
    return render_template('student_dashboard.html')

@app.route('/static/sw.js')
def service_worker():
    response = app.make_response(app.send_static_file('sw.js'))
    response.headers['Service-Worker-Allowed'] = '/'
    response.headers['Content-Type'] = 'application/javascript'
    return response

@app.route('/manifest.json')
def manifest():
    return app.send_static_file('manifest.json')


# ─── API ──────────────────────────────────────────────────────────────────────
@app.route('/api/login', methods=['POST'])
def login():
    data = request.json or {}
    email = data.get('email', '').lower().strip()
    password = data.get('password', '')
    role = data.get('role', 'student')
    is_register = data.get('register', False)

    db = get_db()

    if is_register:
        name = data.get('name', email.split('@')[0].title())
        student_id = data.get('student_id', '')
        try:
            db.execute('INSERT INTO users (email,password,role,name,student_id) VALUES (?,?,?,?,?)',
                       (email, password, role, name, student_id))
            db.commit()
            return jsonify({'success': True, 'role': role, 'name': name})
        except sqlite3.IntegrityError:
            return jsonify({'success': False, 'error': 'Email already registered.'})

    user = db.execute('SELECT * FROM users WHERE email=? AND password=? AND role=?',
                      (email, password, role)).fetchone()
    if user:
        return jsonify({'success': True, 'role': role, 'name': user['name'],
                        'user_id': user['id']})
    # Demo fallback
    return jsonify({'success': True, 'role': role})


@app.route('/api/students', methods=['GET'])
def get_students():
    db = get_db()
    students = db.execute("SELECT id,name,email,student_id,created_at FROM users WHERE role='student'").fetchall()
    return jsonify([dict(s) for s in students])


@app.route('/api/students', methods=['POST'])
def add_student():
    data = request.json or {}
    db = get_db()
    try:
        db.execute('INSERT INTO users (email,password,role,name,student_id) VALUES (?,?,?,?,?)',
                   (data['email'], data.get('password', 'student123'), 'student',
                    data['name'], data.get('student_id', '')))
        db.commit()
        return jsonify({'success': True})
    except sqlite3.IntegrityError:
        return jsonify({'success': False, 'error': 'Email already exists'}), 400


@app.route('/api/students/<int:student_id>', methods=['DELETE'])
def delete_student(student_id):
    db = get_db()
    db.execute('DELETE FROM users WHERE id=? AND role="student"', (student_id,))
    db.commit()
    return jsonify({'success': True})


@app.route('/api/exams', methods=['GET'])
def get_exams():
    db = get_db()
    exams = db.execute('SELECT * FROM exams ORDER BY created_at DESC').fetchall()
    result = []
    for e in exams:
        ex = dict(e)
        qcount = db.execute('SELECT COUNT(*) FROM questions WHERE exam_id=?', (e['id'],)).fetchone()[0]
        ex['question_count'] = qcount
        result.append(ex)
    return jsonify(result)


@app.route('/api/exams', methods=['POST'])
def create_exam():
    data = request.json or {}
    db = get_db()
    c = db.execute('''INSERT INTO exams (title,subject,duration_minutes,passing_score,instructions,status,created_by)
                      VALUES (?,?,?,?,?,?,1)''',
                   (data.get('title', 'Untitled'), data.get('subject', ''),
                    data.get('duration_minutes', 60), data.get('passing_score', 75),
                    data.get('instructions', ''), data.get('status', 'draft')))
    exam_id = c.lastrowid
    for i, q in enumerate(data.get('questions', [])):
        db.execute('''INSERT INTO questions (exam_id,question_text,question_type,choices,correct_answer,order_num)
                      VALUES (?,?,?,?,?,?)''',
                   (exam_id, q['text'], q.get('type', 'mc'),
                    json.dumps(q.get('choices', [])), q.get('correct_answer', 0), i + 1))
    db.commit()
    return jsonify({'success': True, 'exam_id': exam_id})


@app.route('/api/exams/<int:exam_id>', methods=['DELETE'])
def delete_exam(exam_id):
    db = get_db()
    db.execute('DELETE FROM exams WHERE id=?', (exam_id,))
    db.commit()
    return jsonify({'success': True})


@app.route('/api/sessions', methods=['GET'])
def get_sessions():
    db = get_db()
    rows = db.execute('''SELECT s.*, COUNT(v.id) as vcount
                         FROM sessions s
                         LEFT JOIN violations v ON s.session_id=v.session_id
                         GROUP BY s.session_id
                         ORDER BY s.created_at DESC LIMIT 50''').fetchall()
    return jsonify([dict(r) for r in rows])


@app.route('/api/dashboard_stats', methods=['GET'])
def dashboard_stats():
    db = get_db()
    active = len([s for s in exam_sessions.values() if s.is_active])
    total_sessions = db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0]
    total_violations = db.execute('SELECT COUNT(*) FROM violations').fetchone()[0]
    high_risk = db.execute("SELECT COUNT(*) FROM sessions WHERE risk_level='High'").fetchone()[0]
    total_students = db.execute("SELECT COUNT(*) FROM users WHERE role='student'").fetchone()[0]
    total_exams = db.execute("SELECT COUNT(*) FROM exams").fetchone()[0]
    return jsonify({
        'active_sessions': active,
        'total_sessions': total_sessions,
        'total_violations': total_violations,
        'high_risk': high_risk,
        'total_students': total_students,
        'total_exams': total_exams
    })


@app.route('/api/start_session', methods=['POST'])
def start_session():
    data = request.json or {}
    student_name = data.get('student_name', 'Unknown Student')
    duration = data.get('duration', 60)
    session_id = f"EG-{int(time.time())}"
    session = ExamSession(session_id, student_name, duration)
    session.is_active = True
    exam_sessions[session_id] = session

    db = get_db()
    db.execute('''INSERT INTO sessions (session_id, student_name, start_time, is_active, stats)
                  VALUES (?,?,?,1,?)''',
               (session_id, student_name, datetime.now().strftime('%H:%M:%S'),
                json.dumps(session.stats)))
    db.commit()
    logger.info(f"Session started: {session_id} for {student_name}")
    return jsonify({'session_id': session_id, 'student_name': student_name})


@app.route('/api/analyze_frame', methods=['POST'])
def analyze():
    data = request.json or {}
    session_id = data.get('session_id')
    session = exam_sessions.get(session_id)
    if not session:
        return jsonify({'status': 'error', 'message': 'Invalid session'}), 404
    if not session.is_active:
        return jsonify({'status': 'error', 'message': 'Session ended'}), 400
    frame = data.get('frame')
    if not frame:
        return jsonify({'status': 'error', 'message': 'No frame provided'}), 400
    return jsonify(process_frame(session, frame))


@app.route('/api/tab_switch', methods=['POST'])
def tab_switch():
    data = request.json or {}
    session = exam_sessions.get(data.get('session_id'))
    if not session:
        return jsonify({'error': 'not found'}), 404
    session.stats['tab_switches'] += 1
    v = session.log_violation('TAB_SWITCH', 'Student switched browser tab or window', 'high')
    socketio.emit('violation', {'violation': v}, room=data.get('session_id'))
    return jsonify({'logged': True, 'violation': v})


@app.route('/api/end_session', methods=['POST'])
def end_session():
    session_id = (request.json or {}).get('session_id')
    session = exam_sessions.get(session_id)
    if not session:
        return jsonify({'error': 'not found'}), 404
    session.is_active = False
    session.end_time = datetime.now()
    session.save_reports()
    db = get_db()
    session.save_to_db(db)
    report = session.generate_report()
    logger.info(f"Session ended: {session_id}, violations: {len(session.violations)}")
    return jsonify(report)


@app.route('/api/status/<session_id>')
def session_status(session_id):
    session = exam_sessions.get(session_id)
    if not session:
        return jsonify({'error': 'not found'}), 404
    return jsonify({
        'session_id': session_id,
        'is_active': session.is_active,
        'elapsed': round(session.get_elapsed()),
        'violation_count': len(session.violations),
        'stats': session.stats,
        'risk': session.compute_risk()
    })


@app.route('/api/download_report/<session_id>')
def download_report(session_id):
    json_path = f'reports/{session_id}.json'
    if not os.path.exists(json_path):
        session = exam_sessions.get(session_id)
        if session:
            session.save_reports()
        else:
            return jsonify({'error': 'not found'}), 404
    return send_file(json_path, as_attachment=True, download_name=f'{session_id}_report.json')


@app.route('/api/download_csv/<session_id>')
def download_csv(session_id):
    csv_path = f'reports/{session_id}_violations.csv'
    if not os.path.exists(csv_path):
        session = exam_sessions.get(session_id)
        if session:
            session.save_reports()
        else:
            return jsonify({'error': 'not found'}), 404
    return send_file(csv_path, as_attachment=True, download_name=f'{session_id}_violations.csv')


# ─── SocketIO ────────────────────────────────────────────────────────────────
@socketio.on('join')
def on_join(data):
    room = data.get('session_id')
    if room:
        join_room(room)
        emit('joined', {'room': room})


# ─── Run ─────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    init_db()
    logger.info("ExamGuard server starting at http://localhost:5000")
    socketio.run(app, debug=True, host='0.0.0.0', port=5000)