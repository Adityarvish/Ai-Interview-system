/**
 * Result page — v8
 * Single source of truth: reads the flat evaluation object from either
 * sessionStorage (set by interview.js) or the /api/final-report endpoint.
 * No nested wrappers, no per-question data, no duplicate fields.
 */

const API_BASE_URL = window.location.protocol + '//' + window.location.host;

// ── METRIC DEFINITIONS ────────────────────────────────────────────────────────
const METRIC_DEFS = [
    {
        label:       'Technical Knowledge',
        field:       'technical_knowledge_score',
        icon:        'ph-code',
        color:       '#0EA5E9',
        description: 'Accuracy, depth, and correctness of technical answers; terminology and trade-offs',
    },
    {
        label:       'Communication',
        field:       'communication_score',
        icon:        'ph-chat-text',
        color:       '#8B5CF6',
        description: 'Clarity, logical structure, use of concrete examples and articulation',
    },
    {
        label:       'Problem Solving',
        field:       'problem_solving_score',
        icon:        'ph-puzzle-piece',
        color:       '#F59E0B',
        description: 'Systematic approach, consideration of alternatives and trade-offs',
    },
    {
        label:       'Confidence',
        field:       'confidence_score',
        icon:        'ph-microphone',
        color:       '#EF4444',
        description: 'Decisiveness, directness, and professional delivery without excessive hedging',
    },
    {
        label:       'Clarity',
        field:       'clarity_score',
        icon:        'ph-eye',
        color:       '#10B981',
        description: 'Conciseness, absence of filler, and ease of comprehension across all answers',
    },
    {
        label:       'Role Fitment',
        field:       'role_fitment_score',
        icon:        'ph-briefcase',
        color:       '#6366F1',
        description: 'Ownership of own projects, resume depth, ability to explain WHY and HOW',
    },
];

// ── BOOT ──────────────────────────────────────────────────────────────────────
// Wait for DOM before doing anything, including error handling.
document.addEventListener('DOMContentLoaded', () => {
    const interviewId = sessionStorage.getItem('interviewId');
    if (!interviewId) {
        showError('No interview session found. Please start a new interview.');
        return;
    }
    loadResults(interviewId);
});

// ── DATA LOADING ─────────────────────────────────────────────────────────────
async function loadResults(interviewId) {
    try {
        let evaluation = null;

        // Primary: use data already returned by end-interview (fastest, no extra call)
        const cached = sessionStorage.getItem('evaluationData');
        if (cached) {
            try { evaluation = JSON.parse(cached); } catch (_) { evaluation = null; }
        }

        // Fallback / fail-safe: fetch from DB — poll up to 5 times with backoff
        // in case the evaluation write is still in progress when we land here.
        // FIX v10: overall_score === 0 IS valid (it's the safe fallback returned
        // when LLM times out). Only re-poll when the entire evaluation is missing.
        const isIncomplete = (ev) =>
            !ev ||
            ev.overall_score === undefined ||
            ev.overall_score === null ||
            !ev.recommendation;

        if (isIncomplete(evaluation)) {
            console.log('[RESULT] No cached data or incomplete — fetching from DB');
            // FIX v10: More polls with longer delays to handle slow LLM evaluation
            // completing after the page redirect (LLM can take up to 60s now).
            const MAX_POLLS = 5;
            const POLL_DELAYS = [0, 2000, 4000, 6000, 8000];
            for (let i = 0; i < MAX_POLLS; i++) {
                if (POLL_DELAYS[i] > 0) {
                    setLoadingMessage(`Loading results… (attempt ${i + 1}/${MAX_POLLS})`);
                    await sleep(POLL_DELAYS[i]);
                }
                try {
                    const res = await fetch(`${API_BASE_URL}/api/final-report/${interviewId}`);
                    if (!res.ok) throw new Error(`HTTP ${res.status}`);
                    const data = await res.json();
                    if (data.success && data.evaluation && !isIncomplete(data.evaluation)) {
                        evaluation = data.evaluation;
                        console.log(`[RESULT] Got evaluation on poll ${i + 1}`);
                        break;
                    }
                    console.warn(`[RESULT] Poll ${i + 1}: evaluation incomplete or missing`);
                } catch (fetchErr) {
                    console.warn(`[RESULT] Poll ${i + 1} failed:`, fetchErr.message);
                }
            }
        }

        // Clear cache only after we have confirmed valid data
        sessionStorage.removeItem('evaluationData');

        if (isIncomplete(evaluation)) {
            throw new Error('Evaluation data is unavailable after all retries.');
        }

        displayResults(evaluation);

    } catch (err) {
        console.error('[RESULT] Error loading evaluation:', err);
        showError('Could not load evaluation results. Please check your connection and try again.');
    }
}

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }
function setLoadingMessage(msg) {
    const el = document.getElementById('loadingText');
    if (el) el.textContent = msg;
}

