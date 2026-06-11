from config.mongodb import mongodb
from datetime import datetime, timezone
import uuid
import logging

logger = logging.getLogger(__name__)


class MongoService:
    def __init__(self):
        self.db = mongodb.get_db()

    def create_candidate(self, name, resume_path, job_description):
        candidate = {
            'candidate_id': str(uuid.uuid4()),
            'name': name,
            'resume_path': resume_path,
            'job_description': job_description,
            'created_at': datetime.now(timezone.utc).isoformat(),
            'status': 'pending'
        }
        self.db.candidates.insert_one(candidate)
        candidate.pop('_id', None)
        logger.info(f"Created candidate: {candidate['candidate_id']}")
        return candidate

    def get_candidate_by_id(self, candidate_id):
        if not candidate_id:
            return None
        return self.db.candidates.find_one({'candidate_id': candidate_id}, {'_id': 0})

    def create_interview_session(self, candidate_id, interview_id):
        session = {
            'interview_id': interview_id,
            'candidate_id': candidate_id,
            'status': 'in_progress',
            'started_at': datetime.now(timezone.utc).isoformat(),
            'questions_asked': [],
            'current_question_index': 0
        }
        self.db.interview_sessions.insert_one(session)
        logger.info(f"Created interview session: {interview_id}")
        return session

    def get_interview_session(self, interview_id):
        return self.db.interview_sessions.find_one({'interview_id': interview_id}, {'_id': 0})

    def mark_interview_cancelled(self, interview_id):
        self.db.interview_sessions.update_one(
            {'interview_id': interview_id},
            {'$set': {
                'status': 'cancelled',
                'cancelled_at': datetime.now(timezone.utc).isoformat()
            }}
        )
        logger.info(f"Interview cancelled: {interview_id}")

    def save_interview_interaction(self, interview_id, question, answer, transcript, audio_path, scores):
        interaction = {
            'interview_id': interview_id,
            'question': question,
            'answer_transcript': transcript,
            'audio_path': audio_path,
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'scores': scores
        }
        self.db.interviews.insert_one(interaction)
        self.db.interview_sessions.update_one(
            {'interview_id': interview_id},
            {'$push': {'questions_asked': question},
             '$inc': {'current_question_index': 1}}
        )
        interaction.pop('_id', None)
        return interaction

    def get_all_interactions(self, interview_id):
        return list(self.db.interviews
                    .find({'interview_id': interview_id}, {'_id': 0})
                    .sort('timestamp', 1))

    def update_live_session(self, interview_id, data):
        self.db.interview_live_sessions.update_one(
            {'interview_id': interview_id},
            {'$set': {**data, 'updated_at': datetime.now(timezone.utc).isoformat()}},
            upsert=True
        )

    def save_final_evaluation(self, interview_id, evaluation):
        """
        v8: Single source of truth — all fields stored FLAT at the top level.
        No nested 'evaluation' wrapper. The document IS the evaluation.
        Idempotent via upsert.
        """
        doc = {
            'interview_id':              interview_id,
            'candidate_name':            evaluation.get('candidate_name', ''),
            'interview_status':          evaluation.get('interview_status', 'completed'),
            'overall_score':             evaluation.get('overall_score', 0),
            'confidence_score':          evaluation.get('confidence_score', 0),
            'communication_score':       evaluation.get('communication_score', 0),
            'problem_solving_score':     evaluation.get('problem_solving_score', 0),
            'technical_knowledge_score': evaluation.get('technical_knowledge_score', 0),
            'role_fitment_score':        evaluation.get('role_fitment_score', 0),
            'clarity_score':             evaluation.get('clarity_score', 0),
            'recommendation':            evaluation.get('recommendation', 'Hold'),
            'summary':                   evaluation.get('summary', ''),
            'strengths':                 evaluation.get('strengths', []),
            'improvement_areas':         evaluation.get('improvement_areas', []),
            'created_at':                datetime.now(timezone.utc).isoformat(),
        }
        self.db.evaluations.update_one(
            {'interview_id': interview_id},
            {'$set': doc},
            upsert=True
        )
        self.db.interview_sessions.update_one(
            {'interview_id': interview_id},
            {'$set': {
                'status': 'completed' if doc['interview_status'] != 'cancelled' else 'cancelled',
                'completed_at': datetime.now(timezone.utc).isoformat()
            }}
        )
        logger.info(
            f"Saved evaluation: {interview_id} | "
            f"overall={doc['overall_score']} | rec={doc['recommendation']}"
        )
        return doc

    def get_final_evaluation(self, interview_id):
        """Return the flat evaluation document (no nested wrapper)."""
        return self.db.evaluations.find_one(
            {'interview_id': interview_id}, {'_id': 0}
        )
