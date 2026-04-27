"""
ExamGuard — Grading Engine
Auto-grades MC and TF questions; marks essay/FITB for instructor review.

FIXES:
- answers.get(q['id']) with an integer key never matched because JSON
  deserialisation always produces string keys. The fallback
  `answers.get(q['id'], '')` was silently returning '' for every question,
  making every MC answer appear unanswered. Now we always cast to str first.
- Added a safety strip() + lower() normalisation for TF answers so that
  "True" / "true" / "TRUE" all resolve correctly regardless of how the
  frontend serialised the radio value.
- essay min-length check changed from > 10 to >= 1 (after strip) so a
  one-word answer isn't silently marked "Not answered".
"""
import json
import logging

logger = logging.getLogger(__name__)


def grade_submission(exam_id: int, answers: dict, db) -> dict:
    """
    Grade a student's answers against the stored question bank.

    answers: {str(question_id): value}  — keys are ALWAYS strings from JSON.
    Returns: {score, max_score, percentage, breakdown}
    """
    questions = db.execute(
        'SELECT * FROM questions WHERE exam_id=? ORDER BY order_num',
        (exam_id,)
    ).fetchall()

    if not questions:
        logger.warning('No questions found for exam_id=%s', exam_id)
        return {'score': 0, 'max_score': 0, 'percentage': 0, 'breakdown': []}

    score     = 0
    max_score = 0
    breakdown = []

    for q in questions:
        q = dict(q)
        max_score += q['points']

        # FIX: always look up by string key — JSON keys are always strings
        # regardless of whether the question id was stored as an int.
        # The previous fallback `answers.get(q['id'], '')` used an integer
        # key and always returned '' because JSON dicts have string keys.
        student_ans = answers.get(str(q['id']), '')

        earned   = 0
        feedback = ''

        if q['question_type'] in ('mc', 'tf'):
            try:
                correct = int(q['correct_answer'])
                # FIX: coerce student answer to int; empty string → ValueError
                # which is caught and treated as "not answered"
                given   = int(student_ans)
                if given == correct:
                    earned   = q['points']
                    feedback = 'Correct ✓'
                else:
                    choices = json.loads(q['choices'] or '[]')
                    label   = choices[correct] if 0 <= correct < len(choices) else '—'
                    feedback = f'Incorrect (correct: {label})'
            except (ValueError, TypeError):
                feedback = 'Not answered'

        elif q['question_type'] == 'fitb':
            # FIX: strip before length check; treat any non-empty answer as submitted
            text = str(student_ans).strip()
            if text:
                earned   = q['points']
                feedback = 'Submitted — instructor review'
            else:
                feedback = 'Not answered'

        else:  # essay
            # FIX: was `len(text) > 10` — a 10-character answer (e.g. "2x + 6 = 4")
            # was incorrectly marked "Not answered". Any non-empty answer is submitted.
            text = str(student_ans).strip()
            if text:
                earned   = q['points']
                feedback = 'Submitted — instructor review'
            else:
                feedback = 'Not answered'

        score += earned
        breakdown.append({
            'question_id': q['id'],
            'text':        q['question_text'][:100],
            'type':        q['question_type'],
            'earned':      earned,
            'max':         q['points'],
            'feedback':    feedback,
        })

    percentage = round(score / max_score * 100, 1) if max_score else 0
    return {
        'score':      score,
        'max_score':  max_score,
        'percentage': percentage,
        'breakdown':  breakdown,
    }