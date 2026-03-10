// tikscribe frontend

// Detect native app (Capacitor) vs web — API calls need full URL in native
const isNative = window.Capacitor !== undefined;
const API_BASE = isNative ? 'https://tikscribe-web.vercel.app/api' : '/api';
let currentTranscript = null;
let pendingFiles = []; // files queued for upload
let editAttachments = []; // attachments being edited

// On page load, fetch history and check for shared intent
document.addEventListener('DOMContentLoaded', () => {
    loadHistory();
    checkSharedIntent();
});

// ── Share Intent (Android) ───────────────────────────────────
function checkSharedIntent() {
    // Only runs inside Capacitor (native app)
    if (!window.Capacitor) return;

    // Native code injects window._sharedIntentText from Intent.EXTRA_TEXT
    // (same way a text message app reads the shared link)
    function tryApply() {
        if (window._sharedIntentText) {
            const text = window._sharedIntentText;
            window._sharedIntentText = null;
            const urlMatch = text.match(/https?:\/\/[^\s]+/);
            if (urlMatch) {
                document.getElementById('url-input').value = urlMatch[0];
                showToast('URL ready — add notes and tap Transcribe');
                return true;
            }
        }
        return false;
    }

    // Poll for the injected value (native retries injection at staggered delays)
    if (!tryApply()) {
        let attempts = 0;
        const interval = setInterval(() => {
            if (tryApply() || ++attempts >= 15) clearInterval(interval);
        }, 300);
    }
}

// Allow Enter key to submit (but not from textarea)
document.getElementById('url-input').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') submitUrl();
});

// File input handler
document.getElementById('file-input').addEventListener('change', (e) => {
    const files = Array.from(e.target.files);
    files.forEach(file => {
        if (!file.type.startsWith('image/')) return;
        if (pendingFiles.length >= 5) {
            showToast('Max 5 files');
            return;
        }
        pendingFiles.push(file);
    });
    renderFilePreviews();
    e.target.value = ''; // reset so same file can be re-added
});

// Edit file input handler
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
    for (const file of files) {
        results.push(await fileToBase64(file));
    }
    return results;
}

