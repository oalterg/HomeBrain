/* HomeBrain dashboard.
   Values injected by the template live on window.HB (see dashboard.html). */

const HB = window.HB || { productName: 'HomeBrain' };

let currentLogSource = 'manager';
let rawLogData = '';          // raw text kept for client-side filtering

/* =====================================================================
   Dialogs — toast, confirm, prompt

   These replace window.alert / confirm / prompt, which blocked the page,
   could not be themed, and rendered as OS chrome.
   ===================================================================== */

function hbToast(message, kind) {
    let stack = document.querySelector('.toast-stack');
    if (!stack) {
        stack = document.createElement('div');
        stack.className = 'toast-stack';
        document.body.appendChild(stack);
    }
    const el = document.createElement('div');
    el.className = 'toast' + (kind ? ' toast-' + kind : '');
    el.innerHTML = `<div class="toast-body"></div>
                    <button class="toast-close" aria-label="Dismiss">&times;</button>`;
    el.querySelector('.toast-body').textContent = message;

    const dismiss = () => {
        if (!el.isConnected) return;
        el.classList.add('is-leaving');
        setTimeout(() => el.remove(), 180);
    };
    el.querySelector('.toast-close').onclick = dismiss;
    stack.appendChild(el);
    setTimeout(dismiss, kind === 'error' ? 9000 : 5000);
}

/* Shared modal shell. `build(body)` fills the content and returns a
   function producing the resolve value; null means "not valid yet". */
function hbModal({ title, danger, wide, confirmLabel, cancelLabel, build, focus }) {
    return new Promise(resolve => {
        const backdrop = document.createElement('div');
        backdrop.className = 'modal-backdrop';
        backdrop.innerHTML = `
            <div class="modal${danger ? ' modal-danger' : ''}${wide ? ' modal-wide' : ''}"
                 role="dialog" aria-modal="true">
                <h3></h3>
                <div class="modal-body"></div>
                <div class="modal-error"></div>
                <div class="modal-actions">
                    <button data-act="cancel"></button>
                    <button data-act="ok" class="${danger ? 'btn-danger' : 'btn-primary'}"></button>
                </div>
            </div>`;
        backdrop.querySelector('h3').textContent = title;
        backdrop.querySelector('[data-act="cancel"]').textContent = cancelLabel || 'Cancel';
        backdrop.querySelector('[data-act="ok"]').textContent = confirmLabel || 'Confirm';

        const body = backdrop.querySelector('.modal-body');
        const errEl = backdrop.querySelector('.modal-error');
        const getValue = build(body, () => submit());

        const close = value => {
            document.removeEventListener('keydown', onKey);
            backdrop.remove();
            resolve(value);
        };
        const submit = () => {
            const v = getValue(errEl);
            if (v !== null && v !== undefined) close(v);
        };
        const onKey = e => {
            if (e.key === 'Escape') close(null);
            if (e.key === 'Enter' && e.target.tagName !== 'TEXTAREA') { e.preventDefault(); submit(); }
        };

        backdrop.querySelector('[data-act="ok"]').onclick = submit;
        backdrop.querySelector('[data-act="cancel"]').onclick = () => close(null);
        backdrop.onclick = e => { if (e.target === backdrop) close(null); };
        document.addEventListener('keydown', onKey);
        document.body.appendChild(backdrop);

        const first = focus ? backdrop.querySelector(focus) : backdrop.querySelector('[data-act="ok"]');
        if (first) first.focus();
    });
}

/* Ask before doing something. `requireText` demands the user type an
   exact word — that used to be a second, separate prompt() dialog. */
async function hbConfirm({ title, body, detail, confirm, danger, requireText }) {
    const value = await hbModal({
        title,
        danger,
        confirmLabel: confirm || (danger ? 'Delete' : 'Continue'),
        focus: requireText ? 'input' : null,
        build: (root, submit) => {
            if (body) {
                const p = document.createElement('p');
                p.textContent = body;
                root.appendChild(p);
            }
            if (detail) {
                const p = document.createElement('p');
                p.className = 'modal-detail';
                p.textContent = detail;
                root.appendChild(p);
            }
            if (!requireText) return () => true;

            const label = document.createElement('label');
            label.innerHTML = `Type <code></code> to confirm`;
            label.querySelector('code').textContent = requireText;
            const input = document.createElement('input');
            input.type = 'text';
            input.autocomplete = 'off';
            input.placeholder = requireText;
            input.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); submit(); } });
            root.append(label, input);

            return errEl => {
                if (input.value.trim() === requireText) return true;
                errEl.textContent = `Type ${requireText} exactly to continue.`;
                input.focus();
                return null;
            };
        },
    });
    return value === true;
}

/* Ask for a value. Resolves to the string, or null if cancelled. */
function hbPrompt({ title, body, label, value, type, confirm, allowEmpty }) {
    return hbModal({
        title,
        confirmLabel: confirm || 'Save',
        focus: 'input',
        build: (root, submit) => {
            if (body) {
                const p = document.createElement('p');
                p.textContent = body;
                root.appendChild(p);
            }
            if (label) {
                const l = document.createElement('label');
                l.textContent = label;
                root.appendChild(l);
            }
            const input = document.createElement('input');
            input.type = type || 'text';
            input.value = value || '';
            input.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); submit(); } });
            root.appendChild(input);

            return errEl => {
                const v = input.value.trim();
                if (!v && !allowEmpty) {
                    errEl.textContent = 'This field is required.';
                    input.focus();
                    return null;
                }
                return v;
            };
        },
    });
}

/* =====================================================================
   Status helpers
   ===================================================================== */

const STATUS_LABELS = {
    running: 'Running', healthy: 'Healthy', active: 'Active', connected: 'Connected',
    enabled: 'Enabled', unlocked: 'Unlocked', installed: 'Installed', present: 'Present',
    stopped: 'Stopped', offline: 'Offline', disabled: 'Disabled', missing: 'Missing',
    starting: 'Starting', unconfigured: 'Not configured', unknown: 'Unknown',
    not_installed: 'Not installed', locked: 'Locked',
};

const GOOD = ['running', 'healthy', 'active', 'connected', 'enabled', 'unlocked', 'installed', 'present', 'created'];
const BAD = ['stopped', 'offline', 'disabled', 'missing', 'error'];
const PENDING = ['starting', 'unconfigured', 'pending'];

function statusClass(raw) {
    if (GOOD.includes(raw)) return 'running';
    if (BAD.includes(raw)) return 'stopped';
    if (PENDING.includes(raw)) return 'starting';
    return 'unknown';
}

function humanise(raw) {
    if (!raw) return 'Unknown';
    return STATUS_LABELS[raw] || (raw.charAt(0).toUpperCase() + raw.slice(1).replace(/_/g, ' '));
}

/* The single primitive every badge goes through. Also clears the
   skeleton classes, which used to be repeated at seven call sites. */
function applyStatus(el, cls, label) {
    if (!el) return;
    el.className = 'status-badge status-' + cls;
    el.textContent = label;
}

function setStatus(el, raw, label) {
    applyStatus(el, statusClass(raw), label || humanise(raw));
}

function fillText(id, text) {
    const el = document.getElementById(id);
    if (!el) return;
    el.classList.remove('skeleton', 'sk-mid', 'sk-wide', 'sk-small', 'sk-block');
    el.textContent = text;
}

/* Meter: reading above the bar, fill turns red past the threshold. */
function setMeter(barId, textId, percent, text) {
    const bar = document.getElementById(barId);
    const txt = document.getElementById(textId);
    if (bar) {
        bar.style.width = percent + '%';
        bar.classList.toggle('is-high', percent > 85);
    }
    if (txt) txt.textContent = text;
}

function escapeHtml(text) {
    return text
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#039;');
}

/* True when `host` looks like a LAN-only hostname the box should be
   directly reachable on (loopback, RFC1918, mDNS .local). */
function isLanHostname(host) {
    if (!host) return true;
    host = host.toLowerCase();
    if (host === 'localhost' || host === '127.0.0.1' || host === '[::1]') return true;
    if (host.endsWith('.local')) return true;
    if (/^10\./.test(host)) return true;
    if (/^192\.168\./.test(host)) return true;
    if (/^172\.(1[6-9]|2[0-9]|3[01])\./.test(host)) return true;
    return false;
}

// --- Global error capture (client-side logging) ---
window.onerror = function (msg, url, line) {
    fetch('/api/logs/client', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ level: 'ERROR', message: `JS Error: ${msg} (${url}:${line})` }),
    }).catch(e => console.warn('Failed to ship log', e));
};

/* =====================================================================
   Boot & tabs
   ===================================================================== */

async function init() {
    const hourSel = document.getElementById('bk-hour');
    if (hourSel) {
        for (let i = 0; i < 24; i++) {
            const opt = document.createElement('option');
            opt.value = i;
            opt.innerText = i.toString().padStart(2, '0') + ':00';
            hourSel.appendChild(opt);
        }
    }

    document.querySelectorAll('.tab-btn[data-tab]').forEach(btn => {
        btn.addEventListener('click', () => openTab(btn.dataset.tab));
    });

    try {
        // Parallel fetch for speed. loadSystemConfig drives the AI status
        // badges in the Status tab, so fetching it at init means those rows
        // populate immediately instead of staying skeleton until someone
        // opens the Settings tab.
        await Promise.all([fetchStatus(), loadSystemConfig(), pollTask(), fetchVaultStatus(),
            vaultDocsRefresh(), vaultMcpRefresh(), connRefresh(), channelRefresh(), loadRecoveryStatus()]);
    } catch (err) {
        console.error('Initial fetch failed:', err);
    }

    fetchHealth();
    startPolling();
}

/* One scheduler instead of nine independent setIntervals, and it stops
   while the tab is in the background — the box was being polled every
   two seconds by every open dashboard whether or not anyone was looking. */
const POLLERS = [
    [fetchHealth, 300000],       // health.json only refreshes every 30 min
    [fetchStatus, 5000],
    [loadSystemConfig, 10000],   // AI state doesn't churn
    [pollTask, 2000],
    [fetchVaultStatus, 10000],
    [vaultDocsRefresh, 30000],
    [vaultMcpRefresh, 30000],
    [connRefresh, 15000],
    [channelRefresh, 15000],
    [pollLogsIfVisible, 3000],
];
let pollTimers = [];

function startPolling() {
    stopPolling();
    pollTimers = POLLERS.map(([fn, ms]) => setInterval(fn, ms));
}
function stopPolling() {
    pollTimers.forEach(clearInterval);
    pollTimers = [];
}
document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
        stopPolling();
    } else {
        startPolling();
        fetchStatus();
        pollTask();
    }
});

function pollLogsIfVisible() {
    const logsTab = document.getElementById('logs');
    if (logsTab && logsTab.classList.contains('active')) pollLogs();
}

function openTab(id) {
    document.querySelectorAll('.tab-content').forEach(d => d.classList.remove('active'));
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.toggle('active', b.dataset.tab === id));
    const panel = document.getElementById(id);
    if (panel) panel.classList.add('active');

    if (id === 'backup') {
        loadDrives();
        loadBackups();
        loadBackupConfig();
        loadOffsiteConfig();
        loadReplicaStatus();
        loadDiskStats();
        loadOpenClawBackupStatus();
    }
    if (id === 'logs') pollLogs();
    if (id === 'connectivity') {
        loadFtpUsers();
        populateFtpNcUserSelect();
        loadNetworkStatus();
    }
    if (id === 'settings') {
        loadSystemConfig();
        loadSerialDevices();
        connRefresh();
        vaultMcpRefresh();
    }
}

