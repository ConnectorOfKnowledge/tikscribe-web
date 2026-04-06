// tikscribe frontend -- queue-based capture tool

const isNative = window.Capacitor !== undefined;
const API_BASE = isNative ? 'https://tikscribe-web.vercel.app/api' : '/api';
const API_KEY = '';

function apiHeaders(extra = {}) {
    const headers = { 'Content-Type': 'application/json', ...extra };
    if (API_KEY) headers['Authorization'] = `Bearer ${API_KEY}`;
    return headers;
}

let currentTranscript = null;
let pendingFiles = [];
let editAttachments = [];
let selectedRating = 0;
let historyItems = new Map(); // keyed by id for safe onclick lookup

document.addEventListener('DOMContentLoaded', () => {
    loadHistory();
    checkSharedIntent();
});

// ── Share Intent (Android) ───────────────────────────────────
function checkSharedIntent() {
    if (!window.Capacitor) return;

    function tryApply() {
        if (window._sharedIntentText) {
            const text = window._sharedIntentText;
            window._sharedIntentText = null;
            const urlMatch = text.match(/https?:\/\/[^\s]+/);
            if (urlMatch) {
                document.getElementById('url-input').value = urlMatch[0];
                showToast('URL ready -- add notes and tap Send');
                return true;
            }
        }
        return false;
    }

    if (!tryApply()) {
        let attempts = 0;
        const interval = setInterval(() => {
            if (tryApply() || ++attempts >= 15) clearInterval(interval);
        }, 300);
    }
}

document.getElementById('url-input').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') submitUrl();
});

// ── File Handling ────────────────────────────────────────────
document.getElementById('file-input').addEventListener('change', (e) => {
    const files = Array.from(e.target.files);
    files.forEach(file => {
        if (!file.type.startsWith('image/')) return;
        if (pendingFiles.length >= 5) { showToast('Max 5 files'); return; }
        pendingFiles.push(file);
    });
    renderFilePreviews();
    e.target.value = '';
});

document.addEventListener('DOMContentLoaded', () => {
    const editInput = document.getElementById('edit-file-input');
    if (editInput) {
        editInput.addEventListener('change', async (e) => {
            const files = Array.from(e.target.files);
            for (const file of files) {
                if (!file.type.startsWith('image/')) continue;
                if (editAttachments.length >= 5) { showToast('Max 5 files'); break; }
                const b64 = await fileToBase64(file);
                editAttachments.push(b64);
            }
            renderEditAttachments();
            e.target.value = '';
        });
    }
});

function renderFilePreviews() {
    const container = document.getElementById('file-previews');
    container.innerHTML = pendingFiles.map((file, i) => {
        const url = URL.createObjectURL(file);
        return `<div class="file-preview">
            <img src="${url}" alt="${escapeHtml(file.name)}">
            <button class="remove-file" onclick="removeFile(${i})">&times;</button>
        </div>`;
    }).join('');
}

function removeFile(index) {
    pendingFiles.splice(index, 1);
    renderFilePreviews();
}

async function fileToBase64(file) {
    const data = await new Promise((resolve) => {
        const reader = new FileReader();
        reader.onload = () => resolve(reader.result);
        reader.readAsDataURL(file);
    });
    return { name: file.name, type: file.type, size: file.size, data };
}

async function filesToBase64(files) {
    const results = [];
    for (const file of files) results.push(await fileToBase64(file));
    return results;
}

// ── Rating ───────────────────────────────────────────────────
function setRating(value) {
    selectedRating = value;
    updateStarDisplay();
}

function updateStarDisplay() {
    const stars = document.querySelectorAll('#star-rating .star');
    stars.forEach((star, i) => star.classList.toggle('active', i < selectedRating));
    const clearBtn = document.getElementById('rating-clear');
    if (clearBtn) clearBtn.style.display = selectedRating > 0 ? 'inline-block' : 'none';
}

document.addEventListener('DOMContentLoaded', () => {
    const container = document.getElementById('star-rating');
    if (!container) return;
    const stars = container.querySelectorAll('.star');
    stars.forEach((star, i) => {
        star.addEventListener('mouseenter', () => {
            stars.forEach((s, j) => s.classList.toggle('hover', j <= i));
        });
    });
    container.addEventListener('mouseleave', () => {
        stars.forEach(s => s.classList.remove('hover'));
    });
});

