"""
ExamGuard — Exams blueprint
CRUD for exams, questions, enrollments, submissions, student exam list.

CHANGES:
- DELETE /api/exams/<id> now soft-archives instead of permanent deletion
- GET /api/exams excludes archived exams
- /api/student/exams excludes archived exams
"""
import hashlib
import json
import logging
import sqlite3
from datetime import datetime

from flask import Blueprint, request, jsonify, g

from auth import login_required, get_token_from_request, verify_token
from database import get_db
from grading import grade_submission
from blueprints.push_bp import send_push_to_user

logger   = logging.getLogger(__name__)
bp_exams = Blueprint('exams', __name__)


# ── Student exam list ─────────────────────────────────────────────────────────

@bp_exams.get('/api/student/exams')
def student_exams():
    try:
        db    = get_db()
        token = get_token_from_request()
        info  = verify_token(token)

        uid = None
        if info and isinstance(info.get('user_id'), int) and info['user_id'] > 0:
            uid = info['user_id']

        if uid:
            rows = db.execute('''
                SELECT DISTINCT e.*,
                    (SELECT COUNT(*) FROM questions WHERE exam_id=e.id) AS question_count,
                    (SELECT COUNT(*) FROM exam_submissions
                     WHERE exam_id=e.id AND student_id=?) AS already_submitted
                FROM exams e
                LEFT JOIN exam_enrollments ee ON ee.exam_id=e.id
                WHERE e.status IN ('active','scheduled')
                  AND COALESCE(e.is_archived,0)=0
                  AND (ee.student_id=? OR NOT EXISTS (
                        SELECT 1 FROM exam_enrollments WHERE exam_id=e.id))
                ORDER BY CASE e.status WHEN 'active' THEN 0 ELSE 1 END,
                         COALESCE(e.scheduled_start,'9999')
            ''', (uid, uid)).fetchall()
        else:
            rows = db.execute('''
                SELECT e.*,
                    (SELECT COUNT(*) FROM questions WHERE exam_id=e.id) AS question_count,
                    0 AS already_submitted
                FROM exams e
                WHERE e.status IN ('active','scheduled')
                  AND COALESCE(e.is_archived,0)=0
                ORDER BY CASE e.status WHEN 'active' THEN 0 ELSE 1 END,
                         COALESCE(e.scheduled_start,'9999')
            ''').fetchall()

        return jsonify([dict(r) for r in rows])
    except Exception as e:
        logger.error('/api/student/exams error: %s', e, exc_info=True)
        return jsonify({'error': str(e)}), 500


# ── Teacher exam CRUD ─────────────────────────────────────────────────────────

@bp_exams.get('/api/exams')
def get_exams():
    db    = get_db()
    exams = db.execute("SELECT * FROM exams WHERE COALESCE(is_archived,0)=0 ORDER BY created_at DESC").fetchall()
    result = []
    for e in exams:
        ex = dict(e)
        ex['question_count'] = db.execute(
            'SELECT COUNT(*) FROM questions WHERE exam_id=?', (e['id'],)).fetchone()[0]
        ex['enrolled_count'] = db.execute(
            'SELECT COUNT(*) FROM exam_enrollments WHERE exam_id=?', (e['id'],)).fetchone()[0]
        result.append(ex)
    return jsonify(result)


@bp_exams.get('/api/exams/<int:exam_id>')
def get_exam(exam_id):
    db   = get_db()
    exam = db.execute('SELECT * FROM exams WHERE id=?', (exam_id,)).fetchone()
    if not exam:
        return jsonify({'error': 'Not found'}), 404
    questions = db.execute(
        'SELECT * FROM questions WHERE exam_id=? ORDER BY order_num', (exam_id,)
    ).fetchall()
    return jsonify({**dict(exam), 'questions': [dict(q) for q in questions]})