/* Jump to a card in another tab and pulse it. */
function goTo(tab, cardId) {
    openTab(tab);
    const card = document.getElementById(cardId);
    if (!card) return;
    card.scrollIntoView({ behavior: 'smooth', block: 'center' });
    card.classList.remove('flash-highlight');
    void card.offsetWidth;                 // restart the animation if re-clicked
    card.classList.add('flash-highlight');
    setTimeout(() => card.classList.remove('flash-highlight'), 2200);
    return false;
}

/* =====================================================================
   Status tab
   ===================================================================== */

async function fetchHealth() {
    try {
        const res = await fetch('/api/health', { credentials: 'include' });
        if (!res.ok) return;
        const h = await res.json();
        const el = document.getElementById('health-banner');
        if (!el) return;
        const issues = (h.checks || []).filter(c => c.level !== 'ok');
        if (!issues.length || h.overall === 'ok' || h.overall === 'unknown') {
            el.style.display = 'none';
            return;
        }
        el.classList.remove('notice-warning', 'notice-danger');
        if (h.overall === 'crit') el.classList.add('notice-danger');
        else if (h.overall === 'warn') el.classList.add('notice-warning');
        const heading = h.overall === 'info' ? 'Good to know' : 'Needs attention';
        el.innerHTML = `<strong>${heading}</strong><ul>` +
            issues.map(c => `<li>${escapeHtml(c.summary)}</li>`).join('') + '</ul>';
        el.style.display = 'block';
    } catch (e) { /* non-fatal — banner just stays hidden */ }
}

async function fetchStatus() {
    try {
        const res = await fetch('/api/status', { credentials: 'include' });
        if (!res.ok) {
            if (res.status === 401 && !document.hidden) location.reload();
            return;
        }
        const data = await res.json();

        const map = { nc: 'nextcloud', ha: 'homeassistant', db: 'db', tunnel: 'tunnel', vault: 'vaultwarden' };
        Object.keys(map).forEach(k => setStatus(document.getElementById('st-' + k), data[map[k]]));

        const maint = document.getElementById('st-maint');
        if (maint) setStatus(maint, data.maintenance_mode === 'on' ? 'enabled' : 'disabled',
            data.maintenance_mode === 'on' ? 'On' : 'Off');

        if (data.cpu_load !== undefined) fillText('sys-cpu', data.cpu_load + '%');
        if (data.cpu_temp !== undefined) fillText('sys-cpu-temp', data.cpu_temp + '°C');

        if (data.ram_percent !== undefined) {
            setMeter('ram-bar', 'ram-text', data.ram_percent, `${data.ram_text} · ${data.ram_percent}%`);
        }
        if (data.root_percent !== undefined) {
            setMeter('root-bar', 'root-text', data.root_percent,
                `${data.root_percent}% used · ${data.root_free_gb} GB free`);
        }
        if (data.gpu && data.gpu.available) {
            fillText('gpu-util', data.gpu.util_percent + '%');
            if (data.gpu.temp_c !== undefined) fillText('gpu-temp', data.gpu.temp_c + '°C');
            setMeter('gpu-vram-bar', 'gpu-vram-text', data.gpu.vram_percent,
                `${data.gpu.vram_used_gb} / ${data.gpu.vram_total_gb} GB`);
        }
    } catch (e) { /* transient — next poll retries */ }
}

async function pollTask() {
    try {
        const res = await fetch('/api/task_status', { credentials: 'include' });
        if (!res.ok) return;
        const data = await res.json();
        const banner = document.getElementById('global-status');
        if (!banner) return;

        if (data.status === 'idle') {
            banner.innerText = 'System Active';
            banner.removeAttribute('data-state');
        } else if (data.status === 'running') {
            banner.innerText = data.message;
            banner.dataset.state = 'busy';
        } else if (data.status === 'error') {
            banner.innerText = 'Error: ' + data.message;
            banner.dataset.state = 'error';
        } else {
            banner.innerText = data.message;
            banner.removeAttribute('data-state');
        }
    } catch (e) { /* transient */ }
}

/* =====================================================================
   Agent integrations
   ===================================================================== */

function openDetails(id, focusId) {
    const el = document.getElementById(id);
    if (!el) return;
    if (el.tagName === 'DETAILS') el.open = true;
    else el.hidden = false;
    el.scrollIntoView({ behavior: 'smooth', block: 'center' });
    if (focusId) {
        setTimeout(() => {
            const f = document.getElementById(focusId);
            if (f) f.focus();
        }, 350);
    }
}

function closeForm(id) {
    const el = document.getElementById(id);
    if (!el) return;
    if (el.tagName === 'DETAILS') el.open = false;
    else el.hidden = true;
    const hint = document.getElementById('ha-local-hint');
    if (id === 'details-ha' && hint) hint.style.display = 'none';
}

// ── Channel linking ─────────────────────────────────────────────
const CHANNEL_LABELS = {
    telegram: { name: 'Telegram', sub: 'Bot via @BotFather' },
};

async function channelRefresh() {
    try {
        const r = await fetch('/api/channels/status', { credentials: 'include' });
        if (!r.ok) return;
        const d = await r.json();
        const rows = document.getElementById('channel-rows');
        if (!rows) return;
        rows.innerHTML = '';
        for (const ch of (d.channels || [])) {
            const meta = CHANNEL_LABELS[ch.key] || { name: ch.key, sub: '' };
            let badge;
            if (ch.enabled) badge = '<span class="status-badge status-running">Active</span>';
            else if (ch.configured) badge = '<span class="status-badge status-unknown">Configured</span>';
            else badge = '<span class="status-badge status-stopped">Not linked</span>';

            const buttons = [];
            if (ch.key === 'telegram') {
                if (!ch.configured) {
                    buttons.push(`<button class="btn-primary" onclick="openDetails('details-telegram','tg-token')">Link&hellip;</button>`);
                } else {
                    buttons.push(`<button class="btn-primary" onclick="openDetails('details-telegram-pair','tg-pair-code')">Pair&hellip;</button>`);
                    buttons.push(`<button onclick="channelRemove('telegram')">Unlink</button>`);
                }
            }
            rows.insertAdjacentHTML('beforeend',
                `<div class="row-item">
                   <div class="row-main">
                     <strong>${meta.name}</strong>
                     <span class="row-meta">${meta.sub}</span>
                   </div>
                   <div class="row-actions">${badge}${buttons.join('')}</div>
                 </div>`);
        }
    } catch (e) { console.warn('channelRefresh failed', e); }
}

async function channelTelegramAdd() {
    const token = document.getElementById('tg-token').value.trim();
    if (!token) return;
    const msg = document.getElementById('channel-msg');
    msg.innerText = 'Validating token...';
    try {
        const r = await fetch('/api/channels/telegram/add', {
            method: 'POST', credentials: 'include',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ token }),
        });
        const d = await r.json();
        if (r.ok) {
            msg.innerText = `Linked @${d.bot_username || 'bot'}. Restarting agent...`;
            closeForm('details-telegram');
            document.getElementById('tg-token').value = '';
        } else {
            msg.innerText = d.error || 'Failed';
        }
    } catch (e) { msg.innerText = 'Error: ' + e; }
    channelRefresh();
}

async function channelTelegramPair() {
    const code = document.getElementById('tg-pair-code').value.trim();
    if (!code) return;
    const msg = document.getElementById('channel-msg');
    msg.innerText = 'Approving pairing...';
    try {
        const r = await fetch('/api/channels/telegram/pair', {
            method: 'POST', credentials: 'include',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ code }),
        });
        const d = await r.json();
        if (r.ok) {
            msg.innerText = 'Paired! You can now chat with your agent on Telegram.';
            closeForm('details-telegram-pair');
            document.getElementById('tg-pair-code').value = '';
        } else {
            msg.innerText = d.error || 'Pairing failed';
        }
    } catch (e) { msg.innerText = 'Error: ' + e; }
    channelRefresh();
}

async function channelRemove(key) {
    const name = (CHANNEL_LABELS[key] || {}).name || key;
    if (!await hbConfirm({
        title: `Unlink ${name}?`,
        body: 'The agent will stop responding on this channel until you link it again.',
        confirm: 'Unlink', danger: true,
    })) return;
    const msg = document.getElementById('channel-msg');
    msg.innerText = 'Removing...';
    try {
        await fetch(`/api/channels/${key}/remove`, { method: 'POST', credentials: 'include' });
        msg.innerText = 'Removed. Restarting agent...';
    } catch (e) { msg.innerText = 'Error: ' + e; }
    channelRefresh();
}

// ── Integrations ─────────────────────────────────────────────────
const CONN_LABELS = {
    self:          { name: 'Self-tools',     sub: 'Backups, status, restart' },
    homeassistant: { name: 'Home Assistant', sub: 'Lights, scenes, automations' },
    nextcloud:     { name: 'Nextcloud',      sub: 'Files, notes, calendar' },
    vault:         { name: 'Vault',          sub: 'Passwords (read + create)' },
    email:         { name: 'Email',          sub: 'IMAP/SMTP — Proton Bridge or direct' },
};

