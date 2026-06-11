
import logging
import math
import re
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Set

import gevent

from services.llm_service import OllamaService
from services.rag_service import RAGService
from services.evaluator import EvaluatorService
from services.resume_parser import ResumeParser
from config.settings import Config

logger = logging.getLogger(__name__)

STAGES = [
    "greeting",
    "introduction",
    "resume",
    "technical",
    "behavioral",
    "closing",
]

STAGE_MIN_QUESTIONS = {
    "greeting":     1,
    "introduction": 2,
    "resume":       2,
    "technical":    5,
    "behavioral":   2,
    "closing":      1,
}

MIN_QUESTIONS = 10
MAX_QUESTIONS = 16
MAX_GENERATION_RETRIES = 3  

TECH_DOMAINS = {
    "Python": [
        "OOP concepts: classes, inheritance, polymorphism, encapsulation",
        "List vs tuple vs dict — when to use each",
        "Decorators and generators",
        "Pandas: groupby, merge, pivot_table",
        "NumPy: broadcasting, vectorised operations",
        "Exception handling and context managers",
    ],
    "SQL": [
        "INNER / LEFT / RIGHT / FULL OUTER joins — differences and use cases",
        "Window functions: ROW_NUMBER, RANK, DENSE_RANK, LAG, LEAD",
        "Common Table Expressions (CTEs) and recursive CTEs",
        "Indexing strategies and when NOT to index",
        "Normalization: 1NF, 2NF, 3NF vs denormalization",
        "Query optimisation and EXPLAIN plan",
    ],
    "Machine Learning": [
        "Overfitting vs underfitting — bias-variance tradeoff",
        "Cross-validation: k-fold, stratified, leave-one-out",
        "Random Forest vs XGBoost — when to pick which",
        "Feature engineering and selection techniques",
        "Evaluation metrics: precision, recall, F1, ROC-AUC",
        "Class imbalance handling: SMOTE, class weights",
    ],
    "Deep Learning": [
        "Backpropagation and gradient descent",
        "Vanishing / exploding gradients — solutions",
        "CNN architecture: convolution, pooling, flattening",
        "RNN vs LSTM vs GRU",
        "Batch normalisation and dropout",
        "Transfer learning and fine-tuning strategies",
    ],
    "NLP": [
        "Tokenisation, stemming, lemmatisation",
        "TF-IDF vs word embeddings (Word2Vec, GloVe)",
        "Transformer architecture: self-attention, positional encoding",
        "BERT vs GPT — encoder vs decoder models",
        "Named Entity Recognition and text classification",
        "Sequence-to-sequence models and attention mechanisms",
    ],
    "Power BI": [
        "DAX: calculated columns vs measures",
        "CALCULATE, FILTER, ALL, RELATED functions",
        "Star schema vs snowflake schema design",
        "Row-level security implementation",
        "Dashboard optimisation and performance tuning",
        "DirectQuery vs Import mode tradeoffs",
    ],
    "MongoDB": [
        "Document model vs relational — when to choose MongoDB",
        "Aggregation pipeline: $match, $group, $lookup, $project",
        "Indexing in MongoDB: single, compound, text, geospatial",
        "Replication and sharding architecture",
        "Schema design: embedding vs referencing documents",
        "Transactions in MongoDB 4.x+",
    ],
    "Flask": [
        "Application factory pattern and blueprints",
        "Request lifecycle and middleware",
        "REST API design with Flask: status codes, error handling",
        "Flask-SQLAlchemy ORM usage",
        "Authentication: JWT vs session-based",
        "Deploying Flask with Gunicorn and Nginx",
    ],
    "FastAPI": [
        "Async vs sync endpoints — when to use each",
        "Pydantic models for request/response validation",
        "Dependency injection system",
        "Background tasks and lifespan events",
        "OAuth2 and JWT integration",
        "OpenAPI / Swagger auto-documentation",
    ],
    "RAG": [
        "Retrieval-Augmented Generation architecture overview",
        "Vector embeddings and similarity search",
        "Chunking strategies for documents",
        "Re-ranking retrieved documents",
        "Hallucination reduction techniques",
        "Evaluation metrics for RAG pipelines",
    ],
    "Vector Databases": [
        "FAISS: flat index vs IVF vs HNSW",
        "Cosine similarity vs dot product vs Euclidean distance",
        "Pinecone / Weaviate / Chroma — differences",
        "Index building and query latency tradeoffs",
        "Approximate Nearest Neighbour search",
    ],
    "Statistics": [
        "Mean vs median vs mode — when each is appropriate",
        "Standard deviation and variance interpretation",
        "Hypothesis testing: null hypothesis, p-value, significance level",
        "Type I vs Type II errors",
        "Confidence intervals and their interpretation",
        "A/B testing methodology",
    ],
    "ETL": [
        "Data cleaning: handling missing values strategies",
        "Deduplication techniques",
        "Data pipeline orchestration (Airflow, Prefect)",
        "Slowly Changing Dimensions (SCD) Types 1, 2, 3",
        "Data validation and quality checks",
        "Batch vs streaming ETL tradeoffs",
    ],
}


