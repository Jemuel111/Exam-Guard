# ExamGuard

ExamGuard is a web-based exam proctoring system built with Flask. It monitors students during online exams using computer vision, logs behavioral events, and generates reports for instructor review. All flagged behavior requires human review — the system does not issue automated verdicts.

---

## Project Status

The core system is complete and functional. The one feature that was not implemented is real-time face streaming similar to what Google Meet does — where the teacher watches a live feed of the student's webcam during the exam. Instead, the system analyzes frames in the background and sends push notifications to the teacher when a high-risk event is detected.

---

## What's Built

**Authentication and user management**
- JWT-based login with bcrypt password hashing
- Role-based access for teachers and students
- Password reset via email token
- Encrypted PII storage (student names stored via Fernet encryption)

**Exam management (teacher side)**
- Create, edit, archive, and end exams
- Add multiple choice, true/false, fill-in-the-blank, and essay questions
- Enroll specific students or open exams to all
- Schedule exams with start and end times
- Auto-grade objective questions; flag essays for manual grading

**Exam taking (student side)**
- Students see only exams assigned to them
- One submission per exam enforced at the database level
- Webcam consent prompt before the exam begins
- Tab-switch detection via the Browser Visibility API

**Proctoring and CV pipeline**
- Frames captured from the browser every ~1.5 seconds via Canvas API and sent as base64 JPEG to the server
- Face detection using MediaPipe (falls back to OpenCV Haar cascades if MediaPipe is unavailable)
- Face mesh with 468 landmarks for gaze/head pose estimation
- Lighting quality check (brightness + contrast via OpenCV)
- Time-gated violation detection to reduce false positives

| Event | Method | Threshold | Severity |
|---|---|---|---|
| No face detected | MediaPipe face detection | 5 seconds | High |
| Multiple faces | MediaPipe face detection | 2 seconds | Critical |
| Gaze deviation | Head pose via face mesh landmarks | 3 seconds | Medium |
| Tab switch | Browser Visibility API | Immediate | High |
| Poor lighting | OpenCV brightness/contrast | Per frame | Warning |

**Risk scoring**

```
score = (face_absence_events × 15) + (multiple_face_events × 25)
      + (look_away_events × 10)    + (tab_switch_events × 20)

normalized = score / exam_duration_minutes

Low:     < 5
Medium:  5–15
High:    > 15
```

**Reports and archiving**
- Per-session JSON and CSV violation logs saved to disk
- Instructor report view with timeline of events and risk assessment
- Soft-archive for exams, sessions, and users (nothing is hard-deleted)
- Audit log table for all significant actions

**Push notifications**
- Web Push (VAPID) for browser notifications to logged-in teachers
- Firebase Cloud Messaging (FCM) support for mobile if a `serviceAccountKey.json` is provided
- Teachers get notified mid-exam when a session crosses into High risk

**Email**
- Report summary email sent to the teacher when a session ends
- Password reset emails via SMTP

---

## What's Not Implemented

**Real-time webcam streaming to the teacher** — the original plan included a live video feed of the student during the exam, similar to how Google Meet works. This was not built. The teacher sees push notifications for high-risk events and can review the full violation log after the exam ends, but there is no live video panel.

---

## Project Structure

```
Exam-Guard-main/
├── app.py                  # App factory, page routes, SocketIO setup, email
├── auth.py                 # Token generation and verification, bcrypt hashing
├── config.py               # Config classes for dev/production
├── crypto.py               # Fernet encryption for PII fields
├── cv_engine.py            # Frame analysis: face detection, gaze, lighting
├── database.py             # SQLite setup, schema, migrations, seed data
├── grading.py              # Auto-grading logic for objective questions
├── session_model.py        # ExamSession class: state machine, report generation
├── blueprints/
│   ├── auth_bp.py          # /api/login, /api/register, /api/reset-password
│   ├── exams_bp.py         # Exam CRUD, questions, enrollments, submissions
│   ├── sessions_bp.py      # /api/start_session, /api/analyze_frame, /api/end_session
│   ├── admin_bp.py         # User management for teachers
│   ├── archive_bp.py       # Archive/restore endpoints
│   └── push_bp.py          # Web Push and FCM notification endpoints
├── static/
│   ├── sw.js               # Service worker for PWA and push
│   ├── manifest.json       # PWA manifest
│   └── icons/              # App icons
├── templates/              # Jinja2 HTML templates
├── reports/                # Generated JSON and CSV session reports
├── logs/                   # Application logs
├── instance/
│   └── examguard.db        # SQLite database
└── requirements.txt
```

