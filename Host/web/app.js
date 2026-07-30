(() => {
  const regions = {
    chat:   { x: 0.00, y: 0.00, w: 1.00, h: 0.90 },
    side:   { x: 0.00, y: 0.00, w: 0.38, h: 1.00 },
    full:   { x: 0.00, y: 0.00, w: 1.00, h: 1.00 },
    input:  { x: 0.50, y: 0.94 },
  };
  let tab = 'agent';
  let crop = 'full';
  let srcBmp = null;
  let srcW = 960, srcH = 540;
  let filesPath = null;
  let previewPath = null;
  let agentState = null;
  let editMsgId = null;
  let modelCache = [];
  let pendingImages = [];
  let pendingTabSwitch = null;
  let chatPinnedBottom = true;
  let lastChatRenderKey = '';
  let lastRenderedMsgCount = 0;
  let userScrolledUpAt = 0;
  let agentTreeExpanded = false;
  let lastAgentTreeSetKey = '';
  let lastAgentTreeRenderKey = '';
  let viewingTabId = null;
  let activeConversation = { id: '', title: '' };
  let optimisticMessages = [];
  const CONV_CACHE_KEY = 'cdesk:conv:v1';
  const CLIENT_OUTBOX_KEY = 'cdesk:outbox:v1';
  const AGENT_PREFS_KEY = 'cdesk:agentPrefs:v1';
  const MODE_SLASH = {
    agent: '',
    plan: '/plan',
    ask: '/ask',
    debug: '/debug',
    multitask: '/multitask',
    triage: '/multitask',
    edit: '/edit',
  };
  let composerMode = 'agent';
  let stickyModeSlash = '';
  let newAgentWorkspace = '';
  let newAgentModel = '';
  let newAgentMode = 'agent';
  let newAgentBrowsePath = null;

  function readAgentPrefs() {
    try { return JSON.parse(localStorage.getItem(AGENT_PREFS_KEY) || '{}'); }
    catch (e) { return {}; }
  }
  function saveAgentPrefs(patch) {
    try {
      localStorage.setItem(AGENT_PREFS_KEY, JSON.stringify({ ...readAgentPrefs(), ...patch }));
    } catch (e) {}
  }
  function preparePromptText(raw) {
    let body = String(raw || '').trim();
    if (!body) return body;
    if (/^\/(agent|plan|ask|debug|multitask|triage|model|edit)\b/i.test(body)) return body;
    // Mode buttons are the source of truth. stickyModeSlash must always track
    // the selected mode — never a stale /multitask leftover from an earlier chat.
    const slash = MODE_SLASH[composerMode] || '';
    if (slash) return `${slash} ${body}`.trim();
    return body;
  }
  function setComposerMode(mode) {
    composerMode = String(mode || 'agent').toLowerCase();
    stickyModeSlash = MODE_SLASH[composerMode] || '';
    document.querySelectorAll('#modeRow button').forEach((btn) => {
      btn.classList.toggle('on', btn.dataset.mode === composerMode);
    });
    const slash = stickyModeSlash;
    text.placeholder = slash
      ? `Message (${slash} …)`
      : 'Message Cursor agent…';
    saveAgentPrefs({ mode: composerMode });
  }
  function closeNewAgentModal() {
    newAgentModal.classList.remove('open');
    if (!drawer.classList.contains('open')) scrim.classList.remove('open');
    newAgentBrowsePath = null;
  }
  function renderNewAgentWorkspaces(workspaces, roots) {
    const rows = (workspaces || []).map((ws) =>
      `<button type="button" class="pickRow${newAgentWorkspace === ws.path ? ' active' : ''}" data-path="${escapeHtml(ws.path)}">${escapeHtml(ws.label || ws.name)}<span class="sub">${escapeHtml(ws.path)}</span></button>`
    ).join('');
    newAgentWorkspaceList.innerHTML = rows || '<div class="sub" style="padding:8px">No project folders found — browse below.</div>';
    newAgentWorkspaceList.querySelectorAll('[data-path]').forEach((btn) => {
      btn.onclick = () => {
        newAgentWorkspace = btn.dataset.path || '';
        newAgentPath.textContent = newAgentWorkspace;
        newAgentPath.classList.add('picked');
        renderNewAgentWorkspaces(workspaces, roots);
      };
    });
  }
  function renderNewAgentModels(models) {
    const picks = (models || []).filter((m) => m.group !== 'header');
    if (!picks.length) {
      newAgentModelList.innerHTML = '<div class="sub" style="padding:8px">Open Agents in Cursor to load models.</div>';
      return;
    }
    const current = newAgentModel || picks.find((m) => m.selected)?.label || picks[0].label;
    newAgentModel = current;
    newAgentModelList.innerHTML = picks.map((m) =>
      `<button type="button" class="pickRow${m.label === newAgentModel ? ' active' : ''}" data-model="${escapeHtml(m.id || m.label)}">${escapeHtml(m.label)}${m.selected ? '<span class="sub">current</span>' : ''}</button>`
    ).join('');
    newAgentModelList.querySelectorAll('[data-model]').forEach((btn) => {
      btn.onclick = () => {
        newAgentModel = btn.textContent.replace(/\s*current$/i, '').trim();
        renderNewAgentModels(models);
      };
    });
  }
  async function loadNewAgentBrowse(path) {
    const data = await fetch('/api/files' + (path ? `?path=${encodeURIComponent(path)}` : '')).then((r) => r.json());
    if (!data.ok) {
      showToast(data.error || 'Could not browse folders');
      return;
    }
    newAgentBrowsePath = data.path || null;
    const head = data.path
      ? `<button type="button" class="pickRow active" data-use="1">Use this folder<span class="sub">${escapeHtml(data.path)}</span></button>`
        + `<button type="button" class="pickRow ghost" data-parent="${escapeHtml(data.parent || '')}">↑ Up</button>`
      : '';
    const dirs = (data.entries || []).filter((e) => e.isDir).map((e) =>
      `<button type="button" class="pickRow" data-dir="${escapeHtml(e.path)}">${escapeHtml(e.name)}<span class="sub">folder</span></button>`
    ).join('');
    const roots = (data.roots || []).map((r) =>
      `<button type="button" class="pickRow" data-dir="${escapeHtml(r.path)}">${escapeHtml(r.label)}<span class="sub">root</span></button>`
    ).join('');
    newAgentWorkspaceList.innerHTML = head + (data.path ? dirs : roots + dirs);
    newAgentWorkspaceList.querySelectorAll('[data-use]').forEach((btn) => {
      btn.onclick = () => {
        if (!data.path) return;
        newAgentWorkspace = data.path;
        newAgentPath.textContent = data.path;
        newAgentPath.classList.add('picked');
        fetch('/api/workspaces').then((r) => r.json()).then((ws) => {
          renderNewAgentWorkspaces(ws.workspaces || [], ws.roots || []);
        }).catch(() => {});
      };
    });
    newAgentWorkspaceList.querySelectorAll('[data-parent]').forEach((btn) => {
      btn.onclick = () => loadNewAgentBrowse(btn.dataset.parent || null);
    });
    newAgentWorkspaceList.querySelectorAll('[data-dir]').forEach((btn) => {
      btn.onclick = () => {
        const p = btn.dataset.dir || '';
        if (btn.querySelector('.sub')?.textContent === 'folder') {
          loadNewAgentBrowse(p);
          return;
        }
        newAgentWorkspace = p;
        newAgentPath.textContent = p;
        newAgentPath.classList.add('picked');
      };
    });
  }
  async function openNewAgentModal() {
    const prefs = readAgentPrefs();
    newAgentWorkspace = prefs.workspace || newAgentWorkspace || '';
    newAgentModel = prefs.model || newAgentModel || '';
    newAgentMode = prefs.mode || composerMode || 'agent';
    newAgentPath.textContent = newAgentWorkspace || 'Pick a project folder…';
    newAgentPath.classList.toggle('picked', !!newAgentWorkspace);
    document.querySelectorAll('#newAgentModeRow button').forEach((btn) => {
      btn.classList.toggle('on', btn.dataset.mode === newAgentMode);
    });
    openDrawer(false);
    closeSheet();
    editModal.classList.remove('open');
    newAgentModal.classList.add('open');
    scrim.classList.add('open');
    try {
      const ws = await fetch('/api/workspaces').then((r) => r.json());
      renderNewAgentWorkspaces(ws.workspaces || [], ws.roots || []);
    } catch (e) {
      newAgentWorkspaceList.innerHTML = '<div class="sub" style="padding:8px">Could not load workspaces.</div>';
    }
    agentSend({ type: 'list_models' });
  }
  async function createNewAgent() {
    if (!newAgentWorkspace) {
      showToast('Choose a working folder first');
      return;
    }
    saveAgentPrefs({
      workspace: newAgentWorkspace,
      model: newAgentModel,
      mode: newAgentMode,
    });
    setComposerMode(newAgentMode);
    closeNewAgentModal();
    showToast('Creating agent…');
    try {
      const result = await fetch('/api/agent/new', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          workspace: newAgentWorkspace,
          model: newAgentModel,
          mode: newAgentMode,
        }),
        cache: 'no-store',
      }).then((r) => r.json());
      if (!result.ok) {
        showToast(result.error || 'Could not create agent');
        return;
      }
      // Keep the selected mode button authoritative; do not leave a sticky
      // slash that outlives a later tap of Agent.
      setComposerMode(newAgentMode);
      if (result.warnings?.length) showToast(result.warnings[0]);
      else showToast(`Agent ready · ${newAgentWorkspace.split(/[\\/]/).pop() || 'project'}`);
      // Do not keep targeting the previous conversation after creating a new agent.
      activeConversation = { id: '', title: '' };
      viewingTabId = null;
      pendingTabSwitch = null;
      optimisticMessages = [];
      lastChatRenderKey = '';
      chatLog.innerHTML = '<div class="msg"><div class="body" style="opacity:.55">New agent — send a message to start.</div></div>';
    } catch (e) {
      showToast('Create agent failed — host unreachable');
    }
  }
  function readConvStore() {
    try { return JSON.parse(localStorage.getItem(CONV_CACHE_KEY) || '{}'); }
    catch (e) { return {}; }
  }
  function saveConvCacheEntry(tabId, entry) {
    if (!tabId || !entry) return;
    const all = readConvStore();
    all[tabId] = {
      title: entry.title || all[tabId]?.title || '',
      messages: entry.messages || [],
      updatedAt: Date.now(),
    };
    const keys = Object.keys(all).sort((a, b) => (all[b].updatedAt || 0) - (all[a].updatedAt || 0));
    keys.slice(48).forEach((k) => delete all[k]);
    try { localStorage.setItem(CONV_CACHE_KEY, JSON.stringify(all)); } catch (e) {}
  }
  function getConvCache(tabId) {
    return readConvStore()[tabId] || null;
  }
  function switchCacheReady(tabId) {
    const entry = getConvCache(tabId);
    return !!(entry?.messages?.length);
  }
  function mergeServerCaches(caches) {
    if (!caches || typeof caches !== 'object') return;
    Object.keys(caches).forEach((tabId) => {
      const remote = caches[tabId];
      if (!remote?.messages?.length) return;
      const local = getConvCache(tabId);
      const localAt = Number(local?.updatedAt || 0);
      const remoteAt = Number(remote.updatedAt || 0) * (remote.updatedAt < 1e12 ? 1000 : 1);
      const localCount = local?.messages?.length || 0;
      if (!localCount || remoteAt > localAt || remote.messages.length > localCount) {
        saveConvCacheEntry(tabId, remote);
      }
    });
  }
  async function prefetchServerCaches() {
    try {
      const r = await fetch('/api/agent/conversation-caches', { cache: 'no-store' });
      const data = await r.json();
      if (data.ok && data.caches) mergeServerCaches(data.caches);
    } catch (e) {}
  }
  function readClientOutbox() {
    try { return JSON.parse(localStorage.getItem(CLIENT_OUTBOX_KEY) || '[]'); }
    catch (e) { return []; }
  }
  function writeClientOutbox(items) {
    try { localStorage.setItem(CLIENT_OUTBOX_KEY, JSON.stringify(items.slice(-12))); } catch (e) {}
  }
  function setActiveConversation(id, title) {
    activeConversation = { id: String(id || ''), title: String(title || '') };
    viewingTabId = activeConversation.id;
    optimisticMessages = optimisticMessages.filter(
      (m) => !m.tabId || m.tabId === activeConversation.id
    );
    lastAgentTreeRenderKey = '';
    if (agentState) {
      renderSubagentDock(
        agentState,
        (agentState.approvals || []).length > 0 || (agentState.rejects || []).length > 0
      );
    }
  }
  function normalizeConvTitle(title) {
    return String(title || '')
      .replace(/\s+/g, ' ')
      .trim()
      .replace(/\s+(\d+\s*[smhdw]\s+ago|\d+\s+[smhdw]\s+ago|just now|now)$/i, '')
      .trim();
  }
  function titlesMatch(want, got) {
    want = normalizeConvTitle(want).toLowerCase();
    got = normalizeConvTitle(got).toLowerCase();
    if (!want || !got) return false;
    return want === got || got.startsWith(want) || want.startsWith(got);
  }
  function conversationMatches(state, target) {
    if (!target?.id && !target?.title) return true;
    const active = (state?.tabs || []).find((t) => t.active);
    if (target.id && active?.id && target.id === active.id) return true;
    if (target.title && active?.title && titlesMatch(target.title, active.title)) return true;
    const sid = String(state?.selectedTabId || '');
    const stitle = String(state?.selectedTabTitle || '');
    if (target.id && sid && target.id === sid) return true;
    if (target.title && stitle && titlesMatch(target.title, stitle)) return true;
    return false;
  }
  function sendTarget(state) {
    if (activeConversation.id || activeConversation.title) {
      return { id: activeConversation.id, title: activeConversation.title };
    }
    const active = (state?.tabs || []).find((t) => t.active);
    if (active) return { id: active.id || '', title: active.title || '' };
    return {
      id: state?.selectedTabId || '',
      title: state?.selectedTabTitle || '',
    };
  }
  function messagesForActiveConv(state) {
    const st = state || agentState;
    const target = sendTarget(st);
    let messagesForView = st?.messages || [];
    if ((target.id || target.title) && !conversationMatches(st, target)) {
      const cached = getConvCache(target.id);
      messagesForView = cached?.messages?.length ? cached.messages : [];
    }
    return { messagesForView, target };
  }
  function currentTabId(state) {
    return sendTarget(state || agentState).id;
  }
  function promptTarget(state) {
    const t = sendTarget(state || agentState);
    return { tab_id: t.id, tab_title: t.title };
  }
  function outboxMatches(item, payload) {
    if (payload.request_id && item.request_id) return item.request_id === payload.request_id;
    if (item.text !== payload.text) return false;
    if (payload.tab_id && item.tab_id && item.tab_id !== payload.tab_id) return false;
    if (payload.tab_title && item.tab_title && item.tab_title !== payload.tab_title) return false;
    return true;
  }
  function showCachedConversation(tabId, entry, syncing) {
    if (!entry?.messages?.length) return false;
    setActiveConversation(tabId, entry.title || activeConversation.title);
    lastChatRenderKey = '';
    const scoped = optimisticMessages.filter((m) => !m.tabId || m.tabId === tabId);
    renderChatMessages(normalizeMessages(entry.messages, false).concat(scoped));
    if (syncing && tab === 'agent') {
      meta.textContent = `${entry.title || 'Conversation'} · syncing…`;
    }
    return true;
  }
  async function hydrateConversationCache(tabId) {
    const local = getConvCache(tabId);
    try {
      const r = await fetch(`/api/agent/conversation-cache?tab_id=${encodeURIComponent(tabId)}`, { cache: 'no-store' });
      const data = await r.json();
      if (data.ok && data.messages?.length) {
        const localAt = Number(local?.updatedAt || 0);
        const remoteAt = Number(data.updatedAt || 0) * (data.updatedAt < 1e12 ? 1000 : 1);
        const localCount = local?.messages?.length || 0;
        const remoteCount = data.messages.length;
        if (!localCount || remoteAt > localAt || remoteCount > localCount) {
          saveConvCacheEntry(tabId, data);
          return data;
        }
      }
    } catch (e) {}
    return local?.messages?.length ? local : null;
  }
  function appendOptimisticHuman(text, imageCount) {
    const trimmed = String(text || '').trim();
    if (!trimmed && !imageCount) return;
    const { messagesForView, target } = messagesForActiveConv(agentState);
    optimisticMessages.push({
      type: 'human',
      role: 'human',
      text: trimmed || `(📷 ×${imageCount})`,
      id: `local-${Date.now()}`,
      tabId: target.id,
      ago: 'sending…',
      pending: true,
    });
    lastChatRenderKey = '';
    renderChatMessages(
      normalizeMessages(messagesForView, false)
        .concat(optimisticMessages.filter((m) => !m.tabId || m.tabId === target.id))
    );
    chatPinnedBottom = true;
    scrollChatToEnd();
  }
  function normalizeForMatch(s) {
    return String(s || '')
      .replace(/\s+/g, ' ')
      .trim()
      .replace(/^\/(agent|plan|ask|debug|multitask|triage|model|edit)\s+/i, '');
  }
  function markOptimisticSent(text, tabId) {
    const want = normalizeForMatch(text);
    if (!want) return;
    let changed = false;
    optimisticMessages.forEach((opt) => {
      if (tabId && opt.tabId && opt.tabId !== tabId) return;
      if (normalizeForMatch(opt.text) === want || want.includes(normalizeForMatch(opt.text))) {
        opt.pending = false;
        opt.failed = false;
        opt.error = '';
        opt.ago = '';
        changed = true;
      }
    });
    if (changed) lastChatRenderKey = '';
  }
  function failOptimistic(text, error, tabId) {
    const want = normalizeForMatch(text);
    if (!want) return;
    let changed = false;
    optimisticMessages.forEach((opt) => {
      if (tabId && opt.tabId && opt.tabId !== tabId) return;
      if (!opt.pending && !opt.failed) return;
      if (normalizeForMatch(opt.text) === want || want.includes(normalizeForMatch(opt.text))) {
        opt.pending = false;
        opt.failed = true;
        opt.error = String(error || 'Send failed');
        opt.ago = 'failed — tap to retry';
        changed = true;
      }
    });
    if (changed) lastChatRenderKey = '';
  }
  function dismissStaleOptimistic(loading) {
    if (!optimisticMessages.length) return;
    const now = Date.now();
    let changed = false;
    optimisticMessages.forEach((opt) => {
      if (!opt.pending) return;
      const age = now - Number(String(opt.id || '').replace('local-', '') || 0);
      if (age > 45000) {
        opt.pending = false;
        opt.failed = true;
        opt.error = 'No confirmation from Cursor';
        opt.ago = 'failed — tap to retry';
        changed = true;
      }
    });
    if (changed) lastChatRenderKey = '';
  }
  function reconcileOptimistic(messages) {
    if (!optimisticMessages.length) return;
    // Only treat recent human messages as confirmation — older identical text
    // (e.g. "continue") must not clear a still-pending send.
    const humans = (messages || []).filter((m) => {
      const k = String(m.type || m.role || '').toLowerCase();
      return k === 'human';
    }).slice(-8);
    optimisticMessages = optimisticMessages.filter((opt) => {
      const want = normalizeForMatch(opt.text);
      if (!want) return false;
      return !humans.some((m) => {
        const got = normalizeForMatch(m.text);
        if (!got) return false;
        return got === want || got.includes(want) || want.includes(got.slice(0, Math.min(got.length, 160)));
      });
    });
  }
  async function postPromptDurable(payload, keepalive) {
    try {
      const r = await fetch('/api/agent/prompt', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
        keepalive: !!keepalive,
        cache: 'no-store',
      });
      return await r.json();
    } catch (e) {
      return { ok: false, error: String(e) };
    }
  }
  function queueClientOutbox(payload) {
    const items = readClientOutbox();
    const queued = {
      ...payload,
      id: `local-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`,
      request_id: payload.request_id || `phone-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`,
      at: Date.now(),
    };
    items.push(queued);
    writeClientOutbox(items);
    return queued;
  }
  function dropClientOutbox(payload) {
    writeClientOutbox(readClientOutbox().filter((item) => !outboxMatches(item, payload)));
  }
  function outboxPayloadFromItem(item) {
    return {
      text: item.text,
      images: item.images || [],
      tab_id: item.tab_id || item.tabId || '',
      tab_title: item.tab_title || item.tabTitle || '',
      request_id: item.request_id || item.requestId || item.id || '',
    };
  }
  function handlePromptDeliveryResult(result, payload) {
    if (result?.sent) {
      dropClientOutbox(payload);
      markOptimisticSent(payload.text, payload.tab_id);
      return;
    }
    if (result?.queued) {
      dropClientOutbox(payload);
      if (result.direct_error) showToast(result.direct_error);
      return;
    }
    const err = result?.error || result?.direct_error || 'Message could not be sent';
    failOptimistic(payload.text, err, payload.tab_id);
    showToast(err);
  }
  function reconcileOutboxWithMessages(messages, tabId) {
    const items = readClientOutbox();
    if (!items.length) return;
    const humans = (messages || []).filter((m) => {
      const k = String(m.type || m.role || '').toLowerCase();
      return k === 'human';
    });
    items.forEach((item) => {
      if (tabId && item.tab_id && item.tab_id !== tabId) return;
      const want = normalizeForMatch(item.text);
      if (!want) return;
      const found = humans.some((m) => {
        const got = normalizeForMatch(m.text);
        return got && (got === want || got.includes(want) || want.includes(got.slice(0, 160)));
      });
      if (found) {
        dropClientOutbox(outboxPayloadFromItem(item));
        markOptimisticSent(item.text, item.tab_id);
      }
    });
  }
  async function flushClientOutbox(keepalive, force) {
    if (!keepalive && !force && aws && aws.readyState === 1) return;
    const items = readClientOutbox();
    if (!items.length) return;
    for (const item of items) {
      const payload = outboxPayloadFromItem(item);
      const result = await postPromptDurable(payload, keepalive);
      if (result?.sent) {
        writeClientOutbox(readClientOutbox().filter((x) => x.id !== item.id));
        markOptimisticSent(payload.text, payload.tab_id);
      } else if (result?.queued) {
        writeClientOutbox(readClientOutbox().filter((x) => x.id !== item.id));
      } else if (result?.error) {
        failOptimistic(payload.text, result.error, payload.tab_id);
        if (!keepalive) showToast(result.error);
        break;
      } else if (keepalive) {
        break;
      }
    }
  }
  function retryOptimisticMessage(el) {
    const mid = el.dataset.mid || '';
    const opt = optimisticMessages.find((m) => (m.id || m.messageId) === mid);
    if (!opt) return;
    const target = opt.tabId
      ? { tab_id: opt.tabId, tab_title: getConvCache(opt.tabId)?.title || activeConversation.title }
      : promptTarget(agentState);
    opt.pending = true;
    opt.failed = false;
    opt.error = '';
    opt.ago = 'sending…';
    lastChatRenderKey = '';
    const payload = {
      type: 'prompt',
      text: opt.text,
      images: [],
      tab_id: target.tab_id,
      tab_title: target.tab_title,
    };
    const queued = queueClientOutbox(payload);
    postPromptDurable(queued, false).then((result) => handlePromptDeliveryResult(result, queued));
    renderAgent(agentState);
  }

  const canvas = document.getElementById('frame');
  const ctx = canvas.getContext('2d', { alpha: false, desynchronized: true });
  const desktop = document.getElementById('desktop');
  const shell = document.getElementById('shell');
  const meta = document.getElementById('meta');
  const text = document.getElementById('text');
  const imageInput = document.getElementById('imageInput');
  let composerFocused = false;
  const dock = document.getElementById('dock');
  const attachmentTray = document.getElementById('attachmentTray');
  const chatLog = document.getElementById('chatLog');
  const agentBanner = document.getElementById('agentBanner');
  const approvals = document.getElementById('approvals');
  const agentTreeDock = document.getElementById('agentTreeDock');
  const agentTreeToggle = document.getElementById('agentTreeToggle');
  const agentTreeList = document.getElementById('agentTreeList');
  const filesList = document.getElementById('filesList');
  const filesPathEl = document.getElementById('filesPath');
  const rootSel = document.getElementById('rootSel');
  const preview = document.getElementById('preview');
  const previewImg = document.getElementById('previewImg');
  const previewText = document.getElementById('previewText');
  const agentImagePreview = document.getElementById('agentImagePreview');
  const agentImagePreviewImg = document.getElementById('agentImagePreviewImg');
  const agentImagePreviewClose = document.getElementById('agentImagePreviewClose');
  const drawer = document.getElementById('drawer');
  const scrim = document.getElementById('scrim');
  const repoList = document.getElementById('repoList');
  const winList = document.getElementById('winList');
  const sheet = document.getElementById('sheet');
  const sheetBody = document.getElementById('sheetBody');
  const editModal = document.getElementById('editModal');
  const editText = document.getElementById('editText');
  const newAgentModal = document.getElementById('newAgentModal');
  const newAgentPath = document.getElementById('newAgentPath');
  const newAgentWorkspaceList = document.getElementById('newAgentWorkspaceList');
  const newAgentModelList = document.getElementById('newAgentModelList');
  const toast = document.getElementById('toast');
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';

  let ws = null;
  let aws = null;
  let ptr = 'idle';
  let moved = false;
  let startX = 0, startY = 0, startCx = 0, startCy = 0, lastClientY = 0;
  let wheelAcc = 0;
  let lastNorm = { x: 0.5, y: 0.5 };
  let armRight = false;
  let qualityCycle = ['sharp', 'ultra', 'balanced', 'smooth'];
  let qualityIdx = 0;
  const MOVE_PX = 22;
  let frames = 0, tFps = performance.now(), shownFps = 0;
  let pending = null, drawing = false;
  let fitScale = 1, userZoom = 1, panX = 0, panY = 0, pinch = null, lastTapAt = 0;

  function region() { return regions[crop] || regions.full; }
  function escapeHtml(s) {
    return String(s || '').replace(/[&<>"']/g, (c) => ({
      '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
    }[c]));
  }
  function showToast(msg) {
    toast.textContent = msg;
    toast.classList.add('show');
    clearTimeout(showToast._t);
    showToast._t = setTimeout(() => toast.classList.remove('show'), 3500);
    try {
      if (navigator.vibrate) navigator.vibrate(40);
      if (Notification.permission === 'granted') {
        new Notification('CursorDesk', { body: msg, silent: false });
      }
    } catch (e) {}
  }
  function agentImageUrl(image) {
    const path = String(image?.path || '').trim();
    const src = String(image?.src || '').trim();
    if (path) return `/api/agent-image?path=${encodeURIComponent(path)}`;
    if (src.startsWith('local:')) return `/api/agent-image?path=${encodeURIComponent(src.slice(6))}`;
    if (/^data:image\//i.test(src) || /^https?:\/\//i.test(src)) return src;
    if (/\.(png|jpe?g|webp|gif|bmp)(\?|$)/i.test(src) || /^[A-Za-z]:\\/.test(src) || src.startsWith('/'))
      return `/api/agent-image?path=${encodeURIComponent(src)}`;
    return `/api/agent-image?src=${encodeURIComponent(src)}`;
  }
  function isGifMedia(image, src) {
    const hint = String(image?.path || image?.src || src || '');
    return /\.gif(\?|$)/i.test(hint);
  }
  function openImagePreview(url, label) {
    previewPath = null;
    const isGif = /\.gif(\?|$)/i.test(String(url || ''));
    agentImagePreview.classList.toggle('is-gif', isGif);
    agentImagePreviewImg.src = url;
    agentImagePreviewImg.alt = label || (isGif ? 'GIF' : 'Image');
    agentImagePreview.classList.add('show');
    agentImagePreview.setAttribute('aria-hidden', 'false');
  }
  function closeImagePreview() {
    agentImagePreview.classList.remove('show', 'is-gif');
    agentImagePreview.setAttribute('aria-hidden', 'true');
    agentImagePreviewImg.removeAttribute('src');
  }
  function updateChatPinned() {
    const nearBottom = chatLog.scrollHeight - chatLog.scrollTop - chatLog.clientHeight < 96;
    chatPinnedBottom = nearBottom;
    if (!nearBottom) userScrolledUpAt = Date.now();
  }
  function stableImageKey(images) {
    return (images || [])
      .map((i) => String(i.path || i.src || '').split('?')[0].trim())
      .filter(Boolean)
      .sort()
      .join('|');
  }
  function stableTextKey(m, idx, total, loading) {
    const kind = String(m.type || m.role || '').toLowerCase();
    const text = String(m.text || '');
    if (kind === 'footnote' && loading && idx === total - 1) return '__streaming__';
    if (kind === 'footnote') return `fn:${text.slice(0, 120)}`;
    return text.slice(0, 4000);
  }
  function messageRenderKey(msgs, loading) {
    const list = msgs || [];
    return list.map((m, idx) => JSON.stringify({
      id: m.id || m.messageId || '',
      type: m.type || m.role || '',
      text: stableTextKey(m, idx, list.length, loading),
      images: stableImageKey(m.images),
      pending: !!m.pending,
      failed: !!m.failed,
    })).join('\n');
  }
  function scrollChatToEnd() {
    chatLog.scrollTop = chatLog.scrollHeight;
    chatPinnedBottom = true;
  }
  function bindMessageImages(root) {
    root.querySelectorAll('.msgImageBtn').forEach((btn) => {
      if (btn.dataset.bound === '1') return;
      btn.dataset.bound = '1';
      const img = btn.querySelector('img');
      if (img && btn.dataset.gif === '1') {
        img.loading = 'eager';
        img.decoding = 'async';
      }
      btn.onclick = (e) => {
        e.preventDefault();
        e.stopPropagation();
        const url = btn.dataset.img || img?.src;
        if (!url || btn.classList.contains('broken')) return;
        const label = btn.dataset.gif === '1' ? 'GIF' : (img?.alt || 'Image');
        openImagePreview(url, label);
      };
    });
  }
  function renderChatMessages(msgs, opts) {
    const options = opts || {};
    const loading = !!options.loading;
    const renderKey = messageRenderKey(msgs, loading);
    const preserveScroll = !chatPinnedBottom || options.preserveScroll;
    const prevTop = chatLog.scrollTop;
    const prevHeight = chatLog.scrollHeight;
    const msgCount = (msgs || []).length;
    if (renderKey === lastChatRenderKey && chatLog.childElementCount > 0) {
      bindMessageImages(chatLog);
      return;
    }
    lastChatRenderKey = renderKey;
    chatLog.innerHTML = (msgs || []).map((m) => {
      if (m.type === 'footnote') {
        return `<div class="msgFoot">${escapeHtml(m.text)}</div>`;
      }
      const cls = [
        escapeHtml(m.type || m.role || 'message'),
        m.pending ? 'pending' : '',
        m.failed ? 'failed' : '',
      ].filter(Boolean).join(' ');
      const role = messageRoleLabel(m);
      const roleHtml = role ? `<div class="role">${escapeHtml(role)}</div>` : '';
      const editable = !m.failed && (m.editable || m.type === 'human') ? ' data-edit="1"' : '';
      const retry = m.failed ? ' data-retry="1"' : '';
      const mid = escapeHtml(m.messageId || m.id || '');
      const images = (m.images || []).map((image) => {
        const src = agentImageUrl(image);
        const gifCls = isGifMedia(image, src) ? ' is-gif' : '';
        const label = isGifMedia(image, src) ? 'GIF' : (image.alt || 'Agent image');
        const gif = isGifMedia(image, src);
        const lazyAttr = gif ? ' loading="eager" decoding="async"' : ' loading="lazy" decoding="async"';
        return `<button type="button" class="msgImageBtn${gifCls}" data-img="${escapeHtml(src)}" data-gif="${gif ? '1' : '0'}"><img src="${escapeHtml(src)}" alt="${escapeHtml(image.alt || label)}"${lazyAttr} onerror="this.closest('.msgImageBtn')?.classList.add('broken')"/><span class="mediaBadge">${escapeHtml(label)}</span><span class="brokenLabel">Image unavailable</span></button>`;
      }).join('');
      const gallery = images ? `<div class="msgImages">${images}</div>` : '';
      const ago = m.ago ? `<div class="msgTime">${escapeHtml(m.ago)}</div>` : '';
      return `<div class="msg ${cls}" data-mid="${mid}"${editable}${retry}>${roleHtml}${gallery}<div class="body">${escapeHtml(m.text)}</div>${ago}</div>`;
    }).join('') || '<div class="msg"><div class="body" style="opacity:.5">No messages yet. Open a chat from ☰ or tap + Agent.</div></div>';
    const grew = msgCount > lastRenderedMsgCount;
    lastRenderedMsgCount = msgCount;
    if (preserveScroll) {
      const heightDelta = chatLog.scrollHeight - prevHeight;
      chatLog.scrollTop = Math.max(0, prevTop + heightDelta);
    } else if (options.forceScroll || (chatPinnedBottom && grew && Date.now() - userScrolledUpAt > 800)) {
      scrollChatToEnd();
    }
    bindMessageImages(chatLog);
    chatLog.querySelectorAll('.msg[data-edit="1"]').forEach((el) => {
      el.onclick = () => {
        editMsgId = el.dataset.mid || '';
        editText.value = el.querySelector('.body')?.textContent || '';
        sheet.classList.remove('open');
        editModal.classList.add('open');
      };
    });
    chatLog.querySelectorAll('.msg[data-retry="1"]').forEach((el) => {
      el.onclick = () => retryOptimisticMessage(el);
    });
  }
  function renderPendingImages() {
    attachmentTray.innerHTML = pendingImages.map((image, index) =>
      `<div class="pendingImage"><img src="${escapeHtml(image.preview)}" alt="${escapeHtml(image.name)}"/><button type="button" data-remove-image="${index}" aria-label="Remove picture">×</button></div>`
    ).join('');
    attachmentTray.classList.toggle('show', pendingImages.length > 0);
    resizeComposer();
    attachmentTray.querySelectorAll('[data-remove-image]').forEach((btn) => {
      btn.onclick = () => {
        const index = Number(btn.dataset.removeImage);
        const removed = pendingImages.splice(index, 1)[0];
        if (removed?.preview) URL.revokeObjectURL(removed.preview);
        renderPendingImages();
      };
    });
  }
  async function addPictures(files) {
    if (tab !== 'agent') {
      showToast('Pictures can be attached from the Agent tab');
      return;
    }
    for (const file of Array.from(files || [])) {
      if (!file.type.startsWith('image/')) continue;
      if (pendingImages.length >= 4) {
        showToast('You can attach up to 4 pictures');
        break;
      }
      const currentBytes = pendingImages.reduce((sum, image) => sum + image.size, 0);
      if (currentBytes + file.size > 8 * 1024 * 1024) {
        showToast('Pictures must be 8 MB total or less');
        break;
      }
      const dataUrl = await new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result || ''));
        reader.onerror = () => reject(reader.error);
        reader.readAsDataURL(file);
      });
      pendingImages.push({
        name: file.name || 'image',
        mime: file.type,
        size: file.size,
        data: dataUrl.split(',')[1] || '',
        preview: URL.createObjectURL(file),
      });
    }
    imageInput.value = '';
    renderPendingImages();
  }
  function openDrawer(open) {
    drawer.classList.toggle('open', open);
    scrim.classList.toggle('open', open);
    if (open) closeSheet();
  }
  function closeSheet() {
    sheet.classList.remove('open');
    editModal.classList.remove('open');
    closeNewAgentModal();
  }
  function openSheet(title, html) {
    document.getElementById('sheetTitle').textContent = title;
    sheetBody.innerHTML = html;
    editModal.classList.remove('open');
    sheet.classList.add('open');
    openDrawer(false);
  }

  function setTab(next) {
    tab = next;
    shell.classList.toggle('agent-mode', tab === 'agent');
    shell.classList.toggle('desktop-mode', tab === 'desktop');
    shell.classList.toggle('files-mode', tab === 'files');
    shell.classList.toggle('full-crop', tab === 'desktop' && crop === 'full');
    shell.classList.toggle('chat-dock', tab === 'desktop' && crop === 'chat');
    document.getElementById('tabAgent').classList.toggle('active', tab === 'agent');
    document.getElementById('tabDesktop').classList.toggle('active', tab === 'desktop');
    document.getElementById('tabFiles').classList.toggle('active', tab === 'files');
    const deskTools = tab === 'desktop';
    document.getElementById('qualityBtn').style.display = deskTools ? '' : 'none';
    document.getElementById('rmbBtn').style.display = deskTools ? '' : 'none';
    document.getElementById('cropBtn').style.display = deskTools ? '' : 'none';
    document.getElementById('chatsBtn').style.display = tab === 'agent' ? '' : 'none';
    document.getElementById('newAgentBtn').style.display = tab === 'agent' ? '' : 'none';
    document.getElementById('sessionPill').style.display = tab === 'agent' ? '' : 'none';
    document.getElementById('adjust').style.display =
      (tab === 'desktop' && (crop === 'chat' || crop === 'side')) ? 'flex' : 'none';
    text.placeholder = tab === 'agent' ? 'Message agent…' : 'Type into Cursor…';
    if (tab !== 'agent') { openDrawer(false); closeSheet(); }
    if (tab === 'desktop') {
      if (!ws || ws.readyState > 1) connectDesktop();
      layoutCanvas();
    }
    if (tab === 'files') loadFiles(filesPath);
    if (tab === 'agent') renderAgent(agentState);
    updateDockVisibility();
  }

  function cycleCrop() {
    const order = ['full', 'chat', 'side'];
    crop = order[(order.indexOf(crop) + 1) % order.length];
    document.getElementById('cropBtn').textContent =
      crop === 'full' ? 'Full' : (crop === 'chat' ? 'Chat' : 'Side');
    shell.classList.toggle('full-crop', tab === 'desktop' && crop === 'full');
    shell.classList.toggle('chat-dock', tab === 'desktop' && crop === 'chat');
    document.getElementById('adjust').style.display =
      (crop === 'chat' || crop === 'side') ? 'flex' : 'none';
    if (crop !== 'full') resetView();
    layoutCanvas();
    updateDockVisibility();
  }

  function clampPan() {
    if (userZoom <= 1.001) { panX = 0; panY = 0; return; }
    const r = region();
    const cropW = Math.max(1, srcW * r.w);
    const cropH = Math.max(1, srcH * r.h);
    const maxX = (cropW * fitScale * (userZoom - 1)) / 2 + 24;
    const maxY = (cropH * fitScale * (userZoom - 1)) / 2 + 24;
    panX = Math.max(-maxX, Math.min(maxX, panX));
    panY = Math.max(-maxY, Math.min(maxY, panY));
  }
  function applyViewTransform() {
    clampPan();
    canvas.style.transform =
      'translate(calc(-50% + ' + panX + 'px), calc(-50% + ' + panY + 'px)) scale(' + userZoom + ')';
  }
  function resetView() { userZoom = 1; panX = 0; panY = 0; pinch = null; applyViewTransform(); }
  function layoutCanvas() {
    if (tab !== 'desktop') return;
    const r = region();
    const cropW = Math.max(1, srcW * r.w);
    const cropH = Math.max(1, srcH * r.h);
    const vr = desktop.getBoundingClientRect();
    fitScale = Math.min(vr.width / cropW, vr.height / cropH);
    canvas.style.width = (cropW * fitScale) + 'px';
    canvas.style.height = (cropH * fitScale) + 'px';
    if (canvas.width !== Math.round(cropW) || canvas.height !== Math.round(cropH)) {
      canvas.width = Math.round(cropW);
      canvas.height = Math.round(cropH);
    }
    applyViewTransform();
    paint();
  }
  function paint() {
    if (!srcBmp) return;
    const r = region();
    ctx.imageSmoothingEnabled = true;
    ctx.imageSmoothingQuality = 'high';
    ctx.fillStyle = '#111';
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.drawImage(srcBmp, srcW * r.x, srcH * r.y, srcW * r.w, srcH * r.h, 0, 0, canvas.width, canvas.height);
  }
  async function pump() {
    drawing = true;
    while (pending) {
      const buf = pending; pending = null;
      try {
        const bmp = await createImageBitmap(new Blob([buf], { type: 'image/jpeg' }));
        if (srcBmp) srcBmp.close?.();
        srcBmp = bmp; srcW = bmp.width; srcH = bmp.height;
        frames++;
        const now = performance.now();
        if (now - tFps >= 1000) { shownFps = frames; frames = 0; tFps = now; }
        layoutCanvas();
      } catch (e) {}
    }
    drawing = false;
  }

  function resizeComposer() {
    if (!text) return;
    const lines = text.value.split('\n').length;
    const compact = lines >= 4 || text.value.length > 96;
    text.classList.toggle('compact', compact);
    text.style.height = 'auto';
    const maxH = compact ? 156 : 108;
    const h = Math.min(text.scrollHeight, maxH);
    text.style.height = h + 'px';
    syncComposerLayout();
  }

  function syncComposerLayout() {
    if (!dock) return;
    const h = Math.ceil(dock.getBoundingClientRect().height);
    document.documentElement.style.setProperty('--composer-h', h + 'px');
  }

  function updateDockVisibility() {
    const show = tab === 'agent' || (tab === 'desktop' && crop === 'chat');
    document.body.classList.toggle('show-dock', show);
    syncComposerLayout();
  }

  function syncVisualViewport() {
    const vv = window.visualViewport;
    if (!vv || !composerFocused) {
      document.documentElement.style.setProperty('--vv-inset', '0px');
      syncComposerLayout();
      return;
    }
    const raw = Math.max(0, Math.round(window.innerHeight - vv.height - vv.offsetTop));
    const maxInset = Math.round(window.innerHeight * 0.52);
    const inset = Math.min(raw, maxInset);
    document.documentElement.style.setProperty('--vv-inset', inset + 'px');
    syncComposerLayout();
  }

  const CHAT_NOISE = new Set(['thinking', 'work', 'subagent', 'running', 'tool', 'activity']);
  const TIMESTAMP_RE = /^(\d+[smhdw]\s+ago|\d+\s+[smhdw]\s+ago|just now|now)$/i;
  const FOOTNOTE_RE = /^\d+\s+files?\s+changed/i;

  function normalizeMessages(msgs, loading) {
    const out = [];
    for (const m of msgs || []) {
      const kind = String(m.type || m.role || '').toLowerCase();
      let t = String(m.text || '').trim();
      if (CHAT_NOISE.has(kind)) continue;
      if (kind === 'streaming') {
        if (!t) continue;
        const preview = String(m.preview || t);
        out.push({ ...m, type: 'footnote', text: preview.length > 160 ? `${preview.slice(0, 157)}…` : preview });
        continue;
      }
      if (kind === 'timestamp' || TIMESTAMP_RE.test(t)) {
        if (out.length) {
          const prev = out[out.length - 1];
          if (!prev.ago) prev.ago = t;
        }
        continue;
      }
      if (kind === 'status' || kind === 'footnote' || FOOTNOTE_RE.test(t) || /^review\s+/i.test(t)) {
        out.push({ ...m, type: 'footnote', text: t });
        continue;
      }
      if (
        loading &&
        (kind === 'assistant' || kind === 'ai') &&
        t.length < 100 &&
        /^(finished|working|running|exploring|planning|generating|reading|searching|building|thought)/i.test(t)
      ) {
        continue;
      }
      out.push(m);
    }
    if (loading && out.length) {
      const last = out[out.length - 1];
      const kind = String(last.type || last.role || '').toLowerCase();
      // Only collapse the tail while it is genuinely mid-stream. A completed
      // reply must stay a full message even if the agent is busy again.
      if (
        last.streaming &&
        (kind === 'assistant' || kind === 'ai') &&
        !(last.images || []).length
      ) {
        const preview = String(last.preview || last.text || '').trim();
        if (preview) {
          out.pop();
          out.push({
            ...last,
            type: 'footnote',
            text: preview.length > 160 ? `${preview.slice(0, 157)}…` : preview,
          });
        }
      }
    }
    return out;
  }

  function messageRoleLabel(m) {
    const kind = String(m.type || m.role || '').toLowerCase();
    if (kind === 'human') return 'You · tap to edit';
    return '';
  }

  function renderRunner(state, hasApprove, hasReject) {
    const runner = document.getElementById('chatRunner');
    if (!runner) return;
    runner.classList.remove('approval');
    // Dedicated subagent data carries more useful status than the generic
    // parent runner, but only when it belongs to the conversation on screen.
    if (subagentsForActiveConversation(state)?.running > 0 && !hasApprove && !hasReject) {
      runner.classList.remove('show');
      runner.innerHTML = '';
      return;
    }
    if (state.loading) {
      const st = String(state.status || 'Running…').slice(0, 140);
      runner.innerHTML = `<span class="runnerDot" aria-hidden="true"></span><span class="runnerText">${escapeHtml(st)}</span>`;
      runner.classList.add('show');
      return;
    }
    if (hasApprove || hasReject) {
      runner.innerHTML = `<span class="runnerDot" aria-hidden="true"></span><span class="runnerText">${hasApprove ? 'Approval needed' : 'Action needed'}</span>`;
      runner.classList.add('show', 'approval');
      return;
    }
    runner.classList.remove('show');
    runner.innerHTML = '';
  }

  function subagentsForActiveConversation(state) {
    const payload = state?.subagents;
    if (!payload || Array.isArray(payload) || typeof payload !== 'object') return null;
    const source = payload.conversation || {};
    const target = sendTarget(state);
    const sourceId = String(source.id || '');
    const sourceTitle = String(source.title || '');
    const targetId = String(target.id || '');
    const targetTitle = String(target.title || '');
    if ((!sourceId && !sourceTitle) || (!targetId && !targetTitle)) return null;
    const matches =
      !!(sourceId && targetId && sourceId === targetId) ||
      !!(sourceTitle && targetTitle && titlesMatch(targetTitle, sourceTitle));
    return matches ? payload : null;
  }

  function renderSubagentDock(state, hasAction) {
    if (!agentTreeDock || !agentTreeToggle || !agentTreeList) return;
    const payload = subagentsForActiveConversation(state);
    const running = Number(payload?.running || 0);
    if (!payload || running <= 0 || tab !== 'agent') {
      agentTreeDock.classList.remove('show', 'above-runner');
      agentTreeDock.setAttribute('aria-hidden', 'true');
      agentTreeList.innerHTML = '';
      lastAgentTreeRenderKey = '';
      return;
    }

    const groups = Array.isArray(payload.groups) ? payload.groups : [];
    const agents = Array.isArray(payload.agents) ? payload.agents : [];
    const source = payload.conversation || {};
    const setKey = [
      source.id,
      source.title,
      ...groups.map((group) => `${group.id}:${group.state}:${group.count}`),
      ...agents.map((agent) => agent.id || `${agent.index}:${agent.title}`),
    ].join('|');
    if (setKey !== lastAgentTreeSetKey) {
      agentTreeExpanded = false;
      lastAgentTreeSetKey = setKey;
    }
    const errors = Number(payload.error || 0);
    const priorityAgents = [
      ...agents.filter((agent) => agent.state === 'running'),
      ...agents.filter((agent) => agent.state === 'error'),
      ...agents.filter((agent) => agent.state === 'completed'),
    ];
    const summary = `(${running} sub agent${running === 1 ? '' : 's'})`;
    const renderKey = JSON.stringify({
      expanded: agentTreeExpanded,
      action: !!hasAction,
      conversation: source,
      groups: groups.map((group) => [
        group.id, group.state, group.count, group.running, group.action, group.details,
      ]),
      agents: agents.map((agent) => [
        agent.id, agent.state, agent.label, agent.title, agent.status,
      ]),
    });
    agentTreeDock.classList.add('show');
    agentTreeDock.classList.toggle('above-runner', !!hasAction);
    agentTreeDock.setAttribute('aria-hidden', 'false');
    agentTreeToggle.textContent = summary;
    agentTreeToggle.setAttribute('aria-expanded', String(agentTreeExpanded));
    agentTreeToggle.disabled = false;
    agentTreeToggle.setAttribute(
      'aria-label',
      `${running} subagents running. ${errors} failed. Tap to ${agentTreeExpanded ? 'collapse' : 'show details'}.`
    );
    if (renderKey === lastAgentTreeRenderKey) return;
    lastAgentTreeRenderKey = renderKey;
    if (!agentTreeExpanded) {
      agentTreeList.innerHTML = '';
      return;
    }
    if (priorityAgents.length) {
      agentTreeList.innerHTML = priorityAgents.map((agent, index) => {
        const stateName = String(agent.state || 'running').toLowerCase();
        const number = Number(agent.index) || index + 1;
        const label = agent.label || `Agent ${number}`;
        const title = agent.title || label;
        const status = agent.status || (
          stateName === 'running' ? 'Running' : stateName === 'error' ? 'Failed' : 'Completed'
        );
        const accessible = `${label}: ${title}. ${status}. ${stateName}.`;
        const offset = Math.min(index * 4, 16);
        return `<div class="agentTreeCard" data-state="${escapeHtml(stateName)}" style="--agent-offset:${offset}px" role="status" aria-label="${escapeHtml(accessible)}" title="${escapeHtml(accessible)}"><span class="agentTreeIndex"><span class="agentTreeDot" aria-hidden="true"></span>A${number}</span><span class="agentTreeTitle">${escapeHtml(title)}</span><span class="agentTreeStatus">${escapeHtml(status)}</span></div>`;
      }).join('');
      return;
    }
    const group = groups.find((item) => Number(item.running || 0) > 0);
    const detail = group?.details || `${running} subagents`;
    agentTreeList.innerHTML = `<div class="agentTreeCard" data-state="running" role="status" aria-label="${escapeHtml(`${running} subagents running`)}"><span class="agentTreeIndex"><span class="agentTreeDot" aria-hidden="true"></span></span><span class="agentTreeTitle">${escapeHtml(detail)}</span><span class="agentTreeStatus">Running</span></div>`;
  }

  function tabWorking(tabs, chat, state) {
    if (chat.working) return true;
    if (tabs.some((t) => t.working && (t.id === chat.id || t.title === chat.title))) return true;
    const active = chat.active || tabs.some((t) => t.active && (t.id === chat.id || t.title === chat.title));
    if (active && state?.loading) return true;
    return false;
  }

  function renderRepos(state) {
    const repos = state?.repos || [];
    const tabs = state?.tabs || [];
    const chatRow = (c, sub) => {
      const active = c.active || tabs.some((t) => t.active && (t.id === c.id || t.title === c.title));
      const working = tabWorking(tabs, c, state);
      const cls = [active ? 'active' : '', working ? 'working' : ''].filter(Boolean).join(' ');
      const badge = working ? '<span class="workBadge" aria-hidden="true"></span>' : '';
      const meta = working ? 'Working' : sub;
      return `<button type="button" data-id="${escapeHtml(c.id != null ? c.id : '')}" data-title="${escapeHtml(c.title || '')}" class="${cls}">${badge}${escapeHtml(c.title)}<span class="sub">${escapeHtml(meta)}</span></button>`;
    };
    if (repos.length) {
      repoList.innerHTML = repos.map((g) => {
        const head = `<div class="repoHead">${escapeHtml(g.name || 'Chats')}</div>`;
        const chats = (g.chats || []).map((c) => {
          const sub = [c.age, g.name].filter(Boolean).join(' · ');
          return chatRow(c, sub);
        }).join('');
        return head + (chats || '<div class="sub" style="padding:6px">No chats</div>');
      }).join('');
    } else {
      repoList.innerHTML = tabs.map((t) => {
        const sub = [t.age, t.repo].filter(Boolean).join(' · ');
        return chatRow(t, sub);
      }).join('') || '<div class="msg"><div class="body" style="opacity:.5;padding:8px">No chats found</div></div>';
    }
    repoList.querySelectorAll('button[data-id]').forEach((btn) => {
      btn.onclick = () => {
        const tabId = btn.dataset.id;
        const title = normalizeConvTitle(btn.dataset.title || btn.textContent || '');
        setActiveConversation(tabId, title);
        const hasCache = switchCacheReady(tabId);
        pendingTabSwitch = { id: tabId, title, at: Date.now(), hasCache };
        lastChatRenderKey = '';
        repoList.querySelectorAll('button[data-id]').forEach((row) => row.classList.remove('active'));
        btn.classList.add('active');
        if (hasCache) {
          showCachedConversation(tabId, getConvCache(tabId), true);
        } else {
          chatLog.innerHTML = '<div class="msg"><div class="body" style="opacity:.55">Loading conversation…</div></div>';
          lastChatRenderKey = '';
        }
        chatPinnedBottom = true;
        meta.textContent = `opening ${title || 'conversation'}…`;
        hydrateConversationCache(tabId).then((remote) => {
          if (!remote?.messages?.length || pendingTabSwitch?.id !== tabId) return;
          pendingTabSwitch.hasCache = true;
          showCachedConversation(tabId, remote, true);
        });
        agentSend({ type: 'select_tab', id: tabId, tab_title: title });
        openDrawer(false);
      };
    });
    const wins = state?.windows || [];
    winList.innerHTML = wins.map((w) =>
      `<button type="button" data-wid="${escapeHtml(w.id)}" class="${w.active ? 'active' : ''}">${escapeHtml(w.title || w.id)}<span class="sub">${w.active ? 'active' : 'switch'}</span></button>`
    ).join('') || '<div class="msg"><div class="body" style="opacity:.5;padding:8px">No CDP windows</div></div>';
    winList.querySelectorAll('button[data-wid]').forEach((btn) => {
      btn.onclick = () => {
        agentSend({ type: 'switch_window', id: btn.dataset.wid });
        openDrawer(false);
      };
    });
  }

  function renderUsage(state) {
    const u = state?.usage || {};
    const s = state?.usageStats || {};
    const by = s.byModel || {};
    const byLines = Object.keys(by).map((k) => `${k}: ${by[k]}`).join(' · ');
    const ctx = u.contextPercent != null ? `Context ~${u.contextPercent}%` : (u.label || '');
    const plan = u.plan ? `Plan: ${u.plan}` : '';
    return [
      plan,
      ctx,
      `Prompts ${s.prompts || 0}`,
      `Accept ${s.approvals || 0}`,
      `Model swaps ${s.modelChanges || 0}`,
      byLines,
    ].filter(Boolean).join(' · ');
  }

  function renderQueue(state) {
    const q = state?.queue || [];
    const box = document.getElementById('queueBox');
    const list = document.getElementById('queueList');
    if (!q.length) { box.classList.remove('show'); list.innerHTML = ''; return; }
    box.classList.add('show');
    list.innerHTML = q.map((item) =>
      `<button type="button" data-qid="${escapeHtml(item.id)}">${escapeHtml(item.text)}<span class="sub">queued</span></button>`
    ).join('');
  }

  function renderAgent(state) {
    agentState = state;
    if (!state) return;
    document.getElementById('modelBtn').textContent = (state.model || 'Auto') + ' ▾';
    const workingN = (state.tabs || []).filter((t) => tabWorking(state.tabs || [], t, state)).length;
    const workingHint = workingN ? ` · ${workingN} working` : '';
    document.getElementById('drawerSub').textContent =
      (state.workspace || state.targetTitle || 'Cursor') + (state.cdp ? ' · CDP' : ' · offline') + workingHint;
    document.getElementById('chatsBtn').classList.toggle('hasWorking', workingN > 0);

    const session = state.session || {};
    const pill = document.getElementById('sessionPill');
    const kind = session.kind || 'local';
    pill.textContent = session.label || (kind === 'local' ? 'Local' : kind);
    pill.classList.toggle('remote', kind !== 'local');
    pill.classList.toggle('local', kind === 'local');

    const mode = String(state.mode || '').toLowerCase();
    document.querySelectorAll('#modeRow button').forEach((btn) => {
      btn.classList.toggle('on', btn.dataset.mode === composerMode);
    });

    if (!state.cdp || state.error) {
      agentBanner.textContent = state.error || 'Waiting for Cursor CDP on :9222…';
      agentBanner.classList.add('show');
    } else {
      agentBanner.classList.remove('show');
    }

    const hasApprove = (state.approvals || []).length > 0;
    const hasReject = (state.rejects || []).length > 0;
    approvals.classList.toggle('show', hasApprove || hasReject);
    document.getElementById('approveBtn').style.display = hasApprove ? '' : 'none';
    document.getElementById('rejectBtn').style.display = hasReject ? '' : 'none';
    if (hasApprove) document.getElementById('approveBtn').textContent = state.approvals[0].label || 'Accept';
    if (hasReject) document.getElementById('rejectBtn').textContent = state.rejects[0].label || 'Reject';

    const live = document.getElementById('liveStatus');
    live.textContent = '';
    live.classList.remove('show');
    renderRunner(state, hasApprove, hasReject);
    renderSubagentDock(state, hasApprove || hasReject);

    renderQueue(state);
    renderRepos(state);

    const switching = !!state.switching;
    if (pendingTabSwitch) {
      const switchTarget = { id: pendingTabSwitch.id, title: pendingTabSwitch.title };
      const matched = conversationMatches(state, switchTarget);
      const timedOut = Date.now() - pendingTabSwitch.at > 12000;

      if (!switchCacheReady(pendingTabSwitch.id) && !pendingTabSwitch.hasCache) {
        chatLog.innerHTML = '<div class="msg"><div class="body" style="opacity:.55">Loading conversation…</div></div>';
        renderRunner(state, hasApprove, hasReject);
        if (!matched && !timedOut) return;
      } else if (switchCacheReady(pendingTabSwitch.id) && !pendingTabSwitch.hasCache) {
        pendingTabSwitch.hasCache = true;
        showCachedConversation(pendingTabSwitch.id, getConvCache(pendingTabSwitch.id), !matched);
      }

      if ((matched && !switching) || timedOut) {
        if (matched) {
          const activeTab = (state.tabs || []).find((t) => t.active);
          setActiveConversation(
            pendingTabSwitch.id,
            activeTab?.title || pendingTabSwitch.title
          );
        }
        pendingTabSwitch = null;
        lastChatRenderKey = '';
        if (timedOut && !matched) showToast('Still syncing conversation in background');
      } else if (tab === 'agent') {
        meta.textContent = `${pendingTabSwitch.title || 'Conversation'} · syncing…`;
      }
    }

    const activeTab = (state.tabs || []).find((t) => t.active);
    const target = sendTarget(state);
    let messagesForView = state.messages || [];
    let syncingMismatch = false;
    const liveAllowed = conversationMatches(state, target) && !switching;

    if ((target.id || target.title) && !liveAllowed) {
      syncingMismatch = true;
      const cached = getConvCache(target.id);
      messagesForView = cached?.messages?.length ? cached.messages : [];
    } else if (activeTab && (target.id || target.title)) {
      setActiveConversation(activeTab.id, activeTab.title);
      messagesForView = state.messages || [];
    }

    const cacheTabId = target.id || activeTab?.id || state.selectedTabId;
    if (cacheTabId && liveAllowed && (state.messages || []).length) {
      saveConvCacheEntry(cacheTabId, {
        title: activeTab?.title || target.title || state.selectedTabTitle || '',
        messages: state.messages,
      });
    }

    reconcileOptimistic(messagesForView);
    if (liveAllowed) reconcileOutboxWithMessages(messagesForView, cacheTabId);
    dismissStaleOptimistic(!!state.loading);
    const scopedOptimistic = optimisticMessages.filter(
      (m) => !m.tabId || m.tabId === target.id
    );
    const msgs = normalizeMessages(messagesForView, !!state.loading).concat(scopedOptimistic);
    renderChatMessages(msgs, { loading: !!state.loading });

    if (tab === 'agent') {
      const wsName = target.title || activeTab?.title || state.workspace || state.targetTitle || 'Agent';
      meta.textContent = state.cdp
        ? (wsName + (syncingMismatch ? ' · syncing…' : state.loading ? ' · working' : ''))
        : 'CDP offline';
    }
    return;
  }

  function renderModelsSheet(models) {
    modelCache = models || [];
    const groups = { header: [], model: [], other: [] };
    modelCache.forEach((m) => {
      (groups[m.group] || groups.model).push(m);
    });
    // Quick effort shortcuts for common Cursor models
    const effort = ['High', 'Medium', 'Low', 'Fast'];
    const quick = [];
    ['Cursor Grok 4.5', 'Composer 2.5', 'Auto'].forEach((base) => {
      if (base === 'Auto') quick.push({ id: 'label::Auto', label: 'Auto', detail: 'Auto' });
      else effort.forEach((e) => quick.push({
        id: 'label::' + base + ' ' + e,
        label: base + ' ' + e,
        detail: base + ' ' + e,
      }));
    });
    let html = '<div class="headerRow">Quick</div>';
    html += quick.map((m) =>
      `<button type="button" data-model="${escapeHtml(m.label)}">${escapeHtml(m.label)}</button>`
    ).join('');
    html += '<div class="headerRow">From Cursor</div>';
    html += modelCache.map((m) => {
      if (m.group === 'header') return `<div class="headerRow">${escapeHtml(m.label)}</div>`;
      return `<button type="button" data-model="${escapeHtml(m.label)}" class="${m.selected ? 'on' : ''}">${escapeHtml(m.label)}<span class="sub">${escapeHtml(m.detail || '')}</span></button>`;
    }).join('') || '<div class="sub">No models read — picker may have closed.</div>';
    openSheet('Models', html);
    sheetBody.querySelectorAll('button[data-model]').forEach((btn) => {
      btn.onclick = () => {
        agentSend({ type: 'set_model', model: btn.dataset.model });
        closeSheet();
      };
    });
  }

  let agentDelay = 600, agentTimer = null, agentConnecting = false;
  function connectAgent() {
    if (agentConnecting) return;
    agentConnecting = true;
    try { aws = new WebSocket(`${proto}://${location.host}/ws/agent`); }
    catch (e) { agentConnecting = false; scheduleAgent(); return; }
    aws.onopen = () => {
      agentConnecting = false;
      agentDelay = 600;
      if (tab === 'agent') meta.textContent = 'agent live';
      flushClientOutbox(false, true);
      prefetchServerCaches();
      try {
        if (Notification.permission === 'default') Notification.requestPermission();
      } catch (e) {}
    };
    aws.onclose = () => { agentConnecting = false; scheduleAgent(); };
    aws.onerror = () => {};
    aws.onmessage = (ev) => {
      try {
        const msg = JSON.parse(ev.data);
        if (msg.type === 'state') renderAgent(msg.state);
        if (msg.type === 'models') {
          renderModelsSheet(msg.models || []);
          if (newAgentModal.classList.contains('open')) renderNewAgentModels(msg.models || []);
        }
        if (msg.type === 'done') showToast(msg.text || 'Agent finished');
        if (msg.type === 'prompt_sent' && msg.text) {
          const payload = {
            text: msg.text,
            tab_id: msg.tab_id || msg.tabId || '',
            tab_title: msg.tab_title || msg.tabTitle || '',
          };
          markOptimisticSent(msg.text, payload.tab_id);
          dropClientOutbox(payload);
        }
        if (msg.type === 'prompt_failed' && msg.text) {
          const payload = {
            text: msg.text,
            tab_id: msg.tab_id || msg.tabId || '',
            tab_title: msg.tab_title || msg.tabTitle || '',
          };
          if (msg.permanent) {
            dropClientOutbox(payload);
            failOptimistic(msg.text, msg.error || 'Send failed', payload.tab_id);
            showToast(msg.error || 'Message could not be sent');
          } else {
            // Host is still auto-retrying — don't offer manual Retry (avoids duplicates).
            optimisticMessages.forEach((opt) => {
              if (payload.tab_id && opt.tabId && opt.tabId !== payload.tab_id) return;
              if (normalizeForMatch(opt.text) !== normalizeForMatch(msg.text)) return;
              opt.pending = true;
              opt.failed = false;
              opt.ago = `retrying… (${msg.attempts || '?'})`;
            });
            lastChatRenderKey = '';
          }
        }
        if (msg.type === 'cache_update' && msg.cache?.messages?.length) {
          const tabId = msg.tabId || msg.cache.tabId || '';
          if (tabId) {
            saveConvCacheEntry(tabId, msg.cache);
            if (pendingTabSwitch?.id === tabId && !pendingTabSwitch.hasCache) {
              pendingTabSwitch.hasCache = true;
              showCachedConversation(tabId, msg.cache, !conversationMatches(agentState, pendingTabSwitch));
            }
          }
        }
        if (msg.type === 'result') {
          if (msg.text && (msg.sent || msg.queued || msg.direct_error || msg.error)) {
            handlePromptDeliveryResult(msg, {
              text: msg.text,
              tab_id: msg.tab_id || msg.tabId || '',
              tab_title: msg.tab_title || msg.tabTitle || '',
            });
          }
          if (msg.cache?.messages?.length) {
            const tabId = msg.cache.tabId || msg.tab_id || pendingTabSwitch?.id || activeConversation.id;
            if (tabId) {
              const prevCount = getConvCache(tabId)?.messages?.length || 0;
              saveConvCacheEntry(tabId, msg.cache);
              if (pendingTabSwitch?.id === tabId) {
                pendingTabSwitch.hasCache = true;
                if (!conversationMatches(agentState, pendingTabSwitch) || msg.cache.messages.length > prevCount) {
                  lastChatRenderKey = '';
                  showCachedConversation(tabId, msg.cache, !conversationMatches(agentState, pendingTabSwitch));
                }
              }
            }
          }
          if (msg.error && !msg.text) {
            agentBanner.textContent = msg.error;
            agentBanner.classList.add('show');
            showToast(msg.error);
          }
        }
      } catch (e) {}
    };
  }
  function scheduleAgent() {
    if (agentTimer) return;
    agentTimer = setTimeout(() => { agentTimer = null; connectAgent(); }, agentDelay);
    agentDelay = Math.min(8000, Math.floor(agentDelay * 1.6));
  }
  function agentSend(obj) {
    if (obj?.type === 'prompt') {
      const target = promptTarget(agentState);
      const payload = {
        type: 'prompt',
        text: obj.text,
        images: obj.images || [],
        tab_id: obj.tab_id || obj.tabId || target.tab_id,
        tab_title: obj.tab_title || obj.tabTitle || target.tab_title,
      };
      // Prompts have exactly one transport: durable HTTP. Sending the same
      // payload over both WebSocket and the browser outbox raced reconnects
      // and delivered duplicate/split messages, especially with large images.
      const queued = queueClientOutbox(payload);
      postPromptDurable(queued, false).then((result) => handlePromptDeliveryResult(result, queued));
      return;
    }
    if (aws && aws.readyState === 1) aws.send(JSON.stringify({
      ...obj,
      tab_id: obj.tab_id || obj.tabId || promptTarget(agentState).tab_id,
      tab_title: obj.tab_title || obj.tabTitle || promptTarget(agentState).tab_title,
    }));
  }

  let reconnectDelay = 600, reconnectTimer = null, connecting = false;
  let lastFrameAt = 0, lastPongAt = 0;
  function send(obj) {
    if (ws && ws.readyState === 1) ws.send(JSON.stringify(obj));
  }
  function scheduleReconnect(reason) {
    if (reconnectTimer) return;
    if (tab === 'desktop') meta.textContent = reason || 'reconnecting…';
    reconnectTimer = setTimeout(() => { reconnectTimer = null; connectDesktop(); }, reconnectDelay);
    reconnectDelay = Math.min(8000, Math.floor(reconnectDelay * 1.6));
  }
  function connectDesktop() {
    if (connecting) return;
    connecting = true;
    if (tab === 'desktop') meta.textContent = 'connecting stream…';
    try { ws = new WebSocket(`${proto}://${location.host}/ws`); }
    catch (e) { connecting = false; scheduleReconnect('host unreachable'); return; }
    ws.binaryType = 'arraybuffer';
    ws.onopen = () => {
      connecting = false;
      reconnectDelay = 600;
      lastPongAt = performance.now();
      lastFrameAt = performance.now();
      if (tab === 'desktop') meta.textContent = 'stream live';
      try { send({type:'preset', name: qualityCycle[qualityIdx] || 'sharp'}); } catch (e) {}
    };
    ws.onerror = () => { if (tab === 'desktop') meta.textContent = 'stream error'; };
    ws.onclose = () => { connecting = false; scheduleReconnect('lost stream'); };
    ws.onmessage = async (ev) => {
      if (typeof ev.data === 'string') {
        try {
          const msg = JSON.parse(ev.data);
          if (msg.type === 'ping') {
            lastPongAt = performance.now();
            send({type:'pong', t: msg.t || Date.now()});
            return;
          }
          if ((msg.type === 'status' || msg.type === 'stats') && tab === 'desktop') {
            lastPongAt = performance.now();
            const ms = msg.encode_ms != null ? ` · ${msg.encode_ms|0}ms` : '';
            meta.textContent = `${msg.text || 'Cursor'} · ${shownFps|0}fps${ms}`;
          }
        } catch (e) {}
        return;
      }
      lastFrameAt = performance.now();
      lastPongAt = lastFrameAt;
      pending = ev.data;
      if (!drawing) pump();
    };
  }

  setInterval(async () => {
    if (tab !== 'desktop') return;
    const now = performance.now();
    if ((!lastFrameAt || now - lastFrameAt < 12000) && (!lastPongAt || now - lastPongAt < 15000) && ws && ws.readyState === 1) return;
    try {
      const r = await fetch('/health?t=' + Date.now(), {cache:'no-store'});
      if (!r.ok) throw new Error('bad');
      if (!ws || ws.readyState !== 1) connectDesktop();
    } catch (e) { scheduleReconnect('host unreachable'); }
  }, 4000);

  function clientNorm(clientX, clientY) {
    const rect = canvas.getBoundingClientRect();
    const x = (clientX - rect.left) / Math.max(1, rect.width);
    const y = (clientY - rect.top) / Math.max(1, rect.height);
    const r = region();
    return { x: r.x + Math.max(0, Math.min(1, x)) * r.w, y: r.y + Math.max(0, Math.min(1, y)) * r.h };
  }
  function onPtrDown(ev) {
    if (tab !== 'desktop') return;
    desktop.setPointerCapture?.(ev.pointerId);
    ptr = 'pending'; moved = false;
    startX = startCx = ev.clientX; startY = startCy = ev.clientY; lastClientY = ev.clientY;
    lastNorm = clientNorm(ev.clientX, ev.clientY);
  }
  function onPtrMove(ev) {
    if (tab !== 'desktop' || ptr === 'idle' || pinch) return;
    const dx = ev.clientX - startX, dy = ev.clientY - startY;
    if (!moved && Math.hypot(dx, dy) > MOVE_PX) {
      moved = true;
      if (userZoom > 1.02) ptr = 'pan';
      else if (Math.abs(dy) > Math.abs(dx) * 1.2) ptr = 'scroll';
      else {
        ptr = 'mouse';
        send({type:'down', x:lastNorm.x, y:lastNorm.y, button: armRight ? 'right' : 'left'});
      }
    }
    if (ptr === 'pan') {
      panX += ev.clientX - startCx; panY += ev.clientY - startCy;
      startCx = ev.clientX; startCy = ev.clientY; applyViewTransform(); return;
    }
    if (ptr === 'scroll') {
      wheelAcc += (ev.clientY - lastClientY); lastClientY = ev.clientY;
      if (Math.abs(wheelAcc) >= 12) {
        send({type:'wheel', dy: Math.round(wheelAcc), x:lastNorm.x, y:lastNorm.y});
        wheelAcc = 0;
      }
      return;
    }
    if (ptr === 'mouse') {
      lastNorm = clientNorm(ev.clientX, ev.clientY);
      send({type:'move', x:lastNorm.x, y:lastNorm.y});
    }
  }
  function onPtrUp(ev) {
    if (tab !== 'desktop') return;
    if (ptr === 'pending' && !moved) {
      const now = performance.now();
      if (now - lastTapAt < 280 && crop === 'full') { resetView(); lastTapAt = 0; ptr = 'idle'; return; }
      lastTapAt = now;
      const n = clientNorm(ev.clientX, ev.clientY);
      const btn = armRight ? 'right' : 'left';
      armRight = false; document.getElementById('rmbBtn').classList.toggle('on', false);
      send({type:'down', x:n.x, y:n.y, button:btn});
      setTimeout(() => send({type:'up', x:n.x, y:n.y, button:btn}), 40);
    } else if (ptr === 'mouse') {
      const btn = armRight ? 'right' : 'left';
      armRight = false; document.getElementById('rmbBtn').classList.toggle('on', false);
      send({type:'up', x:lastNorm.x, y:lastNorm.y, button:btn});
    }
    ptr = 'idle'; wheelAcc = 0;
  }
  desktop.addEventListener('pointerdown', onPtrDown);
  desktop.addEventListener('pointermove', onPtrMove);
  desktop.addEventListener('pointerup', onPtrUp);
  desktop.addEventListener('pointercancel', () => { ptr = 'idle'; });
  desktop.addEventListener('touchstart', (ev) => {
    if (tab !== 'desktop' || ev.touches.length !== 2) return;
    const a = ev.touches[0], b = ev.touches[1];
    pinch = {
      dist: Math.max(1, Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY)),
      zoom: userZoom, midX: (a.clientX + b.clientX) / 2, midY: (a.clientY + b.clientY) / 2, panX, panY,
    };
    ptr = 'pinch'; moved = true;
  }, {passive:true});
  desktop.addEventListener('touchmove', (ev) => {
    if (!pinch || ev.touches.length < 2) return;
    const a = ev.touches[0], b = ev.touches[1];
    const dist = Math.max(1, Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY));
    userZoom = Math.max(1, Math.min(4.5, pinch.zoom * (dist / pinch.dist)));
    panX = pinch.panX + ((a.clientX + b.clientX) / 2 - pinch.midX);
    panY = pinch.panY + ((a.clientY + b.clientY) / 2 - pinch.midY);
    applyViewTransform();
  }, {passive:true});
  desktop.addEventListener('touchend', (ev) => { if (pinch && ev.touches.length < 2) pinch = null; });

  function fmtSize(n) {
    if (n < 1024) return n + ' B';
    if (n < 1024*1024) return (n/1024).toFixed(1) + ' KB';
    return (n/1024/1024).toFixed(1) + ' MB';
  }
  async function loadFiles(path) {
    const q = path ? `?path=${encodeURIComponent(path)}` : '';
    const data = await fetch('/api/files' + q).then(r => r.json());
    if (!data.ok && data.error) {
      filesList.innerHTML = `<div class="frow"><div class="name">${data.error}</div></div>`;
      return;
    }
    filesPath = data.path || null;
    filesPathEl.textContent = filesPath || 'Quick folders';
    if (data.roots && rootSel.options.length === 0) {
      data.roots.forEach(r => {
        const opt = document.createElement('option');
        opt.value = r.path; opt.textContent = r.label; rootSel.appendChild(opt);
      });
    }
    const rows = [];
    if (!filesPath && data.roots) {
      data.roots.forEach(r => rows.push(`<div class="frow" data-path="${r.path}" data-dir="1"><div class="ico">📁</div><div><div class="name">${r.label}</div><div class="meta">${r.path}</div></div><div></div></div>`));
    } else {
      (data.entries || []).forEach(e => {
        const ico = e.isDir ? '📁' : (e.isImage ? '🖼' : '📄');
        const m = e.isDir ? 'folder' : fmtSize(e.size || 0);
        rows.push(`<div class="frow" data-path="${e.path}" data-dir="${e.isDir?1:0}" data-img="${e.isImage?1:0}" data-text="${e.isText?1:0}"><div class="ico">${ico}</div><div><div class="name">${e.name}</div><div class="meta">${m}</div></div><div></div></div>`);
      });
    }
    filesList.innerHTML = rows.join('') || '<div class="frow"><div class="name">Empty</div></div>';
    filesList.querySelectorAll('.frow').forEach(row => {
      row.onclick = () => {
        if (row.dataset.dir === '1') loadFiles(row.dataset.path);
        else openPreview(row.dataset.path, row.dataset.img === '1', row.dataset.text === '1');
      };
    });
  }
  async function openPreview(path, isImage, isText) {
    previewPath = path;
    preview.classList.add('show');
    previewImg.style.display = 'none';
    previewText.style.display = 'none';
    if (isImage) {
      previewImg.style.display = 'block';
      previewImg.src = '/api/file?path=' + encodeURIComponent(path) + '&t=' + Date.now();
    } else if (isText) {
      previewText.style.display = 'block';
      previewText.textContent = (await fetch('/api/file?path=' + encodeURIComponent(path)).then(r => r.text())).slice(0, 200000);
    } else {
      previewText.style.display = 'block';
      previewText.textContent = 'No in-phone preview for this file type.';
    }
  }

  async function sendText() {
    const v = preparePromptText(text.value);
    if (!v.trim() && !pendingImages.length) return;
    if (tab === 'agent') {
      appendOptimisticHuman(v, pendingImages.length);
      agentSend({
        type: 'prompt',
        text: v,
        images: pendingImages.map(({ name, mime, data }) => ({ name, mime, data })),
        ...promptTarget(agentState),
      });
      pendingImages.forEach((image) => URL.revokeObjectURL(image.preview));
      pendingImages = [];
      renderPendingImages();
      text.value = '';
      resizeComposer();
      return;
    }
    const p = regions.input;
    send({type:'down', x:p.x, y:p.y, button:'left'});
    send({type:'up', x:p.x, y:p.y, button:'left'});
    await new Promise(r => setTimeout(r, 80));
    send({type:'text', text: v + '\n'});
    text.value = '';
    resizeComposer();
  }

  chatLog.addEventListener('scroll', updateChatPinned, { passive: true });
  document.getElementById('tabAgent').onclick = () => setTab('agent');
  document.getElementById('tabDesktop').onclick = () => setTab('desktop');
  document.getElementById('tabFiles').onclick = () => setTab('files');
  document.getElementById('chatsBtn').onclick = () => openDrawer(true);
  document.getElementById('drawerClose').onclick = () => openDrawer(false);
  document.getElementById('sheetClose').onclick = () => closeSheet();
  document.getElementById('editClose').onclick = () => closeSheet();
  scrim.onclick = () => { openDrawer(false); closeSheet(); };
  document.getElementById('newAgentBtn').onclick = () => openNewAgentModal();
  document.getElementById('drawerNew').onclick = () => openNewAgentModal();
  document.getElementById('newAgentClose').onclick = () => closeNewAgentModal();
  document.getElementById('newAgentBrowse').onclick = () => loadNewAgentBrowse(newAgentBrowsePath);
  document.getElementById('newAgentCreate').onclick = () => createNewAgent();
  document.querySelectorAll('#newAgentModeRow button').forEach((btn) => {
    btn.onclick = () => {
      newAgentMode = btn.dataset.mode || 'agent';
      document.querySelectorAll('#newAgentModeRow button').forEach((row) => {
        row.classList.toggle('on', row.dataset.mode === newAgentMode);
      });
    };
  });
  document.getElementById('cropBtn').onclick = cycleCrop;
  document.getElementById('sendBtn').onclick = sendText;
  document.getElementById('attachBtn').onclick = () => imageInput.click();
  imageInput.onchange = () => addPictures(imageInput.files).catch(() => showToast('Could not read picture'));
  text.addEventListener('input', resizeComposer);
  text.addEventListener('focus', () => {
    composerFocused = true;
    syncVisualViewport();
    setTimeout(syncVisualViewport, 120);
  });
  text.addEventListener('blur', () => {
    composerFocused = false;
    setTimeout(syncVisualViewport, 80);
  });
  text.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendText(); }
  });
  document.getElementById('approveBtn').onclick = () => agentSend({ type: 'approve', ...promptTarget(agentState) });
  document.getElementById('rejectBtn').onclick = () => agentSend({ type: 'reject', ...promptTarget(agentState) });
  document.getElementById('modelBtn').onclick = () => agentSend({ type: 'list_models' });
  document.getElementById('usageBtn').onclick = () => {
    const summary = renderUsage(agentState || {}) || 'No usage yet';
    openSheet('Usage', `<div class="sub" style="padding:8px;white-space:pre-wrap">${escapeHtml(summary)}</div>
      <div class="headerRow">Notes</div>
      <div class="sub" style="padding:8px">Cursor rarely exposes full token counts over CDP. This tracks session prompts / accepts / model changes, plus any context % found in the UI.</div>`);
  };
  document.querySelectorAll('#modeRow button').forEach((btn) => {
    btn.onclick = () => setComposerMode(btn.dataset.mode);
  });
  agentTreeToggle.onclick = () => {
    agentTreeExpanded = !agentTreeExpanded;
    lastAgentTreeRenderKey = '';
    renderSubagentDock(
      agentState,
      (agentState?.approvals || []).length > 0 || (agentState?.rejects || []).length > 0
    );
  };
  document.getElementById('editSend').onclick = () => {
    const v = preparePromptText(editText.value.trim());
    if (!v) return;
    agentSend({ type: 'edit_message', id: editMsgId || '', text: v, ...promptTarget(agentState) });
    closeSheet();
  };
  document.getElementById('reconnectBtn').onclick = () => {
    try { aws && aws.close(); } catch (e) {}
    try { ws && ws.close(); } catch (e) {}
    connectAgent();
    if (tab === 'desktop') connectDesktop();
  };
  document.getElementById('qualityBtn').onclick = () => {
    qualityIdx = (qualityIdx + 1) % qualityCycle.length;
    const name = qualityCycle[qualityIdx];
    document.getElementById('qualityBtn').textContent = name[0].toUpperCase() + name.slice(1);
    send({type:'preset', name});
  };
  document.getElementById('rmbBtn').onclick = () => {
    armRight = !armRight;
    document.getElementById('rmbBtn').classList.toggle('on', armRight);
  };
  document.getElementById('widerBtn').onclick = () => {
    if (regions.chat.x <= 0.001) { regions.chat.x = 0.12; regions.chat.w = 0.88; }
    else { regions.chat.x = Math.min(0.45, regions.chat.x + 0.03); regions.chat.w = 1 - regions.chat.x; }
    regions.side.w = Math.min(0.50, regions.chat.x || 0.38);
    layoutCanvas();
  };
  document.getElementById('narrowBtn').onclick = () => {
    regions.chat.x = Math.max(0.00, regions.chat.x - 0.03);
    regions.chat.w = 1 - regions.chat.x;
    regions.side.w = Math.max(0.20, (regions.chat.x || 0.38) - 0.02);
    layoutCanvas();
  };
  document.getElementById('upBtn').onclick = () => {
    if (!filesPath) return;
    const parts = filesPath.replace(/[\\/]+$/,'').split(/[\\/]/);
    parts.pop(); loadFiles(parts.join('\\') || null);
  };
  document.getElementById('refreshFilesBtn').onclick = () => loadFiles(filesPath);
  document.getElementById('explorerBtn').onclick = async () => {
    await fetch('/api/explorer', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({path: filesPath || ''})});
  };
  rootSel.onchange = () => loadFiles(rootSel.value);
  document.getElementById('previewClose').onclick = () => {
    preview.classList.remove('show');
    document.getElementById('previewExplorer').style.display = '';
    document.getElementById('previewOpenPc').style.display = '';
    document.getElementById('previewClose').textContent = 'Close';
  };
  agentImagePreviewClose.onclick = (e) => {
    e.stopPropagation();
    closeImagePreview();
  };
  agentImagePreview.onclick = (e) => {
    if (e.target === agentImagePreview) closeImagePreview();
  };
  document.getElementById('previewExplorer').onclick = async () => {
    await fetch('/api/explorer', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({path: previewPath || ''})});
  };
  document.getElementById('previewOpenPc').onclick = async () => {
    await fetch('/api/open', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({path: previewPath || ''})});
  };

  window.addEventListener('resize', layoutCanvas);
  window.visualViewport?.addEventListener('resize', syncVisualViewport);
  window.visualViewport?.addEventListener('scroll', syncVisualViewport);
  if (dock && typeof ResizeObserver !== 'undefined') {
    new ResizeObserver(syncComposerLayout).observe(dock);
  }
  resizeComposer();
  updateDockVisibility();
  syncVisualViewport();
  setTab('agent');
  setComposerMode(readAgentPrefs().mode || 'agent');
  window.addEventListener('pagehide', () => { flushClientOutbox(true); });
  flushClientOutbox(false, true);
  setInterval(() => {
    if (!aws || aws.readyState !== 1) flushClientOutbox(false);
  }, 20000);
  // The switch timeout is only evaluated when a state message arrives, so a
  // silent host would otherwise leave "Loading conversation…" up indefinitely.
  setInterval(() => {
    if (!pendingTabSwitch) return;
    if (Date.now() - pendingTabSwitch.at < 15000) return;
    const stuckId = pendingTabSwitch.id;
    pendingTabSwitch = null;
    lastChatRenderKey = '';
    const cached = getConvCache(stuckId);
    if (cached?.messages?.length) showCachedConversation(stuckId, cached, true);
    else if (agentState) renderAgent(agentState);
    showToast('Conversation is slow to load — showing what we have');
  }, 3000);
  connectAgent();
})();