// ── RENDERER ──────────────────────────────────────────────────────────────────
function displayResults(ev) {
    document.getElementById('loadingState').classList.add('hidden');
    document.getElementById('resultsContent').classList.remove('hidden');

    // Candidate header
    document.getElementById('candidateName').textContent = ev.candidate_name || 'Candidate';
    const status = ev.interview_status || 'completed';
    document.getElementById('interviewStatus').textContent =
        status === 'cancelled' ? 'Partial Evaluation — Interview Cancelled' : 'Interview Completed';

    // Overall score ring
    const overall = Math.round(Number(ev.overall_score) || 0);
    animateNumber(document.getElementById('overallScore'), overall, 1800);
    animateCircle(document.getElementById('scoreCircle'), overall, 389.6);

    // Recommendation
    const rec = ev.recommendation || 'Hold';
    const badge = document.getElementById('recommendationBadge');
    badge.textContent = rec;
    badge.className = 'inline-block px-5 py-2 rounded-full text-sm font-semibold border ' +
        (rec === 'Shortlist' ? 'tier-shortlist' : rec === 'Hold' ? 'tier-hold' : 'tier-reject');
    const borderColor = rec === 'Shortlist' ? '#86efac' : rec === 'Hold' ? '#fde68a' : '#fca5a5';
    document.getElementById('recommendationCard').style.borderColor = borderColor;

    // Summary
    document.getElementById('summary').textContent = ev.summary || '';

    // ── Score Breakdown ────────────────────────────────────────────────────
    const breakdownContainer = document.getElementById('breakdownContainer');
    breakdownContainer.innerHTML = '';

    METRIC_DEFS.forEach((dim, idx) => {
        const score    = Math.round(Number(ev[dim.field] || 0) * 10) / 10;
        const pct      = Math.min(100, Math.round(score));
        const barColor = pct >= 75 ? '#22c55e' : pct >= 55 ? '#007AFF' : pct >= 35 ? '#f59e0b' : '#ef4444';
        const label    = pct >= 75 ? 'Strong'  : pct >= 55 ? 'Adequate' : pct >= 35 ? 'Developing' : 'Needs Work';

        const el = document.createElement('div');
        el.className = 'dim-card bg-[#FAFAFA] rounded-xl p-5 border-l-4 transition-all mb-3';
        el.style.borderLeftColor = dim.color;
        el.innerHTML = `
          <div class="flex items-start justify-between mb-3">
            <div class="flex items-center gap-2">
              <i class="ph ${dim.icon} text-lg" style="color:${dim.color}"></i>
              <div>
                <span class="text-sm font-semibold text-[#0A0A0A]">${dim.label}</span>
                <span class="ml-2 text-xs font-medium px-2 py-0.5 rounded-full"
                      style="background:${barColor}22;color:${barColor}">${label}</span>
              </div>
            </div>
            <div class="text-right shrink-0">
              <span class="text-2xl font-bold text-[#0A0A0A]">${score.toFixed(0)}</span>
              <span class="text-sm text-[#A3A3A3]">/100</span>
            </div>
          </div>
          <div class="w-full bg-[#E5E5E5] rounded-full h-2.5 mb-2">
            <div id="bar-${idx}" class="h-2.5 rounded-full bar-fill" style="width:0%;background:${barColor}"></div>
          </div>
          <p class="text-xs text-[#A3A3A3]">${dim.description}</p>
        `;
        breakdownContainer.appendChild(el);

        // Animate bar after paint
        requestAnimationFrame(() => {
            setTimeout(() => {
                const bar = document.getElementById(`bar-${idx}`);
                if (bar) bar.style.width = `${pct}%`;
            }, 150 + idx * 80);
        });
    });

    // ── Strengths ──────────────────────────────────────────────────────────
    const strengthsList = document.getElementById('strengthsList');
    strengthsList.innerHTML = '';
    (ev.strengths || []).forEach(s => {
        const li = document.createElement('li');
        li.className = 'flex items-start gap-2 text-sm text-[#525252]';
        li.innerHTML = `<i class="ph ph-check-circle text-green-500 mt-0.5 shrink-0"></i><span>${s}</span>`;
        strengthsList.appendChild(li);
    });

    // ── Improvements ───────────────────────────────────────────────────────
    const improvementsList = document.getElementById('improvementsList');
    improvementsList.innerHTML = '';
    (ev.improvement_areas || []).forEach(s => {
        const li = document.createElement('li');
        li.className = 'flex items-start gap-2 text-sm text-[#525252]';
        li.innerHTML = `<i class="ph ph-arrow-up-right text-amber-500 mt-0.5 shrink-0"></i><span>${s}</span>`;
        improvementsList.appendChild(li);
    });
}

// ── HELPERS ───────────────────────────────────────────────────────────────────
function animateNumber(el, target, duration) {
    if (!el) return;
    const start = performance.now();
    function step(now) {
        const progress = Math.min((now - start) / duration, 1);
        el.textContent = Math.round(progress * target);
        if (progress < 1) requestAnimationFrame(step);
    }
    requestAnimationFrame(step);
}

function animateCircle(circle, percentage, circumference) {
    if (!circle) return;
    const offset = circumference - (percentage / 100) * circumference;
    setTimeout(() => { circle.style.strokeDashoffset = offset; }, 150);
}

function showError(message) {
    const loading = document.getElementById('loadingState');
    const error   = document.getElementById('errorState');
    const msg     = document.getElementById('errorMessage');
    if (loading) loading.classList.add('hidden');
    if (error)   error.classList.remove('hidden');
    if (msg && message) msg.textContent = message;
}