_TOPIC_CLUSTERS: Dict[str, List[str]] = {
    "self_introduction": [
        "introduce yourself", "tell about yourself", "walk through background",
        "educational background", "about yourself", "your background",
        "brief introduction", "tell us about you",
    ],
    "role_interest": [
        "interest in this role", "why this role", "what drew you", "why apply",
        "attracted to this position", "interested in this job",
    ],
    "ml_project": [
        "machine learning project", "ml project", "machine learning model",
        "machine learning work", "machine learning experience", "ml work",
        "machine learning tasks", "ml pipeline", "involving ml", "involving machine learning",
        "built a model", "trained a model", "developed a model",
    ],
    "ml_evaluation": [
        "evaluation metrics", "model evaluation", "precision recall", "f1 score",
        "roc auc", "accuracy metric", "model performance metric",
    ],
    "overfitting": [
        "overfitting", "underfitting", "bias variance", "bias-variance",
        "generalization", "regularization", "overfit",
    ],
    "cross_validation": [
        "cross validation", "cross-validation", "k-fold", "kfold",
        "train test split", "validation strategy",
    ],
    "feature_engineering": [
        "feature engineering", "feature selection", "feature importance",
        "feature extraction", "dimensionality reduction",
    ],
    "class_imbalance": [
        "class imbalance", "imbalanced data", "smote", "oversampling",
        "undersampling", "class weight", "imbalanced class",
    ],
    "sql_joins": [
        "sql join", "inner join", "left join", "right join", "full outer join",
        "join types", "join operation",
    ],
    "sql_window": [
        "window function", "row_number", "rank function", "dense_rank",
        "lag lead", "partition by", "over clause",
    ],
    "technical_challenge": [
        "technical challenge", "challenging problem", "difficult problem",
        "technical problem", "hard challenge", "major challenge",
        "problem you solved", "challenge you faced", "obstacle you overcame",
        "challenging project",
    ],
    "learning_technology": [
        "learn a new technology", "new technology quickly", "learning under deadline",
        "picked up quickly", "had to learn", "new skill quickly",
    ],
    "team_disagreement": [
        "disagreed with", "team disagreement", "conflict with team",
        "different approach", "team member approach", "technical disagreement",
    ],
    "explaining_technical": [
        "explain technical", "non-technical stakeholder", "explain complex",
        "communicate technical", "technical communication",
    ],
    "career_goals": [
        "career in", "see yourself in", "career goals", "future plans",
        "skills to develop", "career direction", "next few years",
    ],
    "resume_project": [
        "project from your resume", "project you worked on", "project you built",
        "project you developed", "your project", "tell me about a project",
        "describe a project", "walk me through a project",
    ],
    "python_oop": [
        "object oriented", "oop", "classes inheritance", "polymorphism",
        "encapsulation", "python class", "inheritance python",
    ],
    "decorators_generators": [
        "decorator", "generator", "yield keyword", "python decorator",
    ],
    "gradient_descent": [
        "gradient descent", "backpropagation", "learning rate", "optimizer",
        "stochastic gradient", "sgd", "adam optimizer",
    ],
    "neural_network_arch": [
        "neural network architecture", "cnn", "convolutional", "lstm",
        "recurrent neural", "transformer architecture", "attention mechanism",
    ],
    "deployment": [
        "model deployment", "deploy model", "production deployment",
        "serving model", "model serving", "api for model", "model in production",
    ],
}

