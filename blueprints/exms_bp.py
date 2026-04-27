"""
ExamGuard — Exams blueprint
CRUD for exams, questions, enrollments, submissions, student exam list.
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

logger   = logging.getLogger(__name__)
bp_exams = Blueprint('exams', __name__)


# ── Student exam list ─────────────────────────────────────────────────────────

@bp_exams.get('/api/student/exams')
def student_exams():
    try:
        db    = get_db()
        token = get_token_from_request()
        info  = verify_token(token)
        uid   = info['user_id'] if (info and info.get('user_id') and info['user_id'] > 0) else None

        if uid:
            rows = db.execute('''
                SELECT DISTINCT e.*,
                    (SELECT COUNT(*) FROM questions WHERE exam_id=e.id) AS question_count
                FROM exams e
                LEFT JOIN exam_enrollments ee ON ee.exam_id=e.id
                WHERE e.status IN ('active','scheduled')
                  AND (ee.student_id=? OR NOT EXISTS (
                        SELECT 1 FROM exam_enrollments WHERE exam_id=e.id))
                ORDER BY CASE e.status WHEN 'active' THEN 0 ELSE 1 END,
                         COALESCE(e.scheduled_start,'9999')
            ''', (uid,)).fetchall()
        else:
            rows = db.execute('''
                SELECT e.*,
                    (SELECT COUNT(*) FROM questions WHERE exam_id=e.id) AS question_count
                FROM exams e
                WHERE e.status IN ('active','scheduled')
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
    exams = db.execute('SELECT * FROM exams ORDER BY created_at DESC').fetchall()
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
    for sid in data.get('enroll_students', []):
        try:
            token = hashlib.sha256(f'{exam_id}{sid}{datetime.now()}'.encode()).hexdigest()
            db.execute('INSERT INTO exam_enrollments (exam_id,student_id,token) VALUES (?,?,?)',
                       (exam_id, sid, token))
        except sqlite3.IntegrityError:
            pass
    db.commit()
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
    return jsonify({'success': True})


@bp_exams.delete('/api/exams/<int:exam_id>')
def delete_exam(exam_id):
    db = get_db()
    db.execute('DELETE FROM exams WHERE id=?', (exam_id,))
    db.commit()
    return jsonify({'success': True})


@bp_exams.post('/api/exams/<int:exam_id>/enroll')
def enroll_students(exam_id):
    data     = request.get_json(silent=True) or {}
    db       = get_db()
    enrolled = []
    for sid in data.get('student_ids', []):
        try:
            token = hashlib.sha256(f'{exam_id}{sid}{datetime.now()}'.encode()).hexdigest()
            db.execute('INSERT INTO exam_enrollments (exam_id,student_id,token) VALUES (?,?,?)',
                       (exam_id, sid, token))
            enrolled.append(sid)
        except sqlite3.IntegrityError:
            pass
    db.commit()
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

    # Resolve student from token
    token      = get_token_from_request()
    info       = verify_token(token)
    student_id = info['user_id'] if (info and info.get('user_id', -1) > 0) else data.get('student_id')

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
    return jsonify({**result, 'passed': passed, 'passing_score': passing})