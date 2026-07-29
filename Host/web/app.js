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
  let chatPinnedBottom = true;
  let lastChatRenderKey = '';

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
  function openImagePreview(url, label) {
    previewPath = null;
    agentImagePreviewImg.src = url;
    agentImagePreviewImg.alt = label || 'Image';
    agentImagePreview.classList.add('show');
    agentImagePreview.setAttribute('aria-hidden', 'false');
  }
  function closeImagePreview() {
    agentImagePreview.classList.remove('show');
    agentImagePreview.setAttribute('aria-hidden', 'true');
    agentImagePreviewImg.removeAttribute('src');
  }
  function updateChatPinned() {
    chatPinnedBottom = chatLog.scrollHeight - chatLog.scrollTop - chatLog.clientHeight < 96;
  }
  function messageRenderKey(msgs) {
    return (msgs || []).map((m) => JSON.stringify({
      id: m.id || m.messageId || '',
      type: m.type || m.role || '',
      text: m.text || '',
      ago: m.ago || '',
      images: (m.images || []).map((i) => i.path || i.src || ''),
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
      if (img) {
        img.removeAttribute('loading');
        img.addEventListener('load', () => {
          if (chatPinnedBottom) scrollChatToEnd();
        }, { once: true });
      }
      btn.onclick = (e) => {
        e.preventDefault();
        e.stopPropagation();
        const url = btn.dataset.img || img?.src;
        if (!url || btn.classList.contains('broken')) return;
        openImagePreview(url, img?.alt || 'Image');
      };
    });
  }
  function renderChatMessages(msgs) {
    const renderKey = messageRenderKey(msgs);
    const preserveScroll = !chatPinnedBottom;
    const prevTop = chatLog.scrollTop;
    if (renderKey === lastChatRenderKey && chatLog.childElementCount > 0) {
      bindMessageImages(chatLog);
      return;
    }
    lastChatRenderKey = renderKey;
    chatLog.innerHTML = (msgs || []).map((m) => {
      if (m.type === 'footnote') {
        return `<div class="msgFoot">${escapeHtml(m.text)}</div>`;
      }
      const cls = escapeHtml(m.type || m.role || 'message');
      const role = messageRoleLabel(m);
      const roleHtml = role ? `<div class="role">${escapeHtml(role)}</div>` : '';
      const editable = m.editable || m.type === 'human' ? ' data-edit="1"' : '';
      const mid = escapeHtml(m.messageId || m.id || '');
      const images = (m.images || []).map((image) => {
        const src = agentImageUrl(image);
        return `<button type="button" class="msgImageBtn" data-img="${escapeHtml(src)}"><img src="${escapeHtml(src)}" alt="${escapeHtml(image.alt || 'Agent image')}" onerror="this.closest('.msgImageBtn')?.classList.add('broken')"/><span class="brokenLabel">Image unavailable</span></button>`;
      }).join('');
      const gallery = images ? `<div class="msgImages">${images}</div>` : '';
      const ago = m.ago ? `<div class="msgTime">${escapeHtml(m.ago)}</div>` : '';
      return `<div class="msg ${cls}" data-mid="${mid}"${editable}>${roleHtml}${gallery}<div class="body">${escapeHtml(m.text)}</div>${ago}</div>`;
    }).join('') || '<div class="msg"><div class="body" style="opacity:.5">No messages yet. Open a chat from ☰ or tap + Agent.</div></div>';
    if (preserveScroll) chatLog.scrollTop = prevTop;
    else scrollChatToEnd();
    bindMessageImages(chatLog);
    chatLog.querySelectorAll('.msg[data-edit="1"]').forEach((el) => {
      el.onclick = () => {
        editMsgId = el.dataset.mid || '';
        editText.value = el.querySelector('.body')?.textContent || '';
        sheet.classList.remove('open');
        editModal.classList.add('open');
      };
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
      const t = String(m.text || '').trim();
      if (CHAT_NOISE.has(kind)) continue;
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
      return `<button type="button" data-id="${escapeHtml(c.id != null ? c.id : '')}" class="${cls}">${badge}${escapeHtml(c.title)}<span class="sub">${escapeHtml(meta)}</span></button>`;
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
        repoList.querySelectorAll('button[data-id]').forEach((row) => row.classList.remove('active'));
        btn.classList.add('active');
        chatLog.innerHTML = '<div class="msg"><div class="body" style="opacity:.55">Loading conversation…</div></div>';
        lastChatRenderKey = '';
        chatPinnedBottom = true;
        meta.textContent = `opening ${btn.firstChild?.textContent || 'conversation'}…`;
        agentSend({ type: 'select_tab', id: btn.dataset.id });
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
      const m = btn.dataset.mode;
      btn.classList.toggle('on', mode === m || (m === 'multitask' && (mode === 'triage' || mode === 'multitask')));
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

    renderQueue(state);
    renderRepos(state);

    const msgs = normalizeMessages(state.messages, !!state.loading);
    renderChatMessages(msgs);

    if (tab === 'agent') {
      const active = (state.tabs || []).find((t) => t.active);
      const wsName = active?.title || state.workspace || state.targetTitle || 'Agent';
      meta.textContent = state.cdp
        ? (wsName + (state.loading ? ' · working' : ''))
        : 'CDP offline';
    }
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
        if (msg.type === 'models') renderModelsSheet(msg.models || []);
        if (msg.type === 'done') showToast(msg.text || 'Agent finished');
        if (msg.type === 'result' && msg.error) {
          agentBanner.textContent = msg.error;
          agentBanner.classList.add('show');
          showToast(msg.error);
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
    if (aws && aws.readyState === 1) aws.send(JSON.stringify(obj));
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
    const v = text.value;
    if (!v.trim() && !pendingImages.length) return;
    if (tab === 'agent') {
      agentSend({
        type: 'prompt',
        text: v,
        images: pendingImages.map(({ name, mime, data }) => ({ name, mime, data })),
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
  document.getElementById('newAgentBtn').onclick = () => agentSend({ type: 'new_chat' });
  document.getElementById('drawerNew').onclick = () => { agentSend({ type: 'new_chat' }); openDrawer(false); };
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
  document.getElementById('approveBtn').onclick = () => agentSend({ type: 'approve' });
  document.getElementById('rejectBtn').onclick = () => agentSend({ type: 'reject' });
  document.getElementById('modelBtn').onclick = () => agentSend({ type: 'list_models' });
  document.getElementById('usageBtn').onclick = () => {
    const summary = renderUsage(agentState || {}) || 'No usage yet';
    openSheet('Usage', `<div class="sub" style="padding:8px;white-space:pre-wrap">${escapeHtml(summary)}</div>
      <div class="headerRow">Notes</div>
      <div class="sub" style="padding:8px">Cursor rarely exposes full token counts over CDP. This tracks session prompts / accepts / model changes, plus any context % found in the UI.</div>`);
  };
  document.querySelectorAll('#modeRow button').forEach((btn) => {
    btn.onclick = () => agentSend({ type: 'set_mode', mode: btn.dataset.mode });
  });
  document.getElementById('editSend').onclick = () => {
    const v = editText.value.trim();
    if (!v) return;
    agentSend({ type: 'edit_message', id: editMsgId || '', text: v });
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
  connectAgent();
})();