async function connRefresh() {
    try {
        const r = await fetch('/api/integrations/status', { credentials: 'include' });
        if (!r.ok) return;
        const d = await r.json();
        const items = (d.integrations || []).filter(x => x.key !== 'self');
        const selfItem = (d.integrations || []).find(x => x.key === 'self');

        const summary = document.getElementById('sys-integrations-status');
        if (summary) {
            const wired = items.filter(x => x.wired).length;
            const total = items.length;
            const missing = items.filter(x => !x.wired).map(x => (CONN_LABELS[x.key] || {}).name || x.key);
            applyStatus(summary, wired === total && total > 0 ? 'running' : 'unknown',
                `${wired} of ${total} connected`);
            summary.title = missing.length ? `Not connected: ${missing.join(', ')}` : 'All integrations connected';
        }

        const note = document.getElementById('conn-self-note');
        if (note && selfItem) {
            note.innerText = selfItem.wired
                ? 'Your agent can also call HomeBrain itself (backups, status, restart).'
                : '';
        }

        const rows = document.getElementById('conn-rows');
        if (!rows) return;
        rows.innerHTML = '';
        for (const it of items) {
            const meta = CONN_LABELS[it.key] || { name: it.key, sub: '' };
            const badge = it.wired
                ? '<span class="status-badge status-running" title="Agent can use this">Connected</span>'
                : (it.configured
                    ? '<span class="status-badge status-unknown" title="Saved but not yet picked up by the agent — Re-sync under Troubleshoot">Pending sync</span>'
                    : '<span class="status-badge status-stopped" title="Not set up yet">Not connected</span>');

            let extras = '';
            // Multi-account integrations surface a per-account list under the
            // row title with a small remove button.
            if (it.key === 'email' && (it.accounts || []).length) {
                extras = renderAccountList(it.accounts.map(a => ({ name: a.name, sub: a.user })), 'email');
            }
            if (it.key === 'homeassistant' && (it.accounts || []).length) {
                extras = renderAccountList(it.accounts.map(a => ({ name: a.name, sub: a.base_url })), 'homeassistant');
            }
            if (it.key === 'nextcloud' && (it.accounts || []).length) {
                extras = renderAccountList(it.accounts.map(a => ({
                    name: a.name, sub: `${a.user || ''} @ ${a.base_url || ''}` })), 'nextcloud');
            }
            if (it.key === 'vault' && it.unlocked) {
                extras = '<span class="hint" style="color:var(--accent);">Unlocked</span>';
            }

            const buttons = [];
            if (it.key === 'nextcloud') {
                // Match against is_local, not name — accounts created via the
                // picker are named after the username, not the legacy 'homebrain'.
                const haveAnyLocal = (it.accounts || []).some(a => a.is_local);
                const localLabel = haveAnyLocal ? 'Add another HomeBrain user…' : 'Add HomeBrain user…';
                buttons.push(`<button class="${haveAnyLocal ? '' : 'btn-primary'}" onclick="connNcAddLocal()" title="Mint an app password against this HomeBrain&rsquo;s Nextcloud for any local user">${localLabel}</button>`);
                buttons.push(`<button onclick="openDetails('details-nc','nc-name')">Add external NC…</button>`);
            }
            if (it.key === 'homeassistant') {
                const hasLocal = (it.accounts || []).some(a => a.name === 'home');
                if (!hasLocal) {
                    buttons.push(`<button onclick="connHaAddLocal()" title="Pre-fill with the HomeBrain-shipped Home Assistant; you only paste the token">Add HomeBrain HA</button>`);
                }
                const cta = (it.accounts || []).length > 0 ? '' : 'btn-primary';
                buttons.push(`<button class="${cta}" onclick="openDetails('details-ha','ha-name')">Add external HA…</button>`);
            }
            if (it.key === 'email') {
                const cta = (it.accounts || []).length > 0 ? '' : 'btn-primary';
                buttons.push(`<button class="${cta}" onclick="openDetails('details-email','em-name')">Add account…</button>`);
            }
            if (it.key === 'vault') {
                if (!it.wired) buttons.push(`<button class="btn-primary" onclick="openDetails('vault-mcp')">Configure…</button>`);
                else if (!it.unlocked) buttons.push(`<button class="btn-primary" onclick="openDetails('vault-mcp','vault-mcp-pw')">Unlock…</button>`);
                else buttons.push(`<button onclick="vaultMcpLock()">Lock</button>`);
            }
            if (it.configured) buttons.push(`<button onclick="connTest('${it.key}')">Test</button>`);

            rows.insertAdjacentHTML('beforeend',
                `<div class="row-item" title="MCP server: ${it.mcp_name}">
                   <div class="row-main">
                     <strong>${meta.name}</strong>
                     <span class="row-meta">${meta.sub}</span>
                     ${extras}
                   </div>
                   <div class="row-actions">${badge}${buttons.join('')}</div>
                 </div>`);
        }

        const sd = (d.integrations || []).find(x => x.key === 'email');
        const cb = document.getElementById('em-send-direct');
        if (cb && sd) cb.checked = !!sd.send_direct_enabled;
    } catch (e) { console.warn('connRefresh failed', e); }
}

async function connReconcile() {
    const msg = document.getElementById('conn-msg');
    msg.innerText = 'Applying...';
    try {
        const r = await fetch('/api/integrations/reconcile', { method: 'POST', credentials: 'include' });
        const d = await r.json();
        msg.innerText = r.ok ? 'Applied. Agent restarted.' : ('Failed: ' + (d.error || ''));
    } catch (e) { msg.innerText = 'Apply failed: ' + e; }
    connRefresh();
}

/* Inline per-account list under an integration row. `kind` selects the
   remove endpoint (email | homeassistant | nextcloud). */
function renderAccountList(accounts, kind) {
    if (!accounts || !accounts.length) return '';
    const items = accounts.map(a => {
        const sub = a.sub ? ` <span class="faint">— ${a.sub}</span>` : '';
        return `<span class="acct-chip">
                  <strong>${a.name}</strong>${sub}
                  <button onclick="connAccountRemove('${kind}','${a.name.replace(/'/g, "\\'")}')"
                          title="Remove account" aria-label="Remove account">&times;</button>
                </span>`;
    }).join('');
    return `<div class="acct-list">${items}</div>`;
}

async function connAccountRemove(kind, name) {
    if (!await hbConfirm({
        title: 'Remove account?',
        body: `The agent will lose access to the ${kind} account "${name}".`,
        confirm: 'Remove', danger: true,
    })) return;
    const path = kind === 'email' ? 'email/remove'
        : kind === 'homeassistant' ? 'homeassistant/remove'
        : 'nextcloud/remove';
    const r = await fetch(`/api/integrations/${path}`, {
        method: 'POST', credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name }),
    });
    const d = await r.json().catch(() => ({}));
    document.getElementById('conn-msg').innerText = r.ok ? `Removed ${kind} account "${name}".` : (d.error || 'Remove failed');
    connRefresh();
}

async function connHaAdd() {
    const body = {
        name: document.getElementById('ha-name').value.trim(),
        base_url: document.getElementById('ha-base-url').value.trim(),
        token: document.getElementById('ha-token-input').value.trim(),
    };
    if (!body.name || !body.base_url || !body.token) {
        hbToast('Label, base URL, and token are all required.', 'error');
        return;
    }
    const r = await fetch('/api/integrations/homeassistant/add', {
        method: 'POST', credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
    });
    const d = await r.json();
    document.getElementById('conn-msg').innerText = r.ok ? `Home Assistant account "${body.name}" added.` : (d.error || 'Add failed');
    if (r.ok) {
        ['ha-name', 'ha-base-url', 'ha-token-input'].forEach(id => document.getElementById(id).value = '');
        closeForm('details-ha');
    }
    connRefresh();
}

async function connNcAdd() {
    const body = {
        name: document.getElementById('nc-name').value.trim(),
        base_url: document.getElementById('nc-base-url').value.trim(),
        user: document.getElementById('nc-user').value.trim(),
        token: document.getElementById('nc-token').value,
    };
    if (!body.name || !body.base_url || !body.user || !body.token) {
        hbToast('Label, base URL, user, and app password are all required.', 'error');
        return;
    }
    const r = await fetch('/api/integrations/nextcloud/add', {
        method: 'POST', credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
    });
    const d = await r.json();
    document.getElementById('conn-msg').innerText = r.ok ? `Nextcloud account "${body.name}" added.` : (d.error || 'Add failed');
    if (r.ok) {
        ['nc-name', 'nc-base-url', 'nc-user', 'nc-token'].forEach(id => document.getElementById(id).value = '');
        closeForm('details-nc');
    }
    connRefresh();
}

function connHaAddLocal() {
    // No backend equivalent of NC's occ user:add-app-password — HA tokens are
    // user-scoped and require the HA UI. Best we can do is pre-fill the form
    // so the user only pastes the token.
    document.getElementById('ha-name').value = 'home';
    document.getElementById('ha-base-url').value = 'http://127.0.0.1:8123';
    const hint = document.getElementById('ha-local-hint');
    if (hint) {
        const tokenUrl = `${window.location.protocol}//${window.location.hostname}:8123/profile/security`;
        hint.innerHTML = `Open <a href="${tokenUrl}" target="_blank" rel="noopener">Home Assistant → Profile → Security</a>, create a long-lived access token, and paste it below.`;
        hint.style.display = 'block';
    }
    openDetails('details-ha', 'ha-token-input');
}

// Cache the local-users payload between renders so we can decide
// password-field auto-fill without round-tripping on each pick.
let _ncLocal = { admin_user: '', admin_password_stored: false, users: [] };

async function connNcAddLocal() {
    const msg = document.getElementById('conn-msg');
    const sel = document.getElementById('nc-local-user');
    sel.innerHTML = '<option>Loading…</option>';
    openDetails('details-nc-local');
    try {
        const r = await fetch('/api/integrations/nextcloud/local_users', { credentials: 'include' });
        const d = await r.json();
        if (!r.ok) {
            msg.innerText = d.error || 'Could not list local users';
            closeForm('details-nc-local');
            return;
        }
        _ncLocal = d;
        const remaining = (d.users || []).filter(u => !u.configured);
        if (!remaining.length) {
            msg.innerText = 'All local Nextcloud users are already added.';
            closeForm('details-nc-local');
            connRefresh();
            return;
        }
        sel.innerHTML = remaining.map(u => {
            const label = u.displayname && u.displayname !== u.id ? `${u.displayname} (${u.id})` : u.id;
            return `<option value="${u.id}">${label}</option>`;
        }).join('');
        connNcLocalUserPicked();
        document.getElementById('nc-local-pass').focus();
    } catch (e) {
        msg.innerText = 'Could not list local users: ' + e;
        closeForm('details-nc-local');
    }
}

function connNcLocalUserPicked() {
    // Selecting the admin user collapses to one click: we already have
    // admin's password in .env, so hide the password field and show a small
    // reassurance hint. Any other user gets the password input back.
    const sel = document.getElementById('nc-local-user');
    const wrap = document.getElementById('nc-local-pass-wrap');
    const pass = document.getElementById('nc-local-pass');
    const hint = document.getElementById('nc-local-hint');
    if (sel.value === _ncLocal.admin_user && _ncLocal.admin_password_stored) {
        wrap.style.display = 'none';
        pass.value = '';
        hint.innerText = 'Using stored admin password.';
    } else {
        wrap.style.display = '';
        hint.innerText = '';
    }
}

async function connNcAddLocalSubmit() {
    const sel = document.getElementById('nc-local-user');
    const pass = document.getElementById('nc-local-pass');
    const user = sel.value;
    if (!user) return;
    const isAdmin = user === _ncLocal.admin_user;
    const body = { user };
    if (!(isAdmin && _ncLocal.admin_password_stored)) {
        if (!pass.value) { pass.focus(); return; }
        body.password = pass.value;
    }
    const r = await fetch('/api/integrations/nextcloud/add_local', {
        method: 'POST', credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
    });
    const d = await r.json();
    document.getElementById('conn-msg').innerText = r.ok ? `HomeBrain user "${user}" added.` : (d.error || 'Add failed');
    if (r.ok) {
        pass.value = '';
        closeForm('details-nc-local');
    }
    connRefresh();
}

async function connTest(key) {
    const m = document.getElementById('conn-msg');
    m.innerText = `Testing ${key}...`;
    try {
        const r = await fetch(`/api/integrations/${key}/test`, { method: 'POST', credentials: 'include' });
        const d = await r.json();
        m.innerText = d.ok ? `${key}: ${d.tool_count} tools — ${(d.tools || []).join(', ')}`
                           : `${key}: ${d.error || 'failed'}`;
    } catch (e) { m.innerText = `${key}: ${e}`; }
}

async function connEmailAdd() {
    const body = {
        name: document.getElementById('em-name').value.trim(),
        user: document.getElementById('em-user').value.trim(),
        imap_host: document.getElementById('em-imap-host').value.trim(),
        imap_port: parseInt(document.getElementById('em-imap-port').value || '993', 10),
        smtp_host: document.getElementById('em-smtp-host').value.trim(),
        smtp_port: parseInt(document.getElementById('em-smtp-port').value || '587', 10),
        password: document.getElementById('em-pass').value,
    };
    const r = await fetch('/api/integrations/email/add', {
        method: 'POST', credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
    });
    const d = await r.json();
    document.getElementById('conn-msg').innerText = r.ok ? 'Email account added.' : (d.error || 'Add failed');
    if (r.ok) {
        ['em-name', 'em-user', 'em-imap-host', 'em-imap-port', 'em-smtp-host', 'em-smtp-port', 'em-pass']
            .forEach(id => document.getElementById(id).value = '');
        closeForm('details-email');
    }
    connRefresh();
}

