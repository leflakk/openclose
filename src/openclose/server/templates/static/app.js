function updateContextBar(info) {
    if (!info || !info.max) return;
    var pct = Math.min(100, Math.round((info.used / info.max) * 100));
    var fill = document.getElementById('context-bar-fill');
    var usedEl = document.getElementById('context-tokens-used');
    var maxEl = document.getElementById('context-tokens-max');
    var pctEl = document.getElementById('context-bar-pct');
    if (fill) {
        fill.style.width = pct + '%';
        fill.className = 'context-bar-fill' + (
            pct >= 90 ? ' level-critical' :
            pct >= 80 ? ' level-high' :
            pct >= 60 ? ' level-medium' :
            ' level-low'
        );
    }
    if (usedEl) usedEl.textContent = info.used.toLocaleString();
    if (maxEl) maxEl.textContent = info.max.toLocaleString();
    if (pctEl) pctEl.textContent = pct + '%';
}

function setHeaderToggleState(btn, active, fullLabel, activeTitleSuffix, inactiveTitleSuffix) {
    if (!btn) return;
    btn.classList.toggle('is-active', !!active);
    btn.setAttribute('aria-pressed', active ? 'true' : 'false');
    var stateEl = btn.querySelector('.header-toggle-state');
    if (stateEl) stateEl.textContent = active ? 'ON' : 'OFF';
    var suffix = active ? activeTitleSuffix : inactiveTitleSuffix;
    btn.setAttribute('title', fullLabel + ': ' + (active ? 'ON' : 'OFF') + (suffix ? ' — ' + suffix : ''));
}

function updateSkipButton(active) {
    setHeaderToggleState(
        document.getElementById('info-skip-btn'),
        active,
        'Auto-approve',
        'auto-approving all tool calls',
        'toggle to auto-approve all tool calls'
    );
}

async function ensureAutoApproveOn(sid) {
    try {
        var s = await fetch('/api/sessions/' + sid + '/skip-permissions').then(function(r) { return r.json(); });
        if (!s.skip_all) {
            s = await fetch('/api/sessions/' + sid + '/skip-permissions', {method: 'POST'}).then(function(r) { return r.json(); });
        }
        return !!s.skip_all;
    } catch (e) {
        console.warn('ensureAutoApproveOn failed', e);
        return false;
    }
}

function updatePlanToggle(active) {
    setHeaderToggleState(
        document.getElementById('info-plan-btn'),
        active,
        'Read Plan File',
        'plan file loaded into model context',
        'toggle to load the plan file into the model context'
    );
}

function updateVideoCompatibleButton(active) {
    setHeaderToggleState(
        document.getElementById('info-video-compatible-btn'),
        active,
        'Video Compatible Model',
        'main LLM accepts video input — Record button enabled',
        'toggle ON when your main LLM accepts video input (required to enable the Record button)'
    );
    syncRecorderEnabled();
}

function syncRecorderEnabled() {
    var recorderBtn = document.getElementById('recorder-toggle-btn');
    if (!recorderBtn) return;
    var videoCompatBtn = document.getElementById('info-video-compatible-btn');
    var videoCompatActive = videoCompatBtn ? videoCompatBtn.getAttribute('aria-pressed') === 'true' : false;
    var state = recorderBtn.dataset.state || 'idle';

    if (state === 'pending') {
        recorderBtn.disabled = true;
        recorderBtn.title = '';
        return;
    }
    if (state === 'recording') {
        recorderBtn.disabled = false;
        recorderBtn.title = '';
        return;
    }
    if (!videoCompatActive) {
        recorderBtn.disabled = true;
        recorderBtn.title = 'Enable the Video Compatible Model toggle to record';
    } else {
        recorderBtn.disabled = false;
        recorderBtn.title = '';
    }
}

