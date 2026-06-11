# AI Voice Interview System

An AI-powered voice interview platform that conducts, evaluates, and scores job interviews end-to-end. Candidates speak their answers aloud; the system transcribes, analyzes, and produces a detailed evaluation report — all powered by the Groq API.

---

## Features

- **Voice-based interviews** — candidates answer questions by speaking; audio is captured in the browser and transcribed via Groq Whisper
- **AI-generated questions** — questions are dynamically generated based on the candidate's resume and job description using LLaMA 3.3 70B
- **Structured interview stages** — greeting → introduction → resume deep-dive → technical → behavioural → closing
- **RAG-enhanced context** — resume and job description are embedded with FAISS + sentence-transformers so every question is grounded in the candidate's actual background
- **AI voice responses** — the interviewer speaks back using Groq Orpheus TTS (natural-sounding voice)
- **Automated evaluation** — a single post-interview LLM call scores 7 dimensions and produces a recommendation (Shortlist / Hold / Reject)
- **Real-time communication** — Flask-SocketIO with gevent for low-latency audio streaming
- **Persistent storage** — all sessions, interactions, and evaluations stored in MongoDB

---

## Tech Stack

| Layer | Technology |
|---|---|
| Backend | Python, Flask, Flask-SocketIO, gevent |
| LLM | Groq API — LLaMA 3.3 70B (primary), LLaMA 3.1 8B (fallback) |
| Speech-to-Text | Groq Whisper (`whisper-large-v3-turbo`) |
| Text-to-Speech | Groq Orpheus TTS |
| Embeddings / RAG | `sentence-transformers/all-MiniLM-L6-v2` + FAISS |
| Database | MongoDB |
| Frontend | Vanilla JS, Tailwind CSS, Socket.IO client |

---

## Project Structure

```
ai_interview_groq/
├── backend/
│   ├── app.py                    # Flask app, SocketIO setup, boot sequence
│   ├── requirements.txt
│   ├── .env.example              # Copy to .env and fill in your keys
│   ├── config/
│   │   ├── settings.py           # Centralised config loaded from .env
│   │   └── mongodb.py            # MongoDB singleton
│   ├── routes/
│   │   ├── interview_routes.py   # REST API endpoints
│   │   └── socket_routes.py      # SocketIO event handlers
│   ├── services/
│   │   ├── interview_engine.py   # Question generation, stage management
│   │   ├── evaluator.py          # Post-interview scoring
│   │   ├── llm_service.py        # Groq LLM client (primary + fallback)
│   │   ├── speech_to_text.py     # Groq Whisper transcription
│   │   ├── text_to_speech.py     # Groq Orpheus TTS
│   │   ├── rag_service.py        # FAISS vector store + retrieval
│   │   ├── resume_parser.py      # PDF/TXT resume extraction
│   │   ├── mongo_service.py      # MongoDB CRUD operations
│   │   └── warm_cache.py         # Singleton caches + audio cleanup loop
│   └── uploads/
│       ├── resumes/              # Uploaded resume files (gitignored)
│       └── audio/                # Temp audio files (gitignored, auto-cleaned)
└── frontend/
    ├── index.html                # Landing page / interview setup form
    ├── interview.html            # Live interview screen
    ├── result.html               # Evaluation results page
    ├── css/style.css
    └── js/
        ├── landing.js
        ├── interview.js
        └── result.js
```

---

## Prerequisites

- Python 3.11+
- MongoDB running locally (or a connection string to a remote instance)
- A [Groq API key](https://console.groq.com/keys) — one key covers LLM, STT, and TTS

---

## Setup

**1. Clone the repo**

```bash
git clone https://github.com/your-username/ai-interview-groq.git
cd ai-interview-groq
```

**2. Create and activate a virtual environment**

```bash
python -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate
```

**3. Install dependencies**

```bash
cd backend
pip install -r requirements.txt
```

**4. Configure environment variables**

```bash
cp .env.example .env
```

Open `.env` and fill in your values:

```env
GROQ_API_KEY=your_groq_api_key_here
MONGO_URL=mongodb://localhost:27017
DB_NAME=ai_interview_db
```

**5. Start MongoDB**

```bash
# macOS (Homebrew)
brew services start mongodb-community

# Ubuntu / Debian
sudo systemctl start mongod
```

**6. Run the server**

```bash
python app.py
```

Open [http://localhost:5000](http://localhost:5000) in your browser.

---

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `GROQ_API_KEY` | ✅ | — | Your Groq Cloud API key |
| `MONGO_URL` | | `mongodb://localhost:27017` | MongoDB connection string |
| `DB_NAME` | | `ai_interview_db` | MongoDB database name |
| `GROQ_PRIMARY_MODEL` | | `llama-3.3-70b-versatile` | Primary LLM model |
| `GROQ_FALLBACK_MODEL` | | `llama-3.1-8b-instant` | Fallback LLM model |
| `GROQ_REQUEST_TIMEOUT` | | `120` | LLM request timeout in seconds |
| `GROQ_TTS_VOICE` | | `daniel` | TTS voice (`daniel`, `austin`, `troy`, `autumn`, `diana`, `hannah`) |
| `FLASK_HOST` | | `0.0.0.0` | Flask bind address |
| `FLASK_PORT` | | `5000` | Flask port |
| `MAX_INTERVIEW_DURATION` | | `2700` | Max interview length in seconds (45 min) |

---

## Interview Flow

1. **Setup** — Candidate enters their name, uploads a resume (PDF or TXT), and pastes the job description
2. **Greeting** — AI introduces itself and opens the session
3. **Introduction** — Candidate is asked to introduce themselves
4. **Resume deep-dive** — Questions are drawn from the candidate's actual resume via RAG
5. **Technical** — Domain-specific questions based on the tech stack detected from the resume (Python, SQL, ML, Deep Learning, NLP, Power BI, MongoDB, etc.)
6. **Behavioural** — Situational and soft-skills questions
7. **Closing** — Candidate is invited to ask questions; session wraps up
8. **Evaluation** — All Q&A pairs are evaluated together in a single LLM call; results page shows scores across 7 dimensions

---

## Evaluation Dimensions

| Dimension | What it measures |
|---|---|
| Overall Score | Weighted mean of the six dimensions below |
| Technical Knowledge | Accuracy, depth, correct use of terminology |
| Communication | Clarity, structure, concrete examples |
| Problem Solving | Systematic reasoning, trade-offs, methodology |
| Confidence | Decisiveness, directness, delivery |
| Role Fitment | Ownership of projects, resume depth |
| Clarity | Conciseness, absence of filler, listener comprehension |

Recommendation thresholds: **≥ 75 → Shortlist**, **50–74 → Hold**, **< 50 → Reject**

---

## API Endpoints

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/api/start-interview` | Upload resume + JD, create a session |
| `GET` | `/api/interview/<id>/status` | Get current session status |
| `GET` | `/api/interview/<id>/results` | Fetch final evaluation |
| `GET` | `/api/tts/health` | Check Groq TTS connectivity |

Real-time events are handled over **Socket.IO** — see `routes/socket_routes.py` for the full event list.

---

## Health Check

To verify TTS is working after setup:

```bash
curl http://localhost:5000/api/tts/health
```

Expected response:

```json
{ "status": "ok", "provider": "GroqOrpheus", "voice": "daniel", ... }
```

---

## License

© 2026 All Rights Reserved.

This project and its source code are proprietary. No part of this codebase may be copied, modified, distributed, or used in any form without explicit written permission from the author.