function renderStarsReadonly(rating) {
    if (!rating) return '';
    let stars = '';
    for (let i = 1; i <= 5; i++) {
        stars += `<span class="star-display ${i <= rating ? 'filled' : ''}">\u2605</span>`;
    }
    return `<span class="rating-display">${stars}</span>`;
}

// ── Submit (Queue-Based -- Instant Return) ───────────────────
async function submitUrl() {
    const input = document.getElementById('url-input');
    const url = input.value.trim();

    if (!url) { input.focus(); return; }

    const notes = document.getElementById('notes-input').value.trim();
    const attachments = pendingFiles.length > 0 ? await filesToBase64(pendingFiles) : [];

    const btn = document.getElementById('submit-btn');
    btn.disabled = true;
    btn.textContent = 'Sending...';

    try {
        const payload = { url };
        if (notes) payload.notes = notes;
        if (selectedRating > 0) payload.rating = selectedRating;
        if (attachments.length > 0) payload.attachments = attachments;

        const res = await fetch(`${API_BASE}/transcribe`, {
            method: 'POST',
            headers: apiHeaders(),
            body: JSON.stringify(payload)
        });

        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.error || 'Failed to submit');
        }

        const data = await res.json();

        // Instant confirmation -- no waiting for processing
        showConfirmationModal(data);
        resetForm();
        loadHistory();

    } catch (err) {
        showToast('Error: ' + err.message);
    } finally {
        btn.disabled = false;
        btn.textContent = 'Send';
    }
}

function resetForm() {
    document.getElementById('url-input').value = '';
    document.getElementById('notes-input').value = '';
    setRating(0);
    pendingFiles = [];
    renderFilePreviews();
}

// ── History ──────────────────────────────────────────────────
async function loadHistory() {
    try {
        const res = await fetch(`${API_BASE}/history`, { headers: apiHeaders() });
        if (!res.ok) return;
        const data = await res.json();

        const list = document.getElementById('history-list');
        const archiveList = document.getElementById('archive-list');

        if (!data.transcripts || data.transcripts.length === 0) {
            list.innerHTML = '<p class="empty-state">No transcripts yet. Paste a URL above to get started.</p>';
            if (archiveList) archiveList.innerHTML = '';
            return;
        }

        // Store items in map for safe onclick lookup (avoids XSS via JSON in attributes)
        historyItems.clear();
        data.transcripts.forEach(t => historyItems.set(t.id, t));

        const active = data.transcripts.filter(t => t.review_status !== 'reviewed');
        const archived = data.transcripts.filter(t => t.review_status === 'reviewed');

        if (active.length === 0) {
            list.innerHTML = '<p class="empty-state">All caught up! No unreviewed transcripts.</p>';
        } else {
            list.innerHTML = renderGroupedHistory(active);
        }

        if (archiveList) {
            if (archived.length === 0) {
                archiveList.innerHTML = '<p class="empty-state">No archived transcripts yet.</p>';
            } else {
                archiveList.innerHTML = archived.map(t => renderHistoryItem(t, true)).join('');
            }
            const archiveSection = document.getElementById('archive-section');
            if (archiveSection) archiveSection.style.display = archived.length > 0 ? 'block' : 'none';
        }
    } catch (err) {
        console.error('Failed to load history:', err);
    }
}

function renderGroupedHistory(transcripts) {
    const groups = {};
    transcripts.forEach(t => {
        const cat = (t.categories && t.categories.length > 0) ? t.categories[0] : 'Uncategorized';
        if (!groups[cat]) groups[cat] = [];
        groups[cat].push(t);
    });

    const sortedCats = Object.keys(groups).sort((a, b) => {
        if (a === 'Uncategorized') return 1;
        if (b === 'Uncategorized') return -1;
        return a.localeCompare(b);
    });

    return sortedCats.map(cat => {
        const items = groups[cat];
        const catId = `cat-${cat.replace(/[^a-zA-Z0-9]/g, '-')}`;
        return `
            <div class="category-group">
                <div class="category-group-header" onclick="toggleCategoryGroup('${catId}')">
                    <span class="category-group-name">${escapeHtml(cat)}</span>
                    <span class="category-group-count">${items.length}</span>
                    <span class="category-group-chevron" id="${catId}-chevron">&#9660;</span>
                </div>
                <div class="category-group-items" id="${catId}">
                    ${items.map(t => renderHistoryItem(t)).join('')}
                </div>
            </div>
        `;
    }).join('');
}

