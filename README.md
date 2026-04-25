# 🛡 ExamGuard — AI-Powered Online Exam Monitoring System

ExamGuard is a **decision-support tool** for online exam proctoring using real-time computer vision. It monitors student behavior during online examinations and generates transparent reports for instructor review — not automatic cheating verdicts.

---

## 📐 System Architecture

```
exam_monitor/
├── app.py                        # Flask backend + CV analysis (OpenCV + MediaPipe)
├── requirements.txt              # Python dependencies
├── templates/
│   ├── index.html                # Landing & consent page
│   ├── exam.html                 # Exam interface with live monitoring
│   ├── report.html               # Instructor report view
│   ├── teacher_dashboard.html    # Teacher portal
│   └── student_dashboard.html   # Student portal
├── logs/                         # Session logs (auto-created)
└── reports/                      # JSON + CSV reports (auto-created)
```

---

## ✅ Current Status: ~45% Complete

### Done ✓
- Full Flask backend with all API endpoints
- MediaPipe face detection + face mesh pipeline
- Gaze analysis via landmark-based head pose estimation
- Lighting quality checks via OpenCV
- Tab-switch detection (Browser Visibility API)
- Risk scoring engine
- All HTML/CSS UI (Landing, Exam, Report, Teacher Dashboard, Student Dashboard)
- Session logging (JSON + CSV export)
- SocketIO real-time event streaming
- Demo login system

### In Progress 🔄
- Real authentication (currently mock/demo users)
- Database integration (sessions stored in-memory only)
- Teacher ↔ Student exam assignment flow

### Planned 📋
- SQLite/PostgreSQL persistent storage
- Email report delivery
- Audio monitoring (pyaudio)
- Multi-exam support with scheduling
- Student result viewing after instructor review

---

## ⚙️ Setup Instructions

### 1. Prerequisites
- Python 3.9–3.11 (MediaPipe requires ≤ 3.11)
- A working webcam
- Modern browser (Chrome/Firefox recommended)

### 2. Install Dependencies

```bash
cd examguard
pip install -r requirements.txt
```

> **Note on MediaPipe:** If you encounter issues on macOS Apple Silicon, use:
> ```bash
> pip install mediapipe-silicon
> ```

### 3. Run the Server

```bash
python app.py
```

Server starts at: **http://localhost:5000**

---

## 🔑 Demo Credentials

| Role    | Email                  | Password     |
|---------|------------------------|--------------|
| Teacher | teacher@school.edu     | teacher123   |
| Student | student@school.edu     | student123   |

> Any email/password combination will also work in demo mode.

---

## 🔍 Detection Capabilities

| Detection Type       | Method                        | Threshold    | Severity |
|----------------------|-------------------------------|--------------|----------|
| No Face Present      | MediaPipe Face Detection      | 5 seconds    | High     |
| Multiple Faces       | MediaPipe Face Detection      | 2 seconds    | Critical |
| Gaze Deviation       | Face Mesh landmark analysis   | 3 seconds    | Medium   |
| Tab Switch           | Browser Visibility API        | Immediate    | High     |
| Poor Lighting        | OpenCV brightness/contrast    | Per frame    | Warning  |

---

## 🔬 Technical Implementation

### Computer Vision Pipeline (per frame)
1. **Frame Capture** — JavaScript captures webcam feed at ~1.5s intervals via Canvas API
2. **Base64 Encoding** — Frame encoded as JPEG and sent to Flask `/api/analyze_frame`
3. **Face Detection** — `mediapipe.solutions.face_detection` (Model 0, short-range)
4. **Face Mesh** — `mediapipe.solutions.face_mesh` with 468 3D landmarks
5. **Gaze Analysis** — Landmark-based head pose estimation using nose tip (1), eye (33, 263), and ear (234, 454) positions
6. **Lighting Check** — OpenCV grayscale mean (brightness) + std dev (contrast)
7. **Event Logging** — Time-gated violation detection with cooldown to avoid duplicate logs

### Gaze Detection Logic
```python
face_width = |right_ear.x - left_ear.x|
eye_center_x = (left_eye.x + right_eye.x) / 2
nose_offset = (nose.x - eye_center_x) / face_width
looking_away = |nose_offset| > 0.3 OR nose.y - eye_center_y < -0.05
```

### Risk Scoring
```
score = (face_absence_events × 15) + (multiple_face_events × 25)
      + (look_away_events × 10)    + (tab_switches × 20)

normalized_score = score / exam_duration_minutes

Low:    < 5
Medium: 5–15
High:   > 15
```

---

## 🌐 API Endpoints

| Method | Endpoint                     | Description                          |
|--------|------------------------------|--------------------------------------|
| GET    | `/`                          | Landing page with consent form       |
| GET    | `/exam`                      | Exam interface                       |
| GET    | `/teacher/dashboard`         | Teacher portal                       |
| GET    | `/student/dashboard`         | Student portal                       |
| GET    | `/report/<session_id>`       | Instructor report page               |
| POST   | `/api/login`                 | Login / register                     |
| POST   | `/api/start_session`         | Initialize a monitoring session      |
| POST   | `/api/analyze_frame`         | Analyze a base64 webcam frame        |
| POST   | `/api/tab_switch`            | Log a tab switch event               |
| POST   | `/api/end_session`           | End session and generate report      |
| GET    | `/api/status/<session_id>`   | Get live session statistics          |
| GET    | `/api/download_report/<id>`  | Download JSON report                 |
| GET    | `/api/download_csv/<id>`     | Download CSV violations log          |

---

## ⚠️ Known Limitations & Mitigations

| Limitation                  | Mitigation                                         |
|-----------------------------|----------------------------------------------------|
| Poor lighting               | Real-time brightness/contrast check + user warning |
| Low camera quality          | Minimum 480p recommended; shown in UI              |
| Network lag                 | Frame analysis batched every 1.5s, non-blocking    |
| False positives (glasses)   | Time-gated events (≥3–5s) reduce false flags       |
| Privacy concerns            | No raw video stored; only event logs saved         |
| Multiple monitors           | Cannot detect; noted as limitation in report       |

---

## 🧭 Ethical Design Principles

1. **Transparency** — Students are told exactly what is monitored before consenting
2. **No Auto-Judgment** — All flags require human instructor review
3. **No Video Storage** — Only event metadata is saved, never raw video
4. **Contextual Awareness** — Reports include disclaimer about environmental limitations
5. **Proportionality** — Only severe/sustained behaviors are flagged (time-gated thresholds)
6. **Student Right to Explain** — Report encourages instructors to hear student perspective

---

## 🚀 Next Steps to Complete

- [ ] Add SQLite database for persistent sessions and users
- [ ] Implement real JWT-based authentication
- [ ] Build teacher → exam → student assignment flow
- [ ] Add audio monitoring with `pyaudio`
- [ ] Email report delivery with `smtplib`
- [ ] Instructor real-time dashboard with WebSocket push

---

## 📄 License & Responsible Use

This software is intended for **educational institutions** as a **supplementary monitoring tool**. It must be used in compliance with applicable privacy laws (FERPA, GDPR, RA 10173 in the Philippines, etc.) and institutional policies. Students must provide informed consent before monitoring begins.

**Never use this system as the sole basis for academic disciplinary action.**
