(() => {
  const esc = (s) => (s || "").replace(/\s+/g, " ").trim();
  const findFirst = (sels) => {
    for (const sel of sels) {
      try {
        const el = document.querySelector(sel);
        if (el) return el;
      } catch (e) {}
    }
    return null;
  };
  const textOf = (el) => {
    if (!el) return "";
    if (el.isContentEditable) return esc(el.innerText || el.textContent || "");
    if (typeof el.value === "string") return esc(el.value);
    return esc(el.innerText || el.textContent || "");
  };
  const btnLabel = (el) =>
    esc(el.getAttribute("aria-label") || el.innerText || el.textContent || "");
  const collectButtons = (sels, texts) => {
    const out = [];
    const seen = new Set();
    for (const sel of sels) {
      try {
        document.querySelectorAll(sel).forEach((el) => {
          if (!(el instanceof HTMLElement)) return;
          const r = el.getBoundingClientRect();
          if (r.width < 2 || r.height < 2) return;
          const label = btnLabel(el);
          if (!label || seen.has(label + sel)) return;
          seen.add(label + sel);
          out.push({ label, selector: sel });
        });
      } catch (e) {}
    }
    for (const el of document.querySelectorAll("button, [role='button']")) {
      if (!(el instanceof HTMLElement)) continue;
      const label = btnLabel(el);
      const hit = texts.find(
        (t) => label === t || label.startsWith(t + " ") || label.includes(t)
      );
      if (!hit) continue;
      const r = el.getBoundingClientRect();
      if (r.width < 2 || r.height < 2) continue;
      if (seen.has(label)) continue;
      seen.add(label);
      const aria = el.getAttribute("aria-label");
      out.push({
        label,
        selector: aria ? 'button[aria-label="' + aria.replace(/"/g, '\\"') + '"]' : null,
        text: hit,
      });
    }
    return out;
  };

  const AGE_RE = /^(\d+)\s*([smhd]|min|hr|mo|w)$/i;
  const isAge = (t) => AGE_RE.test(t) || t === "now" || t === "just now";
  const REPO_STOP = new Set([
    "repositories",
    "pinned",
    "home",
    "more",
    "new agent",
    "search",
    "automations",
    "customize",
    "agents",
    "file",
    "edit",
    "view",
    "help",
    "ide",
  ]);

  // ---- sidebar: group chats under repo / location ----
  const repoGroups = [];
  const flatTabs = [];
  try {
    const lines = (document.body.innerText || "")
      .split("\n")
      .map((s) => s.trim())
      .filter(Boolean);
    const start = Math.max(0, lines.findIndex((l) => /^repositories$/i.test(l)));
    const endMarkers = /^(this pc|ultra plan|chat title|file\b)/i;
    let section = "Repositories";
    let current = { name: "Repositories", kind: "repos", chats: [] };
    const pushSection = () => {
      if (current && (current.chats.length || current.name)) repoGroups.push(current);
    };
    // Also capture Pinned if present before Repositories
    const pinIdx = lines.findIndex((l) => /^pinned$/i.test(l));
    if (pinIdx >= 0 && (start < 0 || pinIdx < start)) {
      current = { name: "Pinned", kind: "pinned", chats: [] };
      for (let i = pinIdx + 1; i < (start >= 0 ? start : lines.length); i++) {
        const t = lines[i];
        if (/^repositories$/i.test(t)) break;
        if (isAge(t)) {
          if (current.chats.length)
            current.chats[current.chats.length - 1].age = t;
          continue;
        }
        if (REPO_STOP.has(t.toLowerCase())) continue;
        if (t.length > 90) continue;
        current.chats.push({ title: t, age: "", id: String(flatTabs.length) });
        flatTabs.push({
          id: String(flatTabs.length),
          title: t,
          age: "",
          repo: "Pinned",
          active: false,
        });
      }
      pushSection();
      current = { name: "Repositories", kind: "repos", chats: [] };
    }

    let repoName = null;
    const slice = start >= 0 ? lines.slice(start + 1, start + 120) : [];
    for (let i = 0; i < slice.length; i++) {
      const t = slice[i];
      if (endMarkers.test(t)) break;
      if (/^(home|more)$/i.test(t) && slice[i + 1] && !isAge(slice[i + 1])) {
        // location buckets like Home / Experiment
        if (repoName) {
          /* keep */
        }
        repoName = t;
        current = { name: t, kind: "location", chats: [] };
        repoGroups.push(current);
        continue;
      }
      if (isAge(t)) {
        if (current.chats.length) current.chats[current.chats.length - 1].age = t;
        if (flatTabs.length) flatTabs[flatTabs.length - 1].age = t;
        continue;
      }
      if (REPO_STOP.has(t.toLowerCase())) continue;
      // Heuristic: short token without spaces often a repo folder name
      const looksRepo =
        t.length <= 48 &&
        !/^\(\d+\)/.test(t) &&
        (t === t.toLowerCase() ||
          /^[a-z0-9._-]+$/i.test(t) ||
          ["CursorDesk", "MyProject"].includes(t));
      const next = slice[i + 1] || "";
      const nextIsChat = next && !isAge(next) && !REPO_STOP.has(next.toLowerCase());
      if (looksRepo && nextIsChat && !isAge(next)) {
        repoName = t;
        current = { name: t, kind: "repo", chats: [] };
        repoGroups.push(current);
        continue;
      }
      if (!current || current.name === "Repositories") {
        current = {
          name: repoName || "Chats",
          kind: repoName ? "repo" : "chats",
          chats: [],
        };
        repoGroups.push(current);
      }
      const chat = {
        title: t,
        age: "",
        id: String(flatTabs.length),
        repo: current.name,
      };
      current.chats.push(chat);
      flatTabs.push({
        id: chat.id,
        title: t,
        age: "",
        repo: current.name,
        active: false,
      });
    }
  } catch (e) {}

  // Prefer live clickable sidebar buttons for accurate ids/active
  const tabEls = [];
  const seenTab = new Set();
  // Only actual conversation rows. Generic sidebar buttons also include New
  // Agent, Search, repository headers, and Customize; counting those made the
  // phone's positional IDs point at the wrong conversation.
  const tabSels = [".glass-sidebar-agent-menu-btn"];
  for (const sel of tabSels) {
    try {
      document.querySelectorAll(sel).forEach((el) => {
        if (!(el instanceof HTMLElement) || seenTab.has(el)) return;
        const r = el.getBoundingClientRect();
        if (r.width < 8 || r.height < 8) return;
        const t = esc(el.getAttribute("aria-label") || el.innerText).slice(0, 120);
        if (!t || t.length < 2) return;
        if (/^(repositories|agents|new agent|new chat|search|automations|customize|pinned|home|more)$/i.test(t))
          return;
        if (/ctrl\+/i.test(t) && t.length < 24) return;
        seenTab.add(el);
        tabEls.push(el);
      });
    } catch (e) {}
  }
  const tabs = tabEls.slice(0, 80).map((el, i) => {
    const raw = esc(el.getAttribute("aria-label") || el.innerText);
    // strip trailing age like "2d" / "1m"
    const title = raw.replace(/\s+(\d+\s*[smhd]|just now|now)$/i, "").trim() || raw;
    const ageMatch = raw.match(/\s+(\d+\s*[smhd]|just now|now)$/i);
    const cls =
      (el.className || "") +
      " " +
      (el.getAttribute("aria-selected") || "") +
      " " +
      (el.getAttribute("data-state") || "");
    const active =
      /selected|active|aria-selected=\"true\"|data-state=\"active\"/i.test(cls) ||
      el.getAttribute("aria-selected") === "true" ||
      !!el.closest('[aria-selected="true"], [data-state="active"], .selected');
    const section = el.closest(".ui-sidebar-section, section");
    const sectionHead = section?.querySelector("[data-section-head], .ui-sidebar-section-head");
    const sectionName = sectionHead ? esc(sectionHead.innerText || sectionHead.textContent) : "";
    const hint = flatTabs.find((f) => f.title === title || raw.startsWith(f.title));
    const working = !!el.querySelector(
      ".glass-sidebar-agent-ascii-loader, .ui-ascii-loading-indicator, .ui-dot-grid-animator"
    );
    return {
      id: el.id || `index:${i}`,
      title: title.slice(0, 100),
      age: ageMatch ? ageMatch[1] : hint?.age || "",
      repo: sectionName || hint?.repo || "Chats",
      active,
      working,
    };
  });
  // Rebuild groups from real rows, avoiding textual sidebar parsing artifacts.
  if (tabs.length) {
    repoGroups.length = 0;
    const byRepo = new Map();
    for (const tab of tabs) {
      const name = tab.repo || "Chats";
      if (!byRepo.has(name)) {
        const group = { name, kind: "repo", chats: [] };
        byRepo.set(name, group);
        repoGroups.push(group);
      }
      byRepo.get(name).chats.push({
        id: tab.id,
        title: tab.title,
        age: tab.age,
        repo: name,
        active: tab.active,
        working: tab.working,
      });
    }
  }

  // Identity of the transcript actually mounted in Cursor. This must come
  // from the focused chat surface, never from the phone's intended selection.
  const focusedTitleEl = findFirst([
    "span.auxiliary-bar-chat-title",
    "[class*='auxiliary-bar-chat-title']",
  ]);
  const focusedTitle = focusedTitleEl
    ? esc(
        focusedTitleEl.getAttribute("title") ||
          focusedTitleEl.innerText ||
          focusedTitleEl.textContent ||
          ""
      )
    : "";
  const normalizeConversationTitle = (value) =>
    esc(value)
      .replace(/\s+(\d+\s*[smhdw]\s+ago|\d+\s+[smhdw]\s+ago|just now|now)$/i, "")
      .trim()
      .toLowerCase();
  const normalizedFocusedTitle = normalizeConversationTitle(focusedTitle);
  const focusedTab =
    tabs.find((tab) => tab.active) ||
    tabs.find(
      (tab) =>
        normalizedFocusedTitle &&
        normalizeConversationTitle(tab.title) === normalizedFocusedTitle
    ) ||
    null;
  const focusedConversation = {
    id: focusedTab ? String(focusedTab.id || "") : "",
    title: focusedTitle || (focusedTab ? focusedTab.title : "") || "",
  };

  // ---- messages + activity (thinking / work / subagents) ----
  const wrappers = Array.from(
    document.querySelectorAll(
      "[data-flat-index], [data-message-index], .composer-rendered-message[data-message-role], [data-message-role][data-message-id], .agent-transcript-row"
    )
  ).filter((el) => el.getClientRects().length > 0);
  const matched = new Set(wrappers);
  const top = wrappers.filter((el) => {
    let p = el.parentElement;
    while (p) {
      if (matched.has(p)) return false;
      p = p.parentElement;
    }
    return true;
  });

  const messages = [];
  const activity = [];
  for (const el of top.slice(-320)) {
    const role = el.getAttribute("data-message-role") || "";
    const kind = el.getAttribute("data-message-kind") || "";
    const idx =
      el.getAttribute("data-flat-index") ||
      el.getAttribute("data-message-index") ||
      el.getAttribute("data-message-id") ||
      "";
    const msgId =
      el.getAttribute("data-message-id") ||
      el.getAttribute("data-server-bubble-id") ||
      idx;
    const images = Array.from(el.querySelectorAll("img, a[href*='.png'], a[href*='.jpg'], a[href*='.jpeg'], a[href*='.webp'], a[href*='.gif']"))
      .map((node, imageIndex) => {
        const img = node.tagName === "IMG" ? node : node.querySelector("img");
        const href = node.tagName === "A" ? node.href || node.getAttribute("href") || "" : "";
        const rect = (img || node).getBoundingClientRect();
        const src =
          (img && (img.currentSrc || img.src || img.getAttribute("data-src"))) ||
          href ||
          "";
        const alt = (img && img.alt) || node.getAttribute("aria-label") || "";
        if (!src && !alt) return null;
        if (img && rect.width < 24 && rect.height < 24 && !/\.(png|jpe?g|webp|gif|bmp)/i.test(src + alt))
          return null;
        return {
          id: `${String(msgId || idx || messages.length)}-${imageIndex}`,
          src: src || alt,
          alt: alt || "Agent image",
        };
      })
      .filter((image) => image && !/^data:image\/svg\+xml/i.test(image.src))
      .slice(0, 12);

    const cls = (el.className || "").toString();
    if (/agent-transcript-row-activity|agent-transcript-row-work-group|agent-transcript-row-tail-status|agent-transcript-activity|agent-transcript-work-group|agent-transcript-tail-status/i.test(cls) ||
        el.querySelector(".agent-transcript-activity-group-collapsible, .agent-transcript-work-group-collapsible, .agent-transcript-tail-status")) {
      const header = el.querySelector(".ui-collapsible-header") || el;
      const label = textOf(header).slice(0, 200);
      let atype = "activity";
      if (/thought|thinking/i.test(label) || /activity-group/i.test(cls)) atype = "thinking";
      if (/worked|work-group|exploring|subagent|task\b/i.test(label + cls)) {
        if (/thought|thinking/i.test(label)) atype = "thinking";
        else if (/running|tail-status/i.test(label + cls)) atype = "running";
        else if (/explor|fetch|command|shell|tool/i.test(label)) atype = "tool";
        else atype = "work";
      }
      if (/sub.?agent|task agent|delegat/i.test(label)) atype = "subagent";
      activity.push({
        id: String(activity.length),
        type: atype,
        text: label,
        expanded: !/collapsed|aria-expanded=\"false\"/i.test(
          (header.getAttribute("aria-expanded") || "") + cls
        ),
      });
      // Feed only real transcript lines; live status is exposed via state.status.
      continue;
    }

    let text = "";
    let type = "message";
    const human = el.querySelector(".aislash-editor-input-readonly");
    const md =
      el.querySelector(".markdown-root") ||
      (cls.includes("agent-transcript-row-markdown") ? el : null);
    const toolLine = el.querySelector(
      ".ui-tool-call-line-action, .composer-tool-former-message, .composer-edit-file-review-wrapper"
    );
    const plan = el.querySelector(
      ".composer-create-plan-container, .plan-execution-title"
    );
    const runCmd = el.querySelector(
      ".composer-terminal-tool-call-block-container"
    );
    const thought = el.querySelector(".ui-collapsible-header");

    if (human || role === "human" || kind === "human") {
      type = "human";
      text = textOf(human || el);
    } else if (plan) {
      type = "plan";
      text = textOf(
        plan.querySelector(".composer-create-plan-title, .plan-execution-title") ||
          plan
      );
    } else if (runCmd) {
      type = "command";
      text = textOf(
        runCmd.querySelector(
          ".composer-terminal-command-expanded-text, .composer-terminal-top-header-description"
        ) || runCmd
      );
    } else if (toolLine) {
      type = "tool";
      text = textOf(toolLine);
      const fn = toolLine.querySelector(".ui-edit-tool-call__filename");
      if (fn) text = esc(fn.textContent);
    } else if (md) {
      type = role === "human" ? "human" : "assistant";
      text = textOf(md);
    } else if (thought && /thought|worked|explor/i.test(textOf(thought))) {
      type = "thinking";
      text = textOf(thought);
    } else {
      text = textOf(el).slice(0, 400);
      if (!text) continue;
      type =
        kind === "tool" ? "tool" : role === "human" ? "human" : "assistant";
    }
    if (!text && !images.length) continue;
    const trimText = text.trim();
    if (/^(\d+[smhdw]\s+ago|\d+\s+[smhdw]\s+ago|just now|now)$/i.test(trimText)) {
      type = "timestamp";
    } else if (
      /^\d+\s+files?\s+changed/i.test(trimText) ||
      /^review\s+/i.test(trimText) ||
      (trimText.length < 96 &&
        /^(finished|working|running|exploring|planning|generating|reading|searching|building|thought for)/i.test(
          trimText
        ))
    ) {
      type = "status";
    }
    messages.push({
      id: String(msgId || messages.length),
      role: role || type,
      kind: kind || type,
      type,
      text: text.slice(0, 4000),
      messageId: msgId || null,
      editable: type === "human",
      images,
    });
  }

  // ---- subagents ----
  // Step-group headers remain mounted when collapsed; detail cards do not.
  // Never move the virtualized transcript to discover more data.
  let subagents = {
    conversation: focusedConversation,
    groups: [],
    agents: [],
    total: 0,
    running: 0,
    completed: 0,
    error: 0,
  };
  try {
    const parser = globalThis.CursorDeskSubagentParser;
    const scrollRoot = document.querySelector(
      ".virtualized-composer-messages-scroll-container"
    );
    const nearLiveTail =
      !scrollRoot ||
      scrollRoot.scrollHeight - scrollRoot.clientHeight - scrollRoot.scrollTop <=
        Math.max(160, scrollRoot.clientHeight * 0.35);
    const notificationRows = Array.from(
      document.querySelectorAll(".agent-transcript-row-notification")
    );
    const lastNotification = notificationRows[notificationRows.length - 1] || null;
    const tidyAgentText = (raw) => {
      let value = esc(raw || "");
      if (!value) return "";
      const half = Math.floor(value.length / 2);
      if (
        value.length % 2 === 0 &&
        value.slice(0, half).trim() === value.slice(half).trim()
      ) {
        value = value.slice(0, half).trim();
      }
      return value.length > 160
        ? value.slice(0, 157).trimEnd() + "..."
        : value;
    };
    const readCard = (card, index) => {
      const row = card.closest(
        "[data-tool-call-id], [data-message-id], [data-react-transcript-row-key]"
      );
      const titleEl = card.querySelector(
        ".subagent-task-card-title, [class*='subagent'][class*='title']"
      );
      const statusRoot = card.querySelector(
        ".subagent-task-card-status-stack, [class*='subagent'][class*='status']"
      );
      const currentStatus =
        statusRoot?.querySelector("[data-slot='current']") ||
        statusRoot?.querySelector("[data-sr-only='true']") ||
        statusRoot;
      const indicator = card.querySelector(
        ".ui-subagent-status-indicator, [class*='subagent-status-indicator']"
      );
      const indicatorClass = String(indicator?.className || "");
      const rowStatus = String(
        row?.getAttribute("data-tool-status") ||
          card.getAttribute("data-status") ||
          ""
      );
      const hasStop =
        !!card.querySelector(
          ".task-subagent-header-pill-button--stop, button[aria-label*='Stop']"
        ) ||
        Array.from(card.querySelectorAll("button")).some((button) =>
          /^stop$/i.test(btnLabel(button))
        );
      const hasLoader = !!card.querySelector(
        ".ui-ascii-loading-indicator, .ui-dot-grid-animator, [class*='running-loader']"
      );
      const signal = `${indicatorClass} ${rowStatus} ${textOf(statusRoot)}`;
      let state = "running";
      if (/error|failed|failure|cancel|stopped|denied/i.test(signal)) {
        state = "error";
      } else if (
        !hasStop &&
        !hasLoader &&
        /complete|success|finished|done|check/i.test(signal)
      ) {
        state = "completed";
      }
      const rawId =
        row?.getAttribute("data-tool-call-id") ||
        row?.getAttribute("data-message-id") ||
        row?.getAttribute("data-react-transcript-row-key") ||
        card.getAttribute("data-task-id") ||
        "";
      return {
        id: rawId || `subagent:${index}`,
        index: index + 1,
        label: `Agent ${index + 1}`,
        title: tidyAgentText(titleEl?.getAttribute("title") || textOf(titleEl)),
        status: tidyAgentText(textOf(currentStatus)),
        state,
        model: tidyAgentText(
          textOf(card.querySelector(".task-subagent-model-hover-trigger"))
        ),
      };
    };
    const descriptors = nearLiveTail
      ? notificationRows
          .map((row, index) => {
            const actionEl = row.querySelector(".ui-collapsible-action");
            const detailsEl = row.querySelector(".ui-collapsible-details");
            const action = textOf(actionEl);
            const details = textOf(detailsEl);
            const countMatch = details.match(/\b(\d+)\s+sub\s*agents?\b/i);
            if (!actionEl || !countMatch) return null;
            const collapsible =
              actionEl.closest(".ui-collapsible") ||
              row.querySelector(".ui-step-group-collapsible");
            const header = row.querySelector(".ui-collapsible-header");
            const expanded =
              header?.getAttribute("aria-expanded") === "true" &&
              !collapsible?.hasAttribute("data-closed");
            const cards = expanded
              ? Array.from(
                  row.querySelectorAll(
                    ".subagent-task-card, [data-testid*='subagent-task-card']"
                  )
                ).filter(
                  (card) =>
                    card instanceof HTMLElement &&
                    (card.matches(".subagent-task-card") ||
                      !!card.querySelector(".subagent-task-card-title"))
                )
              : [];
            return {
              id:
                row.getAttribute("data-react-transcript-row-key") ||
                row.getAttribute("data-message-id") ||
                `subagent-group:${index}:${details}`,
              action,
              details,
              count: Number(countMatch[1]),
              expanded,
              isLastNotification: row === lastNotification,
              agents: cards.slice(-24).map(readCard),
            };
          })
          .filter(Boolean)
      : [];
    const overallLoading = !!(
      document.querySelector(
        ".loading-indicator-v3, .agent-transcript-tail-status, [class*='thinking'], [class*='spinner']"
      ) ||
      /running/i.test(textOf(document.querySelector(".agent-transcript-tail-status")))
    );
    subagents = {
      conversation: focusedConversation,
      ...parser.parseGroups(descriptors, overallLoading),
    };
  } catch (e) {}

  // ---- mode / model ----
  const modeEl = findFirst([
    ".composer-unified-dropdown[data-mode]",
    ".composer-bar-input-buttons[data-mode]",
    "[data-mode]",
  ]);
  let mode = modeEl
    ? modeEl.getAttribute("data-mode") || textOf(modeEl)
    : "";
  // The picker trigger is a span, not a <button>, so match the wrapper itself.
  const modelBtn = findFirst([
    ".ui-model-picker__trigger-text",
    ".glass-model-picker-wrapper",
    ".ui-model-picker__trigger",
    ".composer-unified-dropdown-model",
  ]);
  const model = modelBtn ? textOf(modelBtn) : "";

  // ---- session location / remote ----
  let sessionLabel = "";
  let sessionKind = "local";
  const remoteBits = [];
  for (const el of document.querySelectorAll("*")) {
    if (el.children.length) continue;
    const t = (el.textContent || "").trim();
    if (!t || t.length > 80) continue;
    if (/remote control|this pc|ssh:|wsl:|\[ssh\]|\[wsl\]/i.test(t)) {
      remoteBits.push(t);
    }
  }
  // "This PC" is Cursor's label for a local session, so it must not read as remote.
  const localBit = remoteBits.find((t) => /this pc/i.test(t));
  const remoteBit = remoteBits.find((t) => !/this pc/i.test(t));
  if (remoteBit) {
    sessionLabel = remoteBit;
    if (/remote control/i.test(sessionLabel)) sessionKind = "remote-control";
    else if (/ssh/i.test(sessionLabel)) sessionKind = "ssh";
    else if (/wsl/i.test(sessionLabel)) sessionKind = "wsl";
    else sessionKind = "remote";
  } else {
    sessionLabel = localBit ? localBit.trim() : "Local";
    sessionKind = "local";
  }
  try {
    const cfg =
      window.vscode &&
      window.vscode.context &&
      window.vscode.context.configuration
        ? window.vscode.context.configuration()
        : null;
    const uri = cfg && cfg.workspace && cfg.workspace.uri;
    if (uri && uri.authority) {
      sessionKind = String(uri.scheme || "remote");
      sessionLabel = (uri.scheme || "remote") + ": " + uri.authority;
    }
  } catch (e) {}

  // ---- queue ----
  const queue = [];
  document
    .querySelectorAll(
      "[class*='queue'] [class*='item'], [class*='composer-queue'] > *, [data-queue-item]"
    )
    .forEach((el, i) => {
      const t = textOf(el).slice(0, 500);
      if (!t) return;
      queue.push({ id: String(i), text: t });
    });

  // ---- usage / context ----
  let contextPercent = null;
  let usageLabel = "";
  const bodyText = document.body.innerText || "";
  const pct = bodyText.match(/\b(\d{1,3})%\s*(context|used|left)?/i);
  if (pct) {
    contextPercent = parseInt(pct[1], 10);
    usageLabel = pct[0];
  }
  const planMatch = bodyText.match(/\b(Ultra Plan|Pro Plan|Business|Free Plan)\b/);
  const planName = planMatch ? planMatch[1] : "";

  const statusEl = findFirst([
    "span.auxiliary-bar-chat-title",
    "[class*='auxiliary-bar-chat-title']",
    ".loading-indicator-v3",
    ".agent-transcript-tail-status",
  ]);
  const loading = !!(
    document.querySelector(
      ".loading-indicator-v3, .agent-transcript-tail-status, [class*='thinking'], [class*='spinner']"
    ) || /running/i.test(textOf(document.querySelector(".agent-transcript-tail-status")))
  );
  // Tail status renders the same label twice (visible + screen-reader copy) and can
  // run long, so collapse the duplicate and cap it before it reaches the phone UI.
  const tidyStatus = (raw) => {
    let s = (raw || "").replace(/\s+/g, " ").trim();
    if (!s) return "";
    const doubled = s.match(/^(.+?)\s*\1$/);
    if (doubled) s = doubled[1].trim();
    return s.length > 80 ? s.slice(0, 77).trimEnd() + "..." : s;
  };
  const status = loading
    ? tidyStatus(textOf(document.querySelector(".agent-transcript-tail-status"))) || "running"
    : statusEl
      ? tidyStatus(textOf(statusEl))
      : "idle";

  // Do not infer that the last assistant message is streaming from the global
  // loading indicator. Other conversations can be working, and Cursor leaves
  // completed markdown mounted beside the tail status. The phone already has
  // a dedicated runner for live status, so transcript replies stay full-size.

  let workspace = "";
  try {
    const cfg =
      window.vscode &&
      window.vscode.context &&
      window.vscode.context.configuration
        ? window.vscode.context.configuration()
        : null;
    const uri = cfg && cfg.workspace && cfg.workspace.uri;
    if (uri && uri.path) {
      const parts = String(uri.path).replace(/\\/g, "/").split("/").filter(Boolean);
      workspace = parts[parts.length - 1] || "";
    }
  } catch (e) {}
  if (!workspace) {
    const activeRepo = (tabs.find((t) => t.active) || {}).repo;
    workspace =
      activeRepo ||
      (document.title || "").replace(/\s*-\s*Cursor\s*$/i, "").split(" - ").pop() ||
      "";
  }

  const approveSels = [
    "button.ui-shell-tool-call__run-btn",
    "button.ui-shell-tool-call__allowlist-button",
    "button[aria-label*='Accept']",
    "button[aria-label*='Approve']",
    "button[aria-label*='Run']",
    "button[aria-label*='Allow']",
    ".composer-run-button",
    ".composer-create-plan-build-button",
  ];
  const rejectSels = [
    "button.ui-shell-tool-call__skip-btn",
    "button[aria-label*='Reject']",
    "button[aria-label*='Deny']",
    "button[aria-label*='Cancel']",
    ".composer-skip-button",
  ];

  return {
    ok: true,
    title: document.title || "",
    workspace,
    status,
    loading,
    mode,
    model,
    messages,
    activity,
    subagents,
    approvals: collectButtons(approveSels, [
      "Accept All",
      "Accept",
      "Approve",
      "Run",
      "Allow",
      "Build",
    ]),
    rejects: collectButtons(rejectSels, ["Reject", "Deny", "Cancel", "Skip"]),
    tabs,
    repos: repoGroups,
    queue,
    session: { kind: sessionKind, label: sessionLabel },
    usage: {
      contextPercent,
      label: usageLabel,
      plan: planName,
      model,
    },
    hasNewAgent: !!findFirst([
      '[data-command-id="composer.createNewComposerTab"]',
      '[aria-label*="New Agent"]',
      '[aria-label*="New Chat"]',
      "a.codicon-add-two",
    ]),
    hasInput: !!findFirst([
      "#workbench\\.parts\\.auxiliarybar [contenteditable='true']",
      ".composer-bar [contenteditable='true']",
      "[contenteditable='true']",
      "textarea",
    ]),
  };
})()
