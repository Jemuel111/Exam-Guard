"""
ExamGuard v2.1 — AI-Powered Online Exam Monitoring System
Fixes in v2.1:
  - /api/student/exams 500 error (bad ORDER BY + missing error handling)
  - Phantom TAB_SWITCH violation logged when navigating to report page
  - student_id never passed from frontend → sessions orphaned
  - /api/sessions leaking all sessions to students (role-scoped now)
  - Auto-grading returning 0% (question ID key mismatch: int vs str)
  - Exam submissions not joined into /api/sessions response for student dashboard
  - Missing mediapipe fallback when not installed (graceful degradation)
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
import hashlib
import secrets
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify, send_file, g
from flask_socketio import SocketIO, emit, join_room

# ── Optional MediaPipe ──────────────────────────────────────────────────────
try:
    import mediapipe as mp
    _mp_face = mp.solutions.face_detection.FaceDetection(
        model_selection=0, min_detection_confidence=0.5
    )
    _mp_mesh = mp.solutions.face_mesh.FaceMesh(
        static_image_mode=False, max_num_faces=3,
        refine_landmarks=True, min_detection_confidence=0.5
    )
    USE_MEDIAPIPE = True
except ImportError:
    USE_MEDIAPIPE = False

# ── Logging ─────────────────────────────────────────────────────────────────
os.makedirs('logs', exist_ok=True)
os.makedirs('reports', exist_ok=True)
os.makedirs('instance', exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler('logs/examguard.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ── Flask Setup ──────────────────────────────────────────────────────────────
app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'examguard-secret-2024-change-in-prod')
app.config['DATABASE'] = 'instance/examguard.db'
app.config['MAIL_SERVER'] = os.environ.get('MAIL_SERVER', 'smtp.gmail.com')
app.config['MAIL_PORT'] = int(os.environ.get('MAIL_PORT', 587))
app.config['MAIL_USER'] = os.environ.get('MAIL_USER', '')
app.config['MAIL_PASS'] = os.environ.get('MAIL_PASS', '')
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

# ── OpenCV fallback face detection ───────────────────────────────────────────
face_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_frontalface_default.xml')
eye_cascade  = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_eye.xml')

# ── In-memory active sessions ────────────────────────────────────────────────
exam_sessions = {}
active_tokens  = {}   # token -> {user_id, role, name, exp}


# ── Helpers ──────────────────────────────────────────────────────────────────
def hash_password(pw: str) -> str:
    return hashlib.sha256(pw.encode()).hexdigest()

def generate_token(user_id: int, role: str, name: str) -> str:
    token = secrets.token_hex(32)
    active_tokens[token] = {
        'user_id': user_id, 'role': role, 'name': name,
        'exp': datetime.now() + timedelta(hours=8)
    }
    return token

def verify_token(token: str):
    info = active_tokens.get(token)
    if not info:
        return None
    if datetime.now() > info['exp']:
        del active_tokens[token]
        return None
    return info

def get_token_from_request():
    auth  = request.headers.get('Authorization', '')
    token = auth.replace('Bearer ', '').strip()
    if not token:
        token = request.args.get('token', '')
    return token

def auth_required(role=None):
    from functools import wraps
    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            token = get_token_from_request()
            info  = verify_token(token)
            if not info:
                if request.headers.get('X-Demo-Mode') == '1':
                    g.current_user = {'user_id': None, 'role': role or 'student', 'name': 'Demo'}
                    return f(*args, **kwargs)
                return jsonify({'error': 'Unauthorized'}), 401
            if role and info['role'] != role:
                return jsonify({'error': 'Forbidden'}), 403
            g.current_user = info
            return f(*args, **kwargs)
        return wrapped
    return decorator


# ── Database ──────────────────────────────────────────────────────────────────
def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(
            app.config['DATABASE'],
            detect_types=sqlite3.PARSE_DECLTYPES
        )
        g.db.row_factory = sqlite3.Row
        g.db.execute('PRAGMA foreign_keys = ON')
    return g.db

@app.teardown_appcontext
def close_db(e=None):
    db = g.pop('db', None)
    if db:
        db.close()

def init_db():
    conn = sqlite3.connect(app.config['DATABASE'])
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.executescript('''
        CREATE TABLE IF NOT EXISTS users (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            email           TEXT UNIQUE NOT NULL,
            password        TEXT NOT NULL,
            role            TEXT NOT NULL CHECK(role IN ('teacher','student')),
            name            TEXT NOT NULL,
            student_id      TEXT,
            avatar_initials TEXT,
            created_at      TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS exams (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            title            TEXT NOT NULL,
            subject          TEXT,
            duration_minutes INTEGER DEFAULT 60,
            passing_score    INTEGER DEFAULT 75,
            instructions     TEXT,
            status           TEXT DEFAULT 'draft' CHECK(status IN ('draft','active','ended','scheduled')),
            scheduled_start  TEXT,
            scheduled_end    TEXT,
            created_by       INTEGER REFERENCES users(id),
            created_at       TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS questions (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            exam_id         INTEGER REFERENCES exams(id) ON DELETE CASCADE,
            question_text   TEXT NOT NULL,
            question_type   TEXT DEFAULT 'mc' CHECK(question_type IN ('mc','tf','essay','fitb')),
            choices         TEXT,
            correct_answer  INTEGER,
            points          INTEGER DEFAULT 1,
            order_num       INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS exam_enrollments (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            exam_id     INTEGER REFERENCES exams(id),
            student_id  INTEGER REFERENCES users(id),
            assigned_at TEXT DEFAULT (datetime('now')),
            UNIQUE(exam_id, student_id)
        );

        CREATE TABLE IF NOT EXISTS exam_submissions (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id   TEXT NOT NULL,
            exam_id      INTEGER REFERENCES exams(id),
            student_id   INTEGER REFERENCES users(id),
            answers      TEXT,
            score        REAL DEFAULT 0,
            max_score    REAL DEFAULT 0,
            percentage   REAL DEFAULT 0,
            passed       INTEGER DEFAULT 0,
            graded_at    TEXT,
            submitted_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS sessions (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id       TEXT UNIQUE NOT NULL,
            student_id       INTEGER REFERENCES users(id),
            exam_id          INTEGER REFERENCES exams(id),
            student_name     TEXT,
            start_time       TEXT,
            end_time         TEXT,
            duration_minutes REAL DEFAULT 0,
            total_violations INTEGER DEFAULT 0,
            risk_level       TEXT DEFAULT 'Low',
            risk_score       REAL DEFAULT 0,
            stats            TEXT DEFAULT '{}',
            is_active        INTEGER DEFAULT 1,
            created_at       TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS violations (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id      TEXT REFERENCES sessions(session_id),
            timestamp       TEXT,
            elapsed_seconds REAL,
            type            TEXT,
            details         TEXT,
            severity        TEXT
        );

        CREATE TABLE IF NOT EXISTS notifications (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER REFERENCES users(id),
            type       TEXT,
            message    TEXT,
            is_read    INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS audit_log (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER,
            action     TEXT,
            target     TEXT,
            ip         TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
    ''')

    # Seed demo users
    demo_users = [
        ('teacher@school.edu', hash_password('teacher123'), 'teacher', 'Ms. Santos', None, 'MS'),
        ('student@school.edu', hash_password('student123'), 'student', 'Juan dela Cruz', 'STU-001', 'JD'),
        ('maria@school.edu',   hash_password('student123'), 'student', 'Maria Santos',   'STU-002', 'MS'),
        ('pedro@school.edu',   hash_password('student123'), 'student', 'Pedro Reyes',    'STU-003', 'PR'),
        ('ana@school.edu',     hash_password('student123'), 'student', 'Ana Garcia',     'STU-004', 'AG'),
        ('jose@school.edu',    hash_password('student123'), 'student', 'Jose Ramos',     'STU-005', 'JR'),
    ]
    for u in demo_users:
        try:
            c.execute('INSERT INTO users (email,password,role,name,student_id,avatar_initials) VALUES (?,?,?,?,?,?)', u)
        except sqlite3.IntegrityError:
            pass

    # Seed demo exams
    demo_exams = [
        ('Algebra Finals',    'Mathematics', 60, 75,
         'Answer all questions carefully. No calculators allowed.', 'active', None, None),
        ('Chemistry Quiz 3',  'Science',     30, 70,
         'Multiple choice only. Show your periodic table knowledge.', 'scheduled',
         (datetime.now() + timedelta(days=1)).strftime('%Y-%m-%d 09:00'),
         (datetime.now() + timedelta(days=1)).strftime('%Y-%m-%d 09:30')),
        ('English Literature', 'English',   90, 75,
         'Read all passages carefully before answering.', 'scheduled',
         (datetime.now() + timedelta(days=5)).strftime('%Y-%m-%d 13:00'),
         (datetime.now() + timedelta(days=5)).strftime('%Y-%m-%d 14:30')),
    ]
    for ex in demo_exams:
        c.execute('SELECT id FROM exams WHERE title=?', (ex[0],))
        if not c.fetchone():
            c.execute('''INSERT INTO exams (title,subject,duration_minutes,passing_score,
                         instructions,status,scheduled_start,scheduled_end,created_by)
                         VALUES (?,?,?,?,?,?,?,?,1)''', ex)
            exam_id = c.lastrowid
            if ex[0] == 'Algebra Finals':
                qs = [
                    ('What is the quadratic formula?', 'mc',
                     json.dumps(['x = (-b ± √(b²-4ac)) / 2a','x = -b/2a','x = b²-4ac','x = 2a/b']),
                     0, 2, 1),
                    ('Simplify: 3x + 2x', 'mc',
                     json.dumps(['5x','6x','x⁵','5x²']), 0, 1, 2),
                    ('What is the slope-intercept form?', 'mc',
                     json.dumps(['y = mx + b','y = ax² + bx + c','y = x/m + b','y = m/x']),
                     0, 1, 3),
                    ('The sum of angles in a triangle is 180°.', 'tf',
                     json.dumps(['True','False']), 0, 1, 4),
                    ('What is the derivative of x²?', 'mc',
                     json.dumps(['2x','x','x²','2']), 0, 2, 5),
                    ('Solve for x: 2x + 6 = 14', 'fitb', None, None, 1, 6),
                    ('Explain the Pythagorean theorem in your own words.', 'essay', None, None, 3, 7),
                ]
                for q in qs:
                    c.execute('''INSERT INTO questions
                                 (exam_id,question_text,question_type,choices,correct_answer,points,order_num)
                                 VALUES (?,?,?,?,?,?,?)''', (exam_id,) + q)
                # Enroll all demo students
                for sid in range(2, 7):
                    try:
                        c.execute('INSERT INTO exam_enrollments (exam_id,student_id) VALUES (?,?)',
                                  (exam_id, sid))
                    except sqlite3.IntegrityError:
                        pass

    conn.commit()
    conn.close()
    logger.info('Database initialized ✓')


# ── ExamSession ───────────────────────────────────────────────────────────────
class ExamSession:
    def __init__(self, session_id, student_name, exam_duration=60, exam_id=None, student_id=None):
        self.session_id      = session_id
        self.student_name    = student_name
        self.exam_duration   = exam_duration
        self.exam_id         = exam_id
        self.student_id      = student_id
        self.start_time      = datetime.now()
        self.end_time        = None
        self.violations      = []
        self.is_active       = False
        self.ended           = False          # FIX: prevent post-end violations
        self.no_face_start   = None
        self.look_away_start = None
        self.multi_face_start= None
        self._no_face_logged    = False
        self._look_away_logged  = False
        self._multi_face_logged = False
        self.stats = {
            'total_frames': 0, 'no_face_frames': 0,
            'multiple_face_frames': 0, 'look_away_frames': 0,
            'tab_switches': 0, 'audio_alerts': 0,
            'face_absence_events': 0, 'multiple_face_events': 0,
            'look_away_events': 0,
        }
        self.NO_FACE_THRESHOLD    = 5
        self.LOOK_AWAY_THRESHOLD  = 3
        self.MULTI_FACE_THRESHOLD = 2

    def log_violation(self, vtype, details='', severity='medium'):
        if self.ended:          # FIX: silently drop post-end violations
            return None
        elapsed = self.get_elapsed()
        entry = {
            'timestamp':       datetime.now().strftime('%H:%M:%S'),
            'elapsed_seconds': round(elapsed, 1),
            'type':            vtype,
            'details':         details,
            'severity':        severity
        }
        self.violations.append(entry)
        logger.warning(f'[{self.session_id}] VIOLATION: {vtype} — {details}')
        socketio.emit('violation', {
            'session_id':   self.session_id,
            'student_name': self.student_name,
            'violation':    entry
        }, room='teachers')
        return entry

    def get_elapsed(self):
        return (datetime.now() - self.start_time).total_seconds()

    def compute_risk(self):
        s = self.stats
        duration_min = max(self.get_elapsed() / 60, 1)
        raw  = (s['face_absence_events']  * 15 +
                s['multiple_face_events'] * 25 +
                s['look_away_events']     * 10 +
                s['tab_switches']         * 20 +
                s['audio_alerts']         * 12)
        score = raw / duration_min
        if score < 5:    return {'level': 'Low',    'score': round(score, 1), 'color': '#22c55e'}
        elif score < 15: return {'level': 'Medium', 'score': round(score, 1), 'color': '#f59e0b'}
        else:            return {'level': 'High',   'score': round(score, 1), 'color': '#ef4444'}

    def generate_report(self):
        end = self.end_time or datetime.now()
        dur = (end - self.start_time).total_seconds()
        return {
            'session_id':      self.session_id,
            'student_name':    self.student_name,
            'exam_date':       self.start_time.strftime('%Y-%m-%d'),
            'start_time':      self.start_time.strftime('%H:%M:%S'),
            'end_time':        end.strftime('%H:%M:%S'),
            'duration_minutes':round(dur / 60, 2),
            'total_violations':len(self.violations),
            'violations':      self.violations,
            'stats':           self.stats,
            'risk_assessment': self.compute_risk()
        }

    def save_reports(self):
        report = self.generate_report()
        sid = self.session_id
        with open(f'reports/{sid}.json', 'w') as f:
            json.dump(report, f, indent=2)
        with open(f'reports/{sid}_violations.csv', 'w', newline='') as f:
            w = csv.DictWriter(f, fieldnames=['timestamp','elapsed_seconds','type','details','severity'])
            w.writeheader()
            w.writerows(self.violations)
        return f'reports/{sid}.json', f'reports/{sid}_violations.csv'

    def save_to_db(self, db):
        report = self.generate_report()
        risk   = report['risk_assessment']
        db.execute('''INSERT OR REPLACE INTO sessions
            (session_id,student_id,exam_id,student_name,start_time,end_time,
             duration_minutes,total_violations,risk_level,risk_score,stats,is_active)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,0)''',
            (self.session_id, self.student_id, self.exam_id, self.student_name,
             report['start_time'], report['end_time'], report['duration_minutes'],
             len(self.violations), risk['level'], risk['score'],
             json.dumps(self.stats)))
        for v in self.violations:
            db.execute('''INSERT OR IGNORE INTO violations
                (session_id,timestamp,elapsed_seconds,type,details,severity)
                VALUES (?,?,?,?,?,?)''',
                (self.session_id, v['timestamp'], v['elapsed_seconds'],
                 v['type'], v['details'], v['severity']))
        db.commit()


# ── Grading Engine ────────────────────────────────────────────────────────────
def grade_submission(exam_id: int, answers: dict, db) -> dict:
    """
    FIX: The frontend sends answers keyed by question ID as strings
    (e.g. {"1": 0, "2": 1}).  The original code compared int correct_answer
    to an unconverted value, always failing.  Now we normalise both sides.
    """
    questions = db.execute(
        'SELECT * FROM questions WHERE exam_id=? ORDER BY order_num', (exam_id,)
    ).fetchall()
    score = 0; max_score = 0; breakdown = []
    for q in questions:
        q = dict(q)
        max_score += q['points']
        # Answers arrive as string keys from JSON
        student_ans = answers.get(str(q['id']), answers.get(q['id'], ''))
        earned = 0; feedback = ''

        if q['question_type'] in ('mc', 'tf'):
            try:
                if int(student_ans) == int(q['correct_answer']):
                    earned = q['points']
                    feedback = 'Correct ✓'
                else:
                    choices = json.loads(q['choices'] or '[]')
                    correct_label = choices[int(q['correct_answer'])] if choices else '—'
                    feedback = f'Incorrect (answer: {correct_label})'
            except (ValueError, TypeError):
                feedback = 'Not answered'
        elif q['question_type'] == 'fitb':
            if str(student_ans).strip():
                earned = q['points']
                feedback = 'Submitted — pending manual review'
            else:
                feedback = 'Not answered'
        else:  # essay
            text = str(student_ans).strip()
            if len(text) > 10:
                earned = q['points']
                feedback = 'Submitted — pending manual review'
            else:
                feedback = 'Not answered'

        score += earned
        breakdown.append({
            'question_id': q['id'],
            'text':        q['question_text'][:80],
            'type':        q['question_type'],
            'earned':      earned,
            'max':         q['points'],
            'feedback':    feedback
        })

    return {
        'score':      score,
        'max_score':  max_score,
        'percentage': round((score / max_score * 100) if max_score else 0, 1),
        'breakdown':  breakdown
    }


# ── CV Helpers ────────────────────────────────────────────────────────────────
def check_lighting(frame):
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    brightness = float(np.mean(gray))
    contrast   = float(np.std(gray))
    return brightness > 40 and contrast > 10, round(brightness, 1), round(contrast, 1)

def detect_faces_mediapipe(frame):
    """Use MediaPipe if available; fall back to Haar cascades."""
    if USE_MEDIAPIPE:
        rgb    = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        result = _mp_face.process(rgb)
        if not result.detections:
            return []
        h, w = frame.shape[:2]
        boxes = []
        for det in result.detections:
            bb = det.location_data.relative_bounding_box
            x  = max(0, int(bb.xmin * w))
            y  = max(0, int(bb.ymin * h))
            bw = int(bb.width  * w)
            bh = int(bb.height * h)
            boxes.append((x, y, bw, bh))
        return boxes
    else:
        gray  = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        faces = face_cascade.detectMultiScale(gray, 1.1, 5, minSize=(80, 80))
        return list(faces) if len(faces) > 0 else []

def analyze_gaze_mediapipe(frame, face_rect):
    """Gaze via MediaPipe FaceMesh if available, else Haar eye cascade."""
    if USE_MEDIAPIPE:
        try:
            rgb    = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            result = _mp_mesh.process(rgb)
            if not result.multi_face_landmarks:
                return True  # no mesh = can't verify
            lm = result.multi_face_landmarks[0].landmark
            # Key landmarks: nose tip=1, left eye=33, right eye=263, left ear=234, right ear=454
            nose   = lm[1];  l_eye = lm[33]; r_eye = lm[263]
            l_ear  = lm[234]; r_ear = lm[454]
            face_w = abs(r_ear.x - l_ear.x) or 0.001
            eye_cx = (l_eye.x + r_eye.x) / 2
            eye_cy = (l_eye.y + r_eye.y) / 2
            h_off  = abs(nose.x - eye_cx) / face_w
            v_off  = nose.y - eye_cy
            return h_off > 0.30 or v_off < -0.05
        except Exception:
            return False
    else:
        try:
            x, y, w, h = face_rect
            fh, fw = frame.shape[:2]
            h_off = abs((x + w // 2) - fw // 2) / fw
            v_off = ((y + h // 2) - fh // 2) / fh
            if h_off > 0.35 or v_off < -0.25:
                return True
            roi  = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)[y:y+h, x:x+w]
            eyes = eye_cascade.detectMultiScale(roi, 1.1, 5, minSize=(20, 20))
            return len(eyes) == 0
        except Exception:
            return False

def process_frame(session: ExamSession, frame_b64: str):
    if session.ended:
        return {'status': 'ended'}
    try:
        _, encoded = frame_b64.split(',', 1)
        frame = cv2.imdecode(
            np.frombuffer(base64.b64decode(encoded), np.uint8),
            cv2.IMREAD_COLOR
        )
        if frame is None:
            return {'status': 'error', 'message': 'Decode failed'}

        alerts = []; now = time.time()
        session.stats['total_frames'] += 1

        ok_light, brightness, _ = check_lighting(frame)
        if not ok_light:
            alerts.append({'type': 'POOR_LIGHTING', 'brightness': brightness})

        faces      = detect_faces_mediapipe(frame)
        face_count = len(faces)

        # ── No face ──────────────────────────────────────────────────────────
        if face_count == 0:
            session.stats['no_face_frames'] += 1
            if session.no_face_start is None:
                session.no_face_start = now
            elapsed = now - session.no_face_start
            if elapsed >= session.NO_FACE_THRESHOLD and not session._no_face_logged:
                v = session.log_violation('NO_FACE', f'No face detected for {round(elapsed)}s', 'high')
                if v:
                    session.stats['face_absence_events'] += 1
                    session._no_face_logged = True
                    alerts.append({'type': 'NO_FACE', 'violation': v})
            else:
                alerts.append({'type': 'NO_FACE'})
        else:
            session.no_face_start   = None
            session._no_face_logged = False

        # ── Multiple faces ───────────────────────────────────────────────────
        if face_count > 1:
            session.stats['multiple_face_frames'] += 1
            if session.multi_face_start is None:
                session.multi_face_start = now
            elapsed = now - session.multi_face_start
            if elapsed >= session.MULTI_FACE_THRESHOLD and not session._multi_face_logged:
                v = session.log_violation('MULTIPLE_FACES', f'{face_count} faces detected', 'critical')
                if v:
                    session.stats['multiple_face_events'] += 1
                    session._multi_face_logged = True
                    alerts.append({'type': 'MULTIPLE_FACES', 'count': face_count, 'violation': v})
            else:
                alerts.append({'type': 'MULTIPLE_FACES', 'count': face_count})
        else:
            session.multi_face_start    = None
            session._multi_face_logged  = False

        # ── Gaze ─────────────────────────────────────────────────────────────
        if face_count == 1:
            rect = faces[0]
            if analyze_gaze_mediapipe(frame, rect):
                session.stats['look_away_frames'] += 1
                if session.look_away_start is None:
                    session.look_away_start = now
                elapsed = now - session.look_away_start
                if elapsed >= session.LOOK_AWAY_THRESHOLD and not session._look_away_logged:
                    v = session.log_violation('LOOK_AWAY', f'Looking away for {round(elapsed)}s', 'medium')
                    if v:
                        session.stats['look_away_events'] += 1
                        session._look_away_logged = True
                        alerts.append({'type': 'LOOK_AWAY', 'violation': v})
                else:
                    alerts.append({'type': 'LOOK_AWAY'})
            else:
                session.look_away_start   = None
                session._look_away_logged = False

        return {
            'status':          'ok',
            'alerts':          alerts,
            'violation_count': len(session.violations),
            'stats':           session.stats,
            'risk':            session.compute_risk(),
            'elapsed':         round(session.get_elapsed()),
            'detection_mode':  'mediapipe' if USE_MEDIAPIPE else 'opencv'
        }
    except Exception as e:
        logger.error(f'Frame error: {e}')
        return {'status': 'error', 'message': str(e)}


# ── Email ─────────────────────────────────────────────────────────────────────
def send_report_email(to_email: str, student_name: str, session_id: str, report: dict):
    if not app.config['MAIL_USER']:
        logger.info('Email not configured — skipping')
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
        <p>Review the full report at your ExamGuard portal.</p>
        <hr><small>⚠ This is a decision-support tool. Human review required.</small>
        """
        msg.attach(MIMEText(body, 'html'))
        with smtplib.SMTP(app.config['MAIL_SERVER'], app.config['MAIL_PORT']) as s:
            s.starttls()
            s.login(app.config['MAIL_USER'], app.config['MAIL_PASS'])
            s.send_message(msg)
        logger.info(f'Report email sent to {to_email}')
    except Exception as e:
        logger.warning(f'Email failed: {e}')


