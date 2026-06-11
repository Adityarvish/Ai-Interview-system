import logging
import json
import re
from typing import List, Dict, Optional

from services.llm_service import OllamaService

logger = logging.getLogger(__name__)


class EvaluatorService:
    def __init__(self):
        self.llm = OllamaService()

    @staticmethod
    def _extract_json(raw: str) -> Optional[dict]:
        if not raw:
            return None
        m = re.search(r'```(?:json)?\s*(.+?)```', raw, re.DOTALL)
        if m:
            raw = m.group(1)
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        if not m:
            return None
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            try:
                repaired = re.sub(r',\s*([}\]])', r'\1', m.group(0))
                repaired = repaired.replace("'", '"')
                return json.loads(repaired)
            except Exception:
                return None

    @staticmethod
    def _clamp(value, lo=0, hi=100) -> int:
        try:
            return max(lo, min(hi, int(round(float(value)))))
        except (TypeError, ValueError):
            return 50

    def evaluate_answer(self, question: str, answer: str,
                        job_description: str, resume_context: str = "",
                        stage: str = "") -> Dict:
        """
        v7: Per-question scoring is REMOVED.
        This method is intentionally a no-op stub so call sites in
        interview_engine.py do not crash.  It returns an empty dict;
        nothing is stored as question-level scores.
        """
        return {}

    def generate_final_evaluation(self,
                                   all_interactions: List[Dict],
                                   candidate_name: str,
                                   job_description: str) -> Dict:
        """
        Evaluate the COMPLETE interview in a single LLM call.
        all_interactions: list of {question, answer_transcript, ...} dicts
        Returns the exact schema stored in DB and displayed on the results page.
        """
        if not all_interactions:
            return self._default_final(candidate_name, status='cancelled')

        transcript_parts = []
        for i, item in enumerate(all_interactions, 1):
            q = item.get('question', '').strip()
            a = item.get('answer_transcript', item.get('answer', '')).strip()
            if q or a:
                transcript_parts.append(
                    f"Q{i}: {q}\nA{i}: {a}"
                )

        if not transcript_parts:
            return self._default_final(candidate_name, status='cancelled')

        full_transcript = "\n\n".join(transcript_parts)
        total_questions = len(transcript_parts)

        prompt = self._build_evaluation_prompt(
            candidate_name=candidate_name,
            job_description=job_description,
            transcript=full_transcript,
            total_questions=total_questions,
        )

        raw = None
        try:
            raw = self.llm.generate(prompt, temperature=0.2, max_tokens=900, timeout=180)
        except Exception as e:
            logger.warning(f"[EVAL] LLM call failed: {e}")
            return self._default_final(candidate_name)

        parsed = self._extract_json(raw)
        if not parsed:
            logger.warning(f"[EVAL] JSON parse failed. Raw: {(raw or '')[:300]}")
            return self._default_final(candidate_name)

        overall              = self._clamp(parsed.get('overall_score', 50))
        confidence_score     = self._clamp(parsed.get('confidence_score', 50))
        communication_score  = self._clamp(parsed.get('communication_score', 50))
        problem_solving_score= self._clamp(parsed.get('problem_solving_score', 50))
        technical_score      = self._clamp(parsed.get('technical_knowledge_score', 50))
        role_fitment_score   = self._clamp(parsed.get('role_fitment_score', 50))
        clarity_score        = self._clamp(parsed.get('clarity_score', 50))

        if overall == 0:
            overall = self._clamp(round(
                (confidence_score + communication_score + problem_solving_score +
                 technical_score + role_fitment_score + clarity_score) / 6
            ))

        raw_rec = str(parsed.get('recommendation', '')).strip()
        if raw_rec in ('Shortlist', 'Hold', 'Reject'):
            recommendation = raw_rec
        elif overall >= 75:
            recommendation = 'Shortlist'
        elif overall >= 50:
            recommendation = 'Hold'
        else:
            recommendation = 'Reject'

        summary = str(parsed.get('summary', '')).strip() or self._fallback_summary(candidate_name, overall)

        strengths = parsed.get('strengths', [])
        if not isinstance(strengths, list):
            strengths = [str(strengths)]
        strengths = [str(s).strip() for s in strengths if str(s).strip()][:5]
        if not strengths:
            strengths = ["Completed the full interview process"]

        improvements = parsed.get('improvement_areas', [])
        if not isinstance(improvements, list):
            improvements = [str(improvements)]
        improvements = [str(s).strip() for s in improvements if str(s).strip()][:5]
        if not improvements:
            improvements = ["Continue developing expertise across all evaluated areas"]

        evaluation = {
            'candidate_name':           candidate_name,
            'interview_status':         'completed',
            'overall_score':            overall,
            'confidence_score':         confidence_score,
            'communication_score':      communication_score,
            'problem_solving_score':    problem_solving_score,
            'technical_knowledge_score': technical_score,
            'role_fitment_score':       role_fitment_score,
            'clarity_score':            clarity_score,
            'recommendation':           recommendation,
            'summary':                  summary,
            'strengths':                strengths,
            'improvement_areas':        improvements,
        }

        logger.info(
            f"[EVAL] Final evaluation for '{candidate_name}': "
            f"overall={overall}, rec={recommendation}, "
            f"tech={technical_score}, comm={communication_score}, "
            f"ps={problem_solving_score}, conf={confidence_score}, "
            f"clarity={clarity_score}, fit={role_fitment_score}"
        )
        return evaluation

    @staticmethod
    def _build_evaluation_prompt(candidate_name: str, job_description: str,
                                  transcript: str, total_questions: int) -> str:
        return f"""You are a senior HR evaluator reviewing a completed job interview.
Candidate: {candidate_name}
Role: {job_description[:300]}
Total questions answered: {total_questions}

FULL INTERVIEW TRANSCRIPT:
{transcript[:4000]}

TASK: Evaluate the candidate holistically across the ENTIRE interview.
Score each dimension 0–100 based on ALL answers combined, not any single question.

SCORING CALIBRATION (apply to every metric):
  90–100 : Exceptional — consistent excellence, specific evidence, depth
  75–89  : Strong — mostly good with minor gaps
  55–74  : Adequate — acceptable but inconsistent or surface-level
  35–54  : Weak — significant gaps or frequently vague
  0–34   : Poor — mostly incorrect, unprepared, or non-responsive

METRICS TO SCORE (each 0–100 independently):
- overall_score          : mean of the six scores below (compute it)
- confidence_score       : decisiveness, directness, delivery across answers
- communication_score    : clarity, structure, concrete examples, articulation
- problem_solving_score  : systematic reasoning, trade-offs, methodology
- technical_knowledge_score : accuracy, depth, correct use of terminology
- role_fitment_score     : ownership of own projects, resume depth, WHY/HOW
- clarity_score          : conciseness, absence of filler, listener comprehension

RECOMMENDATION rules:
  overall >= 75  → "Shortlist"
  overall 50–74  → "Hold"
  overall < 50   → "Reject"

Respond with ONLY this JSON (no markdown, no extra text):
{{
  "overall_score": <integer 0-100>,
  "confidence_score": <integer 0-100>,
  "communication_score": <integer 0-100>,
  "problem_solving_score": <integer 0-100>,
  "technical_knowledge_score": <integer 0-100>,
  "role_fitment_score": <integer 0-100>,
  "clarity_score": <integer 0-100>,
  "recommendation": "<Shortlist|Hold|Reject>",
  "summary": "<3-sentence professional summary: overall impression, top strength, main improvement area>",
  "strengths": [
    "<specific strength with evidence from interview>",
    "<specific strength with evidence from interview>",
    "<specific strength with evidence from interview>"
  ],
  "improvement_areas": [
    "<specific area with actionable advice>",
    "<specific area with actionable advice>"
  ]
}}"""

    @staticmethod
    def _fallback_summary(name: str, overall: int) -> str:
        tier = "strong" if overall >= 75 else ("adequate" if overall >= 50 else "limited")
        return (
            f"{name} completed the interview with an overall score of {overall}/100, "
            f"demonstrating {tier} performance across the evaluated dimensions. "
            f"Detailed scores and qualitative feedback are available in the report below."
        )

    @staticmethod
    def _default_final(candidate_name: str, status: str = 'completed') -> Dict:
        return {
            'candidate_name':            candidate_name,
            'interview_status':          status,
            'overall_score':             0,
            'confidence_score':          0,
            'communication_score':       0,
            'problem_solving_score':     0,
            'technical_knowledge_score': 0,
            'role_fitment_score':        0,
            'clarity_score':             0,
            'recommendation':            'Reject',
            'summary':                   'Interview was not completed; insufficient data for evaluation.',
            'strengths':                 [],
            'improvement_areas':         ['Complete a full interview for a meaningful evaluation'],
        }