async function connEmailSendDirect(enabled) {
    await fetch('/api/integrations/email/send-direct-toggle', {
        method: 'POST', credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled }),
    });
    connRefresh();
}

/* =====================================================================
   Vault
   ===================================================================== */

async function fetchVaultStatus() {
    try {
        const r = await fetch('/api/vault/status', { credentials: 'include' });
        if (!r.ok) return;
        const v = await r.json();
        const stateEl = document.getElementById('vault-state');

        if (!v.enabled) {
            setStatus(stateEl, 'disabled');
            return;
        }
        setStatus(stateEl, v.container || 'stopped');
        fillText('vault-url', v.public_url || '(not set)');
        fillText('vault-users', v.users === null ? '—' : String(v.users));

        const signupsEl = document.getElementById('vault-signups');
        if (signupsEl) {
            applyStatus(signupsEl, v.signups_allowed ? 'starting' : 'running',
                v.signups_allowed ? 'Open' : 'Closed');
        }

        const openLink = document.getElementById('vault-open');
        const launchLink = document.getElementById('vault-launch');
        if (v.public_url) {
            if (openLink) openLink.href = v.public_url;
            if (launchLink) { launchLink.href = v.public_url; launchLink.style.display = ''; }
        }

        // First-run prompt: container running, no users yet, signups still open.
        const showBootstrap = v.signups_allowed && (v.users === 0 || v.users === null) &&
            v.container && v.container !== 'stopped';
        const bootstrap = document.getElementById('vault-bootstrap');
        if (bootstrap) bootstrap.style.display = showBootstrap ? '' : 'none';
        const cfgLink = document.getElementById('vault-configure-link');
        if (cfgLink) {
            cfgLink.innerHTML = (showBootstrap ? 'Set up' : 'Configure') + '<span aria-hidden="true">→</span>';
            cfgLink.style.outline = showBootstrap ? '2px solid var(--accent)' : '';
        }
    } catch (e) { /* silent */ }
}

async function vaultBootstrap() {
    const emailEl = document.getElementById('vault-bootstrap-email');
    const msg = document.getElementById('vault-bootstrap-msg');
    const email = (emailEl && emailEl.value || '').trim();
    if (!email) { if (msg) msg.innerText = 'Enter an email.'; return; }
    if (msg) msg.innerText = 'Inviting…';
    try {
        const r = await fetch('/api/vault/bootstrap', {
            method: 'POST', credentials: 'include',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ email }),
        });
        const data = await r.json();
        if (!r.ok) {
            if (msg) msg.innerText = 'Error: ' + (data.error || r.statusText);
            return;
        }
        if (msg) {
            msg.innerText = data.already_bootstrapped
                ? `Already bootstrapped (${data.users} user${data.users === 1 ? '' : 's'}).`
                : 'User invited. Open the vault URL above to set the master password.';
        }
        fetchVaultStatus();
    } catch (e) {
        if (msg) msg.innerText = 'Network error: ' + e.message;
    }
}

async function vaultMcpRefresh() {
    const cli = document.getElementById('vault-mcp-cli');
    if (!cli) return;
    const state = document.getElementById('vault-mcp-state');
    const wired = document.getElementById('vault-mcp-wired');
    const unlockRow = document.getElementById('vault-mcp-unlock-row');
    const lockRow = document.getElementById('vault-mcp-lock-row');
    const wireRow = document.getElementById('vault-mcp-wire-row');
    const wireBtn = document.getElementById('vault-mcp-wire-btn');
    const emailEl = document.getElementById('vault-mcp-email');
    try {
        const r = await fetch('/api/vault/mcp/status', { credentials: 'include' });
        if (!r.ok) return;
        const d = await r.json();
        setStatus(cli, d.bw_installed ? 'installed' : 'missing');
        setStatus(state, d.unlocked ? 'unlocked' : 'locked');
        if (!d.openclaw_available) applyStatus(wired, 'unknown', 'Agent missing');
        else applyStatus(wired, d.openclaw_wired ? 'running' : 'unknown', d.openclaw_wired ? 'Yes' : 'No');

        // Only surface the bw-cli line when it would matter (missing).
        const cliWrap = document.getElementById('vault-mcp-cli-wrap');
        if (cliWrap) cliWrap.style.display = d.bw_installed ? 'none' : 'inline';
        if (unlockRow) unlockRow.style.display = (!d.bw_installed || d.unlocked) ? 'none' : 'flex';
        if (lockRow) lockRow.style.display = d.unlocked ? 'flex' : 'none';
        if (wireRow) wireRow.style.display = d.openclaw_available && !d.openclaw_wired ? 'flex' : 'none';
        if (wireBtn) wireBtn.innerText = d.openclaw_wired ? 'Disable for agent' : 'Enable for agent';

        // Surface the email field only on first unlock (bw needs `login`).
        if (emailEl) {
            const showEmail = d.needs_login && !d.unlocked;
            emailEl.style.display = showEmail ? '' : 'none';
            if (showEmail && !emailEl.value && d.known_email) emailEl.value = d.known_email;
        }
    } catch (e) { /* silent */ }
}

async function vaultMcpWireToggle() {
    const msg = document.getElementById('vault-mcp-msg');
    const btn = document.getElementById('vault-mcp-wire-btn');
    const wiredEl = document.getElementById('vault-mcp-wired');
    const isWired = wiredEl && wiredEl.textContent.trim().toLowerCase() === 'yes';
    const url = isWired ? '/api/vault/mcp/unwire' : '/api/vault/mcp/wire-up';
    if (msg) msg.innerText = isWired ? 'Disabling…' : 'Enabling agent access…';
    if (btn) btn.disabled = true;
    try {
        const r = await fetch(url, { method: 'POST', credentials: 'include' });
        const d = await r.json();
        if (msg) {
            msg.innerText = !r.ok ? 'Error: ' + (d.error || r.statusText)
                : isWired ? 'Agent vault access disabled.'
                : 'Agent vault access enabled. Unlock to use it.';
        }
    } catch (e) {
        if (msg) msg.innerText = 'Network error: ' + e.message;
    } finally {
        if (btn) btn.disabled = false;
        vaultMcpRefresh();
    }
}