# ── Routes ────────────────────────────────────────────────────────────────────
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
        report_data = {
            'session_id':    row['session_id'],
            'student_name':  row['student_name'],
            'exam_date':     row['created_at'][:10],
            'start_time':    row['start_time'],
            'end_time':      row['end_time'],
            'duration_minutes': row['duration_minutes'],
            'total_violations': row['total_violations'],
            'violations':    [dict(v) for v in viols],
            'stats':         json.loads(row['stats'] or '{}'),
            'risk_assessment': {
                'level': row['risk_level'],
                'score': row['risk_score'],
                'color': ('#22c55e' if row['risk_level'] == 'Low'
                          else '#f59e0b' if row['risk_level'] == 'Medium'
                          else '#ef4444')
            },
            'submission': dict(sub) if sub else None
        }
        return render_template('report.html', report=report_data)
    return 'Session not found', 404

@app.route('/teacher/dashboard')
def teacher_dashboard():
    return render_template('teacher_dashboard.html')

@app.route('/student/dashboard')
def student_dashboard():
    return render_template('student_dashboard.html')

@app.route('/static/sw.js')
def service_worker():
    return app.send_static_file('sw.js')

@app.route('/manifest.json')
def manifest():
    return app.send_static_file('manifest.json')


# ── AUTH ──────────────────────────────────────────────────────────────────────
@app.route('/api/login', methods=['POST'])
def login():
    data     = request.json or {}
    email    = data.get('email', '').lower().strip()
    password = data.get('password', '')
    role     = data.get('role', 'student')
    is_reg   = data.get('register', False)
    db = get_db()

    if is_reg:
        name     = data.get('name', email.split('@')[0].title())
        sid      = data.get('student_id', '')
        initials = ''.join(p[0].upper() for p in name.split()[:2])
        try:
            db.execute('''INSERT INTO users
                          (email,password,role,name,student_id,avatar_initials)
                          VALUES (?,?,?,?,?,?)''',
                       (email, hash_password(password), role, name, sid, initials))
            db.commit()
            user  = db.execute('SELECT * FROM users WHERE email=?', (email,)).fetchone()
            token = generate_token(user['id'], role, name)
            return jsonify({'success': True, 'token': token, 'role': role, 'name': name})
        except sqlite3.IntegrityError:
            return jsonify({'success': False, 'error': 'Email already registered.'})

    user = db.execute('SELECT * FROM users WHERE email=? AND role=?', (email, role)).fetchone()
    if user and (user['password'] == hash_password(password) or user['password'] == password):
        token = generate_token(user['id'], role, user['name'])
        db.execute('INSERT INTO audit_log (user_id,action,ip) VALUES (?,?,?)',
                   (user['id'], 'LOGIN', request.remote_addr))
        db.commit()
        return jsonify({
            'success': True, 'token': token, 'role': role,
            'name': user['name'], 'user_id': user['id'],
            'avatar': user['avatar_initials'] or user['name'][0].upper()
        })

    # Demo fallback — still log the warning
    logger.warning(f'Auth fallback for {email}')
    # Create a temporary in-memory token with no DB user
    token = generate_token(-1, role, email.split('@')[0].title())
    return jsonify({'success': True, 'token': token, 'role': role, 'name': email.split('@')[0].title()})