function toggleCategoryGroup(catId) {
    const items = document.getElementById(catId);
    const chevron = document.getElementById(catId + '-chevron');
    if (items.classList.contains('collapsed')) {
        items.classList.remove('collapsed');
        chevron.innerHTML = '&#9660;';
    } else {
        items.classList.add('collapsed');
        chevron.innerHTML = '&#9654;';
    }
}

function renderHistoryItem(t, isArchived = false) {
    const badges = [];
    if (t.notes) badges.push('<span class="history-notes-indicator"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>notes</span>');
    if (t.attachments && t.attachments.length > 0) badges.push(`<span class="history-notes-indicator"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="18" height="18" rx="2" ry="2"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21 15 16 10 5 21"/></svg>${t.attachments.length}</span>`);

    // Status badge for queued/processing items
    const statusBadge = t.status === 'queued' ? '<span class="status-badge queued">Queued</span>'
        : t.status === 'processing' ? '<span class="status-badge processing">Processing...</span>'
        : t.status === 'failed' ? '<span class="status-badge failed">Failed</span>'
        : '';

    const sourceUrl = t.url ? `<a class="history-source-link" href="${escapeHtml(t.url)}" target="_blank" rel="noopener" onclick="event.stopPropagation()">${truncateUrl(t.url, 40)}</a>` : '';

    return `
        <div class="history-item ${isArchived ? 'archived' : ''} ${t.status !== 'completed' ? 'pending' : ''}" ${t.status === 'completed' ? `onclick="viewTranscriptById('${escapeHtml(t.id)}')"` : ''}>
            ${t.thumbnail_url
                ? `<img class="history-thumb" src="${t.thumbnail_url}" alt="">`
                : '<div class="history-thumb"></div>'
            }
            <div class="history-info">
                <h3>${escapeHtml(t.generated_title || t.title || 'Untitled')} ${statusBadge}</h3>
                <p class="meta">${escapeHtml(t.creator || 'Unknown')} | ${formatDuration(t.duration)} | ${formatDate(t.created_at)}${badges.join('')}${renderStarsReadonly(t.rating)}</p>
                ${sourceUrl}
                <div class="history-categories">
                    ${(t.categories || []).map(c => `<span class="category-tag">${escapeHtml(c)}</span>`).join('')}
                </div>
            </div>
        </div>
    `;
}

// ── View Transcript ──────────────────────────────────────────
function viewTranscriptById(id) {
    const data = historyItems.get(id);
    if (data) viewTranscript(data);
}

async function viewTranscript(data) {
    show('status-section');
    hide('result-section');
    setStatus('Loading...', 'Fetching transcript');

    try {
        const res = await fetch(`${API_BASE}/status?id=${data.id}`, { headers: apiHeaders() });
        const full = await res.json();
        showResult(full.transcript ? full : data);
    } catch (err) {
        showResult(data);
    }

    window.scrollTo({ top: 0, behavior: 'smooth' });
}

