"""
ExamGuard - AI-Powered Online Exam Monitoring System
Backend: Flask + OpenCV (mediapipe replaced with OpenCV cascade classifier)
"""

import cv2
import numpy as np
import json
import time
import base64
import os
import csv
import logging
from datetime import datetime
from flask import Flask, render_template, request, jsonify, send_file
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
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

# ─── OpenCV Setup (replaces MediaPipe) ────────────────────────────────────────
face_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_frontalface_default.xml')
eye_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_eye.xml')

# ─── Global State ─────────────────────────────────────────────────────────────
exam_sessions = {}

# ─── Mock user DB (replace with real DB in production) ────────────────────────
MOCK_USERS = {
    "teacher@school.edu": {"password": "teacher123", "role": "teacher", "name": "Ms. Santos"},
    "student@school.edu": {"password": "student123", "role": "student", "name": "Juan dela Cruz"},
}

# ─── Exam Session Class ───────────────────────────────────────────────────────
class ExamSession:
    def __init__(self, session_id, student_name, exam_duration=60):
        self.session_id = session_id
        self.student_name = student_name
        self.exam_duration = exam_duration  # minutes
        self.start_time = datetime.now()
        self.end_time = None
        self.violations = []
        self.is_active = False

        # Timing state
        self.no_face_start = None
        self.look_away_start = None
        self.multi_face_start = None

        # Event-logged flags (prevent duplicate logs within threshold)
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

        # Thresholds in seconds
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

        logger.info(f"[{sid}] Reports saved: {json_path}, {csv_path}")
        return json_path, csv_path


# ─── Computer Vision Helpers ──────────────────────────────────────────────────
def check_lighting(frame):
    """Return True if lighting is acceptable."""
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    brightness = np.mean(gray)
    contrast = np.std(gray)
    return brightness > 40 and contrast > 10, round(float(brightness), 1), round(float(contrast), 1)


def analyze_gaze(frame, face_rect):
    """
    Estimates if student is looking away using eye detection and face position.
    Returns True if looking away.
    """
    try:
        x, y, w, h = face_rect
        frame_h, frame_w = frame.shape[:2]

        # Check horizontal face position relative to frame center
        face_center_x = x + w // 2
        face_center_y = y + h // 2
        h_offset = abs(face_center_x - frame_w // 2) / frame_w
        v_offset = (face_center_y - frame_h // 2) / frame_h

        # If face is too far to the side or too high up, likely looking away
        if h_offset > 0.35 or v_offset < -0.25:
            return True

        # Use eye detection to check gaze within face ROI
        face_roi_gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)[y:y+h, x:x+w]
        eyes = eye_cascade.detectMultiScale(face_roi_gray, scaleFactor=1.1, minNeighbors=5, minSize=(20, 20))

        # No eyes detected within face = likely looking away
        if len(eyes) == 0:
            return True

        return False
    except Exception:
        return False


def detect_faces(frame):
    """Detect faces using OpenCV cascade classifier."""
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    faces = face_cascade.detectMultiScale(
        gray,
        scaleFactor=1.1,
        minNeighbors=5,
        minSize=(80, 80)
    )
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

        # ── Lighting check ────────────────────────────────────────────────────
        ok_light, brightness, contrast = check_lighting(frame)
        if not ok_light:
            alerts.append({'type': 'POOR_LIGHTING', 'brightness': brightness, 'contrast': contrast})

        # ── Face detection ────────────────────────────────────────────────────
        faces = detect_faces(frame)
        face_count = len(faces)

        # No face
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

        # Multiple faces
        if face_count > 1:
            session.stats['multiple_face_frames'] += 1
            if session.multi_face_start is None:
                session.multi_face_start = now
            elapsed_multi = now - session.multi_face_start
            if elapsed_multi >= session.MULTI_FACE_THRESHOLD and not session._multi_face_logged:
                v = session.log_violation('MULTIPLE_FACES', f'{face_count} faces detected for {round(elapsed_multi)}s', 'critical')
                session.stats['multiple_face_events'] += 1
                session._multi_face_logged = True
                alerts.append({'type': 'MULTIPLE_FACES', 'count': face_count, 'violation': v})
            else:
                alerts.append({'type': 'MULTIPLE_FACES', 'count': face_count})
        else:
            session.multi_face_start = None
            session._multi_face_logged = False

        # ── Gaze analysis ─────────────────────────────────────────────────────
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
    return "Session not found", 404

@app.route('/teacher/dashboard')
def teacher_dashboard():
    return render_template('teacher_dashboard.html')

@app.route('/student/dashboard')
def student_dashboard():
    return render_template('student_dashboard.html')

@app.route('/api/login', methods=['POST'])
def login():
    data = request.json or {}
    email = data.get('email', '').lower()
    password = data.get('password', '')
    role = data.get('role', 'student')
    is_register = data.get('register', False)

    if is_register:
        return jsonify({'success': True, 'role': role})

    user = MOCK_USERS.get(email)
    if user and user['password'] == password and user['role'] == role:
        return jsonify({'success': True, 'role': role, 'name': user['name']})

    # Demo mode: allow any login for testing
    return jsonify({'success': True, 'role': role})

@app.route('/api/start_session', methods=['POST'])
def start_session():
    data = request.json or {}
    student_name = data.get('student_name', 'Unknown Student')
    duration = data.get('duration', 60)
    session_id = f"EG-{int(time.time())}"
    session = ExamSession(session_id, student_name, duration)
    session.is_active = True
    exam_sessions[session_id] = session
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
    os.makedirs('reports', exist_ok=True)
    os.makedirs('logs', exist_ok=True)
    logger.info("ExamGuard server starting at http://localhost:5000")
    socketio.run(app, debug=True, host='0.0.0.0', port=5000)