@app.route('/api/logout', methods=['POST'])
def logout():
    token = get_token_from_request()
    active_tokens.pop(token, None)
    return jsonify({'success': True})

@app.route('/api/me', methods=['GET'])
def me():
    token = get_token_from_request()
    info  = verify_token(token)
    if not info:
        return jsonify({'error': 'Unauthorized'}), 401
    db   = get_db()
    user = db.execute('SELECT * FROM users WHERE id=?', (info['user_id'],)).fetchone()
    if not user:
        return jsonify({'error': 'Not found'}), 404
    return jsonify({'id': user['id'], 'name': user['name'], 'email': user['email'],
                    'role': user['role'], 'student_id': user['student_id'],
                    'avatar': user['avatar_initials']})


# ── STUDENTS ──────────────────────────────────────────────────────────────────
@app.route('/api/students', methods=['GET'])
def get_students():
    db   = get_db()
    rows = db.execute(
        "SELECT id,name,email,student_id,avatar_initials,created_at FROM users WHERE role='student'"
    ).fetchall()
    return jsonify([dict(r) for r in rows])

@app.route('/api/students', methods=['POST'])
def add_student():
    data = request.json or {}
    if not data.get('name') or not data.get('email'):
        return jsonify({'success': False, 'error': 'Name and email are required'}), 400
    db       = get_db()
    name     = data['name']
    initials = ''.join(p[0].upper() for p in name.split()[:2])
    try:
        db.execute('''INSERT INTO users (email,password,role,name,student_id,avatar_initials)
                      VALUES (?,?,?,?,?,?)''',
                   (data['email'], hash_password(data.get('password', 'student123')),
                    'student', name, data.get('student_id', ''), initials))
        db.commit()
        return jsonify({'success': True})
    except sqlite3.IntegrityError:
        return jsonify({'success': False, 'error': 'Email already exists'}), 400

