import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    # FIX: os.urandom() on every import = new key on every restart = sessions lost.
    # The fallback is a stable dev key; override via SECRET_KEY env var in production.
    SECRET_KEY = os.environ.get('SECRET_KEY', 'examguard-dev-secret-change-in-production')
    DATABASE   = os.environ.get('DATABASE', 'instance/examguard.db')

    # Detection thresholds
    NO_FACE_THRESHOLD    = int(os.environ.get('NO_FACE_THRESHOLD',    5))
    LOOK_AWAY_THRESHOLD  = int(os.environ.get('LOOK_AWAY_THRESHOLD',  3))
    MULTI_FACE_THRESHOLD = int(os.environ.get('MULTI_FACE_THRESHOLD', 2))

    # Risk score weights
    WEIGHT_FACE_ABSENCE = int(os.environ.get('WEIGHT_FACE_ABSENCE', 15))
    WEIGHT_MULTI_FACE   = int(os.environ.get('WEIGHT_MULTI_FACE',   25))
    WEIGHT_LOOK_AWAY    = int(os.environ.get('WEIGHT_LOOK_AWAY',    10))
    WEIGHT_TAB_SWITCH   = int(os.environ.get('WEIGHT_TAB_SWITCH',   20))
    WEIGHT_AUDIO        = int(os.environ.get('WEIGHT_AUDIO',        12))

    # Risk level cutoffs
    RISK_LOW_CUTOFF    = float(os.environ.get('RISK_LOW_CUTOFF',    5))
    RISK_MEDIUM_CUTOFF = float(os.environ.get('RISK_MEDIUM_CUTOFF', 15))

    # Email
    MAIL_SERVER = os.environ.get('MAIL_SERVER', 'smtp.gmail.com')
    MAIL_PORT   = int(os.environ.get('MAIL_PORT', 587))
    MAIL_USER   = os.environ.get('MAIL_USER', '')
    MAIL_PASS   = os.environ.get('MAIL_PASS', '')

    # Rate limiting
    RATELIMIT_DEFAULT        = '200 per day;50 per hour'
    RATELIMIT_LOGIN          = '10 per minute'
    RATELIMIT_ANALYZE        = '100 per minute'
    RATELIMIT_STORAGE_URI    = 'memory://'

class DevelopmentConfig(Config):
    DEBUG = True

class ProductionConfig(Config):
    DEBUG = False
    FLASK_ENV = 'production'

config = {
    'development': DevelopmentConfig,
    'production':  ProductionConfig,
    'default':     DevelopmentConfig,
}