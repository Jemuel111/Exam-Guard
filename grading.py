"""
ExamGuard — Grading Engine
Auto-grades MC and TF questions; marks essay/FITB for instructor review.
"""
import json
import logging

logger = logging.getLogger(__name__)


def grade_submission(exam_id: int, answers: dict, db) -> dict:
    """
    Grade a student's answers against the stored question bank.

    answers: {str(question_id): value}  — keys are always strings from JSON.
    Returns: {score, max_score, percentage, breakdown}
    """
    questions = db.execute(
        'SELECT * FROM questions WHERE exam_id=? ORDER BY order_num',
        (exam_id,)
    ).fetchall()

    if not questions:
        logger.warning('No questions found for exam_id=%s', exam_id)
        return {'score': 0, 'max_score': 0, 'percentage': 0, 'breakdown': []}

    score = 0
    max_score = 0
    breakdown = []

    for q in questions:
        q = dict(q)
        max_score += q['points']

        # Answers come as string keys from JSON regardless of how they were stored
        student_ans = answers.get(str(q['id']), answers.get(q['id'], ''))
        earned = 0
        feedback = ''

        if q['question_type'] in ('mc', 'tf'):
            try:
                correct = int(q['correct_answer'])
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
            text = str(student_ans).strip()
            if text:
                # Give full marks; instructor reviews exact wording
                earned   = q['points']
                feedback = 'Submitted — instructor review'
            else:
                feedback = 'Not answered'

        else:  # essay
            text = str(student_ans).strip()
            if len(text) > 10:
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