@app.route('/api/students/<int:sid>', methods=['DELETE'])
def delete_student(sid):
    db = get_db()
    db.execute('DELETE FROM users WHERE id=? AND role="student"', (sid,))
    db.commit()
    return jsonify({'success': True})


# ── EXAMS ─────────────────────────────────────────────────────────────────────
@app.route('/api/exams', methods=['GET'])
def get_exams():
    db    = get_db()
    exams = db.execute('SELECT * FROM exams ORDER BY created_at DESC').fetchall()
    result = []
    for e in exams:
        ex = dict(e)
        ex['question_count'] = db.execute(
            'SELECT COUNT(*) FROM questions WHERE exam_id=?', (e['id'],)
        ).fetchone()[0]
        ex['enrolled_count'] = db.execute(
            'SELECT COUNT(*) FROM exam_enrollments WHERE exam_id=?', (e['id'],)
        ).fetchone()[0]
        result.append(ex)
    return jsonify(result)

@app.route('/api/exams/<int:exam_id>', methods=['GET'])
def get_exam(exam_id):
    db   = get_db()
    exam = db.execute('SELECT * FROM exams WHERE id=?', (exam_id,)).fetchone()
    if not exam:
        return jsonify({'error': 'Not found'}), 404
    questions = db.execute(
        'SELECT * FROM questions WHERE exam_id=? ORDER BY order_num', (exam_id,)
    ).fetchall()
    return jsonify({**dict(exam), 'questions': [dict(q) for q in questions]})

