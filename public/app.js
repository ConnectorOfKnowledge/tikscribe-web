// tikscribe frontend

const API_BASE = '/api';
let currentTranscript = null;

// On page load, fetch history
document.addEventListener('DOMContentLoaded', loadHistory);

// Allow Enter key to submit
document.getElementById('url-input').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') submitUrl();
});

async function submitUrl() {
    const input = document.getElementById('url-input');
    const url = input.value.trim();

    if (!url) {
        input.focus();
        return;
    }

    // Show status, hide others
    show('status-section');
    hide('result-section');
    document.getElementById('submit-btn').disabled = true;
    setStatus('Submitting...', 'Sending URL to transcription service');

    try {
        // Submit the URL
        const res = await fetch(`${API_BASE}/transcribe`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ url })
        });

        if (!res.ok) {
            const err = await res.json();
            let msg = err.error || 'Failed to submit URL';
            if (err.traceback) msg += '\n\n' + err.traceback;
            throw new Error(msg);
        }

        const data = await res.json();
        setStatus('Processing...', `Transcribing audio (this takes 30-60 seconds)`);

        // Poll for status
        await pollStatus(data.id);

    } catch (err) {
        setStatus('Error', err.message);
        document.getElementById('submit-btn').disabled = false;
    }
}

async function pollStatus(id) {
    const maxAttempts = 60; // 5 minutes max
    let attempts = 0;

    while (attempts < maxAttempts) {
        attempts++;
        await sleep(5000); // Check every 5 seconds

        try {
            const res = await fetch(`${API_BASE}/status?id=${id}`);
            const data = await res.json();

            if (data.status === 'completed') {
                showResult(data);
                loadHistory(); // Refresh history
                return;
            } else if (data.status === 'error') {
                throw new Error(data.error || 'Transcription failed');
            }

            // Still processing
            setStatus('Transcribing...', `Working on it... (${attempts * 5}s)`);

        } catch (err) {
            setStatus('Error', err.message);
            document.getElementById('submit-btn').disabled = false;
            return;
        }
    }

    setStatus('Timeout', 'Transcription is taking too long. Try again later.');
    document.getElementById('submit-btn').disabled = false;
}

function showResult(data) {
    currentTranscript = data;

    hide('status-section');
    show('result-section');
    document.getElementById('submit-btn').disabled = false;

    // Populate result
    const thumb = document.getElementById('result-thumb');
    if (data.thumbnail_url) {
        thumb.src = data.thumbnail_url;
        thumb.style.display = 'block';
    } else {
        thumb.style.display = 'none';
    }

    document.getElementById('result-title').textContent = data.generated_title || data.title || 'Untitled';
    document.getElementById('result-meta').textContent =
        `${data.creator || 'Unknown'} | ${formatDuration(data.duration)}`;
    document.getElementById('result-transcript').textContent = data.transcript;

    // Categories
    const catContainer = document.getElementById('result-categories');
    catContainer.innerHTML = '';
    if (data.categories && data.categories.length > 0) {
        data.categories.forEach(cat => {
            const tag = document.createElement('span');
            tag.className = 'category-tag';
            tag.textContent = cat;
            catContainer.appendChild(tag);
        });
    }
}

async function loadHistory() {
    try {
        const res = await fetch(`${API_BASE}/history`);
        if (!res.ok) return;
        const data = await res.json();

        const list = document.getElementById('history-list');

        if (!data.transcripts || data.transcripts.length === 0) {
            list.innerHTML = '<p class="empty-state">No transcripts yet. Paste a URL above to get started.</p>';
            return;
        }

        list.innerHTML = data.transcripts.map(t => `
            <div class="history-item" onclick='viewTranscript(${JSON.stringify(t).replace(/'/g, "&#39;")})'>
                ${t.thumbnail_url
                    ? `<img class="history-thumb" src="${t.thumbnail_url}" alt="">`
                    : '<div class="history-thumb"></div>'
                }
                <div class="history-info">
                    <h3>${escapeHtml(t.generated_title || t.title || 'Untitled')}</h3>
                    <p class="meta">${escapeHtml(t.creator || 'Unknown')} | ${formatDuration(t.duration)} | ${formatDate(t.created_at)}</p>
                    <div class="history-categories">
                        ${(t.categories || []).map(c => `<span class="category-tag">${escapeHtml(c)}</span>`).join('')}
                    </div>
                </div>
            </div>
        `).join('');

    } catch (err) {
        console.error('Failed to load history:', err);
    }
}

function viewTranscript(data) {
    showResult(data);
    window.scrollTo({ top: 0, behavior: 'smooth' });
}

function copyTranscript() {
    if (!currentTranscript) return;
    navigator.clipboard.writeText(currentTranscript.transcript).then(() => {
        showToast('Copied to clipboard!');
    });
}

function resetForm() {
    hide('result-section');
    document.getElementById('url-input').value = '';
    document.getElementById('url-input').focus();
}

// Helpers
function show(id) { document.getElementById(id).classList.remove('hidden'); }
function hide(id) { document.getElementById(id).classList.add('hidden'); }
function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

function setStatus(text, detail) {
    document.getElementById('status-text').textContent = text;
    const detailEl = document.getElementById('status-detail');
    detailEl.textContent = detail || '';
    detailEl.style.whiteSpace = detail && detail.includes('\n') ? 'pre-wrap' : 'normal';
    detailEl.style.textAlign = detail && detail.includes('\n') ? 'left' : 'center';
    detailEl.style.fontSize = detail && detail.includes('\n') ? '0.75rem' : '';
}

function formatDuration(seconds) {
    if (!seconds) return '';
    const m = Math.floor(seconds / 60);
    const s = seconds % 60;
    return `${m}:${String(s).padStart(2, '0')}`;
}

function formatDate(dateStr) {
    if (!dateStr) return '';
    const d = new Date(dateStr);
    return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
}

function escapeHtml(str) {
    if (!str) return '';
    return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function showToast(msg) {
    const toast = document.createElement('div');
    toast.className = 'toast';
    toast.textContent = msg;
    document.body.appendChild(toast);
    setTimeout(() => toast.remove(), 2000);
}
