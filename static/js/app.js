/* dsh-pocket · personal app: workspaces → phone sessions → chat.
   No auth, no pairing, no desktop history. Contract: docs/api.md (personal). */
(function () {
  'use strict';

  var DSH = window.DSH;
  var api = DSH.api;
  var ui = DSH.ui;
  var util = DSH.util;

  var WORKSPACE_KEY = 'dsh_pocket_workspace';
  var PAGE_LIMIT = 30;

  var state = {
    health: null,
    workspaces: [],
    workspaceId: null,
    browsePath: '',

    sessions: [],
    nextCursor: null,
    sessionsLoading: false,
    reloadQueued: false,

    activeSessionId: null,
    openedTitle: '',
    detail: null,
    hasMore: false,
    oldestBefore: null,
    loadingOlder: false,

    wsStatus: 'offline',
    streaming: false,
    sending: false,
    pendingTurn: null,
    turnRequestId: null,

    assistantText: {},
    thoughtText: {},
    liveTools: {},
    optimistic: {},
    pendingOptimisticKey: null,
    lastSentText: '',

    permissions: {},
    permissionOrder: [],
    shownPermission: null,

    creating: false,
    resumeBusy: false,
    reconnectBanner: false
  };

  var ws = null;

  /* ------------------------------------------------------------------- utils */

  function readSavedWorkspace() {
    try { return window.localStorage.getItem(WORKSPACE_KEY) || ''; } catch (err) { return ''; }
  }

  function saveWorkspace(id) {
    try { window.localStorage.setItem(WORKSPACE_KEY, id || ''); } catch (err) { /* noop */ }
  }

  function currentWorkspace() {
    for (var i = 0; i < state.workspaces.length; i += 1) {
      if (state.workspaces[i].id === state.workspaceId) return state.workspaces[i];
    }
    return null;
  }

  function isActive(sessionId) {
    return !!state.activeSessionId && sessionId === state.activeSessionId;
  }


  function anchorOptimistic(seq) {
    if (!state.pendingOptimisticKey) return;
    ui.anchorUser(state.pendingOptimisticKey, seq);
  }

  function basename(path) {
    var text = String(path || '').replace(/\/+$/, '');
    var parts = text.split('/');
    return parts[parts.length - 1] || text;
  }

  function firstLine(text, max) {
    var line = String(text || '').split('\n')[0].trim();
    return util.truncate(line, max || 40);
  }

  function toastApiError(err, prefix) {
    if (!err) return;
    var text = util.errorText(err.code, err.message);
    ui.toast(prefix ? prefix + '：' + text : text, 'error');
  }

  function updateSessionInList(sessionId, patch) {
    for (var i = 0; i < state.sessions.length; i += 1) {
      if (state.sessions[i].sessionId !== sessionId) continue;
      for (var key in patch) {
        if (Object.prototype.hasOwnProperty.call(patch, key)) state.sessions[i][key] = patch[key];
      }
      return;
    }
  }

  /* -------------------------------------------------------------------- boot */

  async function bootstrap() {
    ui.showBoot();
    state.health = null;
    state.workspaces = [];
    state.workspaceId = null;

    var health;
    var workspaces;
    try {
      var results = await Promise.all([api.health(), api.workspaces()]);
      health = results[0];
      workspaces = results[1].workspaces || [];
    } catch (err) {
      var code = err && err.code;
      var message;
      if (code === 'E_ACP_UNAVAILABLE') {
        message = 'ACP 后端未就绪：' + (err.message || '无法启动 dsh --profile acp') +
          '。请稍后点重试。';
      } else {
        message = util.errorText(code, err && err.message) +
          '。请确认 dsh-pocket 已在电脑上启动，并且手机与电脑在同一局域网。';
      }
      ui.showBootError(message);
      return;
    }

    state.health = health;
    ui.setModeLabel(health.mode || '');
    applyWorkspaces(workspaces, true);
    ui.showHome();
    updateWorkspaceDisplay();
    startWs();
    await loadSessions(true);
  }

  function applyWorkspaces(list, preferSaved) {
    state.workspaces = Array.isArray(list) ? list : [];
    var saved = preferSaved ? readSavedWorkspace() : state.workspaceId;
    var chosen = null;

    for (var i = 0; i < state.workspaces.length; i += 1) {
      var workspace = state.workspaces[i];
      if (workspace.id === saved && workspace.exists) { chosen = workspace; break; }
    }
    if (!chosen) {
      for (var j = 0; j < state.workspaces.length; j += 1) {
        if (state.workspaces[j].exists) { chosen = state.workspaces[j]; break; }
      }
    }
    if (!chosen && state.workspaces.length) chosen = state.workspaces[0];
    state.workspaceId = chosen ? chosen.id : null;
    if (chosen) saveWorkspace(chosen.id);
  }

  function updateWorkspaceDisplay() {
    var workspace = currentWorkspace();
    ui.setWorkspace(workspace || null);
  }

  /* --------------------------------------------------------------- workspaces */

  function openDrawer() {
    ui.renderWorkspaces(state.workspaces, state.workspaceId, pickWorkspace);
    ui.renderDrawerSessions(state.sessions, function (session) {
      ui.closeDrawer();
      openSession(session);
    });
    ui.openDrawer();
  }

  function pickWorkspace(workspace) {
    if (!workspace || !workspace.exists) return;
    state.workspaceId = workspace.id;
    saveWorkspace(workspace.id);
    updateWorkspaceDisplay();
    ui.closeDrawer();
    ui.setBrowseOpen(false);
    if (ui.isChatOpen()) leaveChat();
    loadSessions(true);
  }

  async function refreshAll() {
    ui.setRefreshBusy(true);
    ui.closeDrawer();
    try {
      var results = await Promise.all([api.health(), api.workspaces()]);
      state.health = results[0];
      ui.setModeLabel(state.health.mode || '');
      applyWorkspaces(results[1].workspaces || [], false);
      updateWorkspaceDisplay();
      await loadSessions(true);
      ui.toast('已刷新', 'success');
    } catch (err) {
      toastApiError(err, '刷新失败');
      ui.setListStatus(util.errorText(err && err.code, err && err.message), {
        kind: 'error',
        onRetry: refreshAll
      });
    } finally {
      ui.setRefreshBusy(false);
    }
  }

  /* ------------------------------------------------------------------ browse */

  async function openBrowse(path) {
    ui.setBrowseOpen(true);
    ui.closeBrowseError();
    try {
      var data = await api.browse(path || '');
      showBrowseData(data);
    } catch (err) {
      if (path) {
        try {
          var fallback = await api.browse('');
          showBrowseData(fallback);
          ui.setBrowseError('当前工作区不在可浏览范围，已打开默认根目录。');
          return;
        } catch (err2) { /* show the original error below */ }
      }
      ui.setBrowseError(util.errorText(err && err.code, err && err.message));
    }
  }

  function showBrowseData(data) {
    state.browsePath = data.path || '';
    ui.renderBrowse(data, {
      onEnter: function (path) { openBrowse(path); },
      onUse: function (path) { createSession({ cwd: path }); }
    });
  }

  function toggleBrowse() {
    if (ui.isBrowseOpen()) {
      ui.setBrowseOpen(false);
      return;
    }
    ui.setBrowseOpen(true);
    var workspace = currentWorkspace();
    openBrowse(state.browsePath || (workspace && workspace.path) || '');
  }

  /* ------------------------------------------------------------------ lists */

  function mergeSessions(existing, incoming) {
    var merged = existing.slice();
    var seen = {};
    merged.forEach(function (session) { seen[session.sessionId] = true; });
    incoming.forEach(function (session) {
      if (!session || !session.sessionId || seen[session.sessionId]) return;
      seen[session.sessionId] = true;
      merged.push(session);
    });
    return merged;
  }

  async function loadSessions(reset) {
    if (!state.workspaceId) {
      state.sessions = [];
      state.nextCursor = null;
      ui.renderSessions([], { onOpen: openSession });
      ui.renderDrawerSessions([], null);
      ui.setListEmpty(false);
      ui.setListMore(false, false);
      ui.setListStatus('先打开抽屉选择一个工作区，或浏览目录新建会话。');
      return;
    }
    if (state.sessionsLoading) {
      if (reset) state.reloadQueued = true;
      return;
    }

    state.sessionsLoading = true;
    var workspaceId = state.workspaceId;
    var cursor = reset ? undefined : state.nextCursor;

    if (reset) {
      state.sessions = [];
      state.nextCursor = null;
      ui.renderSessions([], { onOpen: openSession });
      ui.setListEmpty(false);
      ui.setListMore(false, false);
      ui.setListStatus('正在加载会话…');
    } else {
      ui.setListMore(true, true);
    }

    try {
      var result = await api.sessions({ workspaceId: workspaceId, cursor: cursor, limit: PAGE_LIMIT });
      if (state.workspaceId !== workspaceId) {
        state.reloadQueued = true;
        return;
      }
      var items = Array.isArray(result.sessions) ? result.sessions : [];
      state.sessions = reset ? items : mergeSessions(state.sessions, items);
      state.nextCursor = result.nextCursor || null;
      ui.renderSessions(state.sessions, { onOpen: openSession });
      ui.renderDrawerSessions(state.sessions, function (session) {
        ui.closeDrawer();
        openSession(session);
      });
      ui.setListStatus('');
      ui.setListEmpty(state.sessions.length === 0);
      ui.setListMore(!!state.nextCursor, false);
    } catch (err) {
      ui.setListEmpty(false);
      ui.setListStatus(util.errorText(err && err.code, err && err.message), {
        kind: 'error',
        onRetry: function () { loadSessions(reset); }
      });
    } finally {
      state.sessionsLoading = false;
      if (state.reloadQueued) {
        state.reloadQueued = false;
        loadSessions(true);
      } else if (!reset) {
        ui.setListMore(!!state.nextCursor, false);
      }
    }
  }

  function loadMore() {
    if (!state.nextCursor || state.sessionsLoading) return;
    loadSessions(false);
  }

  /* ------------------------------------------------------------------- chat */

  function leaveChat() {
    if (!state.activeSessionId) {
      ui.showHome();
      return;
    }
    var leavingSession = state.activeSessionId;
    if (ws) ws.leaveSession();

    state.activeSessionId = null;
    state.detail = null;
    state.openedTitle = '';
    state.streaming = false;
    state.sending = false;
    state.pendingTurn = null;
    state.turnRequestId = null;
    state.assistantText = {};
    state.thoughtText = {};
    state.liveTools = {};
    state.optimistic = {};
    state.pendingOptimisticKey = null;
    state.lastSentText = '';
    state.reconnectBanner = false;
    state.shownPermission = null;

    state.permissionOrder.slice().forEach(function (requestId) {
      var request = state.permissions[requestId];
      if (request && request.sessionId === leavingSession) {
        delete state.permissions[requestId];
        var index = state.permissionOrder.indexOf(requestId);
        if (index >= 0) state.permissionOrder.splice(index, 1);
      }
    });

    ui.hidePermission();
    ui.setBanner(null);
    ui.setChatLoading(false);
    ui.clearMessages();
    ui.showHome();
  }

  function backToHome() {
    if (!state.activeSessionId) return;
    if (state.streaming) {
      var ok = window.confirm('回复仍在生成，返回会话列表后会在后台继续。确定返回吗？');
      if (!ok) return;
    }
    leaveChat();
    loadSessions(true);
  }

  async function openSession(sessionOrId, options) {
    var opts = options || {};
    var sessionId = typeof sessionOrId === 'string'
      ? sessionOrId : (sessionOrId && sessionOrId.sessionId);
    if (!sessionId) return;
    if (state.activeSessionId === sessionId && state.detail) return;

    if (ui.isDrawerOpen()) ui.closeDrawer();
    state.activeSessionId = sessionId;
    state.openedTitle = (sessionOrId && sessionOrId.title) || '';
    state.detail = null;
    state.streaming = false;
    state.sending = false;
    state.pendingTurn = null;
    state.turnRequestId = null;
    state.hasMore = false;
    state.oldestBefore = null;
    state.loadingOlder = false;
    state.assistantText = {};
    state.thoughtText = {};
    state.liveTools = {};
    state.optimistic = {};
    state.pendingOptimisticKey = null;
    state.lastSentText = '';
    state.reconnectBanner = false;
    state.shownPermission = null;

    ui.showChat();
    ui.clearMessages();
    ui.setChatHeader({
      title: state.openedTitle || '会话',
      state: 'idle',
      usage: null
    });
    ui.setBanner(null);
    ui.setChatLoading(true);
    ui.setChatEmpty(false);
    ui.setOlderButton(false, false);
    ui.setComposerMode('offline');
    ui.hidePermission();

    var detail;
    var page;
    try {
      if (opts.prefetched) {
        detail = opts.prefetched;
        page = await api.transcript(sessionId, { limit: PAGE_LIMIT });
      } else {
        var results = await Promise.all([
          api.session(sessionId),
          api.transcript(sessionId, { limit: PAGE_LIMIT })
        ]);
        detail = results[0];
        page = results[1];
      }
    } catch (err) {
      if (state.activeSessionId !== sessionId) return;
      state.activeSessionId = null;
      ui.setChatLoading(false);
      ui.clearMessages();
      ui.showHome();
      ui.setListStatus('打开会话失败：' + util.errorText(err && err.code, err && err.message), {
        kind: 'error',
        onRetry: function () { openSession(sessionId); }
      });
      ui.toast(util.errorText(err && err.code, err && err.message), 'error');
      return;
    }

    if (state.activeSessionId !== sessionId) return;

    state.detail = detail;
    state.openedTitle = (detail.summary && detail.summary.title) || state.openedTitle || '会话';
    ui.setChatHeader({
      title: state.openedTitle,
      state: detail.state || 'idle',
      usage: detail.usage || null
    });
    ui.setChatLoading(false);
    applyHistoryPage(page, 'replace');
    ui.setOlderButton(state.hasMore && !!state.oldestBefore, false);
    ui.setChatEmpty(!ui.hasMessages());
    ui.scrollToBottom(false);
    refreshComposer();

    if (!ws) startWs();
    if (ws) ws.subscribeSession(sessionId);
    if (hasPermissionForActive()) showNextPermission();
  }

  function applyHistoryPage(page, mode) {
    if (!page) return;
    var items = Array.isArray(page.items) ? page.items : [];
    var preserved = null;
    var preservedPendingKey = null;

    if (mode === 'replace') {
      preserved = {};
      Object.keys(state.optimistic).forEach(function (key) {
        preserved[key] = state.optimistic[key];
      });
      preservedPendingKey = state.pendingOptimisticKey;
      ui.clearMessages();
      state.assistantText = {};
      state.thoughtText = {};
      state.liveTools = {};
      state.optimistic = {};
      state.pendingOptimisticKey = null;
    }

    var historyUserTexts = {};
    items.forEach(function (item) {
      if (item && item.kind === 'user') {
        var text = item.text || '';
        historyUserTexts[text] = (historyUserTexts[text] || 0) + 1;
      }
      renderHistoryItem(item, mode);
    });

    if (mode === 'replace' && preserved) {
      Object.keys(preserved).forEach(function (key) {
        var text = preserved[key];
        if (historyUserTexts[text] > 0) {
          historyUserTexts[text] -= 1;
          return;
        }
        state.optimistic[key] = text;
        if (key === preservedPendingKey) state.pendingOptimisticKey = key;
        ui.upsertUser({ key: key, seq: null, text: text, pending: true });
      });
    }

    state.hasMore = !!page.hasMore;
    var nextBefore = typeof page.nextBefore === 'number' ? page.nextBefore : null;
    if (!nextBefore && items.length) {
      var firstSeq = null;
      items.forEach(function (item) {
        if (typeof item.seq !== 'number') return;
        if (firstSeq === null || item.seq < firstSeq) firstSeq = item.seq;
      });
      nextBefore = firstSeq;
    }
    state.oldestBefore = nextBefore;

    var pageSession = page.session || {};
    if (pageSession.usage && state.detail) {
      state.detail.usage = pageSession.usage;
      ui.setUsage(pageSession.usage);
    }
    if (pageSession.title && state.detail) {
      state.openedTitle = pageSession.title;
      ui.R.chatTitle.textContent = util.cleanTitle(pageSession.title);
    }
    ui.setChatEmpty(!ui.hasMessages());
  }

  function historyKey(prefix, item, seq) {
    var id = item && (item.id || item.callId);
    if (!id) id = seq === undefined ? 'x' : String(seq);
    return 'h:' + prefix + ':' + id;
  }

  function consumeOptimistic(text) {
    if (!text) return;
    var keys = Object.keys(state.optimistic);
    for (var i = 0; i < keys.length; i += 1) {
      if (state.optimistic[keys[i]] !== text) continue;
      ui.removeMessage(keys[i]);
      delete state.optimistic[keys[i]];
      if (state.pendingOptimisticKey === keys[i]) state.pendingOptimisticKey = null;
      return;
    }
  }

  function fetchFullToolCall(key, sessionId, callId, seq, name, rawInput) {
    api.toolCall(sessionId, callId).then(function (result) {
      if (!result || !isActive(sessionId)) return;
      ui.upsertTool({
        key: key,
        seq: seq,
        name: result.name || name,
        rawInput: result.arguments !== undefined ? result.arguments : rawInput,
        status: result.status,
        output: result.outputText,
        truncated: false
      });
    }).catch(function (err) {
      toastApiError(err, '读取工具输出失败');
    });
  }

  function renderHistoryItem(item, mode) {
    if (!item || !item.kind) return;
    if (item.kind === 'user') {
      if (mode === 'replace') consumeOptimistic(item.text || '');
      ui.upsertUser({
        key: historyKey('user', item, item.seq),
        seq: item.seq,
        text: item.text || '',
        pending: false
      });
      return;
    }
    if (item.kind === 'assistant') {
      var assistantKey = historyKey('assistant', item, item.seq);
      if (item.reasoning) {
        ui.upsertReasoning({
          key: historyKey('reason', item, item.seq),
          seq: item.seq,
          text: item.reasoning,
          live: false
        });
      }
      ui.upsertAssistant({
        key: assistantKey,
        seq: item.seq,
        text: item.text || '',
        streaming: false
      });
      return;
    }
    if (item.kind === 'tool') {
      var callId = item.callId || ('seq-' + item.seq);
      var sessionId = state.activeSessionId;
      var seq = item.seq;
      var name = item.name || 'tool';
      var key = 'h-tool:' + callId;
      ui.upsertTool({
        key: key,
        seq: seq,
        name: name,
        title: name,
        rawInput: item.arguments,
        status: item.status === 'in_progress' ? 'running' : item.status,
        output: item.outputText,
        truncated: !!item.truncated,
        onFetchFull: item.truncated && sessionId
          ? function () { fetchFullToolCall(key, sessionId, callId, seq, name, item.arguments); }
          : null
      });
    }
  }

  async function loadOlder() {
    if (!state.activeSessionId || state.loadingOlder || !state.hasMore) return;
    if (state.oldestBefore === null) return;
    state.loadingOlder = true;
    ui.setOlderButton(true, true);
    var scroll = ui.R.messageScroll;
    var previousHeight = scroll.scrollHeight;
    var previousTop = scroll.scrollTop;
    try {
      var page = await api.transcript(state.activeSessionId, {
        before: state.oldestBefore,
        limit: PAGE_LIMIT
      });
      if (!state.activeSessionId) return;
      applyHistoryPage(page, 'merge');
      ui.setOlderButton(state.hasMore && !!state.oldestBefore, false);
      scroll.scrollTop = Math.max(0, scroll.scrollHeight - previousHeight + previousTop);
    } catch (err) {
      toastApiError(err, '加载更早消息失败');
    } finally {
      state.loadingOlder = false;
      ui.setOlderButton(state.hasMore && !!state.oldestBefore, false);
    }
  }

  function refreshComposer() {
    if (!state.activeSessionId) return;

    if (state.wsStatus !== 'connected') {
      ui.setComposerMode('offline');
      return;
    }
    var sessionState = state.detail ? state.detail.state : 'idle';

    if (sessionState === 'detached') {
      ui.setComposerMode('detached', { resumeBusy: state.resumeBusy });
      return;
    }
    if (hasPermissionForActive()) {
      ui.setComposerMode('awaiting_permission');
      return;
    }
    if (state.sending && !state.streaming) {
      ui.setComposerMode('sending');
      return;
    }
    if (sessionState === 'cancelling') {
      ui.setComposerMode('cancelling');
      return;
    }
    if (sessionState === 'streaming' || sessionState === 'awaiting_permission' ||
      state.streaming || state.pendingTurn) {
      ui.setComposerMode('streaming');
      return;
    }
    if (sessionState === 'error') {
      ui.setComposerMode('error');
      return;
    }
    ui.setComposerMode('idle');
  }

  /* -------------------------------------------------------------------- send */

  function sendMessage() {
    if (!state.activeSessionId) return;
    var text = ui.getComposerText();
    if (!text.trim()) return;

    if (!ws || !ws.isConnected()) {
      ui.toast('实时连接未建立，请稍候重试', 'warn');
      return;
    }
    if (state.streaming || state.sending || state.pendingTurn) {
      ui.toast('上一条消息还在处理中', 'warn');
      return;
    }
    if (hasPermissionForActive()) {
      ui.toast('请先处理工具权限请求', 'warn');
      return;
    }
    if (state.detail && state.detail.state === 'detached') {
      ui.toast('请先点「继续」恢复会话', 'warn');
      return;
    }

    var requestId = util.turnId();
    var clientMessageId = util.uid();
    var key = 'u:' + clientMessageId;
    var clean = text.trim();

    ui.setBanner(null);
    state.sending = true;
    state.pendingTurn = requestId;
    state.turnRequestId = requestId;
    state.lastSentText = clean;
    state.optimistic[key] = clean;
    state.pendingOptimisticKey = key;

    ui.upsertUser({ key: key, seq: null, text: clean, pending: true });
    ui.clearComposer();
    ui.setNewBelow(false);
    ui.scrollToBottom(true);
    refreshComposer();

    var sent = ws.send({
      type: 'prompt',
      requestId: requestId,
      sessionId: state.activeSessionId,
      clientMessageId: clientMessageId,
      text: clean
    });
    if (!sent) {
      delete state.optimistic[key];
      state.pendingOptimisticKey = null;
      state.sending = false;
      state.pendingTurn = null;
      state.turnRequestId = null;
      ui.removeMessage(key);
      ui.setComposerText(text);
      refreshComposer();
      ui.toast('发送失败：实时连接已断开', 'error');
      return;
    }

    window.setTimeout(function () {
      if (state.pendingTurn !== requestId) return;
      if (!state.streaming && state.sending) {
        state.sending = false;
        state.streaming = true;
        if (state.detail) state.detail.state = 'streaming';
        ui.setStateChip('streaming');
        refreshComposer();
      }
    }, 4000);
  }

  function failPrompt(code, message) {
    if (state.pendingOptimisticKey) {
      var key = state.pendingOptimisticKey;
      ui.removeMessage(key);
      delete state.optimistic[key];
      state.pendingOptimisticKey = null;
    }
    if (state.lastSentText) {
      ui.setComposerText(state.lastSentText);
      state.lastSentText = '';
    }
    state.pendingTurn = null;
    state.turnRequestId = null;
    state.sending = false;
    ui.toast(util.errorText(code, message), 'error');
    refreshComposer();
  }

  function cancelTurn() {
    if (!state.activeSessionId) return;
    if (!ws || !ws.isConnected()) {
      ui.toast('实时连接已断开，暂时无法停止', 'warn');
      return;
    }
    var payload = { type: 'cancel', sessionId: state.activeSessionId };
    var requestId = state.turnRequestId || state.pendingTurn;
    if (requestId) payload.requestId = requestId;
    if (!ws.send(payload)) {
      ui.toast('停止请求发送失败', 'error');
      return;
    }
    if (state.detail) state.detail.state = 'cancelling';
    refreshComposer();
    ui.toast('正在停止…');
  }

  async function resumeActive() {
    if (!state.activeSessionId || state.resumeBusy) return;
    var sessionId = state.activeSessionId;
    state.resumeBusy = true;
    refreshComposer();
    try {
      var result = await api.resume(sessionId);
      if (!isActive(sessionId)) return;
      if (state.detail) {
        state.detail.state = result.state || 'idle';
        state.detail.configOptions = result.configOptions || state.detail.configOptions;
        state.detail.lockState = result.lockState || state.detail.lockState;
      }
      ui.setStateChip(state.detail ? state.detail.state : 'idle');
      ui.setBanner(null);
      if (ws) ws.subscribeSession(sessionId);
      ui.toast('会话已恢复', 'success');
    } catch (err) {
      if (!isActive(sessionId)) return;
      showResumeError(err);
    } finally {
      state.resumeBusy = false;
      refreshComposer();
    }
  }

  function showResumeError(err) {
    var code = err && err.code;
    var text = util.errorText(code, err && err.message);
    if (code === 'E_NOT_RESUMABLE' || code === 'E_ACP_UNAVAILABLE') {
      ui.setBanner({
        text: text,
        kind: 'error',
        actionLabel: '新建会话',
        onAction: function () { createSession(); }
      });
    } else {
      ui.setBanner({ text: text, kind: 'error' });
    }
    ui.toast(text, 'error');
  }

  /* --------------------------------------------------------------- create */

  async function createSession(target) {
    if (state.creating) return;

    var body = target || null;
    if (!body) {
      if (!state.workspaceId) {
        openDrawer();
        toggleBrowse();
        ui.toast('先选择一个工作区或目录', 'warn');
        return;
      }
      body = { workspaceId: state.workspaceId };
    }

    state.creating = true;
    ui.setRefreshBusy(true);
    try {
      var result = await api.createSession(body);
      var detail = result && result.session;
      if (!detail || !detail.summary || !detail.summary.sessionId) {
        throw new api.ApiError(0, 'E_INTERNAL', '新建会话返回异常');
      }
      ensureWorkspaceForSession(detail);
      ui.closeDrawer();
      ui.setBrowseOpen(false);
      ui.toast('已创建新会话', 'success');
      await loadSessions(true);
      await openSession(detail.summary.sessionId, { prefetched: detail });
    } catch (err) {
      var text = util.errorText(err && err.code, err && err.message);
      if (ui.isBrowseOpen()) ui.setBrowseError(text);
      else ui.toast(text, 'error');
    } finally {
      state.creating = false;
      ui.setRefreshBusy(false);
    }
  }

  function ensureWorkspaceForSession(detail) {
    var summary = detail.summary || {};
    var workspaceId = summary.workspaceId;
    if (!workspaceId) return;
    var exists = false;
    for (var i = 0; i < state.workspaces.length; i += 1) {
      if (state.workspaces[i].id === workspaceId) { exists = true; break; }
    }
    if (!exists) {
      state.workspaces.push({
        id: workspaceId,
        path: summary.cwd || '',
        title: basename(summary.cwd || '') || '工作区',
        exists: true,
        writable: true,
        sessionCount: 1,
        updatedAt: new Date().toISOString(),
        defaultSessionId: summary.sessionId
      });
    }
    state.workspaceId = workspaceId;
    saveWorkspace(workspaceId);
    updateWorkspaceDisplay();
  }

  /* --------------------------------------------------------------- permissions */

  function permissionCountForActive() {
    var count = 0;
    state.permissionOrder.forEach(function (requestId) {
      var request = state.permissions[requestId];
      if (request && request.sessionId === state.activeSessionId) count += 1;
    });
    return count;
  }

  function hasPermissionForActive() {
    return permissionCountForActive() > 0;
  }

  function showNextPermission() {
    if (state.shownPermission || !state.activeSessionId) return;
    var id = null;
    for (var i = 0; i < state.permissionOrder.length; i += 1) {
      var candidate = state.permissions[state.permissionOrder[i]];
      if (candidate && candidate.sessionId === state.activeSessionId) {
        id = candidate.requestId;
        break;
      }
    }
    if (!id) return;
    var request = state.permissions[id];
    state.shownPermission = id;
    ui.showPermission(request, {
      queueLen: permissionCountForActive(),
      onTimeout: function () { handlePermissionTimeout(id); }
    });
    refreshComposer();
  }

  function removePermissionFromOrder(id) {
    var index = state.permissionOrder.indexOf(id);
    if (index >= 0) state.permissionOrder.splice(index, 1);
  }

  function handlePermissionTimeout(id) {
    window.setTimeout(function () {
      if (!state.permissions[id]) return;
      delete state.permissions[id];
      removePermissionFromOrder(id);
      if (state.shownPermission === id) {
        state.shownPermission = null;
        ui.hidePermission();
      }
      ui.toast('权限请求已超时', 'warn');
      showNextPermission();
      refreshComposer();
    }, 3000);
  }

  function respondPermission(decision) {
    var id = state.shownPermission;
    if (!id) return;
    var request = state.permissions[id];
    if (!request) return;
    ui.setPermissionBusy(true);

    if (ws && ws.isConnected()) {
      var sent = ws.send({ type: 'permission.respond', requestId: id, decision: decision });
      if (sent) return;
    }

    api.respondPermission(request.sessionId, id, decision).then(function () {
      resolvePermission(id, decision);
    }).catch(function (err) {
      ui.setPermissionBusy(false);
      toastApiError(err, '应答权限失败');
    });
  }

  function resolvePermission(id, decision) {
    delete state.permissions[id];
    removePermissionFromOrder(id);
    if (state.shownPermission === id) {
      state.shownPermission = null;
      ui.hidePermission();
    }
    ui.toast(decision === 'allow-once' ? '已允许一次' : '已拒绝',
      decision === 'allow-once' ? 'success' : 'info');
    showNextPermission();
    refreshComposer();
  }

  /* ---------------------------------------------------------------------- ws */

  function startWs() {
    if (!ws) {
      ws = new DSH.WsClient({
        onStatus: handleWsStatus,
        onEvent: handleWsEvent
      });
    }
    ws.start();
  }

  function handleWsStatus(status) {
    var previous = state.wsStatus;
    state.wsStatus = status;
    ui.setConnection(status);
    refreshComposer();

    if ((status === 'reconnecting' || status === 'connecting') && previous === 'connected') {
      state.reconnectBanner = true;
      if (ui.isChatOpen()) {
        ui.setBanner({ text: '实时连接断开，正在重连…', kind: 'warn' });
      }
      ui.toast('实时连接断开，正在重连…', 'warn');
    }

    if (status === 'connected' && previous !== 'connected') {
      if (state.reconnectBanner) {
        state.reconnectBanner = false;
        ui.setBanner(null);
        ui.toast('实时连接已恢复', 'success');
      }
      if (state.activeSessionId && ws && ws.sessionId !== state.activeSessionId) {
        ws.subscribeSession(state.activeSessionId);
      }
      refreshComposer();
    }
  }

  function handleWsEvent(message) {
    if (!message || typeof message.type !== 'string') return;
    switch (message.type) {
      case 'hello':
        break;
      case 'session.state':
        handleSessionState(message);
        break;
      case 'history.snapshot':
        handleSnapshot(message);
        break;
      case 'assistant.delta':
        handleAssistantDelta(message);
        break;
      case 'assistant.thought_delta':
        handleThoughtDelta(message);
        break;
      case 'tool.started':
        handleToolStarted(message);
        break;
      case 'tool.finished':
        handleToolFinished(message);
        break;
      case 'usage':
        handleUsage(message);
        break;
      case 'turn.end':
        handleTurnEnd(message);
        break;
      case 'turn.error':
        handleTurnError(message);
        break;
      case 'permission.request':
        handlePermissionRequest(message);
        break;
      case 'permission.resolved':
        handlePermissionResolved(message);
        break;
      case 'error':
        handleErrorEvent(message);
        break;
      default:
        break;
    }
  }

  function handleSessionState(message) {
    if (!isActive(message.sessionId)) return;
    if (state.detail) {
      state.detail.state = message.state;
      state.detail.lockState = message.lockState || state.detail.lockState;
    }
    ui.setStateChip(message.state);
    if (message.state === 'streaming') {
      state.streaming = true;
      state.sending = false;
    }
    if (message.state === 'idle') {
      state.streaming = false;
      state.sending = false;
      state.pendingTurn = null;
      state.turnRequestId = null;
      ui.finishStreaming();
    }
    if (message.state === 'detached') {
      state.streaming = false;
      state.sending = false;
    }
    refreshComposer();
  }

  function handleSnapshot(message) {
    if (!isActive(message.sessionId)) return;
    var stick = ui.isNearBottom(160);
    state.chatLoading = false;
    ui.setChatLoading(false);
    applyHistoryPage(message, 'replace');
    ui.setOlderButton(state.hasMore && !!state.oldestBefore, false);
    if (message.session && message.session.title) {
      state.openedTitle = message.session.title;
      ui.R.chatTitle.textContent = util.cleanTitle(message.session.title);
    }
    ui.setChatEmpty(!ui.hasMessages());
    refreshComposer();
    if (stick) ui.scrollToBottom(false);
  }

  function afterRender(stick) {
    if (stick) {
      ui.scrollToBottom(false);
      ui.setNewBelow(false);
    } else {
      ui.setNewBelow(true);
    }
  }

  function handleAssistantDelta(message) {
    if (!isActive(message.sessionId)) return;
    var stick = ui.isNearBottom(140);
    anchorOptimistic(message.seq);
    var id = message.messageId || 'assistant';
    state.assistantText[id] = (state.assistantText[id] || '') + (message.text || '');
    state.streaming = true;
    state.sending = false;
    if (state.detail) state.detail.state = 'streaming';
    ui.setStateChip('streaming');
    ui.upsertAssistant({
      key: 'live:' + id,
      seq: message.seq,
      text: state.assistantText[id],
      streaming: true
    });
    refreshComposer();
    afterRender(stick);
  }

  function handleThoughtDelta(message) {
    if (!isActive(message.sessionId)) return;
    var stick = ui.isNearBottom(140);
    anchorOptimistic(message.seq);
    var id = message.messageId || 'thought';
    state.thoughtText[id] = (state.thoughtText[id] || '') + (message.text || '');
    state.streaming = true;
    if (state.detail) state.detail.state = 'streaming';
    ui.setStateChip('streaming');
    ui.upsertReasoning({
      key: 'live-think:' + id,
      seq: message.seq,
      text: state.thoughtText[id],
      live: true
    });
    refreshComposer();
    afterRender(stick);
  }

  function handleToolStarted(message) {
    if (!isActive(message.sessionId)) return;
    var stick = ui.isNearBottom(140);
    anchorOptimistic(message.seq);
    var callId = message.toolCallId || ('tool-' + message.seq);
    state.liveTools[callId] = {
      name: message.title || 'tool',
      rawInput: message.rawInput,
      seq: message.seq
    };
    ui.upsertTool({
      key: 'live-tool:' + callId,
      seq: message.seq,
      name: state.liveTools[callId].name,
      title: message.title,
      rawInput: message.rawInput,
      status: 'running'
    });
    afterRender(stick);
  }

  function handleToolFinished(message) {
    if (!isActive(message.sessionId)) return;
    var stick = ui.isNearBottom(140);
    var callId = message.toolCallId || ('tool-' + message.seq);
    var known = state.liveTools[callId] || {};
    var key = 'live-tool:' + callId;
    var sessionId = state.activeSessionId;
    var seq = known.seq;
    var name = known.name || 'tool';
    var truncated = !!message.truncated;
    ui.upsertTool({
      key: key,
      seq: seq,
      name: name,
      title: known.name,
      rawInput: known.rawInput,
      status: message.status === 'completed' ? 'completed' : 'failed',
      output: message.output,
      truncated: truncated,
      onFetchFull: truncated
        ? function () {
          fetchFullToolCall(key, sessionId, callId, seq, name, known.rawInput);
        }
        : null
    });
    afterRender(stick);
  }

  function handleUsage(message) {
    if (!isActive(message.sessionId)) return;
    if (state.detail) state.detail.usage = { used: message.used, size: message.size };
    ui.setUsage({ used: message.used, size: message.size });
  }

  function handleTurnEnd(message) {
    if (!isActive(message.sessionId)) return;
    state.streaming = false;
    state.sending = false;
    state.pendingTurn = null;
    state.turnRequestId = null;
    if (state.detail) state.detail.state = 'idle';
    ui.setStateChip('idle');
    ui.finishStreaming();
    ui.setNewBelow(false);
    if (state.reconnectBanner) {
      state.reconnectBanner = false;
      ui.setBanner(null);
    }

    var summary = state.detail && state.detail.summary;
    if (summary) {
      summary.updatedAt = Date.now();
      summary.messageCount = (summary.messageCount || 0) + 2;
      if (state.lastSentText) {
        summary.lastMessagePreview = util.truncate(state.lastSentText, 90);
        if (!summary.title || summary.title === '新会话') {
          summary.title = firstLine(state.lastSentText);
          ui.R.chatTitle.textContent = util.cleanTitle(summary.title);
        }
      }
      updateSessionInList(summary.sessionId || state.activeSessionId, {
        title: summary.title,
        updatedAt: summary.updatedAt,
        messageCount: summary.messageCount,
        lastMessagePreview: summary.lastMessagePreview
      });
    }
    if (message.stopReason === 'cancelled') ui.toast('已停止生成', 'warn');
    state.lastSentText = '';
    state.pendingOptimisticKey = null;
    refreshComposer();
  }

  function handleTurnError(message) {
    if (!isActive(message.sessionId)) return;
    state.streaming = false;
    state.sending = false;
    state.pendingTurn = null;
    state.turnRequestId = null;
    if (state.detail) state.detail.state = 'error';
    ui.setStateChip('error');
    ui.finishStreaming();
    var text = util.errorText(message.code, message.message) ||
      (message.message || '回复失败');
    ui.setBanner({
      text: (message.code ? message.code + ' · ' : '') + (message.message || text),
      kind: 'error'
    });
    ui.toast(text, 'error');
    state.pendingOptimisticKey = null;
    state.lastSentText = '';
    refreshComposer();
  }

  function handlePermissionRequest(message) {
    if (!message.requestId) return;
    state.permissions[message.requestId] = message;
    if (state.permissionOrder.indexOf(message.requestId) < 0) {
      state.permissionOrder.push(message.requestId);
    }
    if (isActive(message.sessionId)) {
      showNextPermission();
      refreshComposer();
    } else {
      ui.toast('另一个会话请求工具权限：' + (message.toolName || '工具'), 'warn');
    }
  }

  function handlePermissionResolved(message) {
    var id = message.requestId;
    if (!id) return;
    delete state.permissions[id];
    removePermissionFromOrder(id);
    if (state.shownPermission === id) {
      state.shownPermission = null;
      ui.hidePermission();
    }
    var outcome = message.outcome;
    var label = outcome === 'allow-once' ? '已允许一次'
      : outcome === 'reject-once' ? '已拒绝'
        : outcome === 'timeout' ? '权限请求超时，已自动拒绝'
          : '权限请求已取消';
    ui.toast(label, outcome === 'allow-once' ? 'success' : outcome === 'timeout' ? 'warn' : 'info');
    showNextPermission();
    refreshComposer();
  }

  function handleErrorEvent(message) {
    if (message.code === 'E_PROMPT_IN_FLIGHT') {
      failPrompt(message.code, message.message);
      return;
    }
    ui.toast(util.errorText(message.code, message.message), 'error');
  }

  /* ------------------------------------------------------------------ viewport */

  function syncViewport() {
    var height = window.visualViewport ? window.visualViewport.height : window.innerHeight;
    if (!height) return;
    document.documentElement.style.setProperty('--app-height', height + 'px');
  }

  function bindViewport() {
    syncViewport();
    window.addEventListener('resize', syncViewport);
    window.addEventListener('orientationchange', function () {
      window.setTimeout(syncViewport, 120);
    });
    if (window.visualViewport) {
      window.visualViewport.addEventListener('resize', syncViewport);
      window.visualViewport.addEventListener('scroll', syncViewport);
    }
  }

  /* -------------------------------------------------------------------- bind */

  function bindEvents() {
    ui.R.btnBootRetry.addEventListener('click', function () { bootstrap(); });
    ui.R.btnDrawer.addEventListener('click', openDrawer);
    ui.R.btnRefresh.addEventListener('click', refreshAll);
    ui.R.btnNew.addEventListener('click', function () { createSession(); });
    ui.R.btnEmptyNew.addEventListener('click', function () { createSession(); });
    ui.R.btnDrawerNew.addEventListener('click', function () { createSession(); });
    ui.R.btnBrowseToggle.addEventListener('click', toggleBrowse);
    ui.R.btnBack.addEventListener('click', backToHome);
    ui.R.btnOlder.addEventListener('click', loadOlder);
    ui.R.btnLoadMore.addEventListener('click', loadMore);
    ui.R.btnNewBelow.addEventListener('click', function () {
      ui.setNewBelow(false);
      ui.scrollToBottom(true);
    });

    ui.R.btnSend.addEventListener('click', sendMessage);
    ui.R.btnStop.addEventListener('click', cancelTurn);
    ui.R.btnResume.addEventListener('click', resumeActive);
    ui.R.composerInput.addEventListener('keydown', function (event) {
      if (event.key !== 'Enter' || event.shiftKey || event.isComposing) return;
      if (event.ctrlKey || event.metaKey || event.altKey) return;
      event.preventDefault();
      sendMessage();
    });

    ui.R.messageScroll.addEventListener('scroll', function () {
      if (ui.isNearBottom(48)) ui.setNewBelow(false);
    }, { passive: true });

    ui.bindPermission(respondPermission);

    window.addEventListener('online', function () {
      if (ws) ws.kick();
    });
    window.addEventListener('offline', function () {
      ui.toast('网络已断开', 'warn');
    });
    document.addEventListener('visibilitychange', function () {
      if (document.visibilityState === 'visible' && ws && state.wsStatus !== 'connected') {
        ws.kick();
      }
    });
    window.addEventListener('beforeunload', function () {
      if (ws) ws.close();
    });
  }

  function init() {
    ui.init();
    bindEvents();
    bindViewport();
    bootstrap();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
