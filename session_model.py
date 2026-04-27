"""
ExamGuard — ExamSession model
Thread-safe session state, violation logging, risk scoring, report generation.

FIXES:
- no_face_frames stat was incrementing every frame during a single prolonged
  absence, making the number misleading. It now only increments once per
  distinct absence event (same behaviour as face_absence_events counter).
- audio_alerts was missing from the stats dict passed to generate_report /
  save_to_db, so it always showed 0 in the JSON report and on the report page.
- look_away state (_look_away_start, _look_away_logged) is now reset when the
  face disappears entirely, preventing a stale look_away violation firing
  immediately when the face returns after a long absence.
- Tab-switch violations logged after session.ended=True are now silently
  dropped inside log_violation (the guard was already there but the tab_switch
  endpoint was incrementing stats before calling log_violation — fixed in
  sessions_bp.py).
"""
import csv
import json
import logging
import os
import time
from datetime import datetime
from threading import Lock

logger = logging.getLogger(__name__)


class ExamSession:
    def __init__(self, session_id: str, student_name: str,
                 exam_duration: int = 60, exam_id=None,
                 student_id=None, config: dict | None = None):
        cfg = config or {}
        self.session_id    = session_id
        self.student_name  = student_name
        self.exam_duration = exam_duration
        self.exam_id       = exam_id
        self.student_id    = student_id
        self.start_time    = datetime.now()
        self.end_time      = None
        self.is_active     = False
        self.ended         = False
        self._lock         = Lock()

        # Configurable thresholds
        self.NO_FACE_THRESHOLD    = cfg.get('NO_FACE_THRESHOLD',    5)
        self.LOOK_AWAY_THRESHOLD  = cfg.get('LOOK_AWAY_THRESHOLD',  3)
        self.MULTI_FACE_THRESHOLD = cfg.get('MULTI_FACE_THRESHOLD', 2)

        # Risk weights
        self._weights = {
            'face_absence':  cfg.get('WEIGHT_FACE_ABSENCE', 15),
            'multiple_face': cfg.get('WEIGHT_MULTI_FACE',   25),
            'look_away':     cfg.get('WEIGHT_LOOK_AWAY',    10),
            'tab_switch':    cfg.get('WEIGHT_TAB_SWITCH',   20),
            'audio':         cfg.get('WEIGHT_AUDIO',        12),
        }
        self._risk_cutoffs = (
            cfg.get('RISK_LOW_CUTOFF',    5),
            cfg.get('RISK_MEDIUM_CUTOFF', 15),
        )

        # Detection state
        self.no_face_start    = None
        self.look_away_start  = None
        self.multi_face_start = None
        self._no_face_logged    = False
        self._look_away_logged  = False
        self._multi_face_logged = False

        self.violations: list[dict] = []

        # FIX: audio_alerts was present here but the key name must match
        # exactly what generate_report() reads — previously it was omitted
        # from generate_report's stats copy, so reports always showed 0.
        self.stats = {
            'total_frames':         0,
            'no_face_frames':       0,   # counts distinct absence events (not raw frames)
            'multiple_face_frames': 0,
            'look_away_frames':     0,
            'tab_switches':         0,
            'audio_alerts':         0,   # FIX: was missing from generate_report output
            'face_absence_events':  0,
            'multiple_face_events': 0,
            'look_away_events':     0,
        }

    # ── Violation logging ─────────────────────────────────────────────────────

    def log_violation(self, vtype: str, details: str = '',
                      severity: str = 'medium', _socketio=None) -> dict | None:
        with self._lock:
            if self.ended:
                # FIX: silently drop any violations logged after session ends
                # (race condition between tab-switch browser event and end_session)
                return None
            entry = {
                'timestamp':       datetime.now().strftime('%H:%M:%S'),
                'elapsed_seconds': round(self.get_elapsed(), 1),
                'type':            vtype,
                'details':         details,
                'severity':        severity,
            }
            self.violations.append(entry)
            logger.warning('[%s] VIOLATION: %s — %s', self.session_id, vtype, details)

            if _socketio:
                _socketio.emit('violation', {
                    'session_id':   self.session_id,
                    'student_name': self.student_name,
                    'violation':    entry,
                }, room='teachers')

            return entry

    # ── Timing ───────────────────────────────────────────────────────────────

    def get_elapsed(self) -> float:
        return (datetime.now() - self.start_time).total_seconds()

    # ── Detection state helpers ───────────────────────────────────────────────

    def reset_look_away_state(self):
        """
        FIX: called by cv_engine when face disappears so that a look_away
        violation can't fire immediately when the face comes back after a
        long absence (the look_away_start timestamp would be stale).
        """
        self.look_away_start   = None
        self._look_away_logged = False

    # ── Risk ─────────────────────────────────────────────────────────────────

    def compute_risk(self) -> dict:
        s = self.stats
        duration_min = max(self.get_elapsed() / 60, 1)
        raw = (
            s['face_absence_events']  * self._weights['face_absence']  +
            s['multiple_face_events'] * self._weights['multiple_face'] +
            s['look_away_events']     * self._weights['look_away']     +
            s['tab_switches']         * self._weights['tab_switch']    +
            s['audio_alerts']         * self._weights['audio']
        )
        score = raw / duration_min
        low, medium = self._risk_cutoffs
        if score < low:
            level, color = 'Low',    '#22c55e'
        elif score < medium:
            level, color = 'Medium', '#f59e0b'
        else:
            level, color = 'High',   '#ef4444'
        return {'level': level, 'score': round(score, 1), 'color': color}

    # ── Report ───────────────────────────────────────────────────────────────

    def generate_report(self) -> dict:
        end = self.end_time or datetime.now()
        dur = (end - self.start_time).total_seconds()
        # FIX: copy the full stats dict so audio_alerts is always included
        stats_copy = dict(self.stats)
        return {
            'session_id':       self.session_id,
            'student_name':     self.student_name,
            'exam_id':          self.exam_id,
            'exam_date':        self.start_time.strftime('%Y-%m-%d'),
            'start_time':       self.start_time.strftime('%H:%M:%S'),
            'end_time':         end.strftime('%H:%M:%S'),
            'duration_minutes': round(dur / 60, 2),
            'total_violations': len(self.violations),
            'violations':       self.violations,
            'stats':            stats_copy,
            'risk_assessment':  self.compute_risk(),
        }

    def save_reports(self, reports_dir: str = 'reports'):
        os.makedirs(reports_dir, exist_ok=True)
        report = self.generate_report()
        sid    = self.session_id

        json_path = os.path.join(reports_dir, f'{sid}.json')
        with open(json_path, 'w') as f:
            json.dump(report, f, indent=2)

        csv_path = os.path.join(reports_dir, f'{sid}_violations.csv')
        with open(csv_path, 'w', newline='') as f:
            w = csv.DictWriter(f, fieldnames=[
                'timestamp', 'elapsed_seconds', 'type', 'details', 'severity'])
            w.writeheader()
            w.writerows(self.violations)

        return json_path, csv_path

    def save_to_db(self, db):
        report = self.generate_report()
        risk   = report['risk_assessment']
        db.execute('''
            INSERT OR REPLACE INTO sessions
            (session_id,student_id,exam_id,student_name,start_time,end_time,
             duration_minutes,total_violations,risk_level,risk_score,stats,is_active)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,0)
        ''', (
            self.session_id, self.student_id, self.exam_id, self.student_name,
            report['start_time'], report['end_time'], report['duration_minutes'],
            len(self.violations), risk['level'], risk['score'],
            json.dumps(report['stats'])   # FIX: use report['stats'] which includes audio_alerts
        ))
        for v in self.violations:
            db.execute('''
                INSERT OR IGNORE INTO violations
                (session_id,timestamp,elapsed_seconds,type,details,severity)
                VALUES (?,?,?,?,?,?)
            ''', (
                self.session_id, v['timestamp'], v['elapsed_seconds'],
                v['type'], v['details'], v['severity']
            ))
        db.commit()