_STOP_WORDS: Set[str] = {
    'a','an','the','and','or','but','in','on','at','to','for','of','with',
    'by','from','is','are','was','were','be','been','being','have','has',
    'had','do','does','did','will','would','could','should','may','might',
    'shall','can','need','it','its','this','that','these','those','i','you',
    'he','she','we','they','me','him','her','us','them','my','your','his',
    'our','their','what','which','who','when','where','why','how','tell',
    'describe','explain','talk','about','can','please','give','time','one',
    'just','way','walk','through','any','some','most','more','less','very',
    'really','quite','now','let','also','even','well','back','then','than',
    'if','so','up','out','get','use','used','using','make','made',
}


class DuplicateDetector:
    """
    Three-layer duplicate/near-duplicate detection for interview questions.

    Layer 1 — Exact normalized match
    Layer 2 — Topic cluster match (semantic: catches rewording within a domain)
    Layer 3 — Keyword Jaccard similarity >= 0.45
    """

    @staticmethod
    def _normalize(text: str) -> str:
        text = text.lower().strip()
        text = re.sub(r'[^\w\s]', ' ', text)
        return re.sub(r'\s+', ' ', text).strip()

    @staticmethod
    def _keywords(text: str) -> List[str]:
        words = DuplicateDetector._normalize(text).split()
        return [w for w in words if w not in _STOP_WORDS and len(w) > 2]

    @staticmethod
    def _jaccard(a: set, b: set) -> float:
        if not a or not b:
            return 0.0
        return len(a & b) / len(a | b)

    @staticmethod
    def _topic_cluster(text: str) -> Optional[str]:
        norm = DuplicateDetector._normalize(text)
        for cluster, phrases in _TOPIC_CLUSTERS.items():
            if any(phrase in norm for phrase in phrases):
                return cluster
        return None

    @classmethod
    def is_duplicate(cls, candidate: str, asked_list: List[str]) -> tuple:
        """
        Returns (is_dup: bool, score: float, reason: str).
        Checks ALL previous questions — no sliding window.
        """
        if not candidate or not asked_list:
            return False, 0.0, ""

        norm_cand    = cls._normalize(candidate)
        kw_cand      = set(cls._keywords(candidate))
        cand_cluster = cls._topic_cluster(candidate)

        for i, asked in enumerate(asked_list):
            if cls._normalize(asked) == norm_cand:
                return True, 1.0, f"exact match with Q{i+1}"

            if cand_cluster and cand_cluster == cls._topic_cluster(asked):
                return True, 0.9, f"same topic '{cand_cluster}' as Q{i+1}: '{asked[:70]}'"

            kw_asked = set(cls._keywords(asked))
            j = cls._jaccard(kw_cand, kw_asked)
            if j >= 0.45:
                return True, j, f"Jaccard {j:.2f} with Q{i+1}: '{asked[:70]}'"

        return False, 0.0, ""

    @classmethod
    def covered_clusters(cls, asked_list: List[str]) -> Set[str]:
        """Return all topic clusters covered by the asked questions so far."""
        clusters = set()
        for q in asked_list:
            c = cls._topic_cluster(q)
            if c:
                clusters.add(c)
        return clusters


