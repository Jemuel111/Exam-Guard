"""
ExamGuard — Computer Vision
Thread-safe face detection and gaze analysis.
MediaPipe is initialized per-call (thread-safe); falls back to Haar cascades.

FIXES:
- no_face_frames now increments only once per distinct absence event
  (when the absence timer first starts) rather than every frame, so the
  stat matches face_absence_events in meaning.
- look_away detection state is reset when the face disappears, preventing
  a stale look_away_start from firing a LOOK_AWAY violation immediately
  when the student's face returns after a long NO_FACE absence.
- Gaze analysis is now skipped entirely when face_count == 0 (was already
  gated on face_count == 1 but the early-return path could still reach
  is_looking_away via the else branch in some edge cases).
- decode_frame logs the exact exception and returns None cleanly; callers
  already handle None but the silent failure made debugging hard.
- Fixed duplicate 'if v:' block and corrected push_bp import path.
"""
import base64
import logging
import time

import cv2
import numpy as np

logger = logging.getLogger(__name__)

# ── Optional MediaPipe ────────────────────────────────────────────────────────
try:
    import mediapipe as mp
    _mp_solutions = mp.solutions
    USE_MEDIAPIPE = True
    logger.info('MediaPipe available — using face mesh pipeline')
except ImportError:
    USE_MEDIAPIPE = False
    logger.info('MediaPipe not installed — falling back to OpenCV Haar cascades')

# Haar cascade fallbacks (always available via opencv-python)
_face_cascade = cv2.CascadeClassifier(
    cv2.data.haarcascades + 'haarcascade_frontalface_default.xml')
_eye_cascade = cv2.CascadeClassifier(
    cv2.data.haarcascades + 'haarcascade_eye.xml')


# ── Lighting ─────────────────────────────────────────────────────────────────

def check_lighting(frame: np.ndarray) -> tuple[bool, float, float]:
    gray       = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    brightness = float(np.mean(gray))
    contrast   = float(np.std(gray))
    ok         = brightness > 40 and contrast > 10
    return ok, round(brightness, 1), round(contrast, 1)


# ── Face detection ────────────────────────────────────────────────────────────

def detect_faces(frame: np.ndarray) -> list[tuple]:
    """Returns list of (x, y, w, h) bounding boxes."""
    if USE_MEDIAPIPE:
        return _detect_mediapipe(frame)
    return _detect_haar(frame)


def _detect_mediapipe(frame: np.ndarray) -> list[tuple]:
    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    with _mp_solutions.face_detection.FaceDetection(
            model_selection=0, min_detection_confidence=0.5) as fd:
        result = fd.process(rgb)
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


def _detect_haar(frame: np.ndarray) -> list[tuple]:
    gray  = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    faces = _face_cascade.detectMultiScale(gray, 1.1, 5, minSize=(80, 80))
    return [tuple(f) for f in faces] if len(faces) > 0 else []


# ── Gaze analysis ─────────────────────────────────────────────────────────────

def is_looking_away(frame: np.ndarray, face_rect: tuple) -> bool:
    if USE_MEDIAPIPE:
        return _gaze_mediapipe(frame)
    return _gaze_haar(frame, face_rect)


def _gaze_mediapipe(frame: np.ndarray) -> bool:
    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    with _mp_solutions.face_mesh.FaceMesh(
            static_image_mode=False, max_num_faces=1,
            refine_landmarks=True, min_detection_confidence=0.5) as mesh:
        result = mesh.process(rgb)
    if not result.multi_face_landmarks:
        return True
    lm     = result.multi_face_landmarks[0].landmark
    nose   = lm[1];  l_eye = lm[33];  r_eye = lm[263]
    l_ear  = lm[234]; r_ear = lm[454]
    face_w = abs(r_ear.x - l_ear.x) or 0.001
    eye_cx = (l_eye.x + r_eye.x) / 2
    eye_cy = (l_eye.y + r_eye.y) / 2
    h_off  = abs(nose.x - eye_cx) / face_w
    v_off  = nose.y - eye_cy
    return h_off > 0.30 or v_off < -0.05