@bp_exams.post('/api/exams')
def create_exam():
    data = request.get_json(silent=True) or {}
    if not data.get('title'):
        return jsonify({'error': 'Title is required'}), 400
    db = get_db()
    c  = db.execute('''
        INSERT INTO exams (title,subject,duration_minutes,passing_score,
                           instructions,status,scheduled_start,scheduled_end,created_by)
        VALUES (?,?,?,?,?,?,?,?,1)
    ''', (
        data['title'], data.get('subject', ''),
        int(data.get('duration_minutes', 60)),
        int(data.get('passing_score', 75)),
        data.get('instructions', ''),
        data.get('status', 'draft'),
        data.get('scheduled_start') or None,
        data.get('scheduled_end')   or None,
    ))
    exam_id = c.lastrowid
    for i, q in enumerate(data.get('questions', [])):
        if not q.get('text'):
            continue
        db.execute('''
            INSERT INTO questions
            (exam_id,question_text,question_type,choices,correct_answer,points,order_num)
            VALUES (?,?,?,?,?,?,?)
        ''', (
            exam_id, q['text'], q.get('type', 'mc'),
            json.dumps(q.get('choices', [])),
            q.get('correct_answer', 0),
            int(q.get('points', 1)), i + 1
        ))

    enrolled_ids = []
    for sid in data.get('enroll_students', []):
        try:
            token = hashlib.sha256(
                f'{exam_id}{sid}{str(datetime.now())}'.encode()
            ).hexdigest()
            db.execute('INSERT INTO exam_enrollments (exam_id,student_id,token) VALUES (?,?,?)',
                       (exam_id, sid, token))
            enrolled_ids.append(sid)
        except sqlite3.IntegrityError:
            pass
    db.commit()

    # Notify enrolled students when exam is created (scheduled or active)
    status = data.get('status', 'draft')
    if status in ('scheduled', 'active') and enrolled_ids:
        scheduled_start = data.get('scheduled_start')
        if status == 'scheduled' and scheduled_start:
            notif_title = 'New Exam Scheduled'
            notif_body  = f'{data["title"]} has been scheduled for {scheduled_start}. Duration: {data.get("duration_minutes", 60)} min.'
        elif status == 'active':
            notif_title = 'Exam is Now Live'
            notif_body  = f'{data["title"]} is now available. Open ExamGuard to begin.'
        else:
            notif_title = 'New Exam Uploaded'
            notif_body  = f'{data["title"]} has been added to your exams. Check ExamGuard for details.'

        for sid in enrolled_ids:
            try:
                send_push_to_user(
                    user_id=sid,
                    title=notif_title,
                    body=notif_body,
                    url='/student/dashboard',
                    tag=f'exam-created-{exam_id}',
                    require_interaction=True,
                )
            except Exception as e:
                logger.warning('Push to student %s failed: %s', sid, e)

    return jsonify({'success': True, 'exam_id': exam_id})


@bp_exams.put('/api/exams/<int:exam_id>')
def update_exam(exam_id):
    data = request.get_json(silent=True) or {}
    db   = get_db()
    db.execute('''
        UPDATE exams SET title=?,subject=?,duration_minutes=?,passing_score=?,
        instructions=?,status=?,scheduled_start=?,scheduled_end=?,updated_at=datetime('now')
        WHERE id=?
    ''', (
        data.get('title'), data.get('subject'),
        data.get('duration_minutes'), data.get('passing_score'),
        data.get('instructions'), data.get('status'),
        data.get('scheduled_start') or None,
        data.get('scheduled_end')   or None,
        exam_id
    ))
    db.commit()

    # Notify enrolled students when exam status changes
    new_status = data.get('status')
    if new_status in ('active', 'scheduled'):
        exam     = db.execute('SELECT * FROM exams WHERE id=?', (exam_id,)).fetchone()
        enrolled = db.execute(
            'SELECT student_id FROM exam_enrollments WHERE exam_id=?', (exam_id,)
        ).fetchall()

        if new_status == 'active':
            notif_title = 'Exam is Now Live'
            notif_body  = f'{exam["title"]} is now available. Open ExamGuard to begin.'
            tag         = f'exam-start-{exam_id}'
        else:
            scheduled_start = data.get('scheduled_start') or exam['scheduled_start']
            notif_title = 'Exam Scheduled'
            notif_body  = f'{exam["title"]} has been scheduled for {scheduled_start}. Duration: {exam["duration_minutes"]} min.'
            tag         = f'exam-scheduled-{exam_id}'

        for row in enrolled:
            if row['student_id']:
                try:
                    send_push_to_user(
                        user_id=row['student_id'],
                        title=notif_title,
                        body=notif_body,
                        url='/student/dashboard',
                        tag=tag,
                        require_interaction=True,
                    )
                except Exception as e:
                    logger.warning('Push to student %s failed: %s', row['student_id'], e)

    return jsonify({'success': True})