@dataclass
class InterviewState:
    interview_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    stage: str = "greeting"
    stage_idx: int = 0

    questions_asked:      List[str]  = field(default_factory=list)
    topics_covered:       List[str]  = field(default_factory=list)
    domains_covered:      List[str]  = field(default_factory=list)
    conversation_history: List[Dict] = field(default_factory=list)
    scores_history:       List[Dict] = field(default_factory=list)

    stage_q_counts: Dict[str, int] = field(default_factory=lambda: {s: 0 for s in STAGES})

    current_topic_depth:    int = 0
    last_topic:             str = ""
    last_answer_quality:    str = "unknown"
    technical_depth_reached: int = 0

    used_fallbacks: Set[int] = field(default_factory=set)

    MAX_HISTORY_ENTRIES:   int = field(default=20, init=False, repr=False)
    MAX_QUESTIONS_STORED:  int = field(default=50, init=False, repr=False)
    MAX_TOPICS_STORED:     int = field(default=30, init=False, repr=False)
    MAX_DOMAINS_STORED:    int = field(default=20, init=False, repr=False)

    def advance_stage(self):
        if self.stage_idx < len(STAGES) - 1:
            self.stage_idx += 1
            self.stage = STAGES[self.stage_idx]
            self.current_topic_depth = 0
            logger.info(f"[STATE] Stage → {self.stage}")

    def should_advance_stage(self) -> bool:
        return self.stage_q_counts.get(self.stage, 0) >= STAGE_MIN_QUESTIONS.get(self.stage, 2)

    def record_stage_question(self):
        self.stage_q_counts[self.stage] = self.stage_q_counts.get(self.stage, 0) + 1

    def is_question_duplicate(self, question: str) -> bool:
        """v12: delegate to DuplicateDetector — checks ALL previous questions."""
        is_dup, score, reason = DuplicateDetector.is_duplicate(question, self.questions_asked)
        if is_dup:
            logger.info(f"[DEDUP] Duplicate detected (score={score:.2f}): {reason}")
        return is_dup

    def add_to_history(self, entry: dict):
        self.conversation_history.append(entry)
        if len(self.conversation_history) > self.MAX_HISTORY_ENTRIES:
            excess = len(self.conversation_history) - self.MAX_HISTORY_ENTRIES
            del self.conversation_history[:excess]

    def add_question(self, question: str):
        self.questions_asked.append(question)
        if len(self.questions_asked) > self.MAX_QUESTIONS_STORED:
            excess = len(self.questions_asked) - self.MAX_QUESTIONS_STORED
            del self.questions_asked[:excess]

    def add_topic(self, topic: str):
        if topic and topic not in self.topics_covered:
            self.topics_covered.append(topic)
            if len(self.topics_covered) > self.MAX_TOPICS_STORED:
                del self.topics_covered[0]

    def add_domain(self, domain: str):
        if domain and domain not in self.domains_covered:
            self.domains_covered.append(domain)
            if len(self.domains_covered) > self.MAX_DOMAINS_STORED:
                del self.domains_covered[0]