def _gaze_haar(frame: np.ndarray, face_rect: tuple) -> bool:
    try:
        x, y, w, h = face_rect
        fh, fw     = frame.shape[:2]
        h_off      = abs((x + w // 2) - fw // 2) / fw
        v_off      = ((y + h // 2) - fh // 2) / fh
        if h_off > 0.35 or v_off < -0.25:
            return True
        roi  = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)[y:y+h, x:x+w]
        eyes = _eye_cascade.detectMultiScale(roi, 1.1, 5, minSize=(20, 20))
        return len(eyes) == 0
    except Exception:
        return False


# ── Frame decode ──────────────────────────────────────────────────────────────

def decode_frame(frame_b64: str) -> np.ndarray | None:
    try:
        # Handle both "data:image/jpeg;base64,..." and raw base64
        if ',' in frame_b64:
            _, encoded = frame_b64.split(',', 1)
        else:
            encoded = frame_b64
        buf   = np.frombuffer(base64.b64decode(encoded), np.uint8)
        frame = cv2.imdecode(buf, cv2.IMREAD_COLOR)
        if frame is None:
            logger.error('Frame decode produced None — invalid image data')
        return frame
    except Exception as e:
        logger.error('Frame decode error: %s', e)
        return None


# ── Main analysis ─────────────────────────────────────────────────────────────

def process_frame(session, frame_b64: str, socketio=None) -> dict:
    """
    Analyse one webcam frame against the given ExamSession.
    Updates session.stats in-place and calls session.log_violation() as needed.
    Returns a status dict suitable for JSON response.
    """
    if session.ended:
        return {'status': 'ended'}

    frame = decode_frame(frame_b64)
    if frame is None:
        return {'status': 'error', 'message': 'Frame decode failed'}

    alerts = []
    now    = time.time()
    session.stats['total_frames'] += 1

    ok_light, brightness, _ = check_lighting(frame)
    if not ok_light:
        alerts.append({'type': 'POOR_LIGHTING', 'brightness': brightness})

    faces      = detect_faces(frame)
    face_count = len(faces)

    # ── No face ───────────────────────────────────────────────────────────────
    if face_count == 0:
        # FIX: only increment no_face_frames when the absence timer starts,
        # not every frame, so the stat count equals face_absence_events.
        if session.no_face_start is None:
            session.no_face_start = now
            session.stats['no_face_frames'] += 1  # one count per distinct event

        elapsed = now - session.no_face_start
        if elapsed >= session.NO_FACE_THRESHOLD and not session._no_face_logged:
            v = session.log_violation(
                'NO_FACE', f'No face detected for {round(elapsed)}s', 'high', socketio)
            if v:
                session.stats['face_absence_events'] += 1
                session._no_face_logged = True
                alerts.append({'type': 'NO_FACE', 'violation': v})
        else:
            alerts.append({'type': 'NO_FACE'})

        # FIX: reset look_away state when face disappears so a stale
        # look_away_start doesn't trigger an instant violation on return.
        session.reset_look_away_state()

    else:
        # Face present — reset no_face tracking
        session.no_face_start   = None
        session._no_face_logged = False

    # ── Multiple faces ────────────────────────────────────────────────────────
    if face_count > 1:
        session.stats['multiple_face_frames'] += 1
        if session.multi_face_start is None:
            session.multi_face_start = now
        elapsed = now - session.multi_face_start
        if elapsed >= session.MULTI_FACE_THRESHOLD and not session._multi_face_logged:
            v = session.log_violation(
                'MULTIPLE_FACES', f'{face_count} faces detected', 'critical', socketio)
            if v:
                session.stats['multiple_face_events'] += 1
                session._multi_face_logged = True
                alerts.append({'type': 'MULTIPLE_FACES', 'count': face_count, 'violation': v})
                try:
                    from blueprints.push_bp import send_push_to_teachers
                    send_push_to_teachers(
                        title='🚨 Multiple Faces Detected',
                        body=f'{session.student_name} — {face_count} people visible',
                        url=f'/report/{session.session_id}',
                        tag=f'multiface-{session.session_id}',
                        require_interaction=True,
                    )
                except Exception as push_err:
                    logger.warning('Push notify error: %s', push_err)
        else:
            alerts.append({'type': 'MULTIPLE_FACES', 'count': face_count})
    else:
        session.multi_face_start    = None
        session._multi_face_logged  = False

    # ── Gaze ──────────────────────────────────────────────────────────────────
    # FIX: explicitly gate on face_count == 1; skip gaze entirely if no face
    # or multiple faces (both are already handled above and gaze would be
    # meaningless / misleading in those states).
    if face_count == 1:
        if is_looking_away(frame, faces[0]):
            session.stats['look_away_frames'] += 1
            if session.look_away_start is None:
                session.look_away_start = now
            elapsed = now - session.look_away_start
            if elapsed >= session.LOOK_AWAY_THRESHOLD and not session._look_away_logged:
                v = session.log_violation(
                    'LOOK_AWAY', f'Looking away for {round(elapsed)}s', 'medium', socketio)
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
        'face_count':      face_count,
        'detection_mode':  'mediapipe' if USE_MEDIAPIPE else 'opencv',
    }