async function vaultMcpUnlock() {
    const pw = document.getElementById('vault-mcp-pw');
    const emailEl = document.getElementById('vault-mcp-email');
    const msg = document.getElementById('vault-mcp-msg');
    const v = (pw && pw.value) || '';
    if (!v) { if (msg) msg.innerText = 'Enter your vault master password.'; return; }
    const body = { master_password: v };
    if (emailEl && emailEl.style.display !== 'none' && emailEl.value.trim()) {
        body.email = emailEl.value.trim();
    }
    if (msg) msg.innerText = 'Unlocking…';
    try {
        const r = await fetch('/api/vault/mcp/unlock', {
            method: 'POST', credentials: 'include',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        const d = await r.json();
        if (pw) pw.value = '';
        if (!r.ok) {
            let txt = 'Error: ' + (d.error || r.statusText);
            if (d.detail) txt += ' — ' + d.detail;
            if (msg) msg.innerText = txt;
        } else {
            if (msg) msg.innerText = '';
            document.getElementById('conn-msg').innerText = 'Vault unlocked. The agent can search and create entries.';
            closeForm('vault-mcp');
        }
    } catch (e) {
        if (msg) msg.innerText = 'Network error: ' + e.message;
    } finally {
        vaultMcpRefresh();
    }
}

async function vaultMcpLock() {
    const msg = document.getElementById('vault-mcp-msg');
    try {
        await fetch('/api/vault/mcp/lock', { method: 'POST', credentials: 'include' });
        if (msg) msg.innerText = 'Session locked.';
    } catch (e) {
        if (msg) msg.innerText = 'Lock failed: ' + e.message;
    } finally {
        vaultMcpRefresh();
    }
}

async function vaultDocsRefresh() {
    try {
        const r = await fetch('/api/vault/docs/status', { credentials: 'include' });
        if (!r.ok) return;
        const d = await r.json();
        setStatus(document.getElementById('vault-docs-app'), d.e2ee_enabled ? 'enabled' : 'disabled');
        setStatus(document.getElementById('vault-docs-folder'), d.folder_exists ? 'created' : 'missing');
        const openEl = document.getElementById('vault-docs-open');
        if (openEl) {
            if (d.folder_url) { openEl.href = d.folder_url; openEl.style.display = ''; }
            else openEl.style.display = 'none';
        }
        const btnEl = document.getElementById('vault-docs-setup-btn');
        if (btnEl) btnEl.innerText = (d.e2ee_enabled && d.folder_exists) ? 'Re-run setup' : 'Set up encrypted folder';
    } catch (e) { /* silent */ }
}

async function vaultDocsSetup() {
    const msg = document.getElementById('vault-docs-msg');
    const btn = document.getElementById('vault-docs-setup-btn');
    if (msg) msg.innerText = 'Enabling E2EE app and creating folder…';
    if (btn) btn.disabled = true;
    try {
        const r = await fetch('/api/vault/docs/setup', { method: 'POST', credentials: 'include' });
        const d = await r.json();
        if (msg) {
            msg.innerText = r.ok
                ? 'Ready. Mark the folder as encrypted in your Nextcloud client to activate E2EE.'
                : 'Error: ' + (d.error || r.statusText);
        }
    } catch (e) {
        if (msg) msg.innerText = 'Network error: ' + e.message;
    } finally {
        if (btn) btn.disabled = false;
        vaultDocsRefresh();
    }
}

function vaultOpenAdmin(ev) {
    if (ev) ev.preventDefault();
    // Same-origin reverse-proxy through the manager — auth is handled
    // server-side, the admin token never reaches the browser.
    window.open('/admin/', '_blank', 'noopener');
}

/* =====================================================================
   Tunnel
   ===================================================================== */

function showCloudflareForm() {
    document.getElementById('cf-form-container').style.display = 'block';
}

async function updatePangolin(e) {
    e.preventDefault();
    if (!await hbConfirm({
        title: 'Update tunnel settings?',
        body: 'All service URLs will be rewritten and the tunnels restarted.',
        confirm: 'Update',
    })) return;
    triggerAction('/api/tunnel', 'Update Tunnel', {
        endpoint: document.getElementById('tun-ep').value,
        id: document.getElementById('tun-id').value,
        secret: document.getElementById('tun-sec').value,
        main_domain: document.getElementById('tun-main-domain').value,
    });
    setTimeout(() => location.reload(), 3000);
}

async function revertPangolin() {
    if (!await hbConfirm({
        title: 'Revert to factory defaults?',
        body: 'Endpoint, device ID, secret and domains all return to the values this device shipped with.',
        confirm: 'Revert', danger: true,
    })) return;
    triggerAction('/api/tunnel', 'Revert Tunnel', { action: 'revert' });
    setTimeout(() => location.reload(), 3000);
}

async function updateCloudflare(e, mode) {
    e.preventDefault();
    const prefix = mode === 'switch' ? 'cf-switch-' : 'cf-update-';
    const token = document.getElementById(prefix + 'token').value;
    const service = document.getElementById(prefix + 'service').value;
    const domain = document.getElementById(prefix + 'domain').value;

    if (!token) { hbToast('Tunnel token is required.', 'error'); return; }
    if (!domain && mode === 'switch') { hbToast('Domain is required.', 'error'); return; }

    if (!await hbConfirm({
        title: 'Apply Cloudflare tunnel?',
        body: 'Traffic for the selected service will move to Cloudflare.',
        confirm: 'Apply',
    })) return;
    triggerAction('/api/tunnel/cloudflare', 'Cloudflare Update', { domain, token, service });
    setTimeout(() => location.reload(), 3000);
}

async function revertToFactory() {
    if (!await hbConfirm({
        title: 'Disable Cloudflare?',
        body: 'The box reverts to its factory Pangolin tunnel settings.',
        confirm: 'Disable', danger: true,
    })) return;
    triggerAction('/api/tunnel/revert', 'Revert to Factory', {});
    setTimeout(() => location.reload(), 3000);
}

/* =====================================================================
   System configuration
   ===================================================================== */

async function loadSystemConfig() {
    try {
        const res = await fetch('/api/system/config', { credentials: 'include' });
        const data = await res.json();

        const setToggle = (id, val, activeVal, labels) => {
            const el = document.getElementById(id);
            if (!el) return;
            applyStatus(el, val === activeVal ? 'running' : 'stopped', (labels || {})[val] || humanise(val));
            el.dataset.val = val;
        };

        // Watchdog and PCIe are Pi-only — the rows stay hidden elsewhere.
        if (data.watchdog !== 'unsupported') {
            const row = document.getElementById('watchdog-row');
            if (row) row.style.display = 'table-row';
            setToggle('sys-wd-status', data.watchdog, 'enabled');
        }
        setToggle('sys-cron-status', data.cron, 'cron', { cron: 'System cron', ajax: 'AJAX' });
        if (data.pci !== 'unsupported') {
            const row = document.getElementById('pci-row');
            if (row) row.style.display = 'table-row';
            setToggle('sys-pci-status', data.pci, 'gen3', { gen3: 'Gen 3', gen2: 'Gen 2' });
        }

        updateAIStatus(data.llama_server || 'not_installed', data.openclaw || 'not_installed',
            data.ai_model_id, data.whisper || 'not_installed');
        checkRedisStatus();
    } catch (e) { console.error('Sys Config Load Error', e); }
}

function updateAIStatus(llamaStatus, openclawStatus, currentModelId, whisperStatus) {
    // No-op on hardware without an AI-capable GPU — the AI Assistant card
    // and its DOM nodes don't exist there.
    if (!document.getElementById('sys-llama-status')) return;

    setStatus(document.getElementById('sys-llama-status'), llamaStatus,
        llamaStatus === 'starting' ? 'Loading model…' : undefined);
    document.getElementById('sys-llama-status').dataset.val = llamaStatus;

    setStatus(document.getElementById('sys-openclaw-status'), openclawStatus);
    document.getElementById('sys-openclaw-status').dataset.val = openclawStatus;

    // OpenClaw link — same-origin reverse-proxy mounted on the manager at
    // /openclaw/. Works identically over LAN, mDNS, and Pangolin because the
    // auth gate is the master-password session already protecting this page.
    const isRunning = openclawStatus === 'running';
    const openclawOpen = document.getElementById('openclaw-open');
    const openclawLaunch = document.getElementById('openclaw-launch');
    if (openclawOpen) {
        openclawOpen.href = '/openclaw/';
        openclawOpen.style.display = isRunning ? 'inline-block' : 'none';
    }
    if (openclawLaunch) {
        openclawLaunch.href = '/openclaw/';
        openclawLaunch.style.display = isRunning ? '' : 'none';
    }

    // Hierarchy: when AI is running, "Open AI Assistant" is the primary
    // action and Disable is secondary. When AI is off, the Install/Start
    // toggle becomes primary so users know what to do next at a glance.
    const btn = document.getElementById('btn-ai-toggle');
    if (btn) {
        btn.classList.toggle('btn-primary', !isRunning);
        btn.disabled = false;
        btn.style.opacity = '';
        if (llamaStatus === 'not_installed' && openclawStatus === 'not_installed') btn.innerText = 'Install';
        else if (llamaStatus === 'running' && openclawStatus === 'running') btn.innerText = 'Disable';
        else if (llamaStatus === 'starting' || openclawStatus === 'starting') {
            btn.innerText = 'Starting…';
            btn.disabled = true;
        } else btn.innerText = 'Enable';
    }

    const modelSelector = document.getElementById('ai-model-selector');
    if (modelSelector) {
        if (llamaStatus === 'not_installed' && openclawStatus === 'not_installed') {
            modelSelector.style.display = 'block';
            loadAIModels();
        } else if (llamaStatus === 'running' || llamaStatus === 'disabled') {
            modelSelector.style.display = 'block';
            loadAIModels(currentModelId);
        } else {
            modelSelector.style.display = 'none';
        }
    }

    const whisperRow = document.getElementById('whisper-row');
    if (whisperRow) {
        const known = whisperStatus && whisperStatus !== 'not_installed';
        whisperRow.style.display = known ? '' : 'none';
        if (known) setStatus(document.getElementById('sys-whisper-status'), whisperStatus);
    }
}

let aiModelsLoaded = false;
async function loadAIModels(currentModelId) {
    const select = document.getElementById('ai-model-select');
    if (!select) return;
    if (aiModelsLoaded) {
        if (currentModelId) {
            select.value = currentModelId;
            select.dataset.currentModel = currentModelId;
        }
        return;
    }
    try {
        const res = await fetch('/api/ai/models', { credentials: 'include' });
        const data = await res.json();
        select.innerHTML = '';
        (data.models || []).forEach(m => {
            const opt = document.createElement('option');
            opt.value = m.id;
            opt.textContent = `${m.id} (${(m.min_size_bytes / 1073741824).toFixed(0)} GB)`;
            if (currentModelId ? m.id === currentModelId : m.default) opt.selected = true;
            select.appendChild(opt);
        });
        if (currentModelId) select.dataset.currentModel = currentModelId;
        aiModelsLoaded = true;
    } catch (e) { console.error('Failed to load AI models', e); }
}

function onModelSelectChange() {
    const btn = document.getElementById('btn-switch-model');
    const select = document.getElementById('ai-model-select');
    const llamaStatus = document.getElementById('sys-llama-status')?.dataset?.val;
    const changed = llamaStatus && llamaStatus !== 'not_installed' &&
        select.dataset.currentModel && select.value !== select.dataset.currentModel;
    btn.style.display = changed ? 'inline-block' : 'none';
}

async function switchModel() {
    const modelId = document.getElementById('ai-model-select').value;
    if (!await hbConfirm({
        title: `Switch model to ${modelId}?`,
        body: 'AI services stop, the new model downloads if needed, then everything restarts.',
        detail: 'The assistant is unavailable during the switch.',
        confirm: 'Switch model',
    })) return;
    try {
        const res = await fetch('/api/ai/model/switch', {
            method: 'POST', credentials: 'include',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ model_id: modelId }),
        });
        if (res.ok) {
            showSetupLog();
            document.getElementById('btn-switch-model').style.display = 'none';
            setTimeout(loadSystemConfig, 5000);
        } else {
            const err = await res.json();
            hbToast(err.error || 'Failed to switch model', 'error');
        }
    } catch (e) { hbToast('Failed to start model switch: ' + e, 'error'); }
}

function showSetupLog() {
    currentLogSource = 'setup';
    document.getElementById('log-selector').value = 'setup';
    openTab('logs');
}

async function toggleSystem(feature) {
    if (feature === 'openclaw') return toggleAiStack();

    const el = document.getElementById(
        feature === 'watchdog' ? 'sys-wd-status' : (feature === 'cron' ? 'sys-cron-status' : 'sys-pci-status'));
    const current = el.dataset.val;
    let action, title, body;

    if (feature === 'watchdog') {
        action = current === 'enabled' ? 'disable' : 'enable';
        title = `${action === 'enable' ? 'Enable' : 'Disable'} hardware watchdog?`;
        body = 'The watchdog automatically reboots the system if it freezes.';
    } else if (feature === 'cron') {
        action = 'enable';
        title = 'Enforce system cron for Nextcloud?';
        body = 'System cron is more reliable than Nextcloud’s AJAX scheduler.';
    } else {
        action = current === 'gen3' ? 'disable' : 'enable';
        title = `Switch PCIe to ${action === 'enable' ? 'Gen 3 (experimental)' : 'Gen 2 (stable)'}?`;
        body = 'A reboot is required for this to take effect.';
    }

    if (!await hbConfirm({ title, body, confirm: 'Apply' })) return;
    triggerAction('/api/system/config', 'System Config', { feature, action });
    setTimeout(loadSystemConfig, 3000);
}

async function toggleAiStack() {
    const llamaState = document.getElementById('sys-llama-status').dataset.val;
    const ocState = document.getElementById('sys-openclaw-status').dataset.val;
    const bothRunning = llamaState === 'running' && ocState === 'running';
    const action = bothRunning ? 'disable' : 'enable';
    const freshInstall = action === 'enable' && llamaState === 'not_installed';

    let title, body, detail;
    if (freshInstall) {
        const model = document.getElementById('ai-model-select')?.value || 'the default model';
        title = 'Install the AI stack?';
        body = `Downloads a prebuilt llama-server, the ${model} model, and installs OpenClaw.`;
        detail = 'Progress appears in the Setup log.';
    } else if (action === 'enable') {
        title = 'Start the AI services?';
        body = 'llama-server takes a few minutes to load the model into memory.';
    } else {
        title = 'Stop the AI services?';
        body = 'Both llama-server and OpenClaw will shut down.';
    }

    if (!await hbConfirm({ title, body, detail, confirm: action === 'enable' ? 'Start' : 'Stop', danger: action === 'disable' })) return;

    // Persist model selection before any enable path. The 'disabled' state
    // (binary on disk, service stopped after an upgrade) also hits
    // setup_llama_server, which reads AI_MODEL_FILENAME from .env — so .env
    // must track the dropdown on every enable click, not just first install.
    if (action === 'enable') {
        const modelId = document.getElementById('ai-model-select')?.value;
        if (modelId) {
            await fetch('/api/ai/model', {
                method: 'POST', credentials: 'include',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ model_id: modelId }),
            });
        }
    }
    triggerAction('/api/system/config', 'AI Stack', { feature: 'openclaw', action });
    if (action === 'enable') showSetupLog();
    setTimeout(loadSystemConfig, 5000);
}

async function checkRedisStatus() {
    try {
        const res = await fetch('/api/redis/status', { credentials: 'include' });
        const data = await res.json();
        const el = document.getElementById('sys-redis-status');
        const btn = document.getElementById('btn-redis-fix');
        if (!el) return;
        setStatus(el, data.status === 'connected' ? 'active' : data.status);
        if (btn) btn.style.display = data.status === 'unconfigured' ? 'inline-block' : 'none';
    } catch (e) { /* silent */ }
}

async function configureRedis() {
    if (!await hbConfirm({
        title: 'Configure Redis for Nextcloud?',
        body: 'Nextcloud will use Redis for caching and file locking.',
        confirm: 'Configure',
    })) return;
    triggerAction('/api/redis/configure', 'Redis Configuration');
    setTimeout(checkRedisStatus, 5000);
}