function showResult(data) {
    currentTranscript = data;
    hide('status-section');
    show('result-section');
    hide('edit-panel');
    show('result-actions');

    const thumb = document.getElementById('result-thumb');
    if (data.thumbnail_url) {
        thumb.src = data.thumbnail_url;
        thumb.style.display = 'block';
    } else {
        thumb.style.display = 'none';
    }

    document.getElementById('result-title').textContent = data.generated_title || data.title || 'Untitled';
    const metaText = `${data.creator || 'Unknown'} | ${formatDuration(data.duration)}`;
    document.getElementById('result-meta').innerHTML = escapeHtml(metaText) + renderStarsReadonly(data.rating);

    const sourceLink = document.getElementById('result-source-url');
    if (data.url) {
        sourceLink.href = data.url;
        sourceLink.textContent = truncateUrl(data.url, 50);
        sourceLink.classList.remove('hidden');
    } else {
        sourceLink.classList.add('hidden');
    }

    document.getElementById('result-transcript').textContent = data.transcript || '(No audio transcript)';

    // Visual summary
    const visualBox = document.getElementById('result-visual-box');
    const visualEl = document.getElementById('result-visual');
    if (visualBox && visualEl) {
        if (data.visual_summary) {
            visualEl.textContent = data.visual_summary;
            visualBox.classList.remove('hidden');
        } else {
            visualBox.classList.add('hidden');
        }
    }

    // Notes
    const notesBox = document.getElementById('result-notes-box');
    const notesEl = document.getElementById('result-notes');
    if (data.notes) {
        notesEl.textContent = data.notes;
        notesBox.classList.remove('hidden');
    } else {
        notesBox.classList.add('hidden');
    }

    // Attachments
    const attachBox = document.getElementById('result-attachments-box');
    const attachEl = document.getElementById('result-attachments');
    if (data.attachments && data.attachments.length > 0) {
        attachEl.innerHTML = data.attachments.map(a =>
            `<img src="${a.data}" alt="${escapeHtml(a.name || 'screenshot')}" onclick="window.open(this.src, '_blank')">`
        ).join('');
        attachBox.classList.remove('hidden');
    } else {
        attachBox.classList.add('hidden');
    }

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

function copyTranscript() {
    if (!currentTranscript) return;
    const text = [
        currentTranscript.transcript || '',
        currentTranscript.visual_summary ? '\n--- Visual Summary ---\n' + currentTranscript.visual_summary : ''
    ].filter(Boolean).join('\n');
    navigator.clipboard.writeText(text).then(() => showToast('Copied!'));
}

function backToList() {
    hide('result-section');
    window.scrollTo({ top: 0, behavior: 'smooth' });
}

// ── Edit Mode ────────────────────────────────────────────────
function toggleEdit() {
    if (!currentTranscript) return;
    document.getElementById('edit-notes').value = currentTranscript.notes || '';
    editAttachments = currentTranscript.attachments ? [...currentTranscript.attachments] : [];
    renderEditAttachments();
    show('edit-panel');
    hide('result-actions');
}

function cancelEdit() {
    hide('edit-panel');
    show('result-actions');
}

function renderEditAttachments() {
    const container = document.getElementById('edit-attachments');
    if (!container) return;
    container.innerHTML = editAttachments.map((a, i) =>
        `<div class="edit-attachment-wrap">
            <img src="${a.data}" alt="${escapeHtml(a.name || 'screenshot')}">
            <button class="remove-attach" onclick="removeEditAttachment(${i})">&times;</button>
        </div>`
    ).join('');
}

function removeEditAttachment(index) {
    editAttachments.splice(index, 1);
    renderEditAttachments();
}

async function saveEdit() {
    if (!currentTranscript) return;
    const saveBtn = document.querySelector('#edit-panel .btn-primary');
    saveBtn.disabled = true;
    saveBtn.textContent = 'Saving...';

    try {
        const res = await fetch(`${API_BASE}/review`, {
            method: 'POST',
            headers: apiHeaders(),
            body: JSON.stringify({
                id: currentTranscript.id,
                review_status: currentTranscript.review_status || null,
                notes: document.getElementById('edit-notes').value.trim() || null,
                attachments: editAttachments.length > 0 ? editAttachments : null
            })
        });
        if (!res.ok) throw new Error('Failed to save');
        currentTranscript.notes = document.getElementById('edit-notes').value.trim() || null;
        currentTranscript.attachments = editAttachments.length > 0 ? editAttachments : null;
        showResult(currentTranscript);
        showToast('Saved!');
        loadHistory();
    } catch (err) {
        showToast('Error: ' + err.message);
    } finally {
        saveBtn.disabled = false;
        saveBtn.textContent = 'Save';
    }
}

// ── Confirmation Modal ───────────────────────────────────────
function showConfirmationModal(data) {
    const details = document.getElementById('confirm-details');
    const title = data.title || 'Untitled';
    details.innerHTML = `<strong>${escapeHtml(title)}</strong><br>Queued for processing`;
    document.getElementById('confirm-overlay').classList.remove('hidden');
}

function dismissConfirmation(event) {
    if (event.target.id === 'confirm-overlay') acknowledgeConfirmation();
}

function acknowledgeConfirmation() {
    document.getElementById('confirm-overlay').classList.add('hidden');
    document.getElementById('url-input').focus();
}

// ── Helpers ──────────────────────────────────────────────────
function show(id) { document.getElementById(id)?.classList.remove('hidden'); }
function hide(id) { document.getElementById(id)?.classList.add('hidden'); }

function setStatus(text, detail) {
    document.getElementById('status-text').textContent = text;
    const detailEl = document.getElementById('status-detail');
    detailEl.textContent = detail || '';
}

function formatDuration(seconds) {
    if (!seconds) return '';
    const m = Math.floor(seconds / 60);
    const s = seconds % 60;
    return `${m}:${String(s).padStart(2, '0')}`;
}

function formatDate(dateStr) {
    if (!dateStr) return '';
    return new Date(dateStr).toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
}

function escapeHtml(str) {
    if (!str) return '';
    return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function truncateUrl(url, maxLen) {
    if (!url) return '';
    const clean = url.replace(/^https?:\/\//, '');
    return clean.length > maxLen ? clean.substring(0, maxLen) + '...' : clean;
}

function toggleArchive() {
    const list = document.getElementById('archive-list');
    const toggle = document.getElementById('archive-toggle');
    if (list.classList.contains('hidden')) {
        list.classList.remove('hidden');
        toggle.textContent = 'Hide';
    } else {
        list.classList.add('hidden');
        toggle.textContent = 'Show';
    }
}

// ── TicketDeck Bug Widget ──────────────────────────────────────
const TICKETDECK_URL = 'https://dgnikbbugiuuwokwenlm.supabase.co/rest/v1/tickets';
const TICKETDECK_KEY = 'sb_publishable_L2VH13C5NYtdSBoENpoh9Q_d7iJHDOF';
let bugFiles = [];

document.addEventListener('DOMContentLoaded', () => {
    const bugInput = document.getElementById('bug-file-input');
    if (bugInput) {
        bugInput.addEventListener('change', (e) => {
            Array.from(e.target.files).forEach(file => {
                if (!file.type.startsWith('image/')) return;
                if (bugFiles.length >= 3) { showToast('Max 3 screenshots'); return; }
                bugFiles.push(file);
            });
            renderBugFilePreviews();
            e.target.value = '';
        });
    }
});

function renderBugFilePreviews() {
    const container = document.getElementById('bug-file-previews');
    if (!container) return;
    container.innerHTML = bugFiles.map((file, i) => {
        const url = URL.createObjectURL(file);
        return `<div class="bug-file-preview">
            <img src="${url}" alt="${escapeHtml(file.name)}">
            <button class="remove-bug-file" onclick="removeBugFile(${i})">&times;</button>
        </div>`;
    }).join('');
}

function removeBugFile(index) {
    bugFiles.splice(index, 1);
    renderBugFilePreviews();
}

function toggleBugPanel() {
    const panel = document.getElementById('bug-panel');
    panel.classList.toggle('hidden');
    if (!panel.classList.contains('hidden')) {
        document.getElementById('bug-form').reset();
        bugFiles = [];
        renderBugFilePreviews();
        const status = document.getElementById('bug-status');
        status.classList.add('hidden');
        status.className = 'bug-status-msg hidden';
    }
}

async function submitBugTicket(e) {
    e.preventDefault();
    const btn = document.getElementById('bug-submit-btn');
    const statusEl = document.getElementById('bug-status');
    btn.disabled = true;
    btn.textContent = 'Submitting...';
    statusEl.classList.add('hidden');

    const attachments = bugFiles.length > 0 ? await filesToBase64(bugFiles) : null;
    const ticket = {
        project: 'tikscribe',
        type: document.getElementById('bug-type').value,
        priority: document.getElementById('bug-priority').value,
        title: document.getElementById('bug-title').value.trim().substring(0, 60),
        description: document.getElementById('bug-desc').value.trim() || document.getElementById('bug-title').value.trim(),
        status: 'open',
        tags: ['tikscribe-widget'],
    };
    if (attachments) ticket.attachments = attachments;

    try {
        const res = await fetch(TICKETDECK_URL, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'apikey': TICKETDECK_KEY,
                'Authorization': `Bearer ${TICKETDECK_KEY}`,
                'Prefer': 'return=representation'
            },
            body: JSON.stringify(ticket)
        });
        if (!res.ok) throw new Error((await res.json()).message || 'Failed');

        statusEl.textContent = 'Ticket submitted!';
        statusEl.className = 'bug-status-msg success';
        statusEl.classList.remove('hidden');
        document.getElementById('bug-form').reset();
        bugFiles = [];
        renderBugFilePreviews();
        setTimeout(() => toggleBugPanel(), 2000);
    } catch (err) {
        statusEl.textContent = 'Error: ' + err.message;
        statusEl.className = 'bug-status-msg error';
        statusEl.classList.remove('hidden');
    } finally {
        btn.disabled = false;
        btn.textContent = 'Submit Ticket';
    }
}

function showToast(msg) {
    const toast = document.createElement('div');
    toast.className = 'toast';
    toast.textContent = msg;
    document.body.appendChild(toast);
    setTimeout(() => toast.remove(), 2000);
}
