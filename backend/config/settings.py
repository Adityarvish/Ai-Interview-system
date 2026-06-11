import os
from pathlib import Path
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).parent.parent
load_dotenv(ROOT_DIR / '.env')

class Config:
    MONGO_URL = os.environ.get('MONGO_URL', 'mongodb://localhost:27017')
    DB_NAME   = os.environ.get('DB_NAME',   'ai_interview_db')

    GROQ_API_KEY      = os.environ.get('GROQ_API_KEY', '')
    GROQ_PRIMARY_MODEL  = os.environ.get('GROQ_PRIMARY_MODEL',  'llama-3.3-70b-versatile')
    GROQ_FALLBACK_MODEL = os.environ.get('GROQ_FALLBACK_MODEL', 'llama-3.1-8b-instant')

    FLASK_HOST = os.environ.get('FLASK_HOST', '0.0.0.0')
    FLASK_PORT = int(os.environ.get('FLASK_PORT', 5000))

    MAX_INTERVIEW_DURATION = int(os.environ.get('MAX_INTERVIEW_DURATION', 2700))

    UPLOAD_FOLDER  = ROOT_DIR / 'uploads'
    RESUME_FOLDER  = UPLOAD_FOLDER / 'resumes'
    AUDIO_FOLDER   = UPLOAD_FOLDER / 'audio'

    ALLOWED_RESUME_EXTENSIONS = {'pdf', 'txt'}
    ALLOWED_AUDIO_EXTENSIONS  = {'webm', 'wav', 'ogg', 'mp3'}

    MAX_RESUME_SIZE = 10 * 1024 * 1024
    MAX_AUDIO_SIZE  = 50 * 1024 * 1024

Config.UPLOAD_FOLDER.mkdir(exist_ok=True)
Config.RESUME_FOLDER.mkdir(exist_ok=True)
Config.AUDIO_FOLDER.mkdir(exist_ok=True)