async function submitUrl() {
    const input = document.getElementById('url-input');
    const url = input.value.trim();

    if (!url) {
        input.focus();
        return;
    }

    // Gather notes and attachments
    const notes = document.getElementById('notes-input').value.trim();
    const attachments = pendingFiles.length > 0 ? await filesToBase64(pendingFiles) : [];

    // Show status, hide others
    show('status-section');
    hide('result-section');
    document.getElementById('submit-btn').disabled = true;
    setStatus('Submitting...', 'Sending URL to transcription service');

    try {
        // Submit the URL with notes and attachments
        const payload = { url };
        if (notes) payload.notes = notes;
        if (attachments.length > 0) payload.attachments = attachments;

        const res = await fetch(`${API_BASE}/transcribe`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
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
                showResult(data, true);
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

function showResult(data, isNewSubmission = false) {
    currentTranscript = data;

    hide('status-section');
    show('result-section');
    document.getElementById('submit-btn').disabled = false;

    // Hide edit panel if open
    hide('edit-panel');
    show('result-actions');

    // Success banner
    const banner = document.getElementById('success-banner');
    const summary = document.getElementById('success-summary');
    if (isNewSubmission) {
        let parts = ['Transcript'];
        if (data.notes) parts.push('Notes');
        if (data.attachments && data.attachments.length > 0)
            parts.push(`${data.attachments.length} file${data.attachments.length > 1 ? 's' : ''}`);
        summary.textContent = parts.join(' + ');
        banner.classList.remove('hidden');
        // Show confirmation modal
        showConfirmationModal(data, parts.join(' + '));
    } else {
        banner.classList.add('hidden');
    }

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

    // Source URL
    const sourceLink = document.getElementById('result-source-url');
    if (data.url) {
        sourceLink.href = data.url;
        sourceLink.textContent = truncateUrl(data.url, 50);
        sourceLink.classList.remove('hidden');
    } else {
        sourceLink.classList.add('hidden');
    }

    document.getElementById('result-transcript').textContent = data.transcript;

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

async function loadHistory() {
    try {
        const res = await fetch(`${API_BASE}/history`);
        if (!res.ok) return;
        const data = await res.json();

        const list = document.getElementById('history-list');
        const archiveList = document.getElementById('archive-list');

        if (!data.transcripts || data.transcripts.length === 0) {
            list.innerHTML = '<p class="empty-state">No transcripts yet. Paste a URL above to get started.</p>';
            if (archiveList) archiveList.innerHTML = '';
            return;
        }

        // Split into active (unreviewed) and archived (reviewed)
        const active = data.transcripts.filter(t => t.review_status !== 'reviewed');
        const archived = data.transcripts.filter(t => t.review_status === 'reviewed');

        // Render active list grouped by category
        if (active.length === 0) {
            list.innerHTML = '<p class="empty-state">All caught up! No unreviewed transcripts.</p>';
        } else {
            list.innerHTML = renderGroupedHistory(active);
        }

        // Render archive
        if (archiveList) {
            if (archived.length === 0) {
                archiveList.innerHTML = '<p class="empty-state">No archived transcripts yet.</p>';
            } else {
                archiveList.innerHTML = archived.map(t => renderHistoryItem(t, true)).join('');
            }
            // Show/hide archive section
            const archiveSection = document.getElementById('archive-section');
            if (archiveSection) {
                archiveSection.style.display = archived.length > 0 ? 'block' : 'none';
            }
        }

    } catch (err) {
        console.error('Failed to load history:', err);
    }
}

function renderGroupedHistory(transcripts) {
    // Group by primary category (first category, or 'Uncategorized')
    const groups = {};
    transcripts.forEach(t => {
        const cat = (t.categories && t.categories.length > 0) ? t.categories[0] : 'Uncategorized';
        if (!groups[cat]) groups[cat] = [];
        groups[cat].push(t);
    });

    // Sort categories alphabetically, but put Uncategorized last
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

    const sourceUrl = t.url ? `<a class="history-source-link" href="${escapeHtml(t.url)}" target="_blank" rel="noopener" onclick="event.stopPropagation()">${truncateUrl(t.url, 40)}</a>` : '';

    return `
        <div class="history-item ${isArchived ? 'archived' : ''}" onclick='viewTranscript(${JSON.stringify(t).replace(/'/g, "&#39;")})'>
            ${t.thumbnail_url
                ? `<img class="history-thumb" src="${t.thumbnail_url}" alt="">`
                : '<div class="history-thumb"></div>'
            }
            <div class="history-info">
                <h3>${escapeHtml(t.generated_title || t.title || 'Untitled')}</h3>
                <p class="meta">${escapeHtml(t.creator || 'Unknown')} | ${formatDuration(t.duration)} | ${formatDate(t.created_at)}${badges.join('')}</p>
                ${sourceUrl}
                <div class="history-categories">
                    ${(t.categories || []).map(c => `<span class="category-tag">${escapeHtml(c)}</span>`).join('')}
                </div>
            </div>
        </div>
    `;
}

async function viewTranscript(data) {
    // History list doesn't include transcript text — fetch full record
    show('status-section');
    hide('result-section');
    setStatus('Loading...', 'Fetching transcript');

    try {
        const res = await fetch(`${API_BASE}/status?id=${data.id}`);
        const full = await res.json();
        if (full.transcript) {
            showResult(full);
        } else {
            showResult(data);
        }
    } catch (err) {
        showResult(data); // Show what we have if fetch fails
    }

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
    document.getElementById('notes-input').value = '';
    pendingFiles = [];
    renderFilePreviews();
    document.getElementById('url-input').focus();
}

// ── Edit Mode ────────────────────────────────────────────────

function toggleEdit() {
    if (!currentTranscript) return;
    // Pre-fill edit fields
    document.getElementById('edit-notes').value = currentTranscript.notes || '';
    editAttachments = currentTranscript.attachments ? [...currentTranscript.attachments] : [];
    renderEditAttachments();
    // Show edit panel, hide action buttons
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

    const notes = document.getElementById('edit-notes').value.trim();

    try {
        const res = await fetch(`${API_BASE}/review`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                id: currentTranscript.id,
                review_status: currentTranscript.review_status || null,
                notes: notes || null,
                attachments: editAttachments.length > 0 ? editAttachments : null
            })
        });

        if (!res.ok) throw new Error('Failed to save changes');

        // Update local state
        currentTranscript.notes = notes || null;
        currentTranscript.attachments = editAttachments.length > 0 ? editAttachments : null;
        showResult(currentTranscript);
        showToast('Changes saved!');
        loadHistory();

    } catch (err) {
        showToast('Error: ' + err.message);
    } finally {
        saveBtn.disabled = false;
        saveBtn.textContent = 'Save';
    }
}

// Helpers
function show(id) { document.getElementById(id)?.classList.remove('hidden'); }
function hide(id) { document.getElementById(id)?.classList.add('hidden'); }
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

function truncateUrl(url, maxLen) {
    if (!url) return '';
    // Strip protocol for display
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

// ── Confirmation Modal ───────────────────────────────────────

function showConfirmationModal(data, summaryText) {
    const details = document.getElementById('confirm-details');
    const title = data.generated_title || data.title || 'Untitled';
    const creator = data.creator || 'Unknown';
    details.innerHTML = `<strong>${escapeHtml(title)}</strong><br>${escapeHtml(creator)} &mdash; ${summaryText}`;
    document.getElementById('confirm-overlay').classList.remove('hidden');
}

function dismissConfirmation(event) {
    // Only dismiss if clicking the overlay background, not the modal itself
    if (event.target.id === 'confirm-overlay') {
        acknowledgeConfirmation();
    }
}

function acknowledgeConfirmation() {
    document.getElementById('confirm-overlay').classList.add('hidden');
    // Clear the form for a new entry
    document.getElementById('url-input').value = '';
    document.getElementById('notes-input').value = '';
    pendingFiles = [];
    renderFilePreviews();
    document.getElementById('url-input').focus();
}

function showToast(msg) {
    const toast = document.createElement('div');
    toast.className = 'toast';
    toast.textContent = msg;
    document.body.appendChild(toast);
    setTimeout(() => toast.remove(), 2000);
}

// ── TicketDeck Bug Widget ──────────────────────────────────────
const TICKETDECK_URL = 'https://dgnikbbugiuuwokwenlm.supabase.co/rest/v1/tickets';
const TICKETDECK_KEY = 'sb_publishable_L2VH13C5NYtdSBoENpoh9Q_d7iJHDOF';
let bugFiles = [];

// Bug file input handler
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
    // Reset form when opening
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

    const title = document.getElementById('bug-title').value.trim();
    const description = document.getElementById('bug-desc').value.trim();
    const type = document.getElementById('bug-type').value;
    const priority = document.getElementById('bug-priority').value;

    // Convert bug files to base64
    const attachments = bugFiles.length > 0 ? await filesToBase64(bugFiles) : null;

    const ticket = {
        project: 'tikscribe',
        type,
        priority,
        title: title.substring(0, 60),
        description: description || title,
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

        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.message || 'Failed to submit ticket');
        }

        statusEl.textContent = 'Ticket submitted! Thanks for reporting.';
        statusEl.className = 'bug-status-msg success';
        statusEl.classList.remove('hidden');
        document.getElementById('bug-form').reset();
        bugFiles = [];
        renderBugFilePreviews();

        // Auto-close after 2s
        setTimeout(() => {
            toggleBugPanel();
        }, 2000);

    } catch (err) {
        statusEl.textContent = 'Error: ' + err.message;
        statusEl.className = 'bug-status-msg error';
        statusEl.classList.remove('hidden');
    } finally {
        btn.disabled = false;
        btn.textContent = 'Submit Ticket';
    }
}