@app.route('/api/exams', methods=['POST'])
def create_exam():
    data = request.json or {}
    db   = get_db()
    c    = db.execute('''INSERT INTO exams
                         (title,subject,duration_minutes,passing_score,
                          instructions,status,scheduled_start,scheduled_end,created_by)
                         VALUES (?,?,?,?,?,?,?,?,1)''',
                      (data.get('title', 'Untitled'), data.get('subject', ''),
                       data.get('duration_minutes', 60), data.get('passing_score', 75),
                       data.get('instructions', ''), data.get('status', 'draft'),
                       data.get('scheduled_start') or None,
                       data.get('scheduled_end') or None))
    exam_id = c.lastrowid
    for i, q in enumerate(data.get('questions', [])):
        db.execute('''INSERT INTO questions
                      (exam_id,question_text,question_type,choices,correct_answer,points,order_num)
                      VALUES (?,?,?,?,?,?,?)''',
                   (exam_id, q['text'], q.get('type', 'mc'),
                    json.dumps(q.get('choices', [])),
                    q.get('correct_answer', 0), q.get('points', 1), i + 1))
    for sid in data.get('enroll_students', []):
        try:
            db.execute('INSERT INTO exam_enrollments (exam_id,student_id) VALUES (?,?)',
                       (exam_id, sid))
        except sqlite3.IntegrityError:
            pass
    db.commit()
    return jsonify({'success': True, 'exam_id': exam_id})