---

## Setup

### Prerequisites

- Python 3.9–3.11 (MediaPipe does not support 3.12 yet)
- A working webcam
- Chrome or Firefox

### Install

```bash
git clone <repo-url>
cd Exam-Guard-main
pip install -r requirements.txt
```

### Configure

Copy or edit `.env` with your own values:

```env
SECRET_KEY=your-secret-key
DATABASE=instance/examguard.db
ENCRYPTION_KEY=           # generate with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

MAIL_SERVER=smtp.gmail.com
MAIL_PORT=587
MAIL_USER=you@gmail.com
MAIL_PASS=your-app-password

VAPID_PUBLIC_KEY=          # generate with generate_vapid_keys.py
VAPID_PRIVATE_KEY=
VAPID_CLAIMS_EMAIL=mailto:you@domain.com
```

To generate VAPID keys:

```bash
python generate_vapid_keys.py
```

To enable FCM (optional): place your Firebase `serviceAccountKey.json` in the project root.

### Run

```bash
python app.py
```

Server starts at `http://localhost:5000`.

---

## Default Credentials

| Role | Email | Password |
|---|---|---|
| Teacher | teacher@school.edu | teacher123 |
| Student | student@school.edu | student123 |

These are seeded automatically on first run.

---

## API Reference

| Method | Endpoint | Description |
|---|---|---|
| POST | `/api/login` | Authenticate and receive a session token |
| POST | `/api/register` | Register a new student account |
| POST | `/api/forgot-password` | Send a password reset email |
| POST | `/api/reset-password` | Set a new password using a reset token |
| GET | `/api/exams` | List exams (teacher: all; student: assigned) |
| POST | `/api/exams` | Create an exam |
| PUT | `/api/exams/<id>` | Edit an exam |
| DELETE | `/api/exams/<id>` | Soft-archive an exam |
| POST | `/api/exams/<id>/questions` | Add questions to an exam |
| POST | `/api/exams/<id>/enroll` | Enroll students |
| POST | `/api/start_session` | Begin a monitoring session |
| POST | `/api/analyze_frame` | Submit a base64 webcam frame for analysis |
| POST | `/api/tab_switch` | Log a tab switch event |
| POST | `/api/end_session` | End session and save report |
| GET | `/api/status/<session_id>` | Get live session stats |
| GET | `/api/sessions` | List all sessions |
| GET | `/api/download_report/<id>` | Download session report as JSON |
| GET | `/api/download_csv/<id>` | Download violations log as CSV |
| POST | `/api/subscribe` | Register browser for Web Push |
| POST | `/api/archive_session/<id>` | Soft-archive a session |

---

## Known Limitations

| Limitation | Notes |
|---|---|
| No live webcam feed for teachers | High-risk events trigger push notifications instead |
| Multiple monitors not detectable | Noted in generated reports |
| Glasses can cause false gaze positives | Time-gating (3–5 second threshold) reduces this |
| Token store is in-memory | Tokens are lost on server restart; use a persistent store before deploying |
| MediaPipe limited to Python ≤ 3.11 | Use `mediapipe-silicon` on Apple Silicon if needed |

---

## Ethical Design

- Students are shown exactly what will be monitored before consenting
- No raw video is stored — only event timestamps and metadata
- All flags require instructor review before any action is taken
- Reports include a disclaimer about environmental factors
- Violations use time-gated thresholds to avoid penalizing brief, innocent movements

This system is intended as a supplementary tool for educational institutions. It should not be used as the sole basis for academic disciplinary action. Deployment must comply with applicable privacy laws (FERPA, GDPR, RA 10173, and any institutional policy that applies).