async function changeCloudEmail(current) {
    const newEmail = await hbPrompt({
        title: 'Update registered email',
        label: 'Email address',
        value: current,
        type: 'email',
        confirm: 'Update',
    });
    if (!newEmail || newEmail === current) return;
    if (!newEmail.includes('@')) { hbToast('That does not look like an email address.', 'error'); return; }
    try {
        await triggerAction('/api/cloud/register', 'Update Email', { email: newEmail });
        setTimeout(() => location.reload(), 2000);
    } catch (e) {
        hbToast('Failed to update email.', 'error');
    }
}

/* =====================================================================
   FTP & network
   ===================================================================== */

async function populateFtpNcUserSelect() {
    // Replace the seeded `admin` option with the real on-device NC user list.
    // Degrades silently if the endpoint isn't available (NC container down,
    // partial install) — the seeded admin option stays so the form works.
    const sel = document.getElementById('ftp-nc-user');
    if (!sel) return;
    try {
        const r = await fetch('/api/integrations/nextcloud/local_users', { credentials: 'include' });
        if (!r.ok) return;
        const d = await r.json();
        const users = d.users || [];
        if (!users.length) return;
        const prev = sel.value;
        sel.innerHTML = users.map(u => {
            const label = u.displayname && u.displayname !== u.id ? `${u.displayname} (${u.id})` : u.id;
            return `<option value="${u.id}">${label}</option>`;
        }).join('');
        if (users.some(u => u.id === prev)) sel.value = prev;
        else if (d.admin_user && users.some(u => u.id === d.admin_user)) sel.value = d.admin_user;
    } catch (e) { /* silent — keep the seeded admin option */ }
}

async function loadFtpUsers() {
    const el = document.getElementById('ftp-user-list');
    if (!el) return;
    el.innerHTML = '<span class="skeleton sk-block"></span><span class="skeleton sk-block"></span>';
    try {
        const res = await fetch('/api/ftp/users', { credentials: 'include' });
        const users = await res.json();

        const recipe = document.getElementById('ftp-recipe');
        if (recipe) recipe.style.display = users.length ? 'block' : 'none';

        if (!users.length) {
            el.innerHTML = '<p class="faint small">No FTP users configured.</p>';
            return;
        }
        el.innerHTML =
            '<table><thead><tr><th>FTP user</th><th>Nextcloud user</th><th></th></tr></thead><tbody>' +
            users.map(u => `<tr>
                <td>${escapeHtml(u.ftp_user)}</td>
                <td>${escapeHtml(u.nc_user)}</td>
                <td style="text-align:right"><button class="btn-danger" onclick="deleteFtpUser('${u.ftp_user}')">Delete</button></td>
            </tr>`).join('') +
            '</tbody></table>';
    } catch (e) { el.innerHTML = '<p class="faint small">Failed to load users.</p>'; }
}

async function setupFtp(e) {
    e.preventDefault();
    const ncUser = document.getElementById('ftp-nc-user').value || 'admin';
    const ftpUser = document.getElementById('ftp-user').value;
    const ftpPass = document.getElementById('ftp-pass').value;
    if (!ftpUser || !ftpPass) { hbToast('FTP user and password are required.', 'error'); return; }

    if (!await hbConfirm({
        title: 'Create FTP user?',
        body: `"${ftpUser}" will upload into the Nextcloud account "${ncUser}".`,
        confirm: 'Create',
    })) return;
    triggerAction('/api/ftp/setup', 'FTP Setup', { nc_user: ncUser, ftp_user: ftpUser, ftp_pass: ftpPass });
    document.getElementById('ftp-pass').value = '';
    setTimeout(loadFtpUsers, 3000);
}

async function deleteFtpUser(user) {
    if (!await hbConfirm({
        title: `Delete FTP user "${user}"?`,
        body: 'Any camera uploading with these credentials will stop working.',
        confirm: 'Delete', danger: true,
    })) return;
    triggerAction('/api/ftp/delete', 'Delete FTP User', { ftp_user: user });
    setTimeout(loadFtpUsers, 3000);
}

let _netInfo = null;
let _netCountdownTimer = null;

async function loadNetworkStatus() {
    const statusEl = document.getElementById('net-status');
    if (!statusEl) return;
    try {
        const res = await fetch('/api/network/info', { credentials: 'include' });
        const n = await res.json();
        if (n.error) { statusEl.textContent = 'Network status unavailable: ' + n.error; return; }
        _netInfo = n;

        const isStatic = (n.method === 'manual');
        statusEl.innerHTML = `Current address <code>${n.ip}</code> ` +
            `<span class="muted">— ${isStatic ? 'fixed' : 'automatic (DHCP)'}</span>`;

        const host = document.getElementById('ftp-recipe-host');
        if (host) host.textContent = n.ip;
        const warn = document.getElementById('ftp-recipe-warn');
        if (warn) warn.style.display = isStatic ? 'none' : 'block';

        document.getElementById('net-pin-form').style.display = 'flex';
        const ipInput = document.getElementById('net-ip');
        if (document.activeElement !== ipInput) ipInput.value = n.suggested || n.ip;
        document.getElementById('net-dhcp-btn').style.display = isStatic ? 'inline-block' : 'none';

        // If a change is awaiting confirmation (e.g. we just reconnected at
        // the new address), surface the confirm panel automatically.
        if (n.pending) showConfirmPanel(n.ip, null);
    } catch (e) {
        statusEl.textContent = 'Network status unavailable (offline?).';
    }
}

function showConfirmPanel(newIp, revertSeconds) {
    document.getElementById('net-confirm').style.display = 'block';
    document.getElementById('net-confirm-msg').innerHTML =
        `The box is now reachable at <a href="http://${newIp}/"><code>${newIp}</code></a>. ` +
        `Choose <b>Keep this address</b> to make it permanent. If you don't, it reverts to DHCP automatically.`;
    if (_netCountdownTimer) clearInterval(_netCountdownTimer);
    if (revertSeconds) {
        let left = revertSeconds;
        const cd = document.getElementById('net-countdown');
        const tick = () => {
            cd.textContent = left > 0 ? `auto-revert in ${left}s` : 'reverting…';
            if (left-- <= 0) clearInterval(_netCountdownTimer);
        };
        tick();
        _netCountdownTimer = setInterval(tick, 1000);
    }
}

async function pinNetwork() {
    const ip = document.getElementById('net-ip').value.trim();
    if (!_netInfo) return;
    if (!await hbConfirm({
        title: `Pin this box to ${ip}?`,
        body: `The box's address changes immediately. Reconnect at http://${ip}/ and choose "Keep this address" within about 3 minutes, or it reverts to DHCP on its own.`,
        detail: `Remember to point your camera's FTP server at ${ip} afterwards.`,
        confirm: 'Pin address',
    })) return;

    const body = { ip, prefix: _netInfo.prefix || '24', gateway: _netInfo.gateway, dns: _netInfo.dns, revert: 180 };
    try {
        const res = await fetch('/api/network/pin', {
            method: 'POST', credentials: 'include',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        const data = await res.json();
        if (!res.ok) { hbToast('Could not pin address: ' + (data.error || res.status), 'error'); return; }
        showConfirmPanel(data.new_ip, data.revert_seconds);
        // The current page may lose the box when the IP flips; nudge the user over.
        setTimeout(() => { window.location.href = data.new_url; }, (data.revert_seconds > 20 ? 6000 : 3000));
    } catch (e) {
        // Expected: the connection may drop as the IP changes. Guide the user.
        showConfirmPanel(ip, body.revert);
    }
}

async function confirmNetwork() {
    try {
        const res = await fetch('/api/network/confirm', { method: 'POST', credentials: 'include' });
        const data = await res.json();
        if (!res.ok) { hbToast('Confirm failed: ' + (data.error || res.status), 'error'); return; }
        if (_netCountdownTimer) clearInterval(_netCountdownTimer);
        document.getElementById('net-confirm').style.display = 'none';
        document.getElementById('net-countdown').textContent = '';
        hbToast('Fixed address saved. Point your camera at this IP.');
        loadNetworkStatus();
    } catch (e) {
        hbToast('Confirm failed — are you connected to the box at its new address?', 'error');
    }
}

async function revertDhcp() {
    if (!await hbConfirm({
        title: 'Back to automatic addressing?',
        body: 'The box returns to DHCP and its IP may change again.',
        confirm: 'Use DHCP',
    })) return;
    try {
        await fetch('/api/network/dhcp', { method: 'POST', credentials: 'include' });
        hbToast('Reverting to DHCP. The box address may change.');
    } catch (e) { /* connection may drop as the IP changes */ }
}

async function loadSerialDevices() {
    const sel = document.getElementById('zigbee-device');
    if (!sel) return;
    try {
        const res = await fetch('/api/hardware/serial', { credentials: 'include' });
        const devices = await res.json();
        const currentRes = await fetch('/api/manager/zigbee', { credentials: 'include' });
        const activeDev = (await currentRes.json()).current || 'none';

        sel.innerHTML = '<option value="none">None</option>';
        if (!devices.length) {
            const opt = document.createElement('option');
            opt.disabled = true;
            opt.innerText = 'No USB devices found';
            sel.appendChild(opt);
        } else {
            devices.forEach(d => {
                const opt = document.createElement('option');
                opt.value = d;
                opt.innerText = d;
                sel.appendChild(opt);
            });
        }
        sel.value = activeDev;
    } catch (e) { /* silent */ }
}

async function updateZigbee() {
    triggerAction('/api/manager/zigbee', 'Zigbee Config', { device: document.getElementById('zigbee-device').value });
}

/* =====================================================================
   Backup & storage
   ===================================================================== */

async function loadDrives() {
    const el = document.getElementById('drive-list');
    if (!el) return;
    el.innerHTML = '<span class="skeleton sk-block"></span>';
    try {
        const res = await fetch('/api/drives', { credentials: 'include' });
        const drives = await res.json();
        if (!drives.length) {
            el.innerHTML = '<p class="faint small">No external drives found.</p>';
            return;
        }
        el.innerHTML = drives.map(d => `
            <div class="drive-row">
              <div class="row-main">
                <strong>${escapeHtml(d.path)}</strong>
                <span class="row-meta">${escapeHtml(d.size)} · ${escapeHtml(d.model)}</span>
              </div>
              ${d.is_backup
                ? '<span class="status-badge status-running">Backup drive</span>'
                : `<div class="row-actions">
                     <button class="btn-warning" onclick="mountDrive('${d.path}')">Mount</button>
                     <button class="btn-danger" onclick="formatDrive('${d.path}')">Format</button>
                   </div>`}
            </div>`).join('');
    } catch (e) { el.innerHTML = '<p class="faint small">Error loading drives.</p>'; }
}

async function mountDrive(path) {
    if (!await hbConfirm({
        title: `Use ${path} as the backup drive?`,
        body: 'Existing data on the drive is kept.',
        confirm: 'Mount',
    })) return;
    triggerAction('/api/drives/mount', 'Mount Drive', { path });
}

async function formatDrive(path) {
    // One dialog with a typed confirmation, where this used to be a
    // confirm() immediately followed by a prompt().
    if (!await hbConfirm({
        title: 'Erase this drive?',
        body: `Every byte on ${path} will be destroyed. This cannot be undone.`,
        confirm: 'Erase drive', danger: true, requireText: 'FORMAT',
    })) return;
    await triggerAction('/api/drives/format', 'Format Drive', { path });
    setTimeout(loadDrives, 5000);   // refresh to show the new partition
}

async function loadDiskStats() {
    try {
        const res = await fetch('/api/backup/stats', { credentials: 'include' });
        const d = await res.json();
        if (d.mounted) {
            setMeter('disk-bar', 'disk-text', d.percent,
                `${d.used_gb} / ${d.total_gb} GB` + (d.internal ? ' · internal disk' : ''));
        } else {
            setMeter('disk-bar', 'disk-text', 0, 'Not mounted');
        }
        const cb = document.getElementById('internal-backup');
        if (cb) cb.checked = !!d.internal;
    } catch (e) { /* silent */ }
}

async function toggleInternalBackup(enabled) {
    const res = await fetch('/api/backup/internal', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled }),
    });
    const d = await res.json().catch(() => ({}));
    if (!res.ok) hbToast(d.error || 'Could not change backup storage.', 'error');
    loadDiskStats();
}