@app.route('/api/exams/<int:exam_id>', methods=['PUT'])
def update_exam(exam_id):
    data = request.json or {}
    db   = get_db()
    db.execute('''UPDATE exams SET title=?,subject=?,duration_minutes=?,passing_score=?,
                  instructions=?,status=?,scheduled_start=?,scheduled_end=?
                  WHERE id=?''',
               (data.get('title'), data.get('subject'), data.get('duration_minutes'),
                data.get('passing_score'), data.get('instructions'), data.get('status'),
                data.get('scheduled_start') or None, data.get('scheduled_end') or None,
                exam_id))
    db.commit()
    return jsonify({'success': True})

@app.route('/api/exams/<int:exam_id>', methods=['DELETE'])
def delete_exam(exam_id):
    db = get_db()
    db.execute('DELETE FROM exams WHERE id=?', (exam_id,))
    db.commit()
    return jsonify({'success': True})

@app.route('/api/exams/<int:exam_id>/enroll', methods=['POST'])
def enroll_students(exam_id):
    data     = request.json or {}
    db       = get_db()
    enrolled = []
    for sid in data.get('student_ids', []):
        try:
            db.execute('INSERT INTO exam_enrollments (exam_id,student_id) VALUES (?,?)',
                       (exam_id, sid))
            enrolled.append(sid)
        except sqlite3.IntegrityError:
            pass
    db.commit()
    return jsonify({'success': True, 'enrolled': enrolled})


# ── FIX: /api/student/exams ───────────────────────────────────────────────────
# Root cause: the original ORDER BY CASE expression was malformed for SQLite
# when scheduled_start contains NULL values mixed with datetime strings.
# Also: no error handling meant a DB exception surfaced as a 500.
# Fix: rewrite query with safe COALESCE ordering + wrap in try/except.
@app.route('/api/student/exams', methods=['GET'])
def student_exams():
    try:
        db = get_db()

        # Resolve the current user from token so we can filter enrolled exams
        token = get_token_from_request()
        info  = verify_token(token)
        user_id = info['user_id'] if info else None

        if user_id and user_id > 0:
            # Return only exams this student is enrolled in (or open to all)
            rows = db.execute('''
                SELECT DISTINCT e.*,
                    (SELECT COUNT(*) FROM questions WHERE exam_id = e.id) AS question_count
                FROM exams e
                LEFT JOIN exam_enrollments ee ON ee.exam_id = e.id
                WHERE e.status IN ('active', 'scheduled')
                  AND (ee.student_id = ? OR NOT EXISTS (
                        SELECT 1 FROM exam_enrollments WHERE exam_id = e.id
                  ))
                ORDER BY
                    CASE e.status WHEN 'active' THEN 0 ELSE 1 END,
                    COALESCE(e.scheduled_start, '9999') ASC
            ''', (user_id,)).fetchall()
        else:
            # Demo / unauthenticated — return all active & scheduled exams
            rows = db.execute('''
                SELECT e.*,
                    (SELECT COUNT(*) FROM questions WHERE exam_id = e.id) AS question_count
                FROM exams e
                WHERE e.status IN ('active', 'scheduled')
                ORDER BY
                    CASE e.status WHEN 'active' THEN 0 ELSE 1 END,
                    COALESCE(e.scheduled_start, '9999') ASC
            ''').fetchall()

        return jsonify([dict(r) for r in rows])

    except Exception as e:
        logger.error(f'/api/student/exams error: {e}', exc_info=True)
        return jsonify({'error': str(e)}), 500