function initChat(sessionId) {
    const messagesDiv = document.getElementById('messages');
    const input = document.getElementById('user-input');
    const sendBtn = document.getElementById('send-btn');

    // --- Smart auto-scroll: only scroll if user is near the bottom ---
    let _userScrolledUp = false;
    const _scrollThreshold = 80; // px from bottom to consider "at bottom"
    let _scrollRafPending = false;

    messagesDiv.addEventListener('scroll', function() {
        var distFromBottom = messagesDiv.scrollHeight - messagesDiv.scrollTop - messagesDiv.clientHeight;
        _userScrolledUp = distFromBottom > _scrollThreshold;
    });

    // --- Per-message action buttons (copy / fork) ---
    function _extractMessageText(msgEl) {
        var parts = [];
        // Walk direct/nested children in order: .content and .tool-details
        var nodes = msgEl.querySelectorAll(':scope > .content, :scope > .tool-details');
        for (var i = 0; i < nodes.length; i++) {
            var n = nodes[i];
            if (n.classList.contains('content')) {
                var t = n.textContent.trim();
                if (t) parts.push(t);
            } else if (n.classList.contains('tool-details')) {
                var summary = n.querySelector(':scope > summary');
                var args = n.querySelector('.tool-args');
                var result = n.querySelector('.tool-result-text');
                if (summary) parts.push('tool: ' + summary.textContent.trim());
                if (args && args.textContent.trim()) parts.push('args:\n' + args.textContent.trim());
                if (result && result.textContent.trim()) parts.push('result:\n' + result.textContent.trim());
            }
        }
        return parts.join('\n\n');
    }
    async function _copyText(text) {
        if (navigator.clipboard && window.isSecureContext) {
            await navigator.clipboard.writeText(text);
            return;
        }
        // Fallback for non-secure contexts (e.g. remote LAN IP)
        var ta = document.createElement('textarea');
        ta.value = text;
        ta.setAttribute('readonly', '');
        ta.style.position = 'absolute';
        ta.style.left = '-9999px';
        document.body.appendChild(ta);
        ta.select();
        try { document.execCommand('copy'); } finally { document.body.removeChild(ta); }
    }
    async function _handleCopy(msgEl, btn) {
        var text = _extractMessageText(msgEl);
        if (!text) return;
        try {
            await _copyText(text);
            var prev = btn.innerHTML;
            btn.innerHTML = _CHECK_SVG;
            btn.classList.add('copied');
            setTimeout(function() {
                btn.innerHTML = prev;
                btn.classList.remove('copied');
            }, 1200);
        } catch (e) {
            var err = addMessage('system', 'Copy failed: ' + (e && e.message ? e.message : e), false);
            err.classList.add('error');
        }
    }
    async function _handleFork(msgEl) {
        var id = msgEl.dataset.messageId || '';
        if (!id) {
            addMessage('system', 'Message not ready yet — try again in a moment.', false);
            return;
        }
        var partId = msgEl.dataset.lastPartId || '';
        var body = {up_to_message_id: id};
        if (partId) body.up_to_part_id = partId;
        try {
            var res = await fetch('/api/sessions/' + sessionId + '/fork', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(body),
            });
            if (!res.ok) {
                var msg = 'Fork failed (' + res.status + ')';
                try { var d = await res.json(); if (d && d.error) msg = 'Fork failed: ' + d.error; } catch (e) {}
                var err = addMessage('system', msg, false);
                err.classList.add('error');
                return;
            }
            var data = await res.json();
            window.location.href = '/session/' + data.id;
        } catch (e) {
            var err2 = addMessage('system', 'Fork failed: ' + (e && e.message ? e.message : e), false);
            err2.classList.add('error');
        }
    }
    messagesDiv.addEventListener('click', function(e) {
        var copyBtn = e.target.closest && e.target.closest('.msg-copy');
        var forkBtn = e.target.closest && e.target.closest('.msg-fork');
        if (!copyBtn && !forkBtn) return;
        var msgEl = (copyBtn || forkBtn).closest('.message');
        if (!msgEl) return;
        if (copyBtn) _handleCopy(msgEl, copyBtn);
        else _handleFork(msgEl);
    });

    function scrollToBottom() {
        if (_userScrolledUp) return;
        if (_scrollRafPending) return;
        _scrollRafPending = true;
        requestAnimationFrame(function() {
            _scrollRafPending = false;
            messagesDiv.scrollTop = messagesDiv.scrollHeight;
        });
    }

    // --- Prompt history (up/down arrow, persisted in localStorage) ---
    var _historyKey = 'openclose_prompt_history';
    var _promptHistory = JSON.parse(localStorage.getItem(_historyKey) || '[]');
    var _historyIndex = -1;
    var _historyDraft = '';

    // --- Slash commands ---
    const COMMANDS = [
        {name: '/new',      description: 'Start a new session'},
        {name: '/sessions', description: 'Switch to another session'},
        {name: '/rename',   description: 'Rename this session'},
        {name: '/agents',   description: 'Switch agent'},
        {name: '/model',    description: 'Switch provider / model'},
        {name: '/compact',  description: 'Compress context window'},
        {name: '/undo',     description: 'Remove last message pair'},
        {name: '/export',   description: 'Export session transcript'},
        {name: '/copy',     description: 'Copy last response to clipboard'},
        {name: '/auto_approve', description: 'Toggle auto-approve for all tool calls'},
        {name: '/read_plan_file', description: 'Toggle plan file in/out of model context'},
        {name: '/video_compatible', description: 'Toggle whether the main LLM is video-capable (gates the Record button)'},
        {name: '/help',     description: 'Show available commands'},
    ];

    const cmdBar = document.getElementById('command-bar');

    function showCommandBar(filter) {
        const q = filter.toLowerCase();
        const matches = COMMANDS.filter(c => c.name.includes(q));
        if (matches.length === 0) { hideCommandBar(); return; }
        cmdBar.innerHTML = '';
        matches.forEach((cmd, i) => {
            const div = document.createElement('div');
            div.className = 'cmd-item' + (i === 0 ? ' active' : '');
            div.innerHTML = '<span class="cmd-name">' + cmd.name + '</span><span class="cmd-desc">' + cmd.description + '</span>';
            div.addEventListener('click', () => { executeCommand(cmd.name); });
            cmdBar.appendChild(div);
        });
        cmdBar.style.display = 'block';
    }

    function hideCommandBar() {
        cmdBar.style.display = 'none';
        cmdBar.innerHTML = '';
    }

    function getActiveCommand() {
        const active = cmdBar.querySelector('.cmd-item.active');
        if (!active) return null;
        return active.querySelector('.cmd-name').textContent;
    }

    function moveCommandSelection(dir) {
        const items = cmdBar.querySelectorAll('.cmd-item');
        if (items.length === 0) return;
        let idx = -1;
        items.forEach((el, i) => { if (el.classList.contains('active')) idx = i; });
        items.forEach(el => el.classList.remove('active'));
        idx = (idx + dir + items.length) % items.length;
        items[idx].classList.add('active');
        items[idx].scrollIntoView({block: 'nearest'});
    }

    async function executeCommand(name) {
        hideCommandBar();
        input.value = '';

        switch (name) {
        case '/new': {
            const res = await fetch('/api/sessions', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({title: ''})
            });
            const data = await res.json();
            window.location.href = '/session/' + data.id;
            break;
        }
        case '/sessions': {
            const res = await fetch('/api/sessions');
            const sessions = await res.json();
            showSessionPicker(sessions);
            break;
        }
        case '/rename': {
            const title = prompt('New session title:');
            if (title) {
                await fetch('/api/sessions/' + sessionId + '/rename', {
                    method: 'PATCH',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({title: title})
                });
                document.getElementById('session-title').textContent = title;
            }
            break;
        }
        case '/agents': {
            const res = await fetch('/api/agents');
            const agents = await res.json();
            showAgentPicker(agents);
            break;
        }
        case '/model': {
            const res = await fetch('/api/models');
            const items = await res.json();
            showModelPicker(items);
            break;
        }
        case '/compact': {
            const res = await fetch('/api/sessions/' + sessionId + '/compact', {method: 'POST'});
            const data = await res.json();
            addMessage('system', data.compacted ? 'Context compacted.' : 'No compaction needed.', false);
            break;
        }
        case '/undo': {
            await fetch('/api/sessions/' + sessionId + '/undo', {method: 'POST'});
            window.location.reload();
            break;
        }
        case '/export': {
            const res = await fetch('/api/sessions/' + sessionId + '/export');
            const data = await res.json();
            const blob = new Blob([JSON.stringify(data, null, 2)], {type: 'application/json'});
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = 'session-' + sessionId + '.json';
            a.click();
            URL.revokeObjectURL(url);
            break;
        }
        case '/copy': {
            const msgs = document.querySelectorAll('.message.assistant .content');
            if (msgs.length > 0) {
                const last = msgs[msgs.length - 1].textContent;
                await navigator.clipboard.writeText(last);
                addMessage('system', 'Copied to clipboard.', false);
            }
            break;
        }
        case '/auto_approve': {
            const res = await fetch('/api/sessions/' + sessionId + '/skip-permissions', {method: 'POST'});
            const data = await res.json();
            addMessage('system', data.skip_all ? 'Auto-approve: ON' : 'Auto-approve: OFF', false);
            updateSkipButton(data.skip_all);
            break;
        }
        case '/read_plan_file': {
            const res = await fetch('/api/sessions/' + sessionId + '/plan-in-context', {method: 'POST'});
            const data = await res.json();
            addMessage('system', data.plan_in_context ? 'Read Plan File: ON' : 'Read Plan File: OFF', false);
            updatePlanToggle(data.plan_in_context);
            break;
        }
        case '/video_compatible': {
            const res = await fetch('/api/sessions/' + sessionId + '/video-compatible', {method: 'POST'});
            const data = await res.json();
            addMessage('system', data.video_compatible ? 'Video Compatible Model: ON (Record button enabled)' : 'Video Compatible Model: OFF (Record button disabled)', false);
            updateVideoCompatibleButton(data.video_compatible);
            break;
        }
        case '/help':
            showHelp();
            break;
        }
        // Don't steal focus from a picker dialog the command just opened
        if (!document.querySelector('.picker-overlay')) {
            input.focus();
        }
    }

    function showHelp() {
        let text = 'Available commands:\n';
        COMMANDS.forEach(c => { text += '  ' + c.name.padEnd(12) + ' — ' + c.description + '\n'; });
        addMessage('system', text, false);
    }

    function attachPickerKeyboardNav(overlay, picker) {
        const items = picker.querySelectorAll('.picker-item');
        if (items.length === 0) return;
        let idx = 0;
        items[0].classList.add('active');
        function move(dir) {
            items[idx].classList.remove('active');
            idx = (idx + dir + items.length) % items.length;
            items[idx].classList.add('active');
            items[idx].scrollIntoView({block: 'nearest'});
        }
        overlay.tabIndex = -1;
        overlay.addEventListener('keydown', (e) => {
            if (e.key === 'ArrowDown') { e.preventDefault(); move(1); }
            else if (e.key === 'ArrowUp') { e.preventDefault(); move(-1); }
            else if (e.key === 'Enter')   { e.preventDefault(); items[idx].click(); }
            else if (e.key === 'Escape')  { e.preventDefault(); overlay.remove(); input.focus(); }
        });
        overlay.focus();
    }

    // Keyboard navigation for modal popups built on .permission-overlay /
    // .permission-dialog. Pre-focuses the first action button, traps Tab
    // inside the dialog, and closes on Escape without sending a backend reply.
    function attachDialogKeyboardNav(overlay, dialog, opts) {
        opts = opts || {};
        const focusableSelector =
            'button:not([disabled]), [href], input:not([disabled]), ' +
            'select:not([disabled]), textarea:not([disabled]), ' +
            '[tabindex]:not([tabindex="-1"])';

        function focusables() {
            return Array.from(dialog.querySelectorAll(focusableSelector))
                .filter(el => el.offsetParent !== null);
        }

        const initial = opts.initialFocus
            || dialog.querySelector('.permission-actions button:not([disabled])')
            || focusables()[0];
        if (initial) initial.focus();

        overlay.addEventListener('keydown', function(e) {
            if (e.key === 'Escape') {
                e.preventDefault();
                overlay.remove();
                if (typeof opts.onEscape === 'function') opts.onEscape();
                return;
            }
            if (e.key !== 'Tab') return;
            const list = focusables();
            if (list.length === 0) return;
            const first = list[0];
            const last = list[list.length - 1];
            if (e.shiftKey && document.activeElement === first) {
                e.preventDefault();
                last.focus();
            } else if (!e.shiftKey && document.activeElement === last) {
                e.preventDefault();
                first.focus();
            }
        });
    }

    function showSessionPicker(sessions) {
        const overlay = document.createElement('div');
        overlay.className = 'picker-overlay';
        const picker = document.createElement('div');
        picker.className = 'picker';
        picker.innerHTML = '<div class="picker-title">Sessions</div>';
        sessions.forEach(s => {
            const row = document.createElement('a');
            row.className = 'picker-item';
            row.href = '/session/' + s.id;
            row.textContent = (s.title || 'Untitled') + '  [' + s.agent + ']';
            picker.appendChild(row);
        });
        overlay.appendChild(picker);
        overlay.addEventListener('click', (e) => { if (e.target === overlay) { overlay.remove(); input.focus(); } });
        document.body.appendChild(overlay);
        attachPickerKeyboardNav(overlay, picker);
    }

    function showAgentPicker(agents) {
        const overlay = document.createElement('div');
        overlay.className = 'picker-overlay';
        const picker = document.createElement('div');
        picker.className = 'picker';
        picker.innerHTML = '<div class="picker-title">Agents</div>';
        agents.forEach(a => {
            const row = document.createElement('div');
            row.className = 'picker-item';
            row.textContent = a.name + (a.description ? ' — ' + a.description : '');
            row.addEventListener('click', async () => {
                // Fork current session with selected agent, preserving history
                const res = await fetch('/api/sessions/' + sessionId + '/fork', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({agent: a.name})
                });
                const data = await res.json();
                window.location.href = '/session/' + data.id;
            });
            picker.appendChild(row);
        });
        overlay.appendChild(picker);
        overlay.addEventListener('click', (e) => { if (e.target === overlay) { overlay.remove(); input.focus(); } });
        document.body.appendChild(overlay);
        attachPickerKeyboardNav(overlay, picker);
    }

    async function refreshActiveModelLabel() {
        const el = document.getElementById('active-model-label');
        if (!el) return;
        try {
            const res = await fetch('/api/sessions/' + sessionId + '/model');
            if (!res.ok) { el.textContent = ''; return; }
            const data = await res.json();
            const p = data.effective_provider || '';
            const m = data.effective_model || '';
            el.textContent = (p && m) ? (p + ' / ' + m) : (p || m || '');
        } catch (_e) {
            el.textContent = '';
        }
    }

    function showModelPicker(items) {
        const overlay = document.createElement('div');
        overlay.className = 'picker-overlay';
        const picker = document.createElement('div');
        picker.className = 'picker';
        picker.innerHTML = '<div class="picker-title">Provider / model</div>';
        if (!items || items.length === 0) {
            const empty = document.createElement('div');
            empty.className = 'picker-item';
            empty.textContent = 'No models declared. Edit your config.toml to add [[providers]].';
            picker.appendChild(empty);
        } else {
            items.forEach(it => {
                const row = document.createElement('div');
                row.className = 'picker-item';
                row.textContent = it.label || (it.provider + ' / ' + it.model);
                row.addEventListener('click', async () => {
                    const res = await fetch('/api/sessions/' + sessionId + '/model', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({provider: it.provider, model: it.model})
                    });
                    overlay.remove();
                    if (res.ok) {
                        refreshActiveModelLabel();
                        addMessage('system', 'Switched to ' + it.provider + ' / ' + it.model + '.', false);
                    } else {
                        const err = await res.json().catch(() => ({error: 'switch failed'}));
                        addMessage('system', 'Switch failed: ' + (err.error || 'unknown'), false);
                    }
                    input.focus();
                });
                picker.appendChild(row);
            });
        }
        overlay.appendChild(picker);
        overlay.addEventListener('click', (e) => { if (e.target === overlay) { overlay.remove(); input.focus(); } });
        document.body.appendChild(overlay);
        attachPickerKeyboardNav(overlay, picker);
    }

    // Populate the active-model label on page load. Fire-and-forget — the
    // label is purely informational and stays empty if the request fails.
    refreshActiveModelLabel();

    // --- Markdown rendering ---
    function renderMarkdown(text) {
        if (typeof marked !== 'undefined') {
            return marked.parse(text);
        }
        return text.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/\n/g,'<br>');
    }

    // Inline SVGs for per-message action buttons. Heroicons-style 20x20.
    const _COPY_SVG = '<svg viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">' +
        '<path d="M7 3.5A1.5 1.5 0 0 1 8.5 2h3.879a1.5 1.5 0 0 1 1.06.44l3.122 3.12A1.5 1.5 0 0 1 17 6.622V12.5a1.5 1.5 0 0 1-1.5 1.5h-1v-3.379a3 3 0 0 0-.879-2.121L10.5 5.379A3 3 0 0 0 8.379 4.5H7v-1Z"/>' +
        '<path d="M4.5 6A1.5 1.5 0 0 0 3 7.5v9A1.5 1.5 0 0 0 4.5 18h7a1.5 1.5 0 0 0 1.5-1.5v-5.879a1.5 1.5 0 0 0-.44-1.06L9.44 6.439A1.5 1.5 0 0 0 8.378 6H4.5Z"/></svg>';
    const _CHECK_SVG = '<svg viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">' +
        '<path fill-rule="evenodd" d="M16.704 5.29a1 1 0 0 1 .006 1.414l-8 8.084a1 1 0 0 1-1.42.006l-4-4a1 1 0 1 1 1.414-1.414l3.293 3.293 7.293-7.377a1 1 0 0 1 1.414-.006Z" clip-rule="evenodd"/></svg>';
    const _FORK_SVG = '<svg viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">' +
        '<path d="M6 4.5a1.5 1.5 0 1 0-2.25 1.3v8.4a1.5 1.5 0 1 0 1.5 0v-3.05a3 3 0 0 0 2.4 1.35h2.6v2.05a1.5 1.5 0 1 0 1.5 0V11a2.5 2.5 0 0 0-2.5-2.5h-1.6a1.5 1.5 0 0 1-1.4-.96V5.8A1.5 1.5 0 0 0 6 4.5Z"/></svg>';

    function buildAssistantHeader() {
        const bar = document.createElement('div');
        bar.className = 'message-actions';
        bar.setAttribute('role', 'toolbar');
        bar.setAttribute('aria-label', 'Message actions');
        const copyBtn = document.createElement('button');
        copyBtn.type = 'button';
        copyBtn.className = 'msg-action msg-copy';
        copyBtn.title = 'Copy message';
        copyBtn.setAttribute('aria-label', 'Copy message');
        copyBtn.innerHTML = _COPY_SVG;
        const forkBtn = document.createElement('button');
        forkBtn.type = 'button';
        forkBtn.className = 'msg-action msg-fork';
        forkBtn.title = 'Fork from here';
        forkBtn.setAttribute('aria-label', 'Fork conversation from this message');
        forkBtn.innerHTML = _FORK_SVG;
        bar.appendChild(copyBtn);
        bar.appendChild(forkBtn);
        return bar;
    }

    function addMessage(role, content, useMarkdown, messageId) {
        const div = document.createElement('div');
        div.className = 'message ' + role;
        if (messageId) div.dataset.messageId = messageId;
        if (role === 'assistant') {
            div.appendChild(buildAssistantHeader());
        } else {
            const roleEl = document.createElement('div');
            roleEl.className = 'role';
            roleEl.textContent = role;
            div.appendChild(roleEl);
        }
        const contentEl = document.createElement('div');
        contentEl.className = 'content';
        if (useMarkdown) {
            contentEl.innerHTML = renderMarkdown(content);
        } else {
            contentEl.textContent = content;
        }
        div.appendChild(contentEl);
        messagesDiv.appendChild(div);
        scrollToBottom();
        return div;
    }

    function addToolDetails(parentDiv, toolName, toolArgs) {
        const details = document.createElement('details');
        details.className = 'tool-details';
        const summary = document.createElement('summary');
        summary.textContent = toolName;
        details.appendChild(summary);
        const body = document.createElement('div');
        body.className = 'tool-body';
        if (toolArgs && Object.keys(toolArgs).length > 0) {
            const argsDiv = document.createElement('div');
            argsDiv.className = 'tool-args';
            argsDiv.textContent = JSON.stringify(toolArgs, null, 2);
            body.appendChild(argsDiv);
        }
        details.appendChild(body);
        parentDiv.appendChild(details);
        scrollToBottom();
        return details;
    }

    // --- Subagent step rendering ---
    // Build the visible summary for a sub-agent group: a snippet of the
    // mission text + position (e.g. "1/3") + tool-call count. Falls back to
    // the raw label for non-delegate sub-agents (e.g. "Planner") that have
    // no mission_text.
    function formatMissionDisplay(missionText, label, count) {
        var countPart = (count != null) ? ' (' + count + ')' : '';
        if (missionText) {
            var s = String(missionText).replace(/\s+/g, ' ').trim();
            var snippet = s.length > 50 ? s.slice(0, 50) + '...' : s;
            var posMatch = label && label.match(/^Mission (\d+\/\d+)$/);
            if (posMatch) {
                return snippet + ' ' + posMatch[1] + countPart;
            }
            return snippet + countPart;
        }
        return (label || 'Sub-agent') + countPart;
    }
    function renderSubagentSteps(container, steps) {
        if (!steps || steps.length === 0) return;
        steps.forEach(function(step) {
            if (step.type === 'tool_call') {
                var details = document.createElement('details');
                details.className = 'tool-details subagent-tool';
                var summary = document.createElement('summary');
                summary.textContent = step.tool_name || 'tool';
                details.appendChild(summary);
                var body = document.createElement('div');
                body.className = 'tool-body';
                if (step.content) {
                    var argsDiv = document.createElement('div');
                    argsDiv.className = 'tool-args';
                    try {
                        argsDiv.textContent = JSON.stringify(JSON.parse(step.content), null, 2);
                    } catch(e) {
                        argsDiv.textContent = step.content;
                    }
                    body.appendChild(argsDiv);
                }
                details.appendChild(body);
                details.dataset.toolCallId = step.tool_call_id || '';
                container.appendChild(details);
            } else if (step.type === 'tool_result') {
                var match = container.querySelector(
                    '[data-tool-call-id="' + (step.tool_call_id || '') + '"]'
                );
                if (match) {
                    var resultDiv = document.createElement('div');
                    resultDiv.className = 'tool-result-text';
                    resultDiv.textContent = step.content || '';
                    match.querySelector('.tool-body').appendChild(resultDiv);
                }
            } else if (step.type === 'text') {
                var textDiv = document.createElement('div');
                textDiv.className = 'subagent-text';
                textDiv.textContent = step.content;
                container.appendChild(textDiv);
            }
        });
    }
    // Group steps by subagent_label and render each group in its own section.
    // `perMission` (optional) is the metadata.per_mission array — used to
    // pull mission_text and tool_call_count for the summary line; without
    // it we fall back to the raw label and the (less precise) group length.
    function renderSubagentStepsByLabel(parentBody, steps, perMission) {
        var missionTextByLabel = {};
        var tcCountByLabel = {};
        if (perMission && perMission.length) {
            perMission.forEach(function(pm) {
                if (pm && pm.label) {
                    missionTextByLabel[pm.label] = pm.mission_text || '';
                    tcCountByLabel[pm.label] = pm.tool_call_count;
                }
            });
        }
        // Group by label
        var groups = {};
        var order = [];
        steps.forEach(function(step) {
            var lbl = step.subagent_label || 'Sub-agent';
            if (!groups[lbl]) { groups[lbl] = []; order.push(lbl); }
            groups[lbl].push(step);
        });
        order.forEach(function(lbl) {
            var stepsDetails = document.createElement('details');
            stepsDetails.className = 'subagent-steps';
            stepsDetails.dataset.subagentLabel = lbl;
            var mt = missionTextByLabel[lbl] || '';
            if (mt) stepsDetails.dataset.missionText = mt;
            var count = (tcCountByLabel[lbl] != null)
                ? tcCountByLabel[lbl]
                : groups[lbl].length;
            var stepsSummary = document.createElement('summary');
            stepsSummary.textContent = formatMissionDisplay(mt, lbl, count);
            stepsDetails.appendChild(stepsSummary);
            var stepsBody = document.createElement('div');
            stepsBody.className = 'subagent-steps-body';
            renderSubagentSteps(stepsBody, groups[lbl]);
            stepsDetails.appendChild(stepsBody);
            parentBody.appendChild(stepsDetails);
        });
    }
    // Make available globally for page-reload hydration
    window.renderSubagentSteps = renderSubagentSteps;
    window.renderSubagentStepsByLabel = renderSubagentStepsByLabel;

    // --- Permission dialog ---
    function showPermissionDialog(data) {
        var info;
        try { info = JSON.parse(data.content); } catch (e) { info = data; }
        var requestId = info.request_id || '';
        var toolName = info.tool_name || 'unknown';
        var path = info.path || '';
        var toolArgs = info.tool_args || {};

        var overlay = document.createElement('div');
        overlay.className = 'permission-overlay';

        var dialog = document.createElement('div');
        dialog.className = 'permission-dialog';

        var title = document.createElement('div');
        title.className = 'permission-title';
        title.textContent = toolName === 'doom_loop'
            ? 'Repeated tool call detected'
            : 'Permission Required';
        dialog.appendChild(title);

        var desc = document.createElement('div');
        desc.className = 'permission-desc';
        if (toolName === 'doom_loop') {
            desc.textContent = 'The same tool call has been repeated 3 times with identical arguments. Continue?';
        } else {
            desc.innerHTML = '<strong>Tool:</strong> ' + toolName
                + (path && path !== '*' ? '<br><strong>Path:</strong> ' + path : '')
                + '<br><strong>Args:</strong> <code>' + JSON.stringify(toolArgs, null, 2) + '</code>';
        }
        dialog.appendChild(desc);

        var actions = document.createElement('div');
        actions.className = 'permission-actions';

        function makeBtn(label, reply, cls) {
            var btn = document.createElement('button');
            btn.className = 'btn ' + cls;
            btn.textContent = label;
            btn.addEventListener('click', function() {
                fetch('/api/permissions/' + requestId + '/reply', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({reply: reply})
                });
                overlay.remove();
            });
            return btn;
        }

        actions.appendChild(makeBtn('Allow once', 'once', 'btn-primary'));
        actions.appendChild(makeBtn('Allow always', 'always', 'btn-outline'));
        actions.appendChild(makeBtn('Reject', 'reject', 'btn-danger'));
        dialog.appendChild(actions);

        overlay.appendChild(dialog);
        document.body.appendChild(overlay);
        attachDialogKeyboardNav(overlay, dialog);
    }

    // --- Plan review dialog ---
    function showPlanReviewDialog(data, onReviewed) {
        var info;
        try { info = JSON.parse(data.content); } catch (e) { info = data; }
        var requestId = info.request_id || '';
        var planContent = info.plan_content || '';

        var overlay = document.createElement('div');
        overlay.className = 'permission-overlay';

        var dialog = document.createElement('div');
        dialog.className = 'permission-dialog plan-dialog';

        var title = document.createElement('div');
        title.className = 'permission-title';
        title.textContent = 'Plan Review';
        dialog.appendChild(title);

        var preview = document.createElement('div');
        preview.className = 'plan-content-preview';
        preview.innerHTML = typeof marked !== 'undefined' ? marked.parse(planContent) : planContent.replace(/\n/g, '<br>');
        dialog.appendChild(preview);

        var textarea = document.createElement('textarea');
        textarea.className = 'question-textarea plan-feedback-textarea';
        textarea.placeholder = 'Provide feedback for revision…';
        textarea.rows = 3;
        dialog.appendChild(textarea);

        var actions = document.createElement('div');
        actions.className = 'permission-actions';

        function replyPlan(action, feedback) {
            fetch('/api/plan/' + requestId + '/reply', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({action: action, feedback: feedback || ''})
            });
            overlay.remove();
            if (typeof onReviewed === 'function') {
                onReviewed(action);
            }
        }

        var execBtn = document.createElement('button');
        execBtn.className = 'btn btn-primary';
        execBtn.textContent = 'Execute';
        execBtn.addEventListener('click', function() { replyPlan('execute'); });

        var execClearBtn = document.createElement('button');
        execClearBtn.className = 'btn btn-outline';
        execClearBtn.textContent = 'Accept & Clear';
        execClearBtn.title = 'Accept plan and start a fresh build session';
        execClearBtn.addEventListener('click', function() { replyPlan('execute_clear'); });

        var rejectBtn = document.createElement('button');
        rejectBtn.className = 'btn btn-danger';
        rejectBtn.textContent = 'Reject';
        rejectBtn.addEventListener('click', function() { replyPlan('reject'); });

        var feedbackBtn = document.createElement('button');
        feedbackBtn.className = 'btn btn-outline';
        feedbackBtn.textContent = 'Send Feedback';
        feedbackBtn.addEventListener('click', function() {
            var feedback = textarea.value.trim();
            if (!feedback) { textarea.focus(); return; }
            replyPlan('revise', feedback);
        });

        actions.appendChild(execBtn);
        actions.appendChild(execClearBtn);
        actions.appendChild(feedbackBtn);
        actions.appendChild(rejectBtn);
        dialog.appendChild(actions);

        overlay.appendChild(dialog);
        document.body.appendChild(overlay);
        attachDialogKeyboardNav(overlay, dialog);
    }

    // --- Ask user dialog ---
    function showAskUserDialog(data) {
        var info;
        try { info = JSON.parse(data.content); } catch (e) { info = data; }
        var requestId = info.request_id || '';
        var questions = info.questions || [];
        // Handle double-encoded questions (string instead of array)
        if (typeof questions === 'string') {
            try { questions = JSON.parse(questions); } catch(e) { questions = []; }
        }
        var answers = new Array(questions.length).fill(null);

        var overlay = document.createElement('div');
        overlay.className = 'permission-overlay';

        var dialog = document.createElement('div');
        dialog.className = 'permission-dialog ask-user-dialog';

        var title = document.createElement('div');
        title.className = 'permission-title';
        title.textContent = 'Agent has questions';
        dialog.appendChild(title);

        var questionsContainer = document.createElement('div');
        questionsContainer.className = 'ask-user-questions';

        questions.forEach(function(q, idx) {
            var qDiv = document.createElement('div');
            qDiv.className = 'ask-user-question';

            var qText = document.createElement('div');
            qText.className = 'ask-user-question-text';
            qText.textContent = (idx + 1) + '. ' + q.question;
            qDiv.appendChild(qText);

            var choicesDiv = document.createElement('div');
            choicesDiv.className = 'ask-user-choices';

            q.choices.forEach(function(choice) {
                var btn = document.createElement('button');
                btn.className = 'btn btn-outline';
                btn.textContent = choice;
                btn.addEventListener('click', function() {
                    choicesDiv.querySelectorAll('.btn').forEach(function(b) {
                        b.className = 'btn btn-outline';
                    });
                    btn.className = 'btn btn-primary';
                    answers[idx] = choice;
                    checkAllAnswered();
                });
                choicesDiv.appendChild(btn);
            });

            qDiv.appendChild(choicesDiv);
            questionsContainer.appendChild(qDiv);
        });

        dialog.appendChild(questionsContainer);

        var actions = document.createElement('div');
        actions.className = 'permission-actions';

        var submitBtn = document.createElement('button');
        submitBtn.className = 'btn btn-primary';
        submitBtn.textContent = 'Submit Answers';
        submitBtn.disabled = true;

        function checkAllAnswered() {
            submitBtn.disabled = answers.some(function(a) { return a === null; });
        }

        submitBtn.addEventListener('click', function() {
            var payload = questions.map(function(q, idx) {
                return { question: q.question, answer: answers[idx] };
            });
            fetch('/api/ask-user/' + requestId + '/reply', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ answers: payload })
            });
            overlay.remove();
        });

        actions.appendChild(submitBtn);
        dialog.appendChild(actions);

        overlay.appendChild(dialog);
        document.body.appendChild(overlay);
        const firstChoice = questionsContainer.querySelector('button');
        attachDialogKeyboardNav(overlay, dialog, { initialFocus: firstChoice });
    }

    // --- Interrupt / button state ---
    let currentAbort = null;

    function showInterruptButton() {
        sendBtn.textContent = 'Stop';
        sendBtn.classList.remove('btn-primary');
        sendBtn.classList.add('btn-danger');
        sendBtn.disabled = false;
        sendBtn.removeEventListener('click', sendMessage);
        sendBtn.addEventListener('click', interruptGeneration);
    }

    function showSendButton() {
        sendBtn.textContent = 'Send';
        sendBtn.classList.remove('btn-danger');
        sendBtn.classList.add('btn-primary');
        sendBtn.disabled = false;
        sendBtn.removeEventListener('click', interruptGeneration);
        sendBtn.addEventListener('click', sendMessage);
    }

    async function interruptGeneration() {
        if (currentAbort) {
            currentAbort.abort();
            currentAbort = null;
        }
        try {
            await fetch('/api/sessions/' + sessionId + '/interrupt', {method: 'POST'});
        } catch (e) { /* ignore */ }
        showSendButton();
    }

    // --- Send message / stream ---
    async function sendMessage() {
        const text = input.value.trim();
        if (!text) return;
        _promptHistory.push(text);
        localStorage.setItem(_historyKey, JSON.stringify(_promptHistory));
        _historyIndex = -1;
        _historyDraft = '';

        // Handle ! bash commands
        if (text.startsWith('!')) {
            const cmd = text.substring(1).trim();
            if (cmd) {
                input.value = '';
                addMessage('user', text, false);
                const bashDiv = addMessage('system', 'Running...', false);
                bashDiv.querySelector('.content').className = 'content bash-output';
                try {
                    const res = await fetch('/api/bash', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({command: cmd})
                    });
                    const data = await res.json();
                    let output = data.stdout || '';
                    if (data.stderr) output += (output ? '\n' : '') + data.stderr;
                    if (data.returncode !== 0) output += '\n[exit code: ' + data.returncode + ']';
                    bashDiv.querySelector('.content').textContent = output || '(no output)';
                } catch (err) {
                    bashDiv.querySelector('.content').textContent = 'Error: ' + err.message;
                }
                input.focus();
                return;
            }
        }

        // Handle slash commands
        if (text.startsWith('/')) {
            const cmd = COMMANDS.find(c => c.name === text.split(' ')[0]);
            if (cmd) {
                await executeCommand(cmd.name);
                return;
            }
        }

        input.value = '';
        showInterruptButton();
        addMessage('user', text, false);

        let assistantDiv = null;
        let contentDiv = null;
        let rawText = '';
        let currentTurnMessageId = '';
        const pendingTools = {};
        const pendingSubagentTools = {};
        let subagentStepCount = {};  // parent_tool_call_id -> count
        let lastPlanToolEl = null;
        let lastPlanToolCallId = null;

        function ensureAssistantDiv() {
            if (!assistantDiv || assistantDiv.dataset.closed === '1') {
                assistantDiv = document.createElement('div');
                assistantDiv.className = 'message assistant streaming';
                assistantDiv.appendChild(buildAssistantHeader());
                contentDiv = document.createElement('div');
                contentDiv.className = 'content';
                assistantDiv.appendChild(contentDiv);
                if (currentTurnMessageId) {
                    assistantDiv.dataset.messageId = currentTurnMessageId;
                }
                rawText = '';
                messagesDiv.appendChild(assistantDiv);
                scrollToBottom();
            }
        }

        function finalizeAssistant() {
            if (assistantDiv && contentDiv && rawText) {
                contentDiv.innerHTML = renderMarkdown(rawText);
            }
            if (assistantDiv) {
                assistantDiv.classList.remove('streaming');
            }
            // Reset scroll lock so next generation auto-scrolls again
            _userScrolledUp = false;
        }

        function ensureSubagentContainer(parentEl, label, missionText) {
            var body = parentEl.querySelector('.tool-body');
            if (!body) return null;
            label = label || 'Sub-agent';
            // Find existing container with this label
            var containers = body.querySelectorAll('.subagent-steps');
            for (var i = 0; i < containers.length; i++) {
                if (containers[i].dataset.subagentLabel === label) {
                    if (missionText && !containers[i].dataset.missionText) {
                        containers[i].dataset.missionText = missionText;
                    }
                    return containers[i].querySelector('.subagent-steps-body');
                }
            }
            // Create new container for this label
            var stepsDetails = document.createElement('details');
            stepsDetails.className = 'subagent-steps';
            stepsDetails.open = true;
            stepsDetails.dataset.subagentLabel = label;
            if (missionText) stepsDetails.dataset.missionText = missionText;
            var stepsSummary = document.createElement('summary');
            stepsSummary.textContent = formatMissionDisplay(missionText, label, null) + '...';
            stepsDetails.appendChild(stepsSummary);
            var stepsBody = document.createElement('div');
            stepsBody.className = 'subagent-steps-body';
            stepsDetails.appendChild(stepsBody);
            body.appendChild(stepsDetails);
            return stepsBody;
        }

        try {
            currentAbort = new AbortController();
            const _attachmentsForTurn = [..._attachedExcerpts.values()];
            const response = await fetch('/api/sessions/' + sessionId + '/messages', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({content: text, attachments: _attachmentsForTurn}),
                signal: currentAbort.signal,
            });
            // Attachments are consumed by this turn only.
            _attachedExcerpts.clear();
            renderAttachedChips();

            const reader = response.body.getReader();
            const decoder = new TextDecoder();
            let buffer = '';

            while (true) {
                const {done, value} = await reader.read();
                if (done) break;

                buffer += decoder.decode(value, {stream: true});
                const lines = buffer.split('\n');
                buffer = lines.pop() || '';

                for (const line of lines) {
                    if (!line.startsWith('data: ')) continue;
                    try {
                        const data = JSON.parse(line.slice(6));

                        if (data.type === 'message_start') {
                            if (data.message_id) {
                                currentTurnMessageId = data.message_id;
                                if (assistantDiv && !assistantDiv.dataset.messageId) {
                                    assistantDiv.dataset.messageId = data.message_id;
                                }
                            }

                        } else if (data.type === 'part_persisted') {
                            // A part (typically a flushed TEXT part) was just
                            // persisted; record its id on the current open
                            // bubble so fork-from-streaming targets the right
                            // segment boundary.
                            if (data.part_id && assistantDiv && assistantDiv.dataset.closed !== '1') {
                                assistantDiv.dataset.lastPartId = data.part_id;
                            }

                        } else if (data.type === 'text') {
                            ensureAssistantDiv();
                            rawText += data.content;
                            // Append text instead of replacing entire content
                            contentDiv.appendChild(document.createTextNode(data.content));
                            scrollToBottom();

                        } else if (data.type === 'tool_call') {
                            if (rawText && contentDiv) {
                                contentDiv.innerHTML = renderMarkdown(rawText);
                            }
                            ensureAssistantDiv();
                            // NOTE: We do NOT stamp lastPartId from tool_call —
                            // a tool_call without its result would orphan in
                            // LLM reconstruction if a fork lands on it.
                            // lastPartId only advances on safe boundaries:
                            // tool_result (handled in that branch below) and
                            // part_persisted (flushed text).
                            const toolEl = addToolDetails(
                                assistantDiv,
                                data.tool_name,
                                data.tool_args
                            );
                            if (data.tool_call_id) {
                                pendingTools[data.tool_call_id] = toolEl;
                            }
                            if (data.tool_name === 'plan') {
                                lastPlanToolEl = toolEl;
                                lastPlanToolCallId = data.tool_call_id;
                            }
                            // Track file changes for sidebar
                            trackFileChange(data.tool_name, data.tool_args, data.tool_call_id);

                        } else if (data.type === 'tool_result') {
                            const target = data.tool_call_id && pendingTools[data.tool_call_id];
                            if (target) {
                                const body = target.querySelector('.tool-body');
                                const resultDiv = document.createElement('div');
                                resultDiv.className = 'tool-result-text';
                                resultDiv.textContent = data.tool_result;
                                body.appendChild(resultDiv);
                                // Hydrate subagent groups from metadata for any label
                                // that streaming didn't already create — sub-agents that
                                // emit only text (no tool calls) wouldn't have a live
                                // container, so without this their group goes missing.
                                if (data.metadata && data.metadata.subagent_steps && data.metadata.subagent_steps.length > 0) {
                                    var existing = {};
                                    body.querySelectorAll('.subagent-steps').forEach(function(el) {
                                        existing[el.dataset.subagentLabel || ''] = true;
                                    });
                                    var missing = data.metadata.subagent_steps.filter(function(s) {
                                        return !existing[s.subagent_label || 'Sub-agent'];
                                    });
                                    if (missing.length > 0) {
                                        renderSubagentStepsByLabel(body, missing, data.metadata.per_mission);
                                    }
                                }
                                // For groups already created by streaming, refresh the summary
                                // line with the authoritative mission_text + tool_call_count
                                // from per_mission (streaming may have used a stale count or
                                // missing mission_text).
                                if (data.metadata && data.metadata.per_mission) {
                                    var pmByLabel = {};
                                    data.metadata.per_mission.forEach(function(pm) {
                                        if (pm && pm.label) pmByLabel[pm.label] = pm;
                                    });
                                    body.querySelectorAll('.subagent-steps').forEach(function(el) {
                                        var lbl = el.dataset.subagentLabel || '';
                                        var pm = pmByLabel[lbl];
                                        if (!pm) return;
                                        if (pm.mission_text) el.dataset.missionText = pm.mission_text;
                                        var sumEl = el.querySelector('summary');
                                        if (sumEl) {
                                            sumEl.textContent = formatMissionDisplay(
                                                pm.mission_text || '',
                                                lbl,
                                                pm.tool_call_count
                                            );
                                        }
                                    });
                                }
                                delete pendingTools[data.tool_call_id];
                            }
                            if (assistantDiv) {
                                if (data.part_id) {
                                    assistantDiv.dataset.lastPartId = data.part_id;
                                }
                                assistantDiv.classList.remove('streaming');
                                assistantDiv.dataset.closed = '1';
                            }

                        } else if (data.type === 'subagent_tool_call') {
                            var parentEl = data.parent_tool_call_id && pendingTools[data.parent_tool_call_id];
                            if (parentEl) {
                                var saLabel = (data.metadata && data.metadata.subagent_label) || 'Sub-agent';
                                var saMissionText = (data.metadata && data.metadata.mission_text) || '';
                                var container = ensureSubagentContainer(parentEl, saLabel, saMissionText);
                                if (container) {
                                    var details = document.createElement('details');
                                    details.className = 'tool-details subagent-tool';
                                    details.dataset.toolCallId = data.tool_call_id || '';
                                    var summary = document.createElement('summary');
                                    summary.textContent = data.tool_name || 'tool';
                                    details.appendChild(summary);
                                    var body = document.createElement('div');
                                    body.className = 'tool-body';
                                    if (data.tool_args && Object.keys(data.tool_args).length > 0) {
                                        var argsDiv = document.createElement('div');
                                        argsDiv.className = 'tool-args';
                                        argsDiv.textContent = JSON.stringify(data.tool_args, null, 2);
                                        body.appendChild(argsDiv);
                                    }
                                    details.appendChild(body);
                                    container.appendChild(details);
                                    pendingSubagentTools[data.tool_call_id] = details;
                                    // Update tool-call count per label
                                    var countKey = data.parent_tool_call_id + '_' + saLabel;
                                    subagentStepCount[countKey] = (subagentStepCount[countKey] || 0) + 1;
                                    var stepsEl = container.parentElement;
                                    var summaryEl = stepsEl && stepsEl.querySelector('summary');
                                    if (summaryEl) {
                                        var mt = (stepsEl && stepsEl.dataset.missionText) || saMissionText;
                                        summaryEl.textContent = formatMissionDisplay(mt, saLabel, subagentStepCount[countKey]);
                                    }
                                    scrollToBottom();
                                }
                            }

                        } else if (data.type === 'subagent_tool_result') {
                            var match = data.tool_call_id && pendingSubagentTools[data.tool_call_id];
                            if (match) {
                                var resultDiv = document.createElement('div');
                                resultDiv.className = 'tool-result-text';
                                resultDiv.textContent = data.tool_result || '';
                                match.querySelector('.tool-body').appendChild(resultDiv);
                                delete pendingSubagentTools[data.tool_call_id];
                            }

                        } else if (data.type === 'subagent_text') {
                            // Subagent thinking text — skip for cleaner UI
                            // (the final report comes in the tool_result)

                        } else if (data.type === 'permission_request') {
                            showPermissionDialog(data);

                        } else if (data.type === 'plan_review_request') {
                            var planInfo;
                            try { planInfo = JSON.parse(data.content); } catch(e) { planInfo = data; }
                            var planMd = planInfo.plan_content || '';
                            var capturedPlanEl = lastPlanToolEl;
                            var capturedPlanId = lastPlanToolCallId;

                            showPlanReviewDialog(data, function(action) {
                                if (capturedPlanEl) {
                                    var planDiv = document.createElement('div');
                                    planDiv.className = 'plan-content-visible';
                                    var header = document.createElement('div');
                                    header.className = 'plan-content-header';
                                    var label = action === 'execute' ? 'Accepted' :
                                                action === 'execute_clear' ? 'Accepted & Cleared' :
                                                action === 'reject' ? 'Rejected' :
                                                action === 'revise' ? 'Revision Requested' : action;
                                    header.textContent = 'Plan — ' + label;
                                    planDiv.appendChild(header);
                                    var body = document.createElement('div');
                                    body.className = 'plan-content-body';
                                    body.innerHTML = renderMarkdown(planMd);
                                    planDiv.appendChild(body);
                                    capturedPlanEl.replaceWith(planDiv);
                                }
                                if (capturedPlanId && pendingTools[capturedPlanId]) {
                                    delete pendingTools[capturedPlanId];
                                }
                            });

                        } else if (data.type === 'ask_user_request') {
                            showAskUserDialog(data);

                        } else if (data.type === 'plan_executed') {
                            // Plan accepted — switch agent and enable plan context
                            var planInfo = {};
                            try { planInfo = JSON.parse(data.content); } catch (e) {}
                            if (planInfo.clear_session) {
                                // Create a new build session with plan in context
                                var newRes = await fetch('/api/sessions', {
                                    method: 'POST',
                                    headers: {'Content-Type': 'application/json'},
                                    body: JSON.stringify({title: '', agent: 'build'})
                                });
                                var newData = await newRes.json();
                                await fetch('/api/sessions/' + newData.id + '/plan-in-context', {method: 'POST'});
                                await ensureAutoApproveOn(newData.id);
                                window.location.href = '/session/' + newData.id + '?auto=plan';
                            } else {
                                await fetch('/api/sessions/' + sessionId + '/agent', {
                                    method: 'POST',
                                    headers: {'Content-Type': 'application/json'},
                                    body: JSON.stringify({agent: 'build'})
                                });
                                await fetch('/api/sessions/' + sessionId + '/plan-in-context', {method: 'POST'});
                                var skipOn = await ensureAutoApproveOn(sessionId);
                                updateSkipButton(skipOn);
                                var agentEl = document.getElementById('info-agent');
                                if (agentEl) agentEl.textContent = 'build';
                                var badgeEl = document.querySelector('.agent-badge');
                                if (badgeEl) badgeEl.textContent = 'build';
                                updatePlanToggle(true);
                            }

                        } else if (data.type === 'error') {
                            const errDiv = addMessage('system', 'Error: ' + data.error, false);
                            errDiv.classList.add('error');

                        } else if (data.type === 'context_update' && data.context_info) {
                            updateContextBar(data.context_info);

                        } else if (data.done) {
                            finalizeAssistant();
                        }
                    } catch (e) { /* ignore parse errors */ }
                }
            }
        } catch (err) {
            if (err.name !== 'AbortError') {
                const errDiv = addMessage('system', 'Connection error: ' + err.message, false);
                errDiv.classList.add('error');
            }
        }

        finalizeAssistant();
        currentAbort = null;
        showSendButton();
        // Don't steal focus from a permission/plan/ask-user dialog that the
        // stream itself just opened — the dialog needs the keyboard.
        if (!document.querySelector('.permission-overlay')) {
            input.focus();
        }
    }

    // --- @ file mention ---
    let fileMode = false;   // true when we're in @... completion
    let fileAtPos = -1;     // cursor position of the @ that started it
    let fileFetchTimer = null;

    function getFileMentionQuery() {
        const val = input.value;
        const cursor = input.selectionStart;
        // Walk back from cursor to find the @
        let i = cursor - 1;
        while (i >= 0 && val[i] !== '@' && val[i] !== ' ' && val[i] !== '\n') i--;
        if (i >= 0 && val[i] === '@') {
            fileAtPos = i;
            return val.substring(i + 1, cursor);
        }
        return null;
    }

    async function fetchFiles(query) {
        const res = await fetch('/api/files?q=' + encodeURIComponent(query) + '&limit=15');
        return await res.json();
    }

    function showFileBar(files) {
        if (files.length === 0) { hideCommandBar(); return; }
        cmdBar.innerHTML = '';
        files.forEach((f, i) => {
            const div = document.createElement('div');
            div.className = 'cmd-item' + (i === 0 ? ' active' : '');
            const icon = f.type === 'dir' ? '\uD83D\uDCC1' : '\uD83D\uDCC4';
            div.innerHTML = '<span class="cmd-name">' + icon + ' ' + f.path + '</span>';
            div.addEventListener('click', () => { insertFileMention(f.path); });
            cmdBar.appendChild(div);
        });
        cmdBar.style.display = 'block';
    }

    function insertFileMention(filePath) {
        const val = input.value;
        const cursor = input.selectionStart;
        // Replace from @ to cursor with the file path
        const before = val.substring(0, fileAtPos);
        const after = val.substring(cursor);
        input.value = before + '@' + filePath + ' ' + after;
        const newPos = fileAtPos + 1 + filePath.length + 1;
        input.selectionStart = input.selectionEnd = newPos;
        fileMode = false;
        fileAtPos = -1;
        hideCommandBar();
        input.focus();
    }

    function checkFileMention() {
        const query = getFileMentionQuery();
        if (query !== null) {
            fileMode = true;
            // Debounce the fetch
            clearTimeout(fileFetchTimer);
            fileFetchTimer = setTimeout(async () => {
                const files = await fetchFiles(query);
                if (fileMode) showFileBar(files);
            }, 150);
        } else {
            fileMode = false;
            fileAtPos = -1;
        }
    }

    // --- Input event handling ---
    input.addEventListener('input', () => {
        const val = input.value;

        // Check for / commands (only at start of input)
        if (val.startsWith('/') && !fileMode) {
            showCommandBar(val);
            return;
        }

        // Check for @ file mentions
        // Look back from cursor for an @
        const query = getFileMentionQuery();
        if (query !== null) {
            checkFileMention();
            return;
        }

        // Nothing active
        if (!fileMode) hideCommandBar();
    });

    input.addEventListener('keydown', (e) => {
        if (cmdBar.style.display === 'block') {
            if (e.key === 'ArrowDown') { e.preventDefault(); moveCommandSelection(1); return; }
            if (e.key === 'ArrowUp') { e.preventDefault(); moveCommandSelection(-1); return; }
            if (e.key === 'Tab' || (e.key === 'Enter' && !e.shiftKey)) {
                e.preventDefault();
                if (fileMode) {
                    // Pick the active file
                    const active = cmdBar.querySelector('.cmd-item.active .cmd-name');
                    if (active) {
                        // Strip the icon emoji prefix
                        const text = active.textContent.substring(3);
                        insertFileMention(text);
                    }
                } else {
                    const cmd = getActiveCommand();
                    if (cmd) executeCommand(cmd);
                }
                return;
            }
            if (e.key === 'Escape') {
                fileMode = false;
                fileAtPos = -1;
                hideCommandBar();
                return;
            }
        }
        // Prompt history: ArrowUp / ArrowDown
        if (e.key === 'ArrowUp' && _promptHistory.length > 0) {
            // Only activate when cursor is on the first line
            var textBefore = input.value.slice(0, input.selectionStart);
            if (!textBefore.includes('\n')) {
                e.preventDefault();
                if (_historyIndex === -1) {
                    _historyDraft = input.value;
                    _historyIndex = _promptHistory.length - 1;
                } else if (_historyIndex > 0) {
                    _historyIndex--;
                }
                input.value = _promptHistory[_historyIndex];
                input.selectionStart = input.selectionEnd = input.value.length;
                return;
            }
        }
        if (e.key === 'ArrowDown' && _historyIndex !== -1) {
            var textAfter = input.value.slice(input.selectionEnd);
            if (!textAfter.includes('\n')) {
                e.preventDefault();
                if (_historyIndex < _promptHistory.length - 1) {
                    _historyIndex++;
                    input.value = _promptHistory[_historyIndex];
                } else {
                    _historyIndex = -1;
                    input.value = _historyDraft;
                }
                input.selectionStart = input.selectionEnd = input.value.length;
                return;
            }
        }
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            sendMessage();
        }
    });

    sendBtn.addEventListener('click', sendMessage);

    // --- Drag-and-drop file path insertion ---
    var inputArea = input.parentElement;
    inputArea.addEventListener('dragenter', function(e) { e.preventDefault(); e.stopPropagation(); });
    inputArea.addEventListener('dragover', function(e) {
        e.preventDefault();
        e.stopPropagation();
        e.dataTransfer.dropEffect = 'copy';
    });
    inputArea.addEventListener('drop', function(e) {
        e.preventDefault();
        e.stopPropagation();
        var files = e.dataTransfer.files;
        if (!files || files.length === 0) return;
        var names = [];
        for (var i = 0; i < files.length; i++) names.push(files[i].name);
        Promise.all(names.map(function(name) {
            return fetch('/api/files/resolve?name=' + encodeURIComponent(name))
                .then(function(r) { return r.json(); })
                .then(function(d) { return d.path || name; });
        })).then(function(paths) {
            var text = paths.join(' ');
            var start = input.selectionStart;
            var end = input.selectionEnd;
            input.value = input.value.slice(0, start) + text + input.value.slice(end);
            input.selectionStart = input.selectionEnd = start + text.length;
            input.focus();
        });
    });

    // --- Track file changes for left sidebar ---
    // path -> {tool, action} where action ∈ {'created','modified'}.
    // Map iteration order = insertion order; we render reversed so the
    // most-recently-touched file appears first.
    const _changedFiles = new Map();
    // path -> array of {tool_name, args, tool_call_id} for write/edit/multiedit
    // ops captured live as tool_call SSE events arrive. Merged with the
    // server's persisted ops when the dialog opens, so an in-flight op shows
    // a fallback diff before its tool_result has been written to the DB.
    // Reads are never recorded here — they're excluded from the dialog.
    const _liveFileOps = new Map();

    const _FILE_ACTION_BY_TOOL = {
        write: 'created',
        edit: 'modified',
        multiedit: 'modified',
        read: 'read',
    };
    const _FILE_ACTION_RANK = {created: 3, modified: 2, read: 1};
    const _FILE_BADGE_LETTER = {created: 'C', modified: 'M'};

    function trackFileChange(toolName, toolArgs, toolCallId) {
        const action = _FILE_ACTION_BY_TOOL[toolName];
        if (!action) return;
        const filePath = toolArgs && (toolArgs.file_path || '');
        if (!filePath) return;
        // Reads are excluded from both the sidebar and the dialog.
        if (action === 'read') return;
        if (!_liveFileOps.has(filePath)) _liveFileOps.set(filePath, []);
        _liveFileOps.get(filePath).push({
            tool_name: toolName,
            args: toolArgs || {},
            tool_call_id: toolCallId || '',
        });
        const existing = _changedFiles.get(filePath);
        if (!existing || _FILE_ACTION_RANK[action] >= _FILE_ACTION_RANK[existing.action]) {
            _changedFiles.delete(filePath);
            _changedFiles.set(filePath, {tool: toolName, action: action});
        }
        updateFilesPanel();
    }

    function updateFilesPanel() {
        const list = document.getElementById('files-changed-list');
        if (!list) return;
        if (_changedFiles.size === 0) {
            list.innerHTML = '<p class="empty-sidebar">No files modified yet.</p>';
            return;
        }
        list.innerHTML = '';
        const prefix = (typeof PROJECT_DIR !== 'undefined' ? PROJECT_DIR + '/' : '');
        const entries = Array.from(_changedFiles).reverse();
        for (const [path, info] of entries) {
            const btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'file-change-item clickable';
            const letter = _FILE_BADGE_LETTER[info.action];
            const displayPath = prefix && path.startsWith(prefix) ? path.slice(prefix.length) : path;
            btn.innerHTML =
                '<span class="file-badge ' + info.action + '">' + letter + '</span>' +
                '<span class="file-path" title="' + escapeHtml(path) + '">' +
                escapeHtml(displayPath) + '</span>';
            btn.addEventListener('click', function() {
                openFilePreviewDialog(path, info.action);
            });
            list.appendChild(btn);
        }
    }

    function escapeHtml(s) {
        return String(s)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    function _formatOpHeader(op) {
        const args = op.args || {};
        if (op.tool_name === 'edit' && args.replace_all) return 'edit (replace_all)';
        if (op.tool_name === 'multiedit') {
            const n = Array.isArray(args.edits) ? args.edits.length : 0;
            return 'multiedit (' + n + ' edit' + (n === 1 ? '' : 's') + ')';
        }
        return op.tool_name || '';
    }

    function _appendDiffLine(table, line, extraClass) {
        const tr = document.createElement('tr');
        const lineClass =
            line.type === '+' ? 'added' :
            line.type === '-' ? 'removed' : 'context';
        tr.className = 'diff-line ' + lineClass;
        if (extraClass) tr.classList.add(extraClass);
        const oldCell = document.createElement('td');
        oldCell.className = 'diff-lineno old';
        oldCell.textContent = line.old != null ? String(line.old) : '';
        const newCell = document.createElement('td');
        newCell.className = 'diff-lineno new';
        newCell.textContent = line.new != null ? String(line.new) : '';
        const marker = document.createElement('td');
        marker.className = 'diff-marker';
        marker.textContent = line.type;
        const content = document.createElement('td');
        content.className = 'diff-content';
        content.textContent = line.text != null ? line.text : '';
        tr.appendChild(oldCell);
        tr.appendChild(newCell);
        tr.appendChild(marker);
        tr.appendChild(content);
        table.appendChild(tr);
    }

    function _appendHunkSep(table, label) {
        const tr = document.createElement('tr');
        tr.className = 'diff-hunk-sep';
        const td = document.createElement('td');
        td.colSpan = 4;
        td.textContent = label || '⋮';
        tr.appendChild(td);
        table.appendChild(tr);
    }

    function _renderDiffHunks(hunks) {
        const table = document.createElement('table');
        table.className = 'diff-table';
        (hunks || []).forEach(function(hunk, idx) {
            if (idx > 0) {
                _appendHunkSep(
                    table,
                    '@@ -' + hunk.old_start + ',' + hunk.old_count +
                    ' +' + hunk.new_start + ',' + hunk.new_count + ' @@'
                );
            }
            (hunk.lines || []).forEach(function(line) {
                _appendDiffLine(table, line);
            });
        });
        return table;
    }

    function _emptyOpMessage(text) {
        const el = document.createElement('div');
        el.className = 'file-op-empty';
        el.textContent = text;
        return el;
    }

    function _renderOpBlock(op) {
        const block = document.createElement('div');
        block.className = 'file-op-block';
        const header = document.createElement('div');
        header.className = 'file-op-header';
        header.textContent = _formatOpHeader(op);
        block.appendChild(header);

        const diff = op.diff;
        if (!diff) {
            block.appendChild(_emptyOpMessage('No diff available.'));
            return block;
        }

        if (diff.reconstruction === 'fallback') {
            const hint = document.createElement('div');
            hint.className = 'file-op-hint';
            hint.textContent =
                'Line numbers are approximate (couldn’t reconstruct earlier file state).';
            block.appendChild(hint);
        }

        if (diff.kind === 'multiedit') {
            const subEdits = diff.sub_edits || [];
            if (subEdits.length === 0) {
                block.appendChild(_emptyOpMessage('No edits.'));
            } else {
                subEdits.forEach(function(sub) {
                    const subHeader = document.createElement('div');
                    subHeader.className = 'file-op-subheader';
                    subHeader.textContent = sub.label || '';
                    block.appendChild(subHeader);
                    if (sub.hunks && sub.hunks.length > 0) {
                        block.appendChild(_renderDiffHunks(sub.hunks));
                    } else {
                        block.appendChild(_emptyOpMessage('(no change)'));
                    }
                });
            }
        } else {
            if (diff.hunks && diff.hunks.length > 0) {
                block.appendChild(_renderDiffHunks(diff.hunks));
            } else {
                block.appendChild(_emptyOpMessage('(no change)'));
            }
        }

        return block;
    }

    // Synthesizes a fallback diff for live ops (still in the in-flight
    // assistant turn — not yet persisted, so the server hasn't computed
    // pre/post states for them). Naive: all old_string lines as `-`,
    // all new_string lines as `+`, hunk-relative line numbers.
    function _stringsToHunk(oldStr, newStr) {
        const splitLines = function(s) {
            const lines = String(s == null ? '' : s).split('\n');
            if (lines.length > 0 && lines[lines.length - 1] === '') lines.pop();
            return lines;
        };
        const oldLines = splitLines(oldStr);
        const newLines = splitLines(newStr);
        if (oldLines.length === 0 && newLines.length === 0) return [];
        const lines = [];
        oldLines.forEach(function(t, i) {
            lines.push({type: '-', old: i + 1, new: null, text: t});
        });
        newLines.forEach(function(t, i) {
            lines.push({type: '+', old: null, new: i + 1, text: t});
        });
        return [{
            old_start: 1,
            old_count: oldLines.length,
            new_start: 1,
            new_count: newLines.length,
            lines: lines,
        }];
    }

    function _buildClientFallbackDiff(op) {
        const args = op.args || {};
        const tool = op.tool_name || '';
        if (tool === 'write') {
            return {
                kind: 'write',
                reconstruction: 'fallback',
                hunks: _stringsToHunk('', args.content || ''),
            };
        }
        if (tool === 'edit') {
            return {
                kind: 'edit',
                reconstruction: 'fallback',
                hunks: _stringsToHunk(args.old_string || '', args.new_string || ''),
            };
        }
        if (tool === 'multiedit') {
            const subEdits = (args.edits || []).map(function(ed, i) {
                return {
                    label: 'edit #' + (i + 1),
                    hunks: _stringsToHunk(ed.old_string || '', ed.new_string || ''),
                };
            });
            return {kind: 'multiedit', reconstruction: 'fallback', sub_edits: subEdits};
        }
        return null;
    }

    function openFilePreviewDialog(path, action) {
        const overlay = document.createElement('div');
        overlay.className = 'permission-overlay';
        const dialog = document.createElement('div');
        dialog.className = 'permission-dialog file-preview-dialog';
        overlay.appendChild(dialog);

        const close = function() {
            document.removeEventListener('keydown', onKey);
            overlay.remove();
        };
        const onKey = function(e) {
            if (e.key === 'Escape') { e.preventDefault(); close(); }
        };
        overlay.addEventListener('click', function(e) {
            if (e.target === overlay) close();
        });
        document.addEventListener('keydown', onKey);

        // Header
        const header = document.createElement('div');
        header.className = 'file-preview-header';
        const letter = _FILE_BADGE_LETTER[action];
        const prefix = (typeof PROJECT_DIR !== 'undefined' ? PROJECT_DIR + '/' : '');
        const displayPath = prefix && path.startsWith(prefix) ? path.slice(prefix.length) : path;
        header.innerHTML =
            '<span class="file-badge ' + action + '">' + letter + '</span>' +
            '<span class="file-preview-path" title="' + escapeHtml(path) + '">' +
            escapeHtml(displayPath) + '</span>' +
            '<span class="file-preview-hint" data-role="hint"></span>';
        dialog.appendChild(header);

        // Body placeholder
        const body = document.createElement('div');
        body.className = 'file-preview-body';
        body.innerHTML = '<p class="file-preview-empty">Loading…</p>';
        dialog.appendChild(body);

        // Footer
        const actions = document.createElement('div');
        actions.className = 'permission-actions';
        const closeBtn = document.createElement('button');
        closeBtn.type = 'button';
        closeBtn.className = 'btn btn-sm btn-outline';
        closeBtn.textContent = 'Close';
        closeBtn.addEventListener('click', close);
        actions.appendChild(closeBtn);
        dialog.appendChild(actions);

        document.body.appendChild(overlay);
        closeBtn.focus();

        // Fetch and render
        fetch('/api/sessions/' + encodeURIComponent(sessionId) + '/file-events?path=' + encodeURIComponent(path))
            .then(function(r) {
                return r.json().then(function(d) { return {ok: r.ok, data: d}; });
            })
            .then(function(res) {
                body.innerHTML = '';
                const hintEl = header.querySelector('[data-role="hint"]');
                if (!res.ok) {
                    body.innerHTML = '<p class="file-preview-empty">' +
                        escapeHtml(res.data.error || 'Failed to load file events.') +
                        '</p>';
                    return;
                }
                const d = res.data;
                if (!d.exists) {
                    if (hintEl) hintEl.textContent = '(file no longer on disk)';
                } else if (d.binary) {
                    if (hintEl) hintEl.textContent = '(binary)';
                } else if (d.current_too_large) {
                    if (hintEl) hintEl.textContent = '(truncated — first 200KB shown)';
                }

                // Merge server-persisted ops with any live ops not yet flushed
                // to the DB (the in-progress turn). Dedupe by tool_call_id;
                // for ops without an id, preserve all live entries (rare).
                const serverOps = Array.isArray(d.operations) ? d.operations : [];
                const liveOps = (_liveFileOps.get(path) || []).slice();
                const seenIds = new Set();
                serverOps.forEach(function(op) {
                    if (op.tool_call_id) seenIds.add(op.tool_call_id);
                });
                const merged = serverOps.concat(liveOps.filter(function(op) {
                    return !op.tool_call_id || !seenIds.has(op.tool_call_id);
                }));
                // Live ops lack a server-computed `diff`; build a hunk-relative
                // fallback so they still render in the new diff-table view.
                merged.forEach(function(op) {
                    if (!op.diff) op.diff = _buildClientFallbackDiff(op);
                });

                if (merged.length === 0) {
                    body.appendChild(Object.assign(document.createElement('p'), {
                        className: 'file-preview-empty',
                        textContent: 'No tracked operations on this file in the session history.',
                    }));
                } else {
                    merged.forEach(function(op) {
                        body.appendChild(_renderOpBlock(op));
                    });
                }
            })
            .catch(function(e) {
                body.innerHTML = '<p class="file-preview-empty">Network error: ' +
                    escapeHtml(String(e)) + '</p>';
            });
    }

    // Hydrate from server-rendered initial state (so the list survives reloads
    // and shows up for past sessions).
    if (typeof INITIAL_FILES !== 'undefined' && Array.isArray(INITIAL_FILES)) {
        INITIAL_FILES.forEach(function(entry) {
            if (entry && entry.path && entry.action) {
                _changedFiles.set(entry.path, {tool: entry.tool || '', action: entry.action});
            }
        });
        updateFilesPanel();
    }

    // Expose for sidebar
    window._openclose = window._openclose || {};
    window._openclose.changedFiles = _changedFiles;

    // Initialize context bar from server-rendered data
    if (typeof INITIAL_CONTEXT !== 'undefined') {
        updateContextBar(INITIAL_CONTEXT);
    }

    // Render markdown for existing messages loaded from DB
    document.querySelectorAll('.message.assistant .content').forEach(el => {
        const raw = el.textContent;
        if (raw) el.innerHTML = renderMarkdown(raw);
    });

    // --- Explore Files: attached excerpts (one-shot context) ---
    const _attachedExcerpts = new Map();
    const _attachedDiv = document.getElementById('attached-excerpts');

    function _excerptKey(att) {
        return att.path + '|' + att.start_line + '|' + att.end_line;
    }

    function renderAttachedChips() {
        if (!_attachedDiv) return;
        _attachedDiv.innerHTML = '';
        if (_attachedExcerpts.size === 0) {
            _attachedDiv.hidden = true;
            return;
        }
        _attachedDiv.hidden = false;
        _attachedExcerpts.forEach(function(att, key) {
            const chip = document.createElement('span');
            chip.className = 'excerpt-chip';
            const range = att.start_line === att.end_line
                ? ':L' + att.start_line
                : ':L' + att.start_line + '-' + att.end_line;
            const txt = document.createElement('span');
            txt.className = 'chip-text';
            txt.title = att.path + range;
            txt.textContent = att.path + range;
            const rm = document.createElement('button');
            rm.className = 'chip-remove';
            rm.type = 'button';
            rm.title = 'Remove';
            rm.textContent = '×';
            rm.addEventListener('click', function() {
                _attachedExcerpts.delete(key);
                renderAttachedChips();
            });
            chip.appendChild(txt);
            chip.appendChild(rm);
            _attachedDiv.appendChild(chip);
        });
    }

    // --- Explore Files: panel, tree, viewer, selection bubble ---
    function initExplorer() {
        const btn = document.getElementById('explore-files-btn');
        const panel = document.getElementById('explore-panel');
        const resize = document.getElementById('explore-resize');
        const treeEl = document.getElementById('explore-tree');
        const viewerHeader = document.getElementById('explore-viewer-header');
        const viewerBody = document.getElementById('explore-viewer-body');
        if (!btn || !panel || !treeEl || !viewerHeader || !viewerBody) return;

        let loadedRoot = false;
        let currentFilePath = null;
        let bubbleEl = null;

        function setActive(active) {
            btn.classList.toggle('is-active', active);
            btn.setAttribute('aria-pressed', active ? 'true' : 'false');
            btn.title = active
                ? 'Explore Files: ON — click to collapse'
                : 'Explore Files: OFF — toggle to browse project files and attach selections to the next message';
            const state = btn.querySelector('.header-toggle-state');
            if (state) state.textContent = active ? 'ON' : 'OFF';
        }

        async function loadRoot() {
            treeEl.innerHTML = '<div class="explore-empty">Loading…</div>';
            try {
                const res = await fetch('/api/files/tree?path=');
                const items = await res.json();
                if (!Array.isArray(items)) {
                    treeEl.innerHTML = '<div class="explore-empty">Error loading tree</div>';
                    return;
                }
                treeEl.innerHTML = '';
                const ul = document.createElement('ul');
                items.forEach(function(it) { ul.appendChild(makeNode(it)); });
                treeEl.appendChild(ul);
            } catch (e) {
                treeEl.innerHTML = '<div class="explore-empty">Network error</div>';
            }
        }

        function makeNode(item) {
            const li = document.createElement('li');
            const node = document.createElement('div');
            node.className = 'tree-node ' + (item.type === 'dir' ? 'is-dir' : 'is-file');
            node.dataset.path = item.path;
            node.dataset.type = item.type;
            const icon = document.createElement('span');
            icon.className = 'tree-icon';
            const label = document.createElement('span');
            label.textContent = ' ' + item.name;
            node.appendChild(icon);
            node.appendChild(label);
            li.appendChild(node);
            node.addEventListener('click', async function() {
                if (item.type === 'dir') {
                    if (node.classList.contains('is-open')) {
                        node.classList.remove('is-open');
                        const sub = li.querySelector(':scope > ul');
                        if (sub) sub.remove();
                        node.dataset.loaded = '';
                    } else {
                        node.classList.add('is-open');
                        const sub = document.createElement('ul');
                        li.appendChild(sub);
                        try {
                            const res = await fetch('/api/files/tree?path=' + encodeURIComponent(item.path));
                            const children = await res.json();
                            if (Array.isArray(children)) {
                                children.forEach(function(c) { sub.appendChild(makeNode(c)); });
                            }
                            node.dataset.loaded = '1';
                        } catch (e) {
                            sub.innerHTML = '<li><div class="explore-empty">Network error</div></li>';
                        }
                    }
                } else {
                    document.querySelectorAll('.explore-tree .tree-node.is-selected').forEach(function(n) {
                        n.classList.remove('is-selected');
                    });
                    node.classList.add('is-selected');
                    openFileInViewer(item.path);
                }
            });
            return li;
        }

        async function openFileInViewer(path) {
            clearBubble();
            viewerHeader.innerHTML = '';
            const pathSpan = document.createElement('span');
            pathSpan.className = 'explore-viewer-path';
            pathSpan.textContent = path;
            viewerHeader.appendChild(pathSpan);
            viewerBody.innerHTML = '<div class="placeholder">Loading…</div>';
            currentFilePath = null;
            try {
                const res = await fetch('/api/files/content?path=' + encodeURIComponent(path));
                const data = await res.json();
                if (!data.exists) {
                    viewerBody.innerHTML = '<div class="placeholder">File not found.</div>';
                    return;
                }
                if (data.binary) {
                    viewerBody.innerHTML = '<div class="placeholder">(binary file — not displayed)</div>';
                    return;
                }
                if (data.truncated) {
                    const hint = document.createElement('span');
                    hint.className = 'explore-viewer-hint';
                    hint.textContent = '(truncated — first 200KB)';
                    viewerHeader.appendChild(hint);
                }
                currentFilePath = data.path;
                let raw = String(data.content || '');
                if (raw.endsWith('\n')) raw = raw.slice(0, -1);
                renderFileTable(raw === '' ? [] : raw.split('\n'));
            } catch (e) {
                viewerBody.innerHTML = '<div class="placeholder">Network error</div>';
            }
        }

        function renderFileTable(lines) {
            const table = document.createElement('table');
            table.className = 'diff-table';
            const tbody = document.createElement('tbody');
            for (let i = 0; i < lines.length; i++) {
                const tr = document.createElement('tr');
                tr.className = 'diff-line';
                tr.dataset.line = String(i + 1);
                const tdNum = document.createElement('td');
                tdNum.className = 'diff-lineno';
                tdNum.textContent = String(i + 1);
                const tdContent = document.createElement('td');
                tdContent.className = 'diff-content';
                tdContent.textContent = lines[i];
                tr.appendChild(tdNum);
                tr.appendChild(tdContent);
                tbody.appendChild(tr);
            }
            table.appendChild(tbody);
            viewerBody.innerHTML = '';
            viewerBody.appendChild(table);
        }

        // --- Selection bubble ---
        function clearBubble() {
            if (bubbleEl) { bubbleEl.remove(); bubbleEl = null; }
        }

        function getSelectionInfo() {
            const sel = window.getSelection();
            if (!sel || sel.rangeCount === 0 || sel.isCollapsed) return null;
            const range = sel.getRangeAt(0);
            if (!viewerBody.contains(range.commonAncestorContainer)) return null;
            const startNode = range.startContainer.nodeType === 3
                ? range.startContainer.parentElement
                : range.startContainer;
            const endNode = range.endContainer.nodeType === 3
                ? range.endContainer.parentElement
                : range.endContainer;
            const startTr = startNode && startNode.closest ? startNode.closest('tr.diff-line') : null;
            const endTr = endNode && endNode.closest ? endNode.closest('tr.diff-line') : null;
            if (!startTr || !endTr) return null;
            const startLine = parseInt(startTr.dataset.line, 10);
            const endLine = parseInt(endTr.dataset.line, 10);
            const text = sel.toString();
            if (!text) return null;
            const rect = range.getBoundingClientRect();
            return {
                startLine: Math.min(startLine, endLine),
                endLine: Math.max(startLine, endLine),
                text: text,
                rect: rect,
            };
        }

        viewerBody.addEventListener('mouseup', function() {
            setTimeout(function() {
                const info = getSelectionInfo();
                clearBubble();
                if (!info || !currentFilePath) return;
                const att = {
                    path: currentFilePath,
                    start_line: info.startLine,
                    end_line: info.endLine,
                    text: info.text,
                };
                const key = _excerptKey(att);
                bubbleEl = document.createElement('div');
                bubbleEl.className = 'excerpt-bubble';
                const cb = document.createElement('input');
                cb.type = 'checkbox';
                cb.checked = _attachedExcerpts.has(key);
                cb.title = 'Attach selection to next message';
                cb.addEventListener('mousedown', function(e) { e.stopPropagation(); });
                cb.addEventListener('click', function(e) {
                    e.stopPropagation();
                    if (cb.checked) {
                        _attachedExcerpts.set(key, att);
                    } else {
                        _attachedExcerpts.delete(key);
                    }
                    renderAttachedChips();
                });
                bubbleEl.appendChild(cb);
                const viewerRect = viewerBody.getBoundingClientRect();
                const top = Math.max(
                    0,
                    info.rect.bottom - viewerRect.top + viewerBody.scrollTop + 4
                );
                const left = Math.max(
                    0,
                    info.rect.left - viewerRect.left + viewerBody.scrollLeft
                );
                bubbleEl.style.top = top + 'px';
                bubbleEl.style.left = left + 'px';
                viewerBody.appendChild(bubbleEl);
            }, 10);
        });

        document.addEventListener('mousedown', function(e) {
            if (bubbleEl && !bubbleEl.contains(e.target)) {
                setTimeout(clearBubble, 0);
            }
        });

        // --- Drag-resize handle ---
        let dragging = false;
        let dragStartY = 0;
        let dragStartHeight = 0;

        resize.addEventListener('mousedown', function(e) {
            if (panel.hidden) return;
            dragging = true;
            dragStartY = e.clientY;
            dragStartHeight = panel.getBoundingClientRect().height;
            resize.classList.add('dragging');
            e.preventDefault();
        });
        document.addEventListener('mousemove', function(e) {
            if (!dragging) return;
            const dy = e.clientY - dragStartY;
            const newHeight = Math.max(
                80,
                Math.min(window.innerHeight * 0.85, dragStartHeight + dy)
            );
            panel.style.height = newHeight + 'px';
        });
        document.addEventListener('mouseup', function() {
            if (!dragging) return;
            dragging = false;
            resize.classList.remove('dragging');
        });

        // --- Toggle ---
        btn.addEventListener('click', function() {
            const willOpen = panel.hidden;
            panel.hidden = !willOpen;
            resize.hidden = !willOpen;
            setActive(willOpen);
            if (willOpen) {
                panel.style.height = '';
                if (!loadedRoot) {
                    loadedRoot = true;
                    loadRoot();
                }
            } else {
                clearBubble();
            }
        });

        setActive(false);
    }

    initExplorer();

    // Initial scroll to bottom on page load (force it regardless of _userScrolledUp)
    messagesDiv.scrollTop = messagesDiv.scrollHeight;
    _userScrolledUp = false;
    input.focus();

    // Auto-send kickoff message when redirected from "Accept & Clear"
    var urlParams = new URLSearchParams(window.location.search);
    if (urlParams.get('auto') === 'plan') {
        // Clean the URL so refresh doesn't re-trigger
        history.replaceState(null, '', window.location.pathname);
        input.value = 'Execute the plan.';
        setTimeout(function() { sendMessage(); }, 100);
    }
}