class InterviewEngine:
    def __init__(self):
        self.llm       = OllamaService()
        self.rag       = RAGService()
        self.evaluator = EvaluatorService()
        self.state     = InterviewState()
        self.candidate_info = {}
        self.questions_asked      = self.state.questions_asked
        self.conversation_history = self.state.conversation_history

    def initialize_interview(self, candidate_name: str, resume_path: str,
                             job_description: str) -> dict:
        t_start = time.perf_counter()
        logger.info(f"[INIT] Starting parallel initialisation for '{candidate_name}'")

        try:
            t0 = time.perf_counter()
            resume_text = ResumeParser.parse(resume_path)
            logger.info(f"[INIT] Resume parsed in {int((time.perf_counter()-t0)*1000)} ms "
                        f"({len(resume_text)} chars)")

            results = {}
            errors  = {}

            def _build_rag():
                t = time.perf_counter()
                try:
                    self.rag.create_vector_store(resume_text, job_description)
                    results['rag'] = True
                    logger.info(f"[INIT/RAG] Done in {int((time.perf_counter()-t)*1000)} ms")
                except Exception as e:
                    errors['rag'] = e
                    logger.error(f"[INIT/RAG] Failed: {e}")

            def _extract_jd_tech():
                t = time.perf_counter()
                try:
                    results['jd_tech'] = self._extract_tech_stack(job_description)
                    logger.info(f"[INIT/JD-TECH] Done in {int((time.perf_counter()-t)*1000)} ms "
                                f"→ {results['jd_tech']}")
                except Exception as e:
                    errors['jd_tech'] = e
                    results['jd_tech'] = []

            def _extract_resume_tech():
                t = time.perf_counter()
                try:
                    results['resume_tech'] = self._extract_tech_stack(resume_text)
                    logger.info(f"[INIT/RESUME-TECH] Done in {int((time.perf_counter()-t)*1000)} ms "
                                f"→ {results['resume_tech']}")
                except Exception as e:
                    errors['resume_tech'] = e
                    results['resume_tech'] = []

            greenlets = [
                gevent.spawn(_build_rag),
                gevent.spawn(_extract_jd_tech),
                gevent.spawn(_extract_resume_tech),
            ]
            gevent.joinall(greenlets, timeout=60)

            if 'rag' in errors:
                raise errors['rag']

            jd_tech         = results.get('jd_tech', [])
            resume_tech     = results.get('resume_tech', [])
            matched_domains = self._match_domains(resume_tech + jd_tech)

            self.candidate_info = {
                'name':              candidate_name,
                'resume_text':       resume_text,
                'job_description':   job_description,
                'jd_tech_stack':     jd_tech,
                'resume_tech_stack': resume_tech,
                'matched_domains':   matched_domains,
            }

            total_ms = int((time.perf_counter() - t_start) * 1000)
            logger.info(f"[INIT] Complete in {total_ms} ms | domains={matched_domains[:6]}")
            return {'success': True, 'interview_id': self.state.interview_id}

        except Exception as e:
            logger.exception(f"[INIT] Failed: {e}")
            return {'success': False, 'error': str(e)}

    def _extract_tech_stack(self, text: str) -> List[str]:
        if not text:
            return []
        prompt = (
            "Extract technology names, programming languages, frameworks, tools, "
            "databases, and platforms from the text below. "
            "Output ONLY a comma-separated list. No explanations.\n\n"
            f"Text: {text[:2000]}\n\nTech stack:"
        )
        result = self.llm.generate_short(prompt)
        return [t.strip() for t in result.split(',')
                if t.strip() and len(t.strip()) < 50][:20]

    def _match_domains(self, tech_list: List[str]) -> List[str]:
        matched    = []
        tech_lower = [t.lower() for t in tech_list]
        for domain in TECH_DOMAINS:
            if any(domain.lower() in t or t in domain.lower() for t in tech_lower):
                matched.append(domain)
        if not matched:
            matched = ["Python", "SQL", "Machine Learning"]
        return matched

    def generate_first_question(self) -> str:
        name = self.candidate_info['name']
        greeting = (
            f"Hello {name}, welcome to the interview! "
            f"I'm glad you could join us today. "
            f"To begin, could you please briefly introduce yourself — "
            f"walk me through your educational background, your key experience, "
            f"and what drew you to apply for this role?"
        )
        self._record_question(greeting, stage="greeting")
        self.state.record_stage_question()
        logger.info(f"[GREETING] Greeting generated for '{name}'")
        return greeting

    def generate_follow_up_question(self, previous_answer: str,
                                    question_count: int) -> str:
        t0 = time.perf_counter()
        logger.info(f"[Q{question_count}] stage={self.state.stage} "
                    f"depth={self.state.current_topic_depth} "
                    f"asked={len(self.state.questions_asked)}")

        if self.state.should_advance_stage():
            self.state.advance_stage()

        stage = self.state.stage
        ctx   = ""
        try:
            t_rag = time.perf_counter()
            ctx   = self.rag.get_relevant_info(
                previous_answer or "skills experience projects")
            logger.info(f"[Q{question_count}/RAG] {int((time.perf_counter()-t_rag)*1000)} ms")
        except Exception:
            pass

        covered_clusters = DuplicateDetector.covered_clusters(self.state.questions_asked)

        directive = self._get_question_directive(stage, question_count,
                                                  previous_answer, ctx,
                                                  covered_clusters)
        prompt    = self._build_full_prompt(directive, ctx, question_count)

        t_llm    = time.perf_counter()
        question = self._generate_and_validate(prompt, directive,
                                               fallback_idx=question_count - 1)
        llm_ms   = int((time.perf_counter() - t_llm) * 1000)
        logger.info(f"[Q{question_count}/LLM] {llm_ms} ms")

        self._record_question(question, stage=stage)
        self.state.record_stage_question()
        self.state.current_topic_depth = min(2, self.state.current_topic_depth + 1)

        if stage == "technical":
            for domain in TECH_DOMAINS:
                if (domain.lower() in question.lower() or
                        domain.lower() in previous_answer.lower()):
                    self.state.add_domain(domain)

        total_ms = int((time.perf_counter() - t0) * 1000)
        logger.info(f"[Q{question_count}] Total {total_ms} ms → '{question[:120]}'")
        return question

    def _get_question_directive(self, stage, q_num, prev_answer, ctx,
                                 covered_clusters: Set[str] = None):
        covered_clusters = covered_clusters or set()
        name            = self.candidate_info.get('name', 'the candidate')
        matched_domains = self.candidate_info.get('matched_domains', ["Python", "SQL"])
        depth           = self.state.current_topic_depth
        quality         = self.state.last_answer_quality

        uncovered     = [d for d in matched_domains if d not in self.state.domains_covered]
        target_domain = uncovered[0] if uncovered else (matched_domains[0] if matched_domains else "Python")

        if stage == "greeting":
            return (f"Ask {name} to briefly describe their background and interest "
                    f"in this role. Be warm and welcoming.")

        elif stage == "introduction":
            intros = [
                f"Ask {name} about their educational background and any relevant academic projects.",
                f"Ask {name} what specifically interests them about this role and the technologies it involves.",
            ]
            idx = self.state.stage_q_counts.get("introduction", 0)
            return intros[min(idx, len(intros)-1)]

        elif stage == "resume":
            if depth == 0:
                return (f"Pick ONE specific project from {name}'s resume and ask what problem it solved, "
                        f"what their exact contribution was, and what technology stack they used. "
                        f"Reference the resume context above.")
            elif depth == 1:
                return (f"Follow up on the project {name} just described. "
                        f"Ask a specific technical detail: for example, why they chose that particular "
                        f"model/framework/database, what a major technical challenge was, or how they "
                        f"measured success. Do NOT ask about a new project.")
            else:
                return (f"Go one level deeper on the same topic. Ask about a specific technical decision "
                        f"they made: algorithm choice, performance bottleneck, scalability concern, "
                        f"or trade-off between two approaches. This should feel like a real senior engineer probing.")

        elif stage == "technical":
            sub_topics = TECH_DOMAINS.get(target_domain, [])
            uncovered_subtopics = [s for s in sub_topics
                                   if not any(s.split(':')[0].lower() in q.lower()
                                              for q in self.state.questions_asked)]
            subtopic    = uncovered_subtopics[0] if uncovered_subtopics else (sub_topics[0] if sub_topics else "core concepts")
            depth_label = {0: "foundational", 1: "intermediate", 2: "advanced"}[min(depth, 2)]

            if quality == "weak":
                return (f"The candidate gave a weak answer. Ask a simpler clarifying question about "
                        f"{target_domain} — specifically '{subtopic}'. Help them demonstrate what they DO know.")
            elif quality == "good" and depth >= 1:
                return (f"The candidate answered well. Push deeper: ask an advanced {target_domain} question "
                        f"about '{subtopic}'. Probe for edge cases, performance implications, or production experience.")
            else:
                return (f"Ask a {depth_label} {target_domain} question about: '{subtopic}'. "
                        f"Be specific — ask how something works, when to use it, or a real-world scenario. "
                        f"Do NOT ask 'tell me about {target_domain}' — ask something precise.")

        elif stage == "behavioral":
            behavioral_qs = [
                "Tell me about a time you faced a major technical challenge on a project and how you resolved it.",
                "Describe a situation where you had to learn a completely new technology under a tight deadline.",
                "Tell me about a time you disagreed with a team member's technical approach — how did you handle it?",
                "Give me an example of when you had to explain a complex technical concept to a non-technical stakeholder.",
            ]
            idx    = self.state.stage_q_counts.get("behavioral", 0)
            chosen = behavioral_qs[min(idx, len(behavioral_qs)-1)]
            return f"Ask this exact behavioral question (STAR format expected): {chosen}"

        elif stage == "closing":
            return (f"Ask one final question: where does {name} see their technical career in 2–3 years, "
                    f"and what specific skills they plan to develop. Keep it forward-looking and positive.")

        return "Ask the next most appropriate interview question based on the conversation so far."

    def process_answer(self, answer_transcript: str) -> Dict:
        self.state.add_to_history({
            'role':      'candidate',
            'content':   answer_transcript,
            'timestamp': datetime.now(timezone.utc).isoformat(),
        })
        answer_lower  = answer_transcript.lower()
        word_count    = len(answer_transcript.split())
        tech_keywords = self.candidate_info.get('jd_tech_stack', [])
        keyword_hits  = sum(1 for t in tech_keywords if t.lower() in answer_lower)

        if word_count >= 60 and keyword_hits >= 1:
            self.state.last_answer_quality = "good"
            self.state.technical_depth_reached = min(3, self.state.technical_depth_reached + 1)
        elif word_count < 20:
            self.state.last_answer_quality = "weak"
        else:
            self.state.last_answer_quality = "average"

        for tech in tech_keywords:
            if tech.lower() in answer_lower:
                self.state.add_topic(tech)
        return {}

    def should_continue_interview(self, elapsed_time: int, question_count: int) -> bool:
        if elapsed_time >= Config.MAX_INTERVIEW_DURATION:
            logger.info(f"[STATE] Time limit reached ({elapsed_time}s)")
            return False
        if question_count < MIN_QUESTIONS:
            return True
        if question_count >= MAX_QUESTIONS:
            logger.info(f"[STATE] Max questions reached ({question_count})")
            return False
        if self.state.stage == "closing" and self.state.stage_q_counts.get("closing", 0) >= 1:
            return False
        return True

    def generate_closing_statement(self) -> str:
        name = self.candidate_info.get('name', 'there')
        closing = (
            f"Thank you so much, {name} — it's been a pleasure speaking with you today. "
            f"You've given me a thorough picture of your background and technical skills. "
            f"Our team will review your responses carefully and be in touch within the next few days. "
            f"Do you have any questions for me before we wrap up?"
        )
        self._record_question(closing, stage="closing")
        return closing

    def _build_full_prompt(self, directive, ctx, q_num):
        return self._build_system_prompt() + f"""

INTERVIEW STATE:
- Question #{q_num} | Stage: {self.state.stage}
- Domains covered: {', '.join(self.state.domains_covered) or 'none'}

RESUME CONTEXT (most relevant):
{ctx[:300]}

RECENT CONVERSATION (last 4 exchanges):
{self._build_history_snippet()}

ALL QUESTIONS ASKED SO FAR — NEVER ASK ANYTHING SIMILAR TO THESE:
{self._build_asked_summary()}

YOUR TASK: {directive}

OUTPUT RULES:
- ONE question only. No preamble. No labels. No numbering.
- Under 45 words. End with a question mark.
- Be specific, not generic.
- The question MUST be on a completely different topic than all questions listed above.
"""

    def _build_system_prompt(self):
        name           = self.candidate_info.get('name', 'the candidate')
        jd             = self.candidate_info.get('job_description', '')[:300]
        jd_tech        = ', '.join(self.candidate_info.get('jd_tech_stack', [])[:8])
        resume_summary = self.candidate_info.get('resume_text', '')[:300]
        return f"""You are a senior technical interviewer conducting a real job interview.
You must NEVER repeat or rephrase a question you have already asked.
Each question must cover new ground and add new information to the interview.

CANDIDATE: {name}
ROLE/JD: {jd}
TECH STACK: {jd_tech}
RESUME: {resume_summary}"""

    def _build_history_snippet(self):
        lines = []
        for entry in self.state.conversation_history[-8:]:
            role = "Interviewer" if entry['role'] == 'interviewer' else "Candidate"
            lines.append(f"{role}: {entry['content'][:200]}")
        return "\n".join(lines)

    def _build_asked_summary(self) -> str:
        """
        v12: Send ALL asked questions to the LLM (not just last 5).
        This is the single most important context improvement — the LLM can only
        avoid repeating what it can see.
        """
        if not self.state.questions_asked:
            return "(none yet)"
        lines = []
        for i, q in enumerate(self.state.questions_asked, 1):
            lines.append(f"Q{i}: {q[:120]}")
        return "\n".join(lines)

    def _generate_and_validate(self, prompt: str, directive: str,
                                fallback_idx: int = 0) -> str:
        """
        v12: Retry up to MAX_GENERATION_RETRIES times on duplicate detection.
        Each retry appends the rejected question to the prompt so the LLM
        knows explicitly what NOT to generate.
        """
        retry_prompt = prompt
        rejected     = []

        for attempt in range(MAX_GENERATION_RETRIES):
            try:
                temperature = 0.75 + (attempt * 0.10)
                q = self.llm.generate(retry_prompt, temperature=temperature,
                                      max_tokens=120)
                q = self._clean_question(q)

                if not q:
                    logger.warning(f"[GEN] Attempt {attempt+1}: empty response")
                    continue

                is_dup, score, reason = DuplicateDetector.is_duplicate(
                    q, self.state.questions_asked)

                if not is_dup:
                    if attempt > 0:
                        logger.info(f"[GEN] Accepted on attempt {attempt+1}: '{q[:80]}'")
                    return q

                rejected.append(q)
                logger.warning(f"[GEN] Attempt {attempt+1} duplicate (score={score:.2f}, "
                               f"{reason}): '{q[:80]}'")

                retry_prompt = (
                    prompt
                    + f"\n\nDO NOT GENERATE ANY OF THESE (already rejected as duplicates):\n"
                    + "\n".join(f"- {r}" for r in rejected)
                    + "\n\nGenerate a COMPLETELY DIFFERENT question on a new topic."
                )

            except Exception as e:
                logger.warning(f"[GEN] Attempt {attempt+1} LLM error: {e}")

        logger.warning(f"[GEN] All {MAX_GENERATION_RETRIES} retries duplicate — using fallback")
        return self._unique_fallback(fallback_idx)

    def _unique_fallback(self, hint_idx: int) -> str:
        """
        v12: Pick a fallback that hasn't been used in this interview AND
        doesn't duplicate any asked question.
        """
        fallbacks = [
            "Can you walk me through your most recent technical project and what your specific contributions were?",
            "What programming language or framework are you most comfortable with, and why did you choose it?",
            "Describe a technically challenging problem you solved — what was it and how did you approach it?",
            "Tell me about a time you had to learn a new technology quickly — what was it and how did you adapt?",
            "How do you approach debugging a production issue you've never seen before?",
            "Walk me through how you'd design a scalable REST API — what would your key decisions be?",
            "What evaluation metrics did you use for your ML model, and why those specific metrics?",
            "Explain the difference between a SQL JOIN and a subquery — when would you use each?",
            "How have you handled missing or inconsistent data in a real project?",
            "What do you consider your strongest technical skill, and give a concrete example of applying it?",
            "Describe your experience with version control — how do you structure commits and branches?",
            "What's the most complex SQL query you've written? Walk me through the logic.",
            "How do you decide when to use a NoSQL database vs a relational one?",
            "Explain how you would approach optimising a slow database query in production.",
            "What's your approach to writing unit tests for data pipelines?",
        ]

        candidates = [hint_idx % len(fallbacks)] + \
                     [i for i in range(len(fallbacks)) if i != hint_idx % len(fallbacks)]

        for idx in candidates:
            if idx in self.state.used_fallbacks:
                continue
            q = fallbacks[idx]
            is_dup, _, _ = DuplicateDetector.is_duplicate(q, self.state.questions_asked)
            if not is_dup:
                self.state.used_fallbacks.add(idx)
                logger.info(f"[GEN] Using fallback #{idx}: '{q[:80]}'")
                return q

        logger.error("[GEN] All fallbacks exhausted — returning generic question")
        return "Can you tell me more about your most recent technical work and what you learned from it?"

    def _record_question(self, question, stage=""):
        self.state.add_question(question)
        if stage:
            self.state.add_topic(stage)
        self.state.add_to_history({
            'role':      'interviewer',
            'content':   question,
            'timestamp': datetime.now(timezone.utc).isoformat(),
        })

    @staticmethod
    def _clean_question(text):
        text = (text or '').strip()
        for prefix in ("Question:", "Q:", "Interviewer:", "AI:", "Here is", "Sure,",
                       "Next question:", "Certainly,", "Of course,", "Great,",
                       "Absolutely,", "Thank you,", "Now,"):
            if text.lower().startswith(prefix.lower()):
                text = text[len(prefix):].strip()
        if len(text) >= 2 and text[0] in '"\'':
            text = text[1:].strip()
        if text and text[-1] in '"\'':
            text = text[:-1].strip()
        return text.replace('**', '').replace('__', '').strip()