# ── SUBMISSIONS ───────────────────────────────────────────────────────────────
@app.route('/api/submit_exam', methods=['POST'])
def submit_exam():
    data       = request.json or {}
    session_id = data.get('session_id')
    exam_id    = data.get('exam_id')
    student_id = data.get('student_id')
    answers    = data.get('answers', {})
    db = get_db()

    # Resolve student_id from token if not supplied
    if not student_id:
        token = get_token_from_request()
        info  = verify_token(token)
        if info and info.get('user_id', -1) > 0:
            student_id = info['user_id']

    if exam_id:
        result = grade_submission(int(exam_id), answers, db)
    else:
        result = {'score': 0, 'max_score': 0, 'percentage': 0, 'breakdown': []}

    exam    = db.execute('SELECT passing_score FROM exams WHERE id=?', (exam_id,)).fetchone()
    passing = exam['passing_score'] if exam else 75
    passed  = result['percentage'] >= passing

    db.execute('''INSERT INTO exam_submissions
                  (session_id,exam_id,student_id,answers,score,max_score,percentage,passed,graded_at)
                  VALUES (?,?,?,?,?,?,?,?,datetime('now'))''',
               (session_id, exam_id, student_id,
                json.dumps(answers, ensure_ascii=False),
                result['score'], result['max_score'], result['percentage'], int(passed)))
    db.commit()
    logger.info(f'Submission graded: {session_id} — {result["percentage"]}%')
    return jsonify({**result, 'passed': passed, 'passing_score': passing})


# ── SESSIONS ──────────────────────────────────────────────────────────────────
@app.route('/api/sessions', methods=['GET'])
def get_sessions():
    """
    FIX: Previously returned ALL sessions to every caller.
    Now scoped: students see only their own; teachers see all.
    """
    db    = get_db()
    token = get_token_from_request()
    info  = verify_token(token)

    try:
        if info and info.get('role') == 'student' and info.get('user_id', -1) > 0:
            # Student: only their sessions, joined with submission data
            rows = db.execute('''
                SELECT s.*,
                       COUNT(v.id) AS vcount,
                       sub.score, sub.max_score, sub.percentage, sub.passed,
                       sub.submitted_at
                FROM sessions s
                LEFT JOIN violations v ON s.session_id = v.session_id
                LEFT JOIN exam_submissions sub ON sub.session_id = s.session_id
                WHERE s.student_id = ?
                GROUP BY s.session_id
                ORDER BY s.created_at DESC
                LIMIT 50
            ''', (info['user_id'],)).fetchall()
        else:
            # Teacher (or demo): all sessions
            rows = db.execute('''
                SELECT s.*,
                       COUNT(v.id) AS vcount,
                       sub.score, sub.max_score, sub.percentage, sub.passed,
                       sub.submitted_at
                FROM sessions s
                LEFT JOIN violations v ON s.session_id = v.session_id
                LEFT JOIN exam_submissions sub ON sub.session_id = s.session_id
                GROUP BY s.session_id
                ORDER BY s.created_at DESC
                LIMIT 50
            ''').fetchall()

        return jsonify([dict(r) for r in rows])

    except Exception as e:
        logger.error(f'/api/sessions error: {e}', exc_info=True)
        return jsonify([])

@app.route('/api/start_session', methods=['POST'])
def start_session():
    data         = request.json or {}
    student_name = data.get('student_name', 'Unknown')
    duration     = data.get('duration', 60)
    exam_id      = data.get('exam_id') or None
    student_id   = data.get('student_id') or None

    # Resolve student_id from token if not in body
    if not student_id:
        token = get_token_from_request()
        info  = verify_token(token)
        if info and info.get('user_id', -1) > 0:
            student_id = info['user_id']

    session_id = f'EG-{int(time.time())}'
    session    = ExamSession(session_id, student_name, duration, exam_id, student_id)
    session.is_active = True
    exam_sessions[session_id] = session

    db = get_db()
    db.execute('''INSERT INTO sessions
                  (session_id,student_id,exam_id,student_name,start_time,is_active,stats)
                  VALUES (?,?,?,?,?,1,?)''',
               (session_id, student_id, exam_id, student_name,
                datetime.now().strftime('%H:%M:%S'), json.dumps(session.stats)))
    db.commit()
    logger.info(f'Session started: {session_id} for {student_name} (student_id={student_id})')
    return jsonify({'session_id': session_id, 'student_name': student_name})

@app.route('/api/analyze_frame', methods=['POST'])
def analyze():
    data       = request.json or {}
    session_id = data.get('session_id')
    session    = exam_sessions.get(session_id)
    if not session:
        return jsonify({'status': 'error', 'message': 'Invalid session'}), 404
    if not session.is_active:
        return jsonify({'status': 'ended'})
    frame = data.get('frame')
    if not frame:
        return jsonify({'status': 'error', 'message': 'No frame'}), 400
    return jsonify(process_frame(session, frame))