// --- Sidebar logic (separate from chat) ---
function initSidebars(sessionId) {
    // Load session list into left sidebar (optionally filtered by query, server-side)
    async function loadSessionList(query) {
        const url = query ? '/api/sessions?q=' + encodeURIComponent(query) : '/api/sessions';
        const res = await fetch(url);
        const sessions = await res.json();
        const list = document.getElementById('sidebar-session-list');
        const beforeScroll = list.scrollTop;
        list.innerHTML = '';
        sessions.forEach(s => {
            const item = document.createElement('div');
            item.className = 'sidebar-session-item' + (s.id === sessionId ? ' active' : '');
            item.dataset.sid = s.id;
            item.innerHTML = '<div class="sidebar-session-text">'
                + '<div class="sidebar-session-title">' + (s.title || 'Untitled') + '</div>'
                + '<div class="sidebar-session-meta">' + s.agent + '</div>'
                + '</div>'
                + '<button class="sidebar-session-delete" data-delete="' + s.id + '" title="Delete session">&times;</button>';
            list.appendChild(item);
        });
        // Restore scroll: cross-page (sessionStorage from pagehide) takes priority,
        // else preserve in-place position (rename, delete-other). Search resets to top.
        const cross = sessionStorage.getItem('oc.sidebarSessionsScroll');
        if (cross !== null) {
            list.scrollTop = parseInt(cross, 10) || 0;
            sessionStorage.removeItem('oc.sidebarSessionsScroll');
        } else if (!query) {
            list.scrollTop = beforeScroll;
        }
    }
    loadSessionList();

    // Persist sidebar scroll across navigation (session click, +Create, fork, etc.)
    window.addEventListener('pagehide', () => {
        const sl = document.getElementById('sidebar-session-list');
        if (sl) sessionStorage.setItem('oc.sidebarSessionsScroll', sl.scrollTop);
    });

    // Session search: debounced server-side query over title + message content
    var searchInput = document.getElementById('session-search');
    if (searchInput) {
        var searchTimer = null;
        searchInput.addEventListener('input', function() {
            var query = this.value;
            clearTimeout(searchTimer);
            searchTimer = setTimeout(function() { loadSessionList(query); }, 250);
        });
    }

    // Delegated click handler for session list (navigation + delete)
    document.getElementById('sidebar-session-list').addEventListener('click', async (e) => {
        const delBtn = e.target.closest('[data-delete]');
        if (delBtn) {
            e.preventDefault();
            e.stopPropagation();
            const sid = delBtn.dataset.delete;
            await fetch('/api/sessions/' + sid, {method: 'DELETE'});
            if (sid === sessionId) {
                // Fetch remaining sessions and navigate to the most recent one
                const res = await fetch('/api/sessions');
                const remaining = await res.json();
                if (remaining.length > 0) {
                    window.location.href = '/session/' + remaining[0].id;
                } else {
                    // No sessions left — create a new one
                    const newRes = await fetch('/api/sessions', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({title: ''})
                    });
                    const newSession = await newRes.json();
                    window.location.href = '/session/' + newSession.id;
                }
            } else {
                loadSessionList();
            }
            return;
        }
        const item = e.target.closest('.sidebar-session-item');
        if (item && item.dataset.sid) {
            window.location.href = '/session/' + item.dataset.sid;
        }
    });

    // New session button
    document.getElementById('sidebar-new-btn').addEventListener('click', async () => {
        const res = await fetch('/api/sessions', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({title: ''})
        });
        const data = await res.json();
        window.location.href = '/session/' + data.id;
    });

    // Delete all sessions button
    document.getElementById('sidebar-delete-all-btn').addEventListener('click', async () => {
        const res = await fetch('/api/sessions');
        const sessions = await res.json();
        if (sessions.length === 0) return;
        for (const s of sessions) {
            await fetch('/api/sessions/' + s.id, {method: 'DELETE'});
        }
        const newRes = await fetch('/api/sessions', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({title: ''})
        });
        const newSession = await newRes.json();
        window.location.href = '/session/' + newSession.id;
    });

    // Sidebar toggle buttons
    document.getElementById('toggle-left').addEventListener('click', () => {
        document.getElementById('sidebar-left').classList.toggle('collapsed');
    });
    document.getElementById('toggle-right').addEventListener('click', () => {
        document.getElementById('sidebar-right').classList.toggle('collapsed');
    });

    // Right sidebar tabs + browser screenshot polling
    {
        const sidebarRight = document.getElementById('sidebar-right');
        const tabs = sidebarRight.querySelectorAll('.sidebar-tab');
        const panels = sidebarRight.querySelectorAll('.sidebar-panel');
        const browserImg = document.getElementById('browser-screenshot');
        const browserStatus = document.getElementById('browser-status');
        const POLL_MS = 500;
        let pollTimer = null;
        let pollAbort = null;

        async function refreshBrowserShot(signal) {
            try {
                const res = await fetch('/api/browser/screenshot?t=' + Date.now(), { signal });
                if (signal.aborted) return;
                if (!res.ok) {
                    browserImg.hidden = true;
                    browserStatus.textContent = 'No browser session active.';
                    browserStatus.classList.add('error');
                    return;
                }
                const blob = await res.blob();
                if (signal.aborted) return;
                const next = URL.createObjectURL(blob);
                const prev = browserImg.dataset.url;
                browserImg.dataset.url = next;
                browserImg.src = next;
                browserImg.hidden = false;
                browserStatus.textContent = 'Live (updates every ' + (POLL_MS / 1000) + 's)';
                browserStatus.classList.remove('error');
                if (prev) URL.revokeObjectURL(prev);
            } catch (e) {
                if (signal.aborted) return;
                browserImg.hidden = true;
                browserStatus.textContent = 'Connection error.';
                browserStatus.classList.add('error');
            }
        }
        function startBrowserPolling() {
            if (pollAbort) return;
            pollAbort = new AbortController();
            const signal = pollAbort.signal;
            const tick = async () => {
                if (signal.aborted) return;
                await refreshBrowserShot(signal);
                if (!signal.aborted) pollTimer = setTimeout(tick, POLL_MS);
            };
            tick();
        }
        function stopBrowserPolling() {
            if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
            if (pollAbort) { pollAbort.abort(); pollAbort = null; }
        }
        function activeTabPanel() {
            const t = sidebarRight.querySelector('.sidebar-tab.active');
            return t ? t.dataset.panel : null;
        }

        tabs.forEach(tab => tab.addEventListener('click', () => {
            const target = tab.dataset.panel;
            tabs.forEach(t => t.classList.toggle('active', t === tab));
            panels.forEach(p => { p.style.display = (p.id === target ? '' : 'none'); });
            if (target === 'panel-browser') {
                sidebarRight.classList.add('sidebar-wide');
                startBrowserPolling();
            } else {
                sidebarRight.classList.remove('sidebar-wide');
                stopBrowserPolling();
            }
        }));

        document.getElementById('toggle-right').addEventListener('click', () => {
            if (sidebarRight.classList.contains('collapsed')) {
                stopBrowserPolling();
            } else if (activeTabPanel() === 'panel-browser') {
                startBrowserPolling();
            }
        });
    }

    // Info actions
    document.getElementById('info-rename-btn').addEventListener('click', async () => {
        const title = prompt('New session title:');
        if (title) {
            await fetch('/api/sessions/' + sessionId + '/rename', {
                method: 'PATCH',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({title: title})
            });
            document.getElementById('session-title').textContent = title;
            loadSessionList();
        }
    });

    document.getElementById('info-export-btn').addEventListener('click', async () => {
        const res = await fetch('/api/sessions/' + sessionId + '/export');
        const data = await res.json();
        const blob = new Blob([JSON.stringify(data, null, 2)], {type: 'application/json'});
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = 'session-' + sessionId + '.json';
        a.click();
        URL.revokeObjectURL(url);
    });

    document.getElementById('info-compact-btn').addEventListener('click', async () => {
        const res = await fetch('/api/sessions/' + sessionId + '/compact', {method: 'POST'});
        const data = await res.json();
        alert(data.compacted ? 'Context compacted.' : 'No compaction needed.');
    });

    // Skip permissions toggle button
    var skipBtn = document.getElementById('info-skip-btn');
    if (skipBtn) {
        fetch('/api/sessions/' + sessionId + '/skip-permissions')
            .then(function(r) { return r.json(); })
            .then(function(data) { updateSkipButton(data.skip_all); });
        skipBtn.addEventListener('click', async () => {
            const res = await fetch('/api/sessions/' + sessionId + '/skip-permissions', {method: 'POST'});
            const data = await res.json();
            updateSkipButton(data.skip_all);
        });
    }

    // Plan in context toggle button
    var planBtn = document.getElementById('info-plan-btn');
    if (planBtn) {
        fetch('/api/sessions/' + sessionId + '/plan')
            .then(function(r) { return r.json(); })
            .then(function(data) { updatePlanToggle(data.plan_in_context); });
        planBtn.addEventListener('click', async () => {
            const res = await fetch('/api/sessions/' + sessionId + '/plan-in-context', {method: 'POST'});
            const data = await res.json();
            updatePlanToggle(data.plan_in_context);
        });
    }

    // Video Compatible Model toggle button
    var videoCompatBtn = document.getElementById('info-video-compatible-btn');
    if (videoCompatBtn) {
        fetch('/api/sessions/' + sessionId + '/video-compatible')
            .then(function(r) { return r.json(); })
            .then(function(data) { updateVideoCompatibleButton(data.video_compatible); });
        videoCompatBtn.addEventListener('click', async () => {
            const res = await fetch('/api/sessions/' + sessionId + '/video-compatible', {method: 'POST'});
            const data = await res.json();
            updateVideoCompatibleButton(data.video_compatible);
        });
    }

    // -------------------------------------------------------------------
    // Browser Task Recorder
    // -------------------------------------------------------------------
    var recorderCard = document.getElementById('recorder-card');
    var recorderBtn = document.getElementById('recorder-toggle-btn');
    var recorderStatus = document.getElementById('recorder-status');
    var saveForm = document.getElementById('recorder-save-form');
    var saveBtn = document.getElementById('recorder-save-btn');
    var discardBtn = document.getElementById('recorder-discard-btn');
    var taskNameInput = document.getElementById('recorder-task-name');
    var taskDescInput = document.getElementById('recorder-task-desc');
    var tasksList = document.getElementById('tasks-list');

    var currentRecordingId = null;

    function setRecorderState(state, msg) {
        // state: 'idle' | 'recording' | 'pending'
        if (!recorderCard) return;
        recorderCard.classList.toggle('recording', state === 'recording');
        recorderCard.classList.toggle('idle', state === 'idle' && !msg);
        recorderStatus.textContent = msg || '';
        recorderBtn.dataset.state = state;
        if (state === 'recording') {
            recorderBtn.textContent = 'Stop';
            saveForm.style.display = 'none';
        } else if (state === 'pending') {
            recorderBtn.textContent = 'Record';
            saveForm.style.display = 'flex';
        } else {
            recorderBtn.textContent = 'Record';
            saveForm.style.display = 'none';
        }
        syncRecorderEnabled();
    }

    async function loadTasks() {
        if (!tasksList) return;
        try {
            const res = await fetch('/api/tasks');
            const tasks = await res.json();
            tasksList.innerHTML = '';
            if (!tasks.length) {
                tasksList.innerHTML = '<p class="empty-sidebar">No tasks yet.</p>';
                return;
            }
            tasks.forEach(function(t) {
                const item = document.createElement('div');
                item.className = 'task-item';
                item.innerHTML =
                    '<div class="task-name"></div>' +
                    '<div class="task-desc"></div>' +
                    '<div class="task-actions">' +
                        '<button class="btn btn-outline" data-act="inject">Inject in message</button>' +
                        '<button class="btn btn-danger-outline" data-act="delete">Delete</button>' +
                    '</div>';
                item.querySelector('.task-name').textContent = t.name || t.slug;
                item.querySelector('.task-desc').textContent = t.description || '';
                item.dataset.slug = t.slug;
                tasksList.appendChild(item);
            });
        } catch (e) {
            tasksList.innerHTML = '<p class="empty-sidebar">Failed to load tasks.</p>';
        }
    }

    function injectTaskIntoInput(task) {
        const ta = document.getElementById('user-input');
        if (!ta) return;
        const framed =
            'Reproduce the following workflow to match the goal:\n\n' +
            (task.body || '');
        ta.value = framed;
        ta.focus();
        ta.dispatchEvent(new Event('input', {bubbles: true}));
    }

    if (tasksList) {
        tasksList.addEventListener('click', async (e) => {
            const item = e.target.closest('.task-item');
            if (!item) return;
            const slug = item.dataset.slug;
            const actBtn = e.target.closest('[data-act]');
            const act = actBtn ? actBtn.dataset.act : 'inject';
            if (act === 'delete') {
                await fetch('/api/tasks/' + encodeURIComponent(slug), {method: 'DELETE'});
                loadTasks();
                return;
            }
            // inject (default)
            const res = await fetch('/api/tasks/' + encodeURIComponent(slug));
            if (!res.ok) return;
            const task = await res.json();
            injectTaskIntoInput(task);
        });
    }

    if (recorderBtn) {
        // Restore state on page load
        fetch('/api/recorder/status').then(r => r.json()).then(data => {
            if (data.active) {
                currentRecordingId = data.active.recording_id;
                setRecorderState('recording', 'Recording...');
            } else {
                setRecorderState('idle', '');
            }
        });

        recorderBtn.addEventListener('click', async () => {
            if (recorderCard.classList.contains('recording')) {
                // Stop
                recorderBtn.disabled = true;
                setRecorderState('recording', 'Stopping & encoding...');
                const res = await fetch('/api/recorder/stop', {method: 'POST'});
                const data = await res.json();
                if (!res.ok) {
                    alert('Stop failed: ' + (data.error || res.statusText));
                    setRecorderState('idle', '');
                    currentRecordingId = null;
                    return;
                }
                currentRecordingId = data.recording_id;
                const mb = data.video_size_bytes ? (data.video_size_bytes / (1024*1024)).toFixed(1) + ' MiB' : '?';
                const msg = data.events_count + ' events, ' + data.frames_count +
                    ' frames, ' + (data.duration_s || '?') + 's, ' + mb +
                    ' — saved to ' + (data.video_path || '?');
                setRecorderState('pending', msg);
                console.info('[recorder] stopped:', data);
                taskNameInput.value = '';
                taskDescInput.value = '';
                taskNameInput.focus();
            } else {
                // Start
                recorderBtn.disabled = true;
                const res = await fetch('/api/recorder/start', {method: 'POST'});
                const data = await res.json();
                if (!res.ok) {
                    alert('Start failed: ' + (data.error || res.statusText));
                    setRecorderState('idle', '');
                    return;
                }
                currentRecordingId = data.recording_id;
                setRecorderState('recording', 'Recording...');
            }
        });
    }

    if (saveBtn) {
        saveBtn.addEventListener('click', async () => {
            if (!currentRecordingId) return;
            const name = (taskNameInput.value || '').trim();
            if (!name) { alert('Please enter a task name.'); return; }
            saveBtn.disabled = true;
            discardBtn.disabled = true;
            setRecorderState('pending', 'Annotating with VLM (may take a while)...');
            const res = await fetch('/api/recorder/annotate', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    recording_id: currentRecordingId,
                    name: name,
                    description: (taskDescInput.value || '').trim(),
                }),
            });
            const data = await res.json();
            saveBtn.disabled = false;
            discardBtn.disabled = false;
            if (!res.ok) {
                alert('Annotation failed: ' + (data.error || res.statusText));
                setRecorderState('pending', 'Annotation failed — try again or discard.');
                return;
            }
            currentRecordingId = null;
            setRecorderState('idle', '');
            loadTasks();
        });
    }

    if (discardBtn) {
        discardBtn.addEventListener('click', async () => {
            await fetch('/api/recorder/cancel', {method: 'POST'});
            currentRecordingId = null;
            setRecorderState('idle', '');
        });
    }

    loadTasks();

    // ── Skills ──────────────────────────────────────────────────────
    const skillsList = document.getElementById('skills-list');
    const createSkillBtn = document.getElementById('create-skill-btn');
    let _toolsCache = null;
    const _skillPolls = new Map();  // slug → interval id
    const _skillExpanded = new Set();  // slugs currently showing runs

    async function fetchTools() {
        if (_toolsCache) return _toolsCache;
        try {
            const res = await fetch('/api/skills/tools');
            _toolsCache = res.ok ? await res.json() : [];
        } catch (_) { _toolsCache = []; }
        return _toolsCache;
    }

    async function loadSkills() {
        if (!skillsList) return;
        try {
            const res = await fetch('/api/skills');
            const skills = await res.json();
            skillsList.innerHTML = '';
            if (!skills.length) {
                skillsList.innerHTML = '<p class="empty-sidebar">No skills yet.</p>';
                return;
            }
            skills.forEach(function(s) {
                const item = document.createElement('div');
                item.className = 'skill-item';
                item.dataset.slug = s.slug;
                item.innerHTML =
                    '<div class="skill-name"></div>' +
                    '<div class="skill-goal"></div>' +
                    '<div class="skill-actions">' +
                        '<button class="btn btn-outline" data-act="run">Test</button>' +
                        '<button class="btn btn-outline" data-act="edit">Edit</button>' +
                        '<button class="btn btn-outline" data-act="toggle">Display runs ▼</button>' +
                        '<button class="btn btn-outline" data-act="duplicate">Duplicate</button>' +
                        '<button class="btn btn-danger-outline" data-act="delete">Delete</button>' +
                    '</div>' +
                    '<div class="skill-runs" style="display:none"></div>';
                item.querySelector('.skill-name').textContent = s.name || s.slug;
                item.querySelector('.skill-goal').textContent = s.goal || '';
                skillsList.appendChild(item);
                if (_skillExpanded.has(s.slug)) {
                    const runsEl = item.querySelector('.skill-runs');
                    runsEl.style.display = 'flex';
                    refreshRuns(s.slug, runsEl);
                }
            });
        } catch (e) {
            skillsList.innerHTML = '<p class="empty-sidebar">Failed to load skills.</p>';
        }
    }

    function _fmtTs(iso) {
        if (!iso) return '?';
        try {
            const d = new Date(iso);
            const now = new Date();
            const diff = (now - d) / 1000;
            if (diff < 60) return Math.round(diff) + 's ago';
            if (diff < 3600) return Math.round(diff / 60) + 'm ago';
            if (diff < 86400) return Math.round(diff / 3600) + 'h ago';
            return d.toLocaleDateString();
        } catch (_) { return iso; }
    }

    async function refreshRuns(slug, runsEl) {
        try {
            const res = await fetch('/api/skills/' + encodeURIComponent(slug) + '/runs?limit=5');
            const runs = await res.json();
            if (!runs.length) {
                runsEl.innerHTML = '<div class="skill-run-row">No runs yet.</div>';
                return;
            }
            runsEl.innerHTML = '';
            runs.forEach(function(r) {
                const row = document.createElement('div');
                row.className = 'skill-run-row status-' + (r.status || 'running');
                const stamp = _fmtTs(r.started_at);
                const preview = r.output_preview ? (' · ' + r.output_preview) : '';
                row.innerHTML = '<span>' + stamp + ' · ' + (r.status || 'running') +
                    '</span><span class="skill-run-preview">' + (preview ? preview.replace(/</g, '&lt;') : '') + '</span>';
                runsEl.appendChild(row);
            });
        } catch (_) { /* ignore */ }
    }

    function pollRuns(slug, runsEl) {
        if (_skillPolls.has(slug)) return;
        const iv = setInterval(async () => {
            await refreshRuns(slug, runsEl);
            // Stop polling if no runs are still "running"
            try {
                const res = await fetch('/api/skills/' + encodeURIComponent(slug) + '/runs?limit=3');
                const runs = await res.json();
                if (!runs.some(r => r.status === 'running')) {
                    clearInterval(iv);
                    _skillPolls.delete(slug);
                }
            } catch (_) { /* keep polling */ }
        }, 2000);
        _skillPolls.set(slug, iv);
    }

    if (skillsList) {
        skillsList.addEventListener('click', async (e) => {
            const item = e.target.closest('.skill-item');
            if (!item) return;
            const slug = item.dataset.slug;
            const actBtn = e.target.closest('[data-act]');
            if (!actBtn) return;
            const act = actBtn.dataset.act;
            if (act === 'delete') {
                await fetch('/api/skills/' + encodeURIComponent(slug), {method: 'DELETE'});
                _skillExpanded.delete(slug);
                loadSkills();
                return;
            }
            if (act === 'duplicate') {
                actBtn.disabled = true;
                const res = await fetch('/api/skills/' + encodeURIComponent(slug) + '/duplicate', {method: 'POST'});
                actBtn.disabled = false;
                if (!res.ok) {
                    const d = await res.json().catch(() => ({}));
                    alert('Duplicate failed: ' + (d.error || res.statusText));
                    return;
                }
                loadSkills();
                return;
            }
            if (act === 'run') {
                actBtn.disabled = true;
                const res = await fetch('/api/skills/' + encodeURIComponent(slug) + '/run', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({inputs: {}, trigger_message: ''}),
                });
                actBtn.disabled = false;
                if (!res.ok) {
                    const d = await res.json().catch(() => ({}));
                    alert('Run failed: ' + (d.error || res.statusText));
                    return;
                }
                _skillExpanded.add(slug);
                const runsEl = item.querySelector('.skill-runs');
                runsEl.style.display = 'flex';
                refreshRuns(slug, runsEl);
                pollRuns(slug, runsEl);
                return;
            }
            if (act === 'toggle') {
                const runsEl = item.querySelector('.skill-runs');
                if (_skillExpanded.has(slug)) {
                    _skillExpanded.delete(slug);
                    runsEl.style.display = 'none';
                } else {
                    _skillExpanded.add(slug);
                    runsEl.style.display = 'flex';
                    refreshRuns(slug, runsEl);
                }
                return;
            }
            if (act === 'edit') {
                const res = await fetch('/api/skills/' + encodeURIComponent(slug));
                if (!res.ok) return;
                const data = await res.json();
                openSkillDialog({form: data, mode: 'edit', slug: slug});
            }
        });
    }

    function closeSkillDialog() {
        const ov = document.getElementById('skill-overlay');
        if (ov) ov.remove();
    }

    async function openSkillDialog(opts) {
        opts = opts || {};
        const mode = opts.mode || 'create';
        const tools = await fetchTools();
        closeSkillDialog();
        const overlay = document.createElement('div');
        overlay.className = 'permission-overlay';
        overlay.id = 'skill-overlay';
        const dialog = document.createElement('div');
        dialog.className = 'permission-dialog skill-dialog';
        dialog.innerHTML = skillDialogHtml(mode);
        overlay.appendChild(dialog);
        document.body.appendChild(overlay);

        const promptInput = dialog.querySelector('#skill-user-prompt');
        const statusEl = dialog.querySelector('#skill-dialog-status');
        const regenBtn = dialog.querySelector('#skill-regen-btn');
        const regenNote = dialog.querySelector('#skill-regen-note');
        const saveBtn = dialog.querySelector('#skill-save-btn');
        const cancelBtn = dialog.querySelector('#skill-cancel-btn');
        cancelBtn.addEventListener('click', closeSkillDialog);

        let regenSessionId = sessionId;

        function setRegenNote(text, variant) {
            if (!regenNote) return;
            regenNote.textContent = text || '';
            regenNote.className = 'skill-regen-note' + (variant ? ' ' + variant : '');
        }

        async function resolveRegenSession() {
            const src = opts.form && opts.form.source_session;
            if (!src || src === sessionId) {
                regenSessionId = sessionId;
                setRegenNote('');
                return;
            }
            try {
                const r = await fetch('/api/sessions/' + encodeURIComponent(src) + '/exists');
                const d = await r.json();
                if (d.exists) {
                    regenSessionId = src;
                    setRegenNote('Using original session', 'info');
                } else {
                    regenSessionId = sessionId;
                    setRegenNote('Original session deleted — will regenerate from current session', 'warn');
                }
            } catch (_) {
                regenSessionId = sessionId;
                setRegenNote('Could not verify original session — will use current', 'warn');
            }
        }

        function setStatus(text, isError) {
            statusEl.textContent = text || '';
            statusEl.className = 'skill-dialog-status' + (isError ? ' error' : '');
        }

        function fillForm(form) {
            dialog.querySelector('#skill-name').value = form.name || '';
            dialog.querySelector('#skill-slug').value = form.slug || '';
            dialog.querySelector('#skill-goal').value = form.goal || '';
            dialog.querySelector('#skill-prose').value = form.required_tools_prose || '';
            dialog.querySelector('#skill-procedure').value = form.procedure || '';
            dialog.querySelector('#skill-pitfalls').value = form.pitfalls || '';
            dialog.querySelector('#skill-verification').value = form.verification || '';
            renderParams(form.parameters || []);
            renderTools(tools, form.required_tools || []);
        }

        function readForm() {
            return {
                name: dialog.querySelector('#skill-name').value.trim(),
                slug: dialog.querySelector('#skill-slug').value.trim(),
                goal: dialog.querySelector('#skill-goal').value.trim(),
                required_tools_prose: dialog.querySelector('#skill-prose').value,
                procedure: dialog.querySelector('#skill-procedure').value,
                pitfalls: dialog.querySelector('#skill-pitfalls').value,
                verification: dialog.querySelector('#skill-verification').value,
                parameters: collectParams(),
                required_tools: collectTools(),
                source_session: opts.form?.source_session || sessionId,
            };
        }

        function renderParams(params) {
            const host = dialog.querySelector('#skill-params');
            host.innerHTML = '';
            params.forEach(p => host.appendChild(paramRow(p)));
        }

        function paramRow(p) {
            p = p || {name: '', type: 'string', required: false, default: ''};
            const row = document.createElement('div');
            row.className = 'skill-param-row';
            row.innerHTML =
                '<input type="text" class="p-name" placeholder="name">' +
                '<select class="p-type"><option value="string">string</option><option value="int">int</option><option value="bool">bool</option></select>' +
                '<label class="skill-param-req"><input type="checkbox" class="p-req"> req</label>' +
                '<input type="text" class="p-def" placeholder="default">' +
                '<button class="btn btn-danger-outline" data-act="rm">×</button>';
            row.querySelector('.p-name').value = p.name || '';
            row.querySelector('.p-type').value = p.type || 'string';
            row.querySelector('.p-req').checked = !!p.required;
            row.querySelector('.p-def').value = p.default || '';
            row.querySelector('[data-act="rm"]').addEventListener('click', (e) => {
                e.preventDefault(); row.remove();
            });
            return row;
        }

        function collectParams() {
            const rows = dialog.querySelectorAll('#skill-params .skill-param-row');
            const out = [];
            rows.forEach(r => {
                const name = r.querySelector('.p-name').value.trim();
                if (!name) return;
                out.push({
                    name: name,
                    type: r.querySelector('.p-type').value,
                    required: r.querySelector('.p-req').checked,
                    default: r.querySelector('.p-def').value,
                });
            });
            return out;
        }

        function renderTools(all, selected) {
            const host = dialog.querySelector('#skill-tools');
            host.innerHTML = '';
            const selectedMap = {};
            selected.forEach(t => { selectedMap[t.name] = t; });
            all.forEach(t => {
                const sel = selectedMap[t.name];
                const row = document.createElement('div');
                row.className = 'skill-tool-row' + (t.sensitive ? ' sensitive' : '');
                const checked = sel ? 'checked' : '';
                const tag = t.sensitive ? '<span class="sensitive-tag">sensitive</span>' : '';
                row.innerHTML =
                    '<label><input type="checkbox" class="tool-cb" data-name="' + t.name +
                        '" data-sensitive="' + (t.sensitive ? '1' : '0') + '" ' + checked + '> ' +
                        t.name + '</label>' + tag;
                if (t.sensitive) {
                    const auth = document.createElement('div');
                    auth.className = 'skill-tool-authorize';
                    auth.innerHTML = '<input type="checkbox" class="tool-auth" data-name="' + t.name + '"> I authorize this skill to use <code>' + t.name + '</code>';
                    row.appendChild(auth);
                    const cb = row.querySelector('.tool-cb');
                    const authCb = row.querySelector('.tool-auth');
                    if (sel) authCb.checked = true;
                    cb.addEventListener('change', () => {
                        auth.style.display = cb.checked ? 'flex' : 'none';
                        if (!cb.checked) authCb.checked = false;
                    });
                    auth.style.display = cb.checked ? 'flex' : 'none';
                }
                host.appendChild(row);
            });
        }

        function collectTools() {
            const rows = dialog.querySelectorAll('#skill-tools .skill-tool-row');
            const out = [];
            for (const r of rows) {
                const cb = r.querySelector('.tool-cb');
                if (!cb.checked) continue;
                const sensitive = cb.dataset.sensitive === '1';
                if (sensitive) {
                    const auth = r.querySelector('.tool-auth');
                    if (!auth || !auth.checked) {
                        throw new Error('Sensitive tool "' + cb.dataset.name + '" requires explicit authorization.');
                    }
                }
                out.push({name: cb.dataset.name, sensitive: sensitive});
            }
            return out;
        }

        dialog.querySelector('#skill-add-param').addEventListener('click', (e) => {
            e.preventDefault();
            dialog.querySelector('#skill-params').appendChild(paramRow(null));
        });

        // Initial population
        if (mode === 'edit' && opts.form) {
            fillForm(opts.form);
            setStatus('Editing skill "' + opts.form.name + '" (v' + opts.form.version + ')');
            await resolveRegenSession();
        } else {
            // Create: generate from current session
            fillForm({name: '', slug: '', goal: '', parameters: [], required_tools: [], required_tools_prose: '', procedure: '', pitfalls: '', verification: ''});
            await regenerate(promptInput.value);
        }

        async function regenerate(userPrompt) {
            setStatus('Generating skill from conversation…');
            regenBtn.disabled = true; saveBtn.disabled = true;
            try {
                const res = await fetch('/api/skills/generate', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({session_id: regenSessionId, user_prompt: userPrompt || ''}),
                });
                const data = await res.json();
                if (!res.ok) throw new Error(data.error || res.statusText);
                fillForm(data.form);
                setStatus('Form generated. Review and save.');
            } catch (err) {
                setStatus('Generation failed: ' + err.message, true);
            } finally {
                regenBtn.disabled = false; saveBtn.disabled = false;
            }
        }

        regenBtn.addEventListener('click', (e) => {
            e.preventDefault();
            regenerate(promptInput.value);
        });

        saveBtn.addEventListener('click', async (e) => {
            e.preventDefault();
            let body;
            try {
                body = readForm();
            } catch (err) {
                setStatus(err.message, true);
                return;
            }
            if (!body.name) { setStatus('Name is required.', true); return; }
            saveBtn.disabled = true;
            const url = mode === 'edit' ? '/api/skills/' + encodeURIComponent(opts.slug) : '/api/skills';
            const method = mode === 'edit' ? 'PUT' : 'POST';
            try {
                const res = await fetch(url, {
                    method: method,
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(body),
                });
                const data = await res.json();
                if (!res.ok) throw new Error(data.error || res.statusText);
                setStatus('Saved.');
                closeSkillDialog();
                loadSkills();
            } catch (err) {
                setStatus('Save failed: ' + err.message, true);
                saveBtn.disabled = false;
            }
        });
    }

    function skillDialogHtml(mode) {
        const title = mode === 'edit' ? 'Edit Skill' : 'Create Skill';
        return (
            '<div class="permission-title">' + title + '</div>' +
            '<div class="skill-form-group">' +
                '<label>Additional instructions for generation (optional)</label>' +
                '<textarea id="skill-user-prompt" rows="2" placeholder="e.g., Focus on the PR review steps only."></textarea>' +
            '</div>' +
            '<div class="skill-form-group">' +
                '<label>Name</label><input type="text" id="skill-name" placeholder="Daily PR digest">' +
            '</div>' +
            '<div class="skill-form-group">' +
                '<label>Slug</label><input type="text" id="skill-slug" placeholder="daily-pr-digest">' +
            '</div>' +
            '<div class="skill-form-group">' +
                '<label>Goal (one sentence)</label><input type="text" id="skill-goal">' +
            '</div>' +
            '<div class="skill-form-group">' +
                '<label>Parameters</label>' +
                '<div id="skill-params"></div>' +
                '<button class="btn btn-outline" id="skill-add-param">+ Add parameter</button>' +
            '</div>' +
            '<div class="skill-form-group">' +
                '<label>Required tools</label>' +
                '<div id="skill-tools" class="skill-tools-list"></div>' +
            '</div>' +
            '<div class="skill-form-group">' +
                '<label>Required tools — explanation (optional)</label>' +
                '<textarea id="skill-prose" rows="2"></textarea>' +
            '</div>' +
            '<div class="skill-form-group">' +
                '<label>Procedure</label>' +
                '<textarea id="skill-procedure" rows="8"></textarea>' +
            '</div>' +
            '<div class="skill-form-group">' +
                '<label>Pitfalls</label>' +
                '<textarea id="skill-pitfalls" rows="3"></textarea>' +
            '</div>' +
            '<div class="skill-form-group">' +
                '<label>Verification</label>' +
                '<textarea id="skill-verification" rows="3"></textarea>' +
            '</div>' +
            '<div id="skill-dialog-status" class="skill-dialog-status"></div>' +
            '<div class="skill-actions-row">' +
                '<div class="skill-regen-group">' +
                    '<button class="btn btn-outline" id="skill-regen-btn">Regenerate</button>' +
                    '<span id="skill-regen-note" class="skill-regen-note"></span>' +
                '</div>' +
                '<div class="skill-actions-right">' +
                    '<button class="btn btn-outline" id="skill-cancel-btn">Cancel</button>' +
                    '<button class="btn btn-primary" id="skill-save-btn">Save</button>' +
                '</div>' +
            '</div>'
        );
    }

    if (createSkillBtn) {
        createSkillBtn.addEventListener('click', () => openSkillDialog({mode: 'create'}));
    }

    loadSkills();

    // ── Jobs ────────────────────────────────────────────────────────
    const jobsList = document.getElementById('jobs-list');
    const createJobBtn = document.getElementById('create-job-btn');
    let _channelsCache = null;
    const _jobRunPolls = new Map();  // job_id → interval id
    const _jobExpanded = new Set();  // currently showing runs

    async function fetchChannels() {
        if (_channelsCache) return _channelsCache;
        try {
            const r = await fetch('/api/jobs/channels');
            _channelsCache = r.ok ? await r.json() : [];
        } catch (_) { _channelsCache = []; }
        return _channelsCache;
    }

    async function fetchSkillsList() {
        try {
            const r = await fetch('/api/skills');
            return r.ok ? await r.json() : [];
        } catch (_) { return []; }
    }

    function _fmtJobTiming(j) {
        if (j.timing.mode === 'one_shot') {
            if (j.timing.executed) return 'One-shot · executed';
            return 'One-shot · ' + (j.timing.run_at || '?');
        }
        return (j.timing.cron || '?') + ' (' + (j.timing.timezone || 'UTC') + ')';
    }

    async function loadJobs() {
        if (!jobsList) return;
        try {
            const r = await fetch('/api/jobs');
            const jobs = await r.json();
            jobsList.innerHTML = '';
            if (!jobs.length) {
                jobsList.innerHTML = '<p class="empty-sidebar">No jobs yet.</p>';
                return;
            }
            jobs.forEach(j => {
                const item = document.createElement('div');
                item.className = 'job-item' + (j.enabled ? '' : ' disabled');
                item.dataset.jobId = j.id;
                item.innerHTML =
                    '<div class="job-name"></div>' +
                    '<div class="job-next"></div>' +
                    '<label class="job-toggle"><input type="checkbox" class="job-enabled"' + (j.enabled ? ' checked' : '') + '> enabled</label>' +
                    '<div class="job-actions">' +
                        '<button class="btn btn-outline" data-act="run">Test</button>' +
                        '<button class="btn btn-outline" data-act="edit">Edit</button>' +
                        '<button class="btn btn-outline" data-act="toggle">Display runs ▼</button>' +
                        '<button class="btn btn-outline" data-act="duplicate">Duplicate</button>' +
                        '<button class="btn btn-danger-outline" data-act="delete">Delete</button>' +
                    '</div>' +
                    '<div class="job-runs" style="display:none"></div>';
                item.querySelector('.job-name').textContent = j.name;
                const next = j.next_run ? ('next: ' + j.next_run) : '';
                item.querySelector('.job-next').textContent = _fmtJobTiming(j) + (next ? ' · ' + next : '');
                jobsList.appendChild(item);
                if (_jobExpanded.has(j.id)) {
                    const runsEl = item.querySelector('.job-runs');
                    runsEl.style.display = 'flex';
                    refreshJobRuns(j.id, runsEl);
                }
            });
        } catch (e) {
            jobsList.innerHTML = '<p class="empty-sidebar">Failed to load jobs.</p>';
        }
    }

    async function refreshJobRuns(jobId, runsEl) {
        try {
            const r = await fetch('/api/jobs/' + encodeURIComponent(jobId) + '/runs?limit=5');
            const runs = await r.json();
            if (!runs.length) {
                runsEl.innerHTML = '<div class="job-run-row">No runs yet.</div>';
                return;
            }
            runsEl.innerHTML = '';
            runs.forEach(r => {
                const row = document.createElement('div');
                row.className = 'job-run-row status-' + (r.status || 'running');
                const skillsFmt = (r.skills || []).map(s =>
                    ({'passed':'✓','failed':'✗','skipped':'–','running':'•'}[s.status] || '·') + s.slug
                ).join(' ');
                row.textContent = _fmtTs(r.started_at) + ' · ' + (r.status || 'running') +
                    ' · ' + r.duration_s.toFixed(1) + 's · ' + skillsFmt;
                runsEl.appendChild(row);
            });
        } catch (_) { /* ignore */ }
    }

    function pollJobRuns(jobId, runsEl) {
        if (_jobRunPolls.has(jobId)) return;
        const iv = setInterval(async () => {
            await refreshJobRuns(jobId, runsEl);
            try {
                const r = await fetch('/api/jobs/' + encodeURIComponent(jobId) + '/runs?limit=3');
                const runs = await r.json();
                if (!runs.some(x => x.status === 'running')) {
                    clearInterval(iv);
                    _jobRunPolls.delete(jobId);
                    loadJobs();  // refresh next-run display
                }
            } catch (_) { /* keep polling */ }
        }, 2500);
        _jobRunPolls.set(jobId, iv);
    }

    if (jobsList) {
        jobsList.addEventListener('click', async (e) => {
            const item = e.target.closest('.job-item');
            if (!item) return;
            const jobId = item.dataset.jobId;
            if (e.target.classList.contains('job-enabled')) {
                const enabled = e.target.checked;
                await fetch('/api/jobs/' + encodeURIComponent(jobId) + '/enable', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({enabled: enabled}),
                });
                loadJobs();
                return;
            }
            const actBtn = e.target.closest('[data-act]');
            if (!actBtn) return;
            const act = actBtn.dataset.act;
            if (act === 'delete') {
                await fetch('/api/jobs/' + encodeURIComponent(jobId), {method: 'DELETE'});
                _jobExpanded.delete(jobId);
                loadJobs();
                return;
            }
            if (act === 'duplicate') {
                actBtn.disabled = true;
                const r = await fetch('/api/jobs/' + encodeURIComponent(jobId) + '/duplicate', {method: 'POST'});
                actBtn.disabled = false;
                if (!r.ok) {
                    const d = await r.json().catch(() => ({}));
                    alert('Duplicate failed: ' + (d.error || r.statusText));
                    return;
                }
                loadJobs();
                return;
            }
            if (act === 'run') {
                actBtn.disabled = true;
                const r = await fetch('/api/jobs/' + encodeURIComponent(jobId) + '/run', {method: 'POST'});
                actBtn.disabled = false;
                if (!r.ok) {
                    const d = await r.json().catch(() => ({}));
                    alert('Run failed: ' + (d.error || r.statusText));
                    return;
                }
                _jobExpanded.add(jobId);
                const runsEl = item.querySelector('.job-runs');
                runsEl.style.display = 'flex';
                refreshJobRuns(jobId, runsEl);
                pollJobRuns(jobId, runsEl);
                return;
            }
            if (act === 'toggle') {
                const runsEl = item.querySelector('.job-runs');
                if (_jobExpanded.has(jobId)) {
                    _jobExpanded.delete(jobId);
                    runsEl.style.display = 'none';
                } else {
                    _jobExpanded.add(jobId);
                    runsEl.style.display = 'flex';
                    refreshJobRuns(jobId, runsEl);
                }
                return;
            }
            if (act === 'edit') {
                const r = await fetch('/api/jobs/' + encodeURIComponent(jobId));
                if (!r.ok) return;
                const data = await r.json();
                openJobDialog({mode: 'edit', job: data});
            }
        });
    }

    function closeJobDialog() {
        const ov = document.getElementById('job-overlay');
        if (ov) ov.remove();
    }

    function _browserTimezone() {
        try { return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'; }
        catch (_) { return 'UTC'; }
    }

    async function openJobDialog(opts) {
        opts = opts || {};
        const mode = opts.mode || 'create';
        const [skills, channels] = await Promise.all([fetchSkillsList(), fetchChannels()]);
        closeJobDialog();

        const overlay = document.createElement('div');
        overlay.className = 'permission-overlay';
        overlay.id = 'job-overlay';
        const dialog = document.createElement('div');
        dialog.className = 'permission-dialog job-dialog';
        dialog.innerHTML = jobDialogHtml(mode, channels);
        overlay.appendChild(dialog);
        document.body.appendChild(overlay);

        const tz = dialog.querySelector('#job-tz');
        tz.value = (opts.job && opts.job.timing && opts.job.timing.timezone) || _browserTimezone();

        // Skill selection state
        let ordered = (opts.job ? opts.job.skills : []).slice();
        const skillMap = {};     // slug -> full Skill (with parameters); cached
        const skillParams = {};  // slug -> { paramName: stringValue } — source of truth
        if (opts.job && opts.job.skill_parameters) {
            for (const [slug, vals] of Object.entries(opts.job.skill_parameters)) {
                skillParams[slug] = Object.assign({}, vals);
            }
        }

        async function ensureSkillLoaded(slug) {
            if (skillMap[slug]) return skillMap[slug];
            try {
                const r = await fetch('/api/skills/' + encodeURIComponent(slug));
                if (!r.ok) return null;
                skillMap[slug] = await r.json();
                return skillMap[slug];
            } catch (_) { return null; }
        }

        // Pre-fetch full skill data for already-selected skills so the first
        // render shows their parameter inputs populated.
        await Promise.all(ordered.map(ensureSkillLoaded));

        function buildParamsSection(slug) {
            const skill = skillMap[slug];
            if (!skill) return null;
            const params = skill.parameters || [];
            if (!params.length) return null;
            const wrap = document.createElement('div');
            wrap.className = 'job-skill-params';
            wrap.style.cssText = 'margin:6px 0 4px 22px;padding:6px 8px;border-left:2px solid var(--border);display:flex;flex-direction:column;gap:4px';
            if (!skillParams[slug]) skillParams[slug] = {};
            const vals = skillParams[slug];
            params.forEach(p => {
                const name = p.name;
                const hasVal = Object.prototype.hasOwnProperty.call(vals, name);
                const current = hasVal ? vals[name] : (p.default || '');
                const row = document.createElement('div');
                row.className = 'job-skill-param-row';
                row.style.cssText = 'display:flex;align-items:center;gap:6px;font-size:0.85em';
                const label = document.createElement('label');
                label.style.cssText = 'min-width:140px;color:var(--text-muted);font-weight:400';
                const req = p.required ? ' <span style="color:var(--danger-hover)" title="required">*</span>' : '';
                label.innerHTML = name + req + ' <span style="color:var(--text-muted)">(' + (p.type || 'string') + ')</span>';
                let input;
                if (p.type === 'bool') {
                    input = document.createElement('input');
                    input.type = 'checkbox';
                    input.checked = current === 'true' || current === '1';
                    vals[name] = input.checked ? 'true' : 'false';
                    input.addEventListener('change', () => {
                        vals[name] = input.checked ? 'true' : 'false';
                    });
                } else if (p.type === 'int') {
                    input = document.createElement('input');
                    input.type = 'number';
                    input.step = '1';
                    input.value = current;
                    input.style.cssText = 'flex:1;padding:2px 4px';
                    vals[name] = String(current);
                    input.addEventListener('input', () => { vals[name] = input.value; });
                } else {
                    input = document.createElement('input');
                    input.type = 'text';
                    input.value = current;
                    input.style.cssText = 'flex:1;padding:2px 4px';
                    vals[name] = String(current);
                    input.addEventListener('input', () => { vals[name] = input.value; });
                }
                row.appendChild(label);
                row.appendChild(input);
                wrap.appendChild(row);
            });
            return wrap;
        }

        function renderSkills() {
            const host = dialog.querySelector('#job-skills-list');
            host.innerHTML = '';
            if (!skills.length) {
                host.innerHTML = '<p style="font-size:0.85em;color:var(--text-muted)">No skills yet — create one first.</p>';
                return;
            }
            // Surface any ordered slug that no longer exists in the skills list
            // (deleted after the job was saved) so the user knows it'll fail.
            const knownSlugs = new Set(skills.map(s => s.slug));
            ordered.filter(sl => !knownSlugs.has(sl)).forEach(sl => {
                const warn = document.createElement('div');
                warn.className = 'job-skills-row';
                warn.style.cssText = 'color:var(--accent-hover);font-size:0.85em';
                warn.textContent = 'Unknown skill: ' + sl + ' (deleted — will fail to run)';
                host.appendChild(warn);
            });
            skills.forEach(s => {
                const runIdx = ordered.indexOf(s.slug);
                const isSel = runIdx !== -1;
                const row = document.createElement('div');
                row.className = 'job-skills-row';
                let controls = '';
                if (isSel) {
                    controls =
                        '<span class="order-num">' + (runIdx + 1) + '</span>' +
                        '<button class="btn btn-outline" data-act="up"' + (runIdx === 0 ? ' disabled' : '') + '>↑</button>' +
                        '<button class="btn btn-outline" data-act="down"' + (runIdx === ordered.length - 1 ? ' disabled' : '') + '>↓</button>';
                }
                row.innerHTML =
                    '<label><input type="checkbox" data-slug="' + s.slug + '"' + (isSel ? ' checked' : '') + '> ' +
                        s.name + ' <span style="color:var(--text-muted)">(' + s.slug + ')</span></label>' +
                    controls;
                row.querySelector('input').addEventListener('change', async (e) => {
                    const slug = e.target.dataset.slug;
                    const i = ordered.indexOf(slug);
                    if (e.target.checked && i === -1) {
                        ordered.push(slug);
                        await ensureSkillLoaded(slug);
                    }
                    if (!e.target.checked && i !== -1) ordered.splice(i, 1);
                    renderSkills();
                });
                if (isSel) {
                    row.querySelector('[data-act="up"]').addEventListener('click', (e) => {
                        e.preventDefault();
                        if (runIdx === 0) return;
                        [ordered[runIdx - 1], ordered[runIdx]] = [ordered[runIdx], ordered[runIdx - 1]];
                        renderSkills();
                    });
                    row.querySelector('[data-act="down"]').addEventListener('click', (e) => {
                        e.preventDefault();
                        if (runIdx === ordered.length - 1) return;
                        [ordered[runIdx + 1], ordered[runIdx]] = [ordered[runIdx], ordered[runIdx + 1]];
                        renderSkills();
                    });
                    const paramsSection = buildParamsSection(s.slug);
                    if (paramsSection) row.appendChild(paramsSection);
                }
                host.appendChild(row);
            });
        }

        renderSkills();

        // Timing mode toggle
        const modeRecur = dialog.querySelector('#job-mode-recur');
        const modeOnce = dialog.querySelector('#job-mode-once');
        const recurBlock = dialog.querySelector('#job-timing-recurring');
        const onceBlock = dialog.querySelector('#job-timing-oneshot');

        function syncMode() {
            const isRecur = modeRecur.checked;
            recurBlock.style.display = isRecur ? 'block' : 'none';
            onceBlock.style.display = isRecur ? 'none' : 'block';
        }
        modeRecur.addEventListener('change', syncMode);
        modeOnce.addEventListener('change', syncMode);

        // Natural-language/cron translator
        const cronInput = dialog.querySelector('#job-cron');
        const previewEl = dialog.querySelector('#job-cron-preview');
        let _previewTimer = null;

        async function refreshPreview() {
            const text = cronInput.value.trim();
            if (!text) {
                previewEl.innerHTML = '<span style="color:var(--text-muted)">Enter a cron expression or a phrase like "every weekday at 8am".</span>';
                return;
            }
            previewEl.innerHTML = '<span style="color:var(--text-muted)">Parsing…</span>';
            try {
                const r = await fetch('/api/jobs/cron/parse', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({text: text, timezone: tz.value || 'UTC'}),
                });
                const d = await r.json();
                if (!r.ok) {
                    previewEl.innerHTML = '<span style="color:var(--danger-hover)">' + (d.error || r.statusText) + '</span>';
                    previewEl.dataset.cron = '';
                    return;
                }
                const nexts = (d.next_occurrences || []).map(n => '<li>' + n + '</li>').join('');
                previewEl.innerHTML =
                    '<div>cron: <code>' + d.cron + '</code></div>' +
                    (d.description ? '<div style="margin-top:4px">' + d.description + '</div>' : '') +
                    '<div style="margin-top:6px;color:var(--text-muted)">Next 5 fires (' + (d.timezone || 'UTC') + '):</div>' +
                    '<ul class="cron-next" style="margin:2px 0 0 16px;padding:0">' + nexts + '</ul>';
                previewEl.dataset.cron = d.cron;
            } catch (e) {
                previewEl.innerHTML = '<span style="color:var(--danger-hover)">Network error: ' + e.message + '</span>';
                previewEl.dataset.cron = '';
            }
        }
        cronInput.addEventListener('input', () => {
            clearTimeout(_previewTimer);
            _previewTimer = setTimeout(refreshPreview, 450);
        });
        tz.addEventListener('change', refreshPreview);

        // Pre-populate form if editing
        if (mode === 'edit' && opts.job) {
            dialog.querySelector('#job-name').value = opts.job.name || '';
            if (opts.job.timing.mode === 'one_shot') {
                modeOnce.checked = true;
                dialog.querySelector('#job-run-at').value = opts.job.timing.run_at || '';
            } else {
                modeRecur.checked = true;
                cronInput.value = opts.job.timing.cron || '';
                setTimeout(refreshPreview, 0);
            }
            dialog.querySelector('input[name="on-failure"][value="' + opts.job.on_failure + '"]').checked = true;
            dialog.querySelector('#job-notify-channel').value = opts.job.notification.channel || '';
            dialog.querySelector('input[name="notify-on"][value="' + opts.job.notification.notify_on + '"]').checked = true;
            dialog.querySelector('#job-include-output').checked = !!opts.job.notification.include_output;
            dialog.querySelector('#job-enabled').checked = !!opts.job.enabled;
        } else {
            modeRecur.checked = true;
            dialog.querySelector('#job-enabled').checked = true;
            dialog.querySelector('#job-include-output').checked = true;
            dialog.querySelector('input[name="on-failure"][value="stop"]').checked = true;
            dialog.querySelector('input[name="notify-on"][value="failure"]').checked = true;
        }
        syncMode();

        dialog.querySelector('#job-cancel-btn').addEventListener('click', closeJobDialog);
        dialog.querySelector('#job-save-btn').addEventListener('click', async (e) => {
            e.preventDefault();
            const statusEl = dialog.querySelector('#job-dialog-status');
            function setStatus(msg, isErr) {
                statusEl.textContent = msg;
                statusEl.className = 'job-dialog-status' + (isErr ? ' error' : '');
            }
            const name = dialog.querySelector('#job-name').value.trim();
            if (!name) return setStatus('Name is required.', true);
            if (!ordered.length) return setStatus('Select at least one skill.', true);
            const isRecur = modeRecur.checked;
            let timing;
            if (isRecur) {
                const cron = (previewEl.dataset.cron || cronInput.value).trim();
                if (!cron) return setStatus('Cron expression is required and must parse successfully.', true);
                timing = {mode: 'recurring', cron: cron, timezone: tz.value || 'UTC', run_at: '', executed: false};
            } else {
                const runAt = dialog.querySelector('#job-run-at').value;
                if (!runAt) return setStatus('Pick a date/time.', true);
                timing = {mode: 'one_shot', cron: '', run_at: runAt, timezone: tz.value || 'UTC', executed: false};
            }
            // Build skill_parameters payload, only including slugs whose skill
            // has declared parameters. Fall back to each param's default when
            // the user left the field untouched.
            const skillParamsOut = {};
            for (const slug of ordered) {
                const skill = skillMap[slug];
                if (!skill || !skill.parameters || !skill.parameters.length) continue;
                const vals = skillParams[slug] || {};
                const out = {};
                for (const p of skill.parameters) {
                    const v = Object.prototype.hasOwnProperty.call(vals, p.name)
                        ? vals[p.name]
                        : (p.default || '');
                    out[p.name] = String(v);
                }
                skillParamsOut[slug] = out;
            }
            // Required-param validation: surface missing values before sending
            // a request that would just fail inside the skill run.
            for (const slug of ordered) {
                const skill = skillMap[slug];
                if (!skill) continue;
                for (const p of (skill.parameters || [])) {
                    if (!p.required) continue;
                    const v = (skillParamsOut[slug] || {})[p.name];
                    if (!v) {
                        return setStatus(
                            'Parameter "' + p.name + '" is required for skill "' + (skill.name || skill.slug) + '".',
                            true,
                        );
                    }
                }
            }
            const body = {
                name: name,
                skills: ordered,
                skill_parameters: skillParamsOut,
                timing: timing,
                on_failure: dialog.querySelector('input[name="on-failure"]:checked').value,
                notification: {
                    channel: dialog.querySelector('#job-notify-channel').value || '',
                    notify_on: dialog.querySelector('input[name="notify-on"]:checked').value,
                    include_output: dialog.querySelector('#job-include-output').checked,
                },
                enabled: dialog.querySelector('#job-enabled').checked,
            };
            const url = mode === 'edit' ? '/api/jobs/' + encodeURIComponent(opts.job.id) : '/api/jobs';
            const method = mode === 'edit' ? 'PUT' : 'POST';
            setStatus('Saving…');
            try {
                const r = await fetch(url, {
                    method: method,
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(body),
                });
                const d = await r.json();
                if (!r.ok) return setStatus(d.error || r.statusText, true);
                closeJobDialog();
                loadJobs();
            } catch (err) {
                setStatus('Network error: ' + err.message, true);
            }
        });
    }

    function jobDialogHtml(mode, channels) {
        const title = mode === 'edit' ? 'Edit Job' : 'Create Job';
        const channelOpts = ['<option value="">(none)</option>']
            .concat(channels.map(c =>
                '<option value="' + c.alias + '">' + c.alias + ' (' + c.platform + ')</option>'
            )).join('');
        return (
            '<div class="permission-title">' + title + '</div>' +
            '<div class="job-form-group">' +
                '<label>Name</label><input type="text" id="job-name" placeholder="Nightly UI tests">' +
            '</div>' +
            '<div class="job-form-group">' +
                '<label>Skills</label>' +
                '<div id="job-skills-list" class="job-skills-list"></div>' +
                '<div style="margin-top:4px;font-size:0.8em;color:var(--text-muted)">Checked skills run serial in numbered order. Use ↑/↓ to reorder.</div>' +
            '</div>' +
            '<div class="job-form-group">' +
                '<label>Timing</label>' +
                '<div class="job-radio-row">' +
                    '<label><input type="radio" name="job-mode" id="job-mode-recur" value="recurring"> Recurring</label>' +
                    '<label><input type="radio" name="job-mode" id="job-mode-once" value="one_shot"> One-shot</label>' +
                '</div>' +
                '<div id="job-timing-recurring" class="job-timing-block">' +
                    '<label>Cron expression or natural language</label>' +
                    '<input type="text" id="job-cron" placeholder="e.g. 0 8 * * 1-5 or \'every weekday at 8am\'">' +
                    '<div class="cron-preview" id="job-cron-preview"><span style="color:var(--text-muted)">Enter a cron expression or a phrase like "every weekday at 8am".</span></div>' +
                '</div>' +
                '<div id="job-timing-oneshot" class="job-timing-block" style="display:none">' +
                    '<label>Run at</label>' +
                    '<input type="datetime-local" id="job-run-at">' +
                '</div>' +
                '<div style="margin-top:8px">' +
                    '<label>Timezone</label>' +
                    '<input type="text" id="job-tz" placeholder="UTC">' +
                '</div>' +
            '</div>' +
            '<div class="job-form-group">' +
                '<label>On skill failure</label>' +
                '<div class="job-radio-row">' +
                    '<label><input type="radio" name="on-failure" value="stop"> Stop (default)</label>' +
                    '<label><input type="radio" name="on-failure" value="continue"> Continue with next skill</label>' +
                '</div>' +
            '</div>' +
            '<div class="job-form-group">' +
                '<label>Notification channel</label>' +
                '<select id="job-notify-channel">' + channelOpts + '</select>' +
                '<div class="job-radio-row" style="margin-top:8px">' +
                    '<label><input type="radio" name="notify-on" value="failure"> On failure</label>' +
                    '<label><input type="radio" name="notify-on" value="always"> Always</label>' +
                    '<label><input type="radio" name="notify-on" value="verification_fail"> Verification fail</label>' +
                '</div>' +
                '<label class="job-toggle" style="margin-top:8px;font-weight:400"><input type="checkbox" id="job-include-output"> Include outputs in notification</label>' +
            '</div>' +
            '<div class="job-form-group">' +
                '<label class="job-toggle" style="font-weight:400"><input type="checkbox" id="job-enabled"> Enabled</label>' +
            '</div>' +
            '<div id="job-dialog-status" class="job-dialog-status"></div>' +
            '<div class="job-actions-row">' +
                '<button class="btn btn-outline" id="job-cancel-btn">Cancel</button>' +
                '<button class="btn btn-primary" id="job-save-btn">Save</button>' +
            '</div>'
        );
    }

    if (createJobBtn) {
        createJobBtn.addEventListener('click', () => openJobDialog({mode: 'create'}));
    }

    loadJobs();
}

// --- Theme toggle (sun/moon button in top-bar) ---
(function initThemeToggle() {
    var btn = document.getElementById('theme-toggle');
    if (!btn) return;
    function syncBtn(theme) {
        var isDark = theme === 'dark';
        btn.setAttribute('aria-pressed', isDark ? 'true' : 'false');
        btn.title = isDark ? 'Switch to light theme' : 'Switch to dark theme';
    }
    syncBtn(document.documentElement.dataset.theme || 'light');
    btn.addEventListener('click', function () {
        var next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
        document.documentElement.dataset.theme = next;
        try { localStorage.setItem('oc-theme', next); } catch (e) {}
        syncBtn(next);
    });
})();