async function loadBackupConfig() {
    try {
        const res = await fetch('/api/backup/config', { credentials: 'include' });
        const d = (await res.json()) || {};
        // Robust defaults if keys are missing.
        if (!d.retention) d.retention = 7;
        if (!d.hour) d.hour = 3;
        if (d.minute === undefined || d.minute === null) d.minute = 0;
        if (!d.day_week) d.day_week = '*';
        if (!d.day_month) d.day_month = '*';

        document.getElementById('bk-retention').value = d.retention;
        document.getElementById('bk-hour').value = d.hour;
        document.getElementById('bk-min').value = d.minute;

        let freq = 'daily';
        if (d.day_week !== '*') {
            freq = 'weekly';
            document.getElementById('bk-dow').value = d.day_week;
        } else if (d.day_month !== '*') {
            freq = 'monthly';
            document.getElementById('bk-dom').value = d.day_month;
        }
        document.getElementById('bk-freq').value = freq;
        updateFreqUI();
    } catch (e) { console.error('Failed to load backup config:', e); }
}

function updateFreqUI() {
    const freq = document.getElementById('bk-freq').value;
    document.getElementById('ui-dow').style.display = freq === 'weekly' ? 'block' : 'none';
    document.getElementById('ui-dom').style.display = freq === 'monthly' ? 'block' : 'none';
}

async function saveBackupConfig(e) {
    e.preventDefault();
    const freq = document.getElementById('bk-freq').value;
    const body = {
        retention: document.getElementById('bk-retention').value,
        hour: document.getElementById('bk-hour').value,
        minute: document.getElementById('bk-min').value,
        day_week: freq === 'weekly' ? document.getElementById('bk-dow').value : '*',
        day_month: freq === 'monthly' ? document.getElementById('bk-dom').value : '*',
    };
    const res = await fetch('/api/backup/config', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
    });
    if (res.ok) hbToast('Backup schedule saved.');
    else hbToast('Could not save the backup schedule.', 'error');
}

async function runBackup() {
    triggerAction('/api/backup/now', 'Manual Backup', { strategy: document.getElementById('backup-strategy').value });
}

async function loadOffsiteConfig() {
    try {
        const res = await fetch('/api/backup/offsite', { credentials: 'include' });
        const d = await res.json();
        document.getElementById('os-enabled').checked = !!d.enabled;
        if (d.type) document.getElementById('os-type').value = d.type;
        document.getElementById('os-host').value = d.host || '';
        document.getElementById('os-user').value = d.user || '';
        document.getElementById('os-pass').value = '';
        document.getElementById('os-pass').placeholder = d.has_pass ? '(unchanged)' : '';
        document.getElementById('os-path').value = d.path || '';
        updateOffsiteUI();
    } catch (e) { /* silent */ }
}

function updateOffsiteUI() {
    const labels = {
        sftp:   ['Host (host or host:port)', 'backup.example.com', 'Username', 'Password'],
        webdav: ['WebDAV URL', 'https://cloud.example.com/remote.php/dav/files/USER', 'Username', 'App password'],
        s3:     ['Endpoint URL', 'https://s3.example.com', 'Access key ID', 'Secret key'],
    }[document.getElementById('os-type').value];
    document.getElementById('os-host-label').innerText = labels[0];
    document.getElementById('os-host').placeholder = labels[1];
    document.getElementById('os-user-label').innerText = labels[2];
    document.getElementById('os-pass-label').innerText = labels[3];
    document.getElementById('os-path-label').innerText =
        document.getElementById('os-type').value === 's3' ? 'Bucket / prefix' : 'Remote folder';
}

async function saveOffsiteConfig(e) {
    e.preventDefault();
    const status = document.getElementById('os-status');
    const enabled = document.getElementById('os-enabled').checked;
    const body = {
        enabled,
        type: document.getElementById('os-type').value,
        host: document.getElementById('os-host').value,
        user: document.getElementById('os-user').value,
        pass: document.getElementById('os-pass').value,
        path: document.getElementById('os-path').value,
    };
    status.style.color = '';
    status.innerText = 'Saving...';
    const res = await fetch('/api/backup/offsite', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
    });
    const d = await res.json().catch(() => ({}));
    if (!res.ok) {
        status.style.color = 'var(--danger)';
        status.innerText = d.error || 'Error saving settings.';
        return;
    }
    if (!enabled) {
        status.innerText = 'Off-site copy disabled.';
        return;
    }
    status.innerText = 'Testing connection...';
    const t = await fetch('/api/backup/offsite/test', { method: 'POST' });
    const td = await t.json().catch(() => ({}));
    if (t.ok) {
        status.style.color = 'var(--accent)';
        status.innerText = 'Connected — backups are copied off-site after each run.';
        document.getElementById('os-pass').value = '';
        document.getElementById('os-pass').placeholder = '(unchanged)';
    } else {
        status.style.color = 'var(--danger)';
        status.innerText = td.error || 'Connection test failed.';
    }
}

let replicaEnabled = false;

async function loadReplicaStatus() {
    const status = document.getElementById('replica-status');
    const btn = document.getElementById('replica-btn');
    if (!status) return;
    try {
        const res = await fetch('/api/backup/replica', { credentials: 'include' });
        const d = await res.json();
        if (!res.ok) throw new Error(d.error);
        replicaEnabled = !!d.enabled;
        if (replicaEnabled) {
            status.innerHTML = `<span class="status-badge status-running">Receiving</span> ` +
                `<span class="muted small">at <code>${d.url}</code> · ${d.used} used</span>`;
            btn.innerText = 'Disable and delete received archives';
            btn.className = 'btn-danger';
        } else {
            status.innerHTML = '<span class="muted small">Not enabled.</span>';
            btn.innerText = 'Enable';
            btn.className = '';
        }
        btn.disabled = false;
    } catch (e) {
        status.innerHTML = '<span class="muted small">Status unavailable.</span>';
    }
}

async function toggleReplica() {
    const btn = document.getElementById('replica-btn');
    if (replicaEnabled) {
        if (!await hbConfirm({
            title: 'Stop receiving backups?',
            body: 'This deletes the replica account and every archive received from the other HomeBrain.',
            detail: 'That box’s own local backups are unaffected.',
            confirm: 'Delete archives', danger: true, requireText: 'DELETE',
        })) return;
    }
    btn.disabled = true;
    const res = await fetch('/api/backup/replica', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: replicaEnabled ? 'disable' : 'enable' }),
    });
    const d = await res.json().catch(() => ({}));
    if (!res.ok) {
        hbToast(d.error || 'Operation failed.', 'error');
        btn.disabled = false;
        return;
    }
    const creds = document.getElementById('replica-creds');
    if (d.pass) {
        document.getElementById('replica-url').innerText = d.url;
        document.getElementById('replica-pass').innerText = d.pass;
        creds.style.display = 'block';
    } else {
        creds.style.display = 'none';
    }
    loadReplicaStatus();
}

let backupIndex = {};   // name -> {encrypted, ...} from /api/backups/list

async function loadBackups() {
    try {
        const res = await fetch('/api/backups/list', { credentials: 'include' });
        const list = await res.json();
        const sel = document.getElementById('backup-list');
        sel.innerHTML = '';
        backupIndex = {};
        list.forEach(b => {
            backupIndex[b.name] = b;
            const opt = document.createElement('option');
            opt.value = b.name;
            opt.innerText = `${b.encrypted ? '🔒 ' : ''}${b.name} (${b.type}, ${b.size})`;
            sel.appendChild(opt);
        });
    } catch (e) { /* silent */ }
}

async function confirmRestore() {
    const file = document.getElementById('backup-list').value;
    if (!file) return;
    if (!await hbConfirm({
        title: 'Restore this snapshot?',
        body: `All current data is wiped and replaced with ${file}.`,
        detail: 'This cannot be undone.',
        confirm: 'Restore', danger: true, requireText: 'RESTORE',
    })) return;

    const payload = { filename: file };
    if (backupIndex[file] && backupIndex[file].encrypted) {
        // Normally decrypts with the current master password. A passphrase is
        // only needed when the archive predates a password change.
        const pw = await hbPrompt({
            title: 'Archive passphrase',
            body: 'Leave this empty to use the current master password. Only enter a passphrase if this backup was made before a master-password change.',
            label: 'Passphrase (optional)',
            type: 'password',
            confirm: 'Restore',
            allowEmpty: true,
        });
        if (pw === null) return;      // cancelled
        if (pw) payload.passphrase = pw;
    }
    triggerAction('/api/restore', 'Restore', payload);
}

function loadOpenClawBackupStatus() {
    fetch('/api/openclaw/backup-status')
        .then(r => r.json())
        .then(data => {
            setStatus(document.getElementById('oc-backup-config-status'),
                data.config_present ? 'present' : 'missing');
            const sizeEl = document.getElementById('oc-backup-workspace-size');
            if (sizeEl) {
                sizeEl.textContent = data.workspace_size_mb !== null ? data.workspace_size_mb + ' MB' : 'Not found';
            }
            const warnEl = document.getElementById('oc-backup-size-warning');
            if (warnEl) warnEl.style.display = data.workspace_size_warning ? 'inline' : 'none';
            const inclEl = document.getElementById('oc-backup-include-workspace');
            if (inclEl) inclEl.checked = data.backup_workspace;
            const exclEl = document.getElementById('oc-backup-exclude-caches');
            if (exclEl) exclEl.checked = data.exclude_caches;
        })
        .catch(() => {});
}

function updateOpenClawBackupSetting(key, value) {
    fetch('/api/backup/openclaw-settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ [key]: value }),
    }).catch(() => {});
}

/* =====================================================================
   Actions, maintenance, updates
   ===================================================================== */

async function triggerAction(endpoint, name, body = {}) {
    try {
        const res = await fetch(endpoint, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        if (res.status === 401) { location.reload(); return; }
        const data = await res.json();
        // Accept both 'started' (async task) and 'success' (immediate).
        if (data.status === 'started' || data.status === 'success') {
            hbToast(data.message || `${name} started.`);
            pollTask();
        } else {
            hbToast(data.error || `${name} failed.`, 'error');
        }
    } catch (e) {
        console.error(e);
        hbToast('Request failed — see the browser console for details.', 'error');
    }
}

async function toggleMaintenance(mode) {
    await fetch('/api/maintenance/mode', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mode }),
    });
    setTimeout(fetchStatus, 1000);
}