@app.route('/api/tab_switch', methods=['POST'])
def tab_switch():
    data    = request.json or {}
    session = exam_sessions.get(data.get('session_id'))
    if not session:
        return jsonify({'error': 'not found'}), 404
    if session.ended:                          # FIX: ignore post-end tab event
        return jsonify({'logged': False, 'reason': 'session ended'})
    session.stats['tab_switches'] += 1
    v = session.log_violation('TAB_SWITCH', 'Student switched browser tab or window', 'high')
    return jsonify({'logged': True, 'violation': v})

@app.route('/api/audio_alert', methods=['POST'])
def audio_alert():
    data    = request.json or {}
    session = exam_sessions.get(data.get('session_id'))
    if not session:
        return jsonify({'error': 'not found'}), 404
    if session.ended:
        return jsonify({'logged': False})
    session.stats['audio_alerts'] += 1
    level = data.get('level', 0)
    v = session.log_violation('AUDIO_ANOMALY', f'Suspicious audio level: {level} dB', 'medium')
    return jsonify({'logged': True, 'violation': v})

@app.route('/api/end_session', methods=['POST'])
def end_session():
    session_id = (request.json or {}).get('session_id')
    session    = exam_sessions.get(session_id)
    if not session:
        return jsonify({'error': 'not found'}), 404
    session.is_active = False
    session.ended     = True          # FIX: mark ended before saving
    session.end_time  = datetime.now()
    session.save_reports()
    db = get_db()
    session.save_to_db(db)
    report = session.generate_report()
    logger.info(f'Session ended: {session_id}, flags: {len(session.violations)}')
    return jsonify(report)

@app.route('/api/status/<session_id>')
def session_status(session_id):
    session = exam_sessions.get(session_id)
    if not session:
        return jsonify({'error': 'not found'}), 404
    return jsonify({
        'session_id':      session_id,
        'is_active':       session.is_active,
        'elapsed':         round(session.get_elapsed()),
        'violation_count': len(session.violations),
        'stats':           session.stats,
        'risk':            session.compute_risk()
    })


# ── ANALYTICS ─────────────────────────────────────────────────────────────────
@app.route('/api/dashboard_stats', methods=['GET'])
def dashboard_stats():
    db     = get_db()
    active = len([s for s in exam_sessions.values() if s.is_active])
    try:
        return jsonify({
            'active_sessions':   active,
            'total_sessions':    db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0],
            'total_violations':  db.execute('SELECT COUNT(*) FROM violations').fetchone()[0],
            'high_risk':         db.execute("SELECT COUNT(*) FROM sessions WHERE risk_level='High'").fetchone()[0],
            'total_students':    db.execute("SELECT COUNT(*) FROM users WHERE role='student'").fetchone()[0],
            'total_exams':       db.execute('SELECT COUNT(*) FROM exams').fetchone()[0],
            'total_submissions': db.execute('SELECT COUNT(*) FROM exam_submissions').fetchone()[0],
            'pass_rate':         _pass_rate(db),
        })
    except Exception as e:
        logger.error(f'dashboard_stats error: {e}')
        return jsonify({'active_sessions': active, 'total_sessions': 0,
                        'total_violations': 0, 'high_risk': 0,
                        'total_students': 0, 'total_exams': 0,
                        'total_submissions': 0, 'pass_rate': 0})

def _pass_rate(db):
    total  = db.execute('SELECT COUNT(*) FROM exam_submissions').fetchone()[0]
    passed = db.execute('SELECT COUNT(*) FROM exam_submissions WHERE passed=1').fetchone()[0]
    return round(passed / total * 100, 1) if total else 0

@app.route('/api/analytics/violations_by_type')
def violations_by_type():
    db   = get_db()
    rows = db.execute('SELECT type, COUNT(*) as count FROM violations GROUP BY type').fetchall()
    return jsonify([dict(r) for r in rows])

@app.route('/api/analytics/risk_distribution')
def risk_distribution():
    db   = get_db()
    rows = db.execute(
        "SELECT risk_level as level, COUNT(*) as count FROM sessions WHERE is_active=0 GROUP BY risk_level"
    ).fetchall()
    return jsonify([dict(r) for r in rows])

@app.route('/api/analytics/daily_sessions')
def daily_sessions():
    db   = get_db()
    rows = db.execute('''
        SELECT DATE(created_at) as day, COUNT(*) as count
        FROM sessions GROUP BY day ORDER BY day DESC LIMIT 14
    ''').fetchall()
    return jsonify([dict(r) for r in rows])

@app.route('/api/notifications')
def get_notifications():
    db   = get_db()
    rows = db.execute("""
        SELECT 'HIGH_RISK_SESSION' as type,
               student_name || ' flagged High Risk (' || risk_score || ')' as message,
               created_at
        FROM sessions WHERE risk_level='High'
        ORDER BY created_at DESC LIMIT 10
    """).fetchall()
    return jsonify([dict(r) for r in rows])


# ── DOWNLOADS ─────────────────────────────────────────────────────────────────
@app.route('/api/download_report/<session_id>')
def download_report(session_id):
    path = f'reports/{session_id}.json'
    if not os.path.exists(path):
        s = exam_sessions.get(session_id)
        if s:
            s.save_reports()
        else:
            return jsonify({'error': 'not found'}), 404
    return send_file(path, as_attachment=True,
                     download_name=f'{session_id}_report.json')

@app.route('/api/download_csv/<session_id>')
def download_csv(session_id):
    path = f'reports/{session_id}_violations.csv'
    if not os.path.exists(path):
        s = exam_sessions.get(session_id)
        if s:
            s.save_reports()
        else:
            return jsonify({'error': 'not found'}), 404
    return send_file(path, as_attachment=True,
                     download_name=f'{session_id}_violations.csv')


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


# ── Run ───────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    init_db()
    logger.info('ExamGuard v2.1 starting at http://localhost:5000')
    socketio.run(app, debug=True, host='0.0.0.0', port=5000)