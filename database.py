"""
ExamGuard — Database
Thread-safe SQLite access with row_factory, WAL mode, and FK enforcement.

ADDITIONS:
- is_archived + archived_at columns on exams, users, sessions for soft-delete
- password_reset_tokens table for forgot-password flow
"""
import sqlite3
import json
import logging
import os
import hashlib
from flask import g, current_app
from crypto import encrypt, decrypt

logger = logging.getLogger(__name__)


def get_db():
    if 'db' not in g:
        db_path = current_app.config['DATABASE']
        os.makedirs(os.path.dirname(db_path) if os.path.dirname(db_path) else '.', exist_ok=True)
        g.db = sqlite3.connect(db_path, detect_types=sqlite3.PARSE_DECLTYPES, check_same_thread=False)
        g.db.row_factory = sqlite3.Row
        g.db.execute('PRAGMA journal_mode=WAL')
        g.db.execute('PRAGMA foreign_keys=ON')
        g.db.execute('PRAGMA synchronous=NORMAL')
    return g.db


def close_db(e=None):
    db = g.pop('db', None)
    if db:
        db.close()


def init_db(app):
    """Create all tables and seed demo data."""
    import bcrypt

    db_path = app.config['DATABASE']
    os.makedirs(os.path.dirname(db_path) if os.path.dirname(db_path) else '.', exist_ok=True)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    conn.execute('PRAGMA journal_mode=WAL')
    c = conn.cursor()

    c.executescript('''
        CREATE TABLE IF NOT EXISTS users (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            email           TEXT,
            email_hash      TEXT UNIQUE NOT NULL DEFAULT '',
            password_hash   TEXT NOT NULL,
            role            TEXT NOT NULL CHECK(role IN ('teacher','student')),
            name            TEXT NOT NULL,
            student_id      TEXT,
            avatar_initials TEXT,
            is_active       INTEGER DEFAULT 1,
            is_archived     INTEGER DEFAULT 0,
            archived_at     TEXT,
            created_at      TEXT DEFAULT (datetime('now')),
            last_login      TEXT
        );

        CREATE TABLE IF NOT EXISTS password_reset_tokens (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER REFERENCES users(id) ON DELETE CASCADE,
            token      TEXT UNIQUE NOT NULL,
            expires_at TEXT NOT NULL,
            used       INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
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
            is_archived      INTEGER DEFAULT 0,
            archived_at      TEXT,
            created_at       TEXT DEFAULT (datetime('now')),
            updated_at       TEXT DEFAULT (datetime('now'))
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
            exam_id     INTEGER REFERENCES exams(id) ON DELETE CASCADE,
            student_id  INTEGER REFERENCES users(id) ON DELETE CASCADE,
            assigned_at TEXT DEFAULT (datetime('now')),
            token       TEXT,
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
            is_archived      INTEGER DEFAULT 0,
            archived_at      TEXT,
            created_at       TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS violations (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id      TEXT REFERENCES sessions(session_id) ON DELETE CASCADE,
            timestamp       TEXT,
            elapsed_seconds REAL,
            type            TEXT,
            details         TEXT,
            severity        TEXT
        );

        CREATE TABLE IF NOT EXISTS audit_log (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER REFERENCES users(id),
            action     TEXT NOT NULL,
            target     TEXT,
            ip         TEXT,
            user_agent TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_sessions_student  ON sessions(student_id);
        CREATE INDEX IF NOT EXISTS idx_sessions_exam     ON sessions(exam_id);
        CREATE INDEX IF NOT EXISTS idx_violations_session ON violations(session_id);
        CREATE INDEX IF NOT EXISTS idx_audit_user        ON audit_log(user_id);
    ''')

    # Run migrations for existing databases (add new columns if missing)
    migrations = [
        ("users", "is_archived", "ALTER TABLE users ADD COLUMN is_archived INTEGER DEFAULT 0"),
        ("users", "archived_at", "ALTER TABLE users ADD COLUMN archived_at TEXT"),
        ("users", "email_hash",  "ALTER TABLE users ADD COLUMN email_hash TEXT"),
        ("exams", "is_archived", "ALTER TABLE exams ADD COLUMN is_archived INTEGER DEFAULT 0"),
        ("exams", "archived_at", "ALTER TABLE exams ADD COLUMN archived_at TEXT"),
        ("sessions", "is_archived", "ALTER TABLE sessions ADD COLUMN is_archived INTEGER DEFAULT 0"),
        ("sessions", "archived_at", "ALTER TABLE sessions ADD COLUMN archived_at TEXT"),
    ]
    for table, col, sql in migrations:
        try:
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
            if col not in cols:
                conn.execute(sql)
                logger.info('Migration applied: %s.%s', table, col)
        except Exception as e:
            logger.warning('Migration skipped %s.%s: %s', table, col, e)

    conn.commit()
    conn.close()
    logger.info('Database initialized ✓')