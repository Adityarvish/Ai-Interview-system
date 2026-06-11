/**
 * Landing page logic.
 *
 * Submits candidate name + resume + job description to /api/start-interview.
 * On success, stores interview_id, first question text, and first question TTS
 * audio (base64) in sessionStorage so the interview page can pick them up.
 */
const API_BASE_URL = window.location.protocol + '//' + window.location.host;

const form = document.getElementById('interviewForm');
const dropzone = document.getElementById('resumeDropzone');
const fileInput = document.getElementById('resumeFile');
const fileNameLabel = document.getElementById('fileName');
const startButton = document.getElementById('startButton');
const loadingState = document.getElementById('loadingState');
const errorMessage = document.getElementById('errorMessage');

let selectedFile = null;

// ---- Resume Upload Handlers ----
dropzone.addEventListener('click', () => fileInput.click());

dropzone.addEventListener('dragover', (e) => {
    e.preventDefault();
    dropzone.classList.add('border-[#0A0A0A]', 'bg-[#E5E5E5]');
});
dropzone.addEventListener('dragleave', () => {
    dropzone.classList.remove('border-[#0A0A0A]', 'bg-[#E5E5E5]');
});
dropzone.addEventListener('drop', (e) => {
    e.preventDefault();
    dropzone.classList.remove('border-[#0A0A0A]', 'bg-[#E5E5E5]');
    if (e.dataTransfer.files.length > 0) handleFileSelect(e.dataTransfer.files[0]);
});
fileInput.addEventListener('change', (e) => {
    if (e.target.files.length > 0) handleFileSelect(e.target.files[0]);
});

function handleFileSelect(file) {
    const name = (file.name || '').toLowerCase();
    const isPdf = name.endsWith('.pdf');
    const isTxt = name.endsWith('.txt');
    if (!isPdf && !isTxt) {
        showError('Please upload a PDF or TXT file');
        return;
    }
    if (file.size > 10 * 1024 * 1024) {
        showError('File size must be less than 10MB');
        return;
    }
    selectedFile = file;
    fileNameLabel.textContent = `Selected: ${file.name}`;
    fileNameLabel.classList.remove('hidden');
    hideError();
}

// ---- Form Submission ----
form.addEventListener('submit', async (e) => {
    e.preventDefault();

    const candidateName = document.getElementById('candidateName').value.trim();
    const jobDescription = document.getElementById('jobDescription').value.trim();

    if (!candidateName || !jobDescription || !selectedFile) {
        showError('Please fill in all fields and upload a resume');
        return;
    }

    startButton.disabled = true;
    loadingState.classList.remove('hidden');
    hideError();

    try {
        const formData = new FormData();
        formData.append('candidate_name', candidateName);
        formData.append('job_description', jobDescription);
        formData.append('resume', selectedFile);

        const response = await fetch(`${API_BASE_URL}/api/start-interview`, {
            method: 'POST',
            body: formData
        });
        const data = await response.json();

        if (!response.ok || !data.success) {
            throw new Error(data.error || `Server responded ${response.status}`);
        }

        // Persist to sessionStorage for interview.html
        sessionStorage.setItem('interviewId', data.interview_id);
        sessionStorage.setItem('candidateId', data.candidate_id);
        sessionStorage.setItem('firstQuestion', data.first_question || '');

        window.location.href = 'interview.html';

    } catch (error) {
        console.error('start-interview error:', error);
        showError(error.message || 'Failed to start interview. Please try again.');
        startButton.disabled = false;
        loadingState.classList.add('hidden');
    }
});

function showError(msg) {
    errorMessage.querySelector('p').textContent = msg;
    errorMessage.classList.remove('hidden');
}
function hideError() {
    errorMessage.classList.add('hidden');
}