@bp_exams.delete('/api/exams/<int:exam_id>')
def delete_exam(exam_id):
    """Soft-delete: archive the exam instead of permanently deleting it."""
    db = get_db()
    db.execute(
        "UPDATE exams SET is_archived=1, archived_at=datetime('now') WHERE id=?",
        (exam_id,)
    )
    db.commit()
    return jsonify({'success': True, 'archived': True})


@bp_exams.post('/api/exams/<int:exam_id>/enroll')
def enroll_students(exam_id):
    data     = request.get_json(silent=True) or {}
    db       = get_db()
    enrolled = []
    for sid in data.get('student_ids', []):
        try:
            token = hashlib.sha256(
                f'{exam_id}{sid}{str(datetime.now())}'.encode()
            ).hexdigest()
            db.execute('INSERT INTO exam_enrollments (exam_id,student_id,token) VALUES (?,?,?)',
                       (exam_id, sid, token))
            enrolled.append(sid)
        except sqlite3.IntegrityError:
            pass
    db.commit()

    # Notify newly enrolled students
    if enrolled:
        exam = db.execute('SELECT * FROM exams WHERE id=?', (exam_id,)).fetchone()
        if exam:
            if exam['status'] == 'active':
                notif_title = 'Exam is Now Live'
                notif_body  = f'You have been enrolled in {exam["title"]}. It is currently active — open ExamGuard to begin.'
            elif exam['status'] == 'scheduled' and exam['scheduled_start']:
                notif_title = 'New Exam Scheduled'
                notif_body  = f'You have been enrolled in {exam["title"]}, scheduled for {exam["scheduled_start"]}.'
            else:
                notif_title = 'New Exam Uploaded'
                notif_body  = f'You have been enrolled in {exam["title"]}. Check ExamGuard for details.'

            for sid in enrolled:
                try:
                    send_push_to_user(
                        user_id=sid,
                        title=notif_title,
                        body=notif_body,
                        url='/student/dashboard',
                        tag=f'exam-enroll-{exam_id}',
                        require_interaction=True,
                    )
                except Exception as e:
                    logger.warning('Push to student %s failed: %s', sid, e)

    return jsonify({'success': True, 'enrolled': enrolled})


# ── Submissions ───────────────────────────────────────────────────────────────

@bp_exams.post('/api/submit_exam')
def submit_exam():
    data       = request.get_json(silent=True) or {}
    session_id = data.get('session_id')
    exam_id    = data.get('exam_id')
    answers    = data.get('answers', {})

    if not session_id:
        return jsonify({'error': 'session_id required'}), 400

    db = get_db()

    token      = get_token_from_request()
    info       = verify_token(token)
    student_id = None
    if info and isinstance(info.get('user_id'), int) and info['user_id'] > 0:
        student_id = info['user_id']
    if student_id is None:
        student_id = data.get('student_id')

    if exam_id:
        result = grade_submission(int(exam_id), answers, db)
    else:
        result = {'score': 0, 'max_score': 0, 'percentage': 0, 'breakdown': []}

    exam    = db.execute('SELECT passing_score FROM exams WHERE id=?', (exam_id,)).fetchone()
    passing = exam['passing_score'] if exam else 75
    passed  = result['percentage'] >= passing

    db.execute('''
        INSERT INTO exam_submissions
        (session_id,exam_id,student_id,answers,score,max_score,percentage,passed,graded_at)
        VALUES (?,?,?,?,?,?,?,?,datetime('now'))
    ''', (
        session_id, exam_id, student_id,
        json.dumps(answers, ensure_ascii=False),
        result['score'], result['max_score'], result['percentage'], int(passed)
    ))
    db.commit()
    logger.info('Submission graded: %s — %.1f%%', session_id, result['percentage'])

    # Notify student of their result
    if student_id:
        exam_title = ''
        if exam_id:
            ex = db.execute('SELECT title FROM exams WHERE id=?', (exam_id,)).fetchone()
            if ex:
                exam_title = ex['title']
        status = '✅ Passed' if passed else '❌ Not passed'
        try:
            send_push_to_user(
                user_id=student_id,
                title=f'{status} — {exam_title or "Exam"} Results',
                body=f'You scored {result["percentage"]:.1f}% ({result["score"]}/{result["max_score"]} pts). Tap to view your report.',
                url='/student/dashboard',
                tag=f'result-{session_id}',
                require_interaction=True,
            )
        except Exception as e:
            logger.warning('Push result to student %s failed: %s', student_id, e)

    return jsonify({**result, 'passed': passed, 'passing_score': passing})