let pendingUpdateTarget = '';

function resetUpdateUI() {
    document.getElementById('update-msg').innerText = '';
    document.getElementById('btn-do-update').style.display = 'none';
}

async function checkManagerUpdate() {
    const btn = document.getElementById('btn-check-update');
    const msg = document.getElementById('update-msg');
    const doBtn = document.getElementById('btn-do-update');
    const channel = document.getElementById('update-channel').value;

    btn.disabled = true;
    btn.innerText = 'Checking…';
    msg.innerText = '';
    doBtn.style.display = 'none';

    try {
        const res = await fetch(`/api/manager/check_update?channel=${channel}`, { credentials: 'include' });
        const data = await res.json();
        pendingUpdateTarget = data.target_ref;
        msg.innerText = data.message;
        msg.style.color = data.available ? 'var(--accent)' : 'var(--text-dim)';
        if (data.available) {
            doBtn.style.display = 'inline-block';
            doBtn.innerText = `Install ${data.target_ref}`;
        }
    } catch (e) {
        msg.innerText = 'Check failed.';
        msg.style.color = 'var(--danger)';
    } finally {
        btn.disabled = false;
        btn.innerText = 'Check now';
    }
}

async function doManagerUpdate() {
    const channel = document.getElementById('update-channel').value;
    if (!await hbConfirm({
        title: `Update ${HB.productName} Manager?`,
        body: `Installs ${channel} ${pendingUpdateTarget}.`,
        detail: 'The interface restarts and this page reloads.',
        confirm: 'Install update',
    })) return;

    const res = await fetch('/api/manager/update', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ channel, target_ref: pendingUpdateTarget }),
    });
    const data = await res.json();
    if (data.status === 'started') {
        hbToast('Update started. This page reloads in 15 seconds.');
        setTimeout(() => location.reload(), 15000);
    } else {
        hbToast('Update failed to start: ' + (data.error || 'unknown error'), 'error');
    }
}

/* =====================================================================
   Logs
   ===================================================================== */

function changeLogSource(val) {
    currentLogSource = val;
    pollLogs();
}

async function pollLogs() {
    const statusEl = document.getElementById('log-status');
    if (!statusEl) return;
    statusEl.innerText = 'Fetching…';
    try {
        const res = await fetch('/api/logs/' + currentLogSource, { credentials: 'include' });
        if (!res.ok) return;
        const txt = await res.text();
        if (txt !== rawLogData) {
            rawLogData = txt;
            renderLogs();
            statusEl.innerText = 'Updated ' + new Date().toLocaleTimeString();
        } else {
            statusEl.innerText = 'No changes';
        }
    } catch (e) { /* silent */ }
}

function renderLogs() {
    const div = document.getElementById('console-output');
    const showErr = document.getElementById('chk-error').checked;
    const showWarn = document.getElementById('chk-warn').checked;
    const showInfo = document.getElementById('chk-info').checked;
    const showDebug = document.getElementById('chk-debug').checked;

    let html = '';
    rawLogData.split('\n').forEach(line => {
        if (!line.trim()) return;
        const upper = line.toUpperCase();
        let cssClass = 'log-debug';
        let visible = false;

        if (upper.includes('ERROR') || upper.includes('CRITICAL') ||
            upper.includes('EXCEPTION') || upper.includes('TRACEBACK')) {
            cssClass = 'log-error';
            visible = showErr;
        } else if (upper.includes('WARN')) {
            cssClass = 'log-warning';
            visible = showWarn;
        } else if (upper.includes('INFO')) {
            cssClass = 'log-info';
            visible = showInfo;
        } else {
            visible = showDebug;   // raw lines and stack traces
        }
        if (visible) html += `<div class="log-line ${cssClass}">${escapeHtml(line)}</div>`;
    });

    div.innerHTML = html;
    div.scrollTop = div.scrollHeight;
}

/* =====================================================================
   Recovery phrase
   ===================================================================== */

async function loadRecoveryStatus() {
    const line = document.getElementById('recovery-status-line');
    const btn = document.getElementById('recovery-gen-btn');
    const banner = document.getElementById('recovery-prompt');
    if (!line) return;
    // The Status-tab banner only nags when a phrase is genuinely missing and
    // can be generated; default to hidden so a failed status probe never
    // strands a stale warning on screen.
    if (banner) banner.style.display = 'none';
    try {
        const r = await fetch('/api/recovery/status', { credentials: 'include' });
        const d = await r.json();
        if (!d.wordlist_ok) {
            line.textContent = 'Recovery wordlist unavailable on this device — contact support.';
            if (btn) btn.disabled = true;
            return;
        }
        if (d.configured) {
            let when = '';
            if (d.created_at) {
                try { when = ' on ' + new Date(parseInt(d.created_at, 10) * 1000).toLocaleDateString(); } catch (e) {}
            }
            line.textContent = `A recovery phrase is configured${when}.`;
            if (btn) btn.textContent = 'Regenerate recovery phrase';
        } else {
            line.textContent = 'No recovery phrase is set. Generate one now so you can recover access if you forget your master password.';
            if (btn) btn.textContent = 'Generate recovery phrase';
            if (banner) banner.style.display = 'block';
        }
    } catch (e) { /* leave the placeholder text */ }
}

async function regenerateRecovery() {
    const btn = document.getElementById('recovery-gen-btn');
    const msg = document.getElementById('recovery-msg');
    // Only confirm the destructive case (replacing a working phrase).
    // First-time generation is exactly what we're nudging the user to do.
    const replacing = btn && /regenerate/i.test(btn.textContent || '');
    if (replacing && !await hbConfirm({
        title: 'Generate a new recovery phrase?',
        body: 'Your previous phrase stops working immediately.',
        confirm: 'Generate', danger: true,
    })) return;

    btn.disabled = true;
    msg.textContent = 'Generating…';
    try {
        const r = await fetch('/api/recovery/regenerate', { method: 'POST', credentials: 'include' });
        const d = await r.json();
        if (d.status === 'ok' && d.recovery_phrase) {
            document.getElementById('recovery-phrase-box').textContent = d.recovery_phrase;
            document.getElementById('recovery-reveal').style.display = 'block';
            msg.textContent = '';
            await loadRecoveryStatus();
        } else {
            msg.textContent = d.error || 'Failed to generate phrase';
        }
    } catch (e) {
        msg.textContent = 'Network error — try again';
    } finally {
        btn.disabled = false;
    }
}

/* =====================================================================
   Factory reset
   ===================================================================== */

async function showNuclearModal() {
    const confirmed = await hbModal({
        title: 'Permanent factory wipe',
        danger: true,
        wide: true,
        confirmLabel: 'Wipe everything',
        focus: '#nuclear-current-pw',
        build: (root, submit) => {
            root.innerHTML = `
                <div class="notice notice-danger" style="margin-bottom:14px;">
                  <strong>This permanently destroys:</strong>
                  <ul>
                    <li>All Nextcloud files and configuration</li>
                    <li>All Home Assistant automations and history</li>
                    <li>Your entire HomeBrain Vault — passwords, TOTP, attachments</li>
                    <li>OpenClaw agent memory, chats, and every integration</li>
                    <li>Downloaded AI models (by default)</li>
                    <li>Every secret and runtime state on this device</li>
                  </ul>
                </div>
                <p class="modal-detail">
                  Your backup drive (<code>/mnt/backup</code>) is not touched.
                  The device reboots when the wipe completes.
                </p>
                <label for="nuclear-current-pw">Current master password</label>
                <input type="password" id="nuclear-current-pw" autocomplete="current-password">
                <label for="nuclear-phrase">Type <code>DESTROY ALL DATA</code> to confirm</label>
                <input type="text" id="nuclear-phrase" class="mono" placeholder="DESTROY ALL DATA" autocomplete="off">
                <label class="check" style="margin-top:12px;">
                  <input type="checkbox" id="nuclear-wipe-models" checked>
                  <span>Delete downloaded AI models (saves 10–30 GB, needs re-download later)</span>
                </label>
                <label class="check" style="margin-top:8px;">
                  <input type="checkbox" id="nuclear-wipe-runtime">
                  <span>Also delete AI runtime binaries — forces a slow rebuild</span>
                </label>`;
            root.querySelector('#nuclear-phrase')
                .addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); submit(); } });

            return errEl => {
                const pw = root.querySelector('#nuclear-current-pw').value;
                const phrase = root.querySelector('#nuclear-phrase').value;
                if (!pw) {
                    errEl.textContent = 'Current master password is required.';
                    return null;
                }
                if (phrase !== 'DESTROY ALL DATA') {
                    errEl.textContent = 'Type "DESTROY ALL DATA" exactly to continue.';
                    return null;
                }
                return {
                    current_password: pw,
                    confirmation_phrase: phrase,
                    wipe_ai_models: root.querySelector('#nuclear-wipe-models').checked,
                    wipe_ai_runtime: root.querySelector('#nuclear-wipe-runtime').checked,
                };
            };
        },
    });
    if (!confirmed) return;

    try {
        const res = await fetch('/api/system/nuclear-reset', {
            method: 'POST', credentials: 'include',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(confirmed),
        });
        const data = await res.json();
        if (res.ok && data.status === 'started') showNuclearProgressScreen();
        else hbToast(data.error || 'Factory reset could not start.', 'error');
    } catch (e) {
        hbToast('Network error — factory reset did not start.', 'error');
    }
}

function showNuclearProgressScreen() {
    stopPolling();
    const overlay = document.createElement('div');
    overlay.className = 'modal-backdrop';
    overlay.style.background = '#000';
    overlay.innerHTML = `
        <div style="text-align:center; max-width:520px; color:#fff;">
          <h2 style="color:#ff5555;">Factory reset in progress</h2>
          <p id="nuclear-progress-msg">Stopping all services and destroying data.<br>This takes 30–90 seconds.</p>
          <p style="opacity:0.7;">The device reboots automatically when complete.<br>Do not power it off.</p>
          <p class="mono" style="opacity:0.6; font-size:0.85em;">Status: <span id="nuclear-task-status">running</span></p>
        </div>`;
    document.body.appendChild(overlay);

    const poll = setInterval(async () => {
        try {
            const r = await fetch('/api/task_status', { credentials: 'include' });
            const s = await r.json();
            const msgEl = document.getElementById('nuclear-progress-msg');
            const stEl = document.getElementById('nuclear-task-status');
            if (s.status === 'success') {
                clearInterval(poll);
                if (msgEl) msgEl.innerHTML = 'Reset complete.<br>The device is rebooting now.';
                if (stEl) stEl.textContent = 'rebooting';
            } else if (s.status === 'error') {
                clearInterval(poll);
                if (msgEl) msgEl.textContent = 'An error occurred. Check the logs after reboot.';
            } else if (stEl) {
                stEl.textContent = s.status || 'running';
            }
        } catch (e) {
            clearInterval(poll);   // connection lost = reboot in progress
        }
    }, 1500);
}

/* --- boot --- */

// The subdomain preview under the tunnel form.
const tunDomainEl = document.getElementById('tun-main-domain');
if (tunDomainEl) {
    tunDomainEl.addEventListener('input', e => {
        const val = e.target.value || '…';
        ['preview-mgr', 'preview-nc', 'preview-ha'].forEach(id => {
            const el = document.getElementById(id);
            if (el) el.innerText = val;
        });
    });
}

init();
