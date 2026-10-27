/* dsh-pocket · DOM rendering.
   Server text never reaches innerHTML directly: the tiny Markdown subset is
   parsed only after HTML escaping, code blocks are built with textContent. */
(function () {
  'use strict';

  var DSH = window.DSH;
  var util = DSH.util;
  var el = util.el;
  var icon = util.icon;
  var escapeHtml = util.escapeHtml;

  var R = {};
  var nodeIndex = {};
  var currentBrowse = null;

  var permissionTimer = null;
  var permissionDeadline = 0;
  var permissionTotal = 0;
  var permissionOnTimeout = null;
  var permissionDecision = null;

  function $(id) { return document.getElementById(id); }

  function cacheRefs() {
    [
      'screenBoot', 'bootSpinner', 'bootText', 'bootError', 'bootErrorText', 'btnBootRetry',
      'screenHome', 'btnDrawer', 'wsName', 'wsPath', 'connDot', 'btnRefresh', 'btnNew',
      'sessionCount', 'listStatus', 'sessionList', 'listEmpty', 'btnEmptyNew', 'btnLoadMore',
      'modeLabel',
      'screenChat', 'btnBack', 'chatTitle', 'stateChip', 'usageText', 'connDotChat',
      'chatBanner', 'bannerText', 'bannerAction', 'messageScroll', 'btnOlder', 'messageList',
      'chatLoading', 'chatEmpty', 'btnNewBelow', 'composer', 'composerHint', 'composerInput',
      'btnSend', 'btnStop', 'btnResume',
      'drawerBackdrop', 'drawer', 'btnDrawerClose', 'workspaceList', 'btnBrowseToggle',
      'browsePanel', 'btnBrowseUp', 'browsePath', 'browseEntries', 'browseError',
      'btnBrowseUse', 'drawerSessions', 'drawerSessionCount', 'btnDrawerNew',
      'permissionOverlay', 'permTool', 'permNote', 'permArgs', 'permQueue', 'permTimeText',
      'btnPermAllow', 'btnPermReject', 'toastHost'
    ].forEach(function (id) { R[id] = $(id); });
  }

  /* ------------------------------------------------------------------ screens */

  function showBoot() {
    R.screenBoot.hidden = false;
    R.screenHome.hidden = true;
    R.screenChat.hidden = true;
    R.bootError.hidden = true;
    R.bootSpinner.hidden = false;
    R.bootText.textContent = '正在连接 dsh-pocket…';
  }

  function setBootText(text) {
    R.bootText.textContent = text;
    R.bootSpinner.hidden = false;
    R.bootError.hidden = true;
  }

  function showBootError(text) {
    R.bootSpinner.hidden = true;
    R.bootText.textContent = '无法连接';
    R.bootErrorText.textContent = text;
    R.bootError.hidden = false;
  }

  function showHome() {
    R.screenBoot.hidden = true;
    R.screenHome.hidden = false;
    R.screenChat.hidden = true;
    closeDrawer();
  }

  function showChat() {
    R.screenBoot.hidden = true;
    R.screenHome.hidden = true;
    R.screenChat.hidden = false;
    closeDrawer();
  }

  function isChatOpen() { return !R.screenChat.hidden; }

  function setWorkspace(workspace) {
    var title = workspace ? (workspace.title || workspace.path || '工作区') : '选择工作区';
    R.wsName.textContent = title;
    R.wsPath.textContent = workspace && workspace.path ? workspace.path : '';
    R.wsPath.title = workspace && workspace.path ? workspace.path : '';
    document.title = workspace ? title + ' · dsh pocket' : 'dsh pocket';
  }

  function setModeLabel(mode) {
    R.modeLabel.textContent = mode === 'mock' ? 'mock 模式' : (mode === 'acp' ? 'ACP 模式' : 'dsh-pocket');
  }

  function setConnection(status) {
    var labels = {
      connected: '实时连接正常',
      connecting: '正在连接',
      reconnecting: '正在重连',
      offline: '实时连接断开'
    };
    [R.connDot, R.connDotChat].forEach(function (dot) {
      if (!dot) return;
      dot.dataset.status = status;
      dot.title = labels[status] || status;
      dot.setAttribute('aria-label', labels[status] || status);
    });
  }

  /* --------------------------------------------------------------- session list */

  function setSessionCount(count) {
    R.sessionCount.textContent = count > 0 ? count + ' 个' : '';
  }

  function renderSessions(sessions, handlers) {
    R.sessionList.textContent = '';
    var list = Array.isArray(sessions) ? sessions : [];
    list.forEach(function (session) {
      var card = el('button', 'session-card');
      card.type = 'button';
      card.dataset.sessionId = session.sessionId;

      var iconBox = el('span', 'session-icon');
      iconBox.appendChild(icon('chat', 'sm'));
      card.appendChild(iconBox);

      var body = el('span', 'session-body');
      body.appendChild(el('span', 'session-title', util.cleanTitle(session.title)));
      if (session.lastMessagePreview) {
        body.appendChild(el('span', 'session-preview', session.lastMessagePreview));
      }
      var meta = el('span', 'session-meta');
      meta.appendChild(el('span', '', util.relativeTime(session.updatedAt || session.createdAt)));
      if (typeof session.messageCount === 'number' && session.messageCount > 0) {
        meta.appendChild(el('span', 'dot-sep', '·'));
        meta.appendChild(el('span', '', session.messageCount + ' 条消息'));
      }
      body.appendChild(meta);
      card.appendChild(body);

      var chev = el('span', 'chevron');
      chev.appendChild(icon('chevron-right', 'sm'));
      card.appendChild(chev);

      card.addEventListener('click', function () {
        if (handlers && typeof handlers.onOpen === 'function') handlers.onOpen(session);
      });
      R.sessionList.appendChild(card);
    });
    setSessionCount(list.length);
  }

  function setListStatus(text, options) {
    var opts = options || {};
    R.listStatus.textContent = '';
    R.listStatus.dataset.kind = opts.kind || '';
    if (!text) {
      R.listStatus.hidden = true;
      return;
    }
    R.listStatus.hidden = false;
    R.listStatus.appendChild(el('span', '', text));
    if (typeof opts.onRetry === 'function') {
      var retry = el('button', 'list-retry', '重试');
      retry.type = 'button';
      retry.addEventListener('click', opts.onRetry);
      R.listStatus.appendChild(retry);
    }
  }

  function setListEmpty(visible) { R.listEmpty.hidden = !visible; }

  function setListMore(visible, loading) {
    R.btnLoadMore.hidden = !visible;
    R.btnLoadMore.disabled = !!loading;
    R.btnLoadMore.textContent = loading ? '加载中…' : '加载更多';
  }

  function setRefreshBusy(busy) { R.btnRefresh.disabled = !!busy; }

  /* ------------------------------------------------------------------- drawer */

  function openDrawer() {
    R.drawerBackdrop.hidden = false;
    closeBrowseError();
  }

  function closeDrawer() {
    R.drawerBackdrop.hidden = true;
  }

  function isDrawerOpen() { return !R.drawerBackdrop.hidden; }

  function renderWorkspaces(workspaces, currentId, onPick) {
    R.workspaceList.textContent = '';
    var list = Array.isArray(workspaces) ? workspaces : [];
    if (!list.length) {
      R.workspaceList.appendChild(el('div', 'drawer-empty', '还没有最近工作区。可以浏览目录新建。'));
      return;
    }
    list.forEach(function (workspace) {
      var row = el('button', 'workspace-row');
      row.type = 'button';
      row.dataset.active = workspace.id === currentId ? 'true' : 'false';
      row.dataset.exists = workspace.exists ? 'true' : 'false';
      row.disabled = !workspace.exists;

      var avatar = el('span', 'ws-avatar', String(workspace.title || workspace.path || '?').slice(0, 2).toUpperCase());
      row.appendChild(avatar);

      var info = el('span', 'ws-info');
      info.appendChild(el('span', 'ws-title', workspace.title || workspace.path || '工作区'));
      info.appendChild(el('span', 'ws-sub', workspace.path || ''));
      row.appendChild(info);

      if (!workspace.exists) {
        row.appendChild(el('span', 'ws-state missing', '不存在'));
      } else if (workspace.id === currentId) {
        var check = el('span', 'ws-check');
        check.appendChild(icon('check', 'sm'));
        row.appendChild(check);
      } else if (typeof workspace.sessionCount === 'number' && workspace.sessionCount > 0) {
        row.appendChild(el('span', 'ws-state', workspace.sessionCount + ' 个'));
      }

      row.addEventListener('click', function () {
        if (!workspace.exists || typeof onPick !== 'function') return;
        onPick(workspace);
      });
      R.workspaceList.appendChild(row);
    });
  }

  function renderDrawerSessions(sessions, onOpen) {
    R.drawerSessions.textContent = '';
    var list = Array.isArray(sessions) ? sessions : [];
    R.drawerSessionCount.textContent = list.length ? list.length + ' 个' : '';
    if (!list.length) {
      R.drawerSessions.appendChild(el('div', 'drawer-empty', '当前工作区还没有手机会话。'));
      return;
    }
    list.slice(0, 12).forEach(function (session) {
      var row = el('button', 'drawer-session');
      row.type = 'button';
      row.appendChild(el('span', 'drawer-session-title', util.cleanTitle(session.title)));
      row.appendChild(el('span', 'drawer-session-time',
        util.relativeTime(session.updatedAt || session.createdAt)));
      row.addEventListener('click', function () {
        if (typeof onOpen === 'function') onOpen(session);
      });
      R.drawerSessions.appendChild(row);
    });
  }

  function setBrowseOpen(open) {
    R.browsePanel.hidden = !open;
    R.btnBrowseToggle.textContent = open ? '收起浏览' : '浏览目录';
    if (!open) closeBrowseError();
  }

  function isBrowseOpen() { return !R.browsePanel.hidden; }

  function renderBrowse(data, handlers) {
    currentBrowse = data;
    R.browsePath.textContent = data.path || '—';
    R.browsePath.title = data.path || '';
    R.btnBrowseUp.disabled = !data.parent;
    R.btnBrowseUp.onclick = function () {
      if (data.parent && handlers && typeof handlers.onEnter === 'function') handlers.onEnter(data.parent);
    };

    R.browseEntries.textContent = '';
    var entries = Array.isArray(data.entries) ? data.entries : [];
    if (!entries.length) {
      R.browseEntries.appendChild(el('div', 'browse-empty', '没有子目录'));
    } else {
      entries.forEach(function (entry) {
        var row = el('button', 'browse-row');
        row.type = 'button';
        row.appendChild(icon('folder', 'sm'));
        row.appendChild(el('span', '', entry.name));
        row.addEventListener('click', function () {
          if (handlers && typeof handlers.onEnter === 'function') handlers.onEnter(entry.path);
        });
        R.browseEntries.appendChild(row);
      });
    }
    R.btnBrowseUse.onclick = function () {
      if (handlers && typeof handlers.onUse === 'function') handlers.onUse(data.path);
    };
  }

  function setBrowseError(text) {
    R.browseError.textContent = text || '';
    R.browseError.hidden = !text;
  }

  function closeBrowseError() { setBrowseError(''); }

  function getBrowsePath() { return currentBrowse ? currentBrowse.path : ''; }

  /* --------------------------------------------------------------------- chat */

  var STATE_CHIP = {
    idle: '就绪',
    streaming: '回复中',
    error: '出错'
  };

  function chipState(state) {
    if (state === 'error') return 'error';
    if (state === 'streaming' || state === 'awaiting_permission' || state === 'cancelling') return 'streaming';
    return 'idle';
  }

  function setChatHeader(opts) {
    var data = opts || {};
    R.chatTitle.textContent = util.cleanTitle(data.title || '会话');
    R.chatTitle.title = data.title || '';
    setStateChip(data.state || 'idle');
    setUsage(data.usage || null);
  }

  function setStateChip(state) {
    var key = chipState(state);
    R.stateChip.dataset.state = key;
    R.stateChip.textContent = STATE_CHIP[key];
  }

  function setUsage(usage) {
    R.usageText.textContent = util.usageText(usage || null);
  }

  function setBanner(banner) {
    if (!banner) {
      R.chatBanner.hidden = true;
      R.bannerAction.hidden = true;
      R.bannerAction.onclick = null;
      return;
    }
    R.chatBanner.dataset.kind = banner.kind || 'warn';
    R.bannerText.textContent = banner.text || '';
    if (banner.actionLabel) {
      R.bannerAction.hidden = false;
      R.bannerAction.textContent = banner.actionLabel;
      R.bannerAction.onclick = banner.onAction || null;
    } else {
      R.bannerAction.hidden = true;
      R.bannerAction.onclick = null;
    }
    R.chatBanner.hidden = false;
  }

  function clearMessages() {
    R.messageList.textContent = '';
    nodeIndex = {};
    setChatEmpty(false);
    setNewBelow(false);
    setOlderButton(false, false);
  }

  function setChatLoading(visible) { R.chatLoading.hidden = !visible; }

  function setChatEmpty(visible) { R.chatEmpty.hidden = !visible; }

  function setOlderButton(visible, loading) {
    R.btnOlder.hidden = !visible;
    R.btnOlder.disabled = !!loading;
    R.btnOlder.dataset.loading = loading ? 'true' : 'false';
    R.btnOlder.textContent = loading ? '加载中…' : '加载更早消息';
  }

  function setNewBelow(visible) { R.btnNewBelow.hidden = !visible; }

  function seqAttr(seq) {
    return (typeof seq === 'number' && isFinite(seq)) ? String(seq) : 'inf';
  }

  function insertOrdered(node, seq, rank) {
    var targetSeq = seqAttr(seq);
    var targetNum = targetSeq === 'inf' ? Number.MAX_SAFE_INTEGER : Number(targetSeq);
    var targetRank = rank || 0;
    var children = R.messageList.children;
    for (var i = 0; i < children.length; i += 1) {
      var child = children[i];
      var childSeqText = child.dataset.seq;
      if (childSeqText === undefined) continue;
      var childNum = childSeqText === 'inf' ? Number.MAX_SAFE_INTEGER : Number(childSeqText);
      var childRank = Number(child.dataset.rank || '0');
      if (childNum > targetNum || (childNum === targetNum && childRank > targetRank)) {
        R.messageList.insertBefore(node, child);
        return;
      }
    }
    R.messageList.appendChild(node);
  }

  function hasMessages() { return R.messageList.children.length > 0; }

  function upsertUser(opts) {
    var node = nodeIndex[opts.key];
    if (!node) {
      node = el('div', 'msg-user');
      node.dataset.key = opts.key;
      node.dataset.seq = seqAttr(opts.seq);
      node.dataset.rank = '0';
      nodeIndex[opts.key] = node;
      insertOrdered(node, opts.seq, 0);
    }
    node.textContent = opts.text || '';
    node.dataset.pending = opts.pending ? 'true' : 'false';
    setChatEmpty(false);
    return node;
  }


  function anchorUser(key, seq) {
    var node = nodeIndex[key];
    if (!node || node.dataset.seq !== 'inf') return;
    if (typeof seq !== 'number' || !isFinite(seq)) return;
    node.dataset.seq = seqAttr(seq);
    if (node.parentNode) node.parentNode.removeChild(node);
    insertOrdered(node, seq, 0);
  }

  function upsertAssistant(opts) {
    var node = nodeIndex[opts.key];
    if (!node) {
      node = el('div', 'msg-assistant');
      node.dataset.key = opts.key;
      node.dataset.seq = seqAttr(opts.seq);
      node.dataset.rank = '2';
      var avatar = el('div', 'avatar', 'dsh');
      var body = el('div', 'msg-body');
      var bubble = el('div', 'assistant-bubble');
      var md = el('div', 'md');
      bubble.appendChild(md);
      body.appendChild(bubble);
      node.appendChild(avatar);
      node.appendChild(body);
      node._refs = { bubble: bubble, md: md };
      nodeIndex[opts.key] = node;
      insertOrdered(node, opts.seq, 2);
    }
    renderMarkdown(node._refs.md, opts.text || '');
    node._refs.bubble.hidden = !opts.text && !opts.streaming;
    node._refs.bubble.classList.toggle('streaming', !!opts.streaming);
    setChatEmpty(false);
    return node;
  }

  function reasoningUserToggled(node) {
    node.addEventListener('toggle', function () {
      if (node._auto) return;
      node.dataset.userToggled = '1';
    });
  }

  function setReasoningOpen(node, open) {
    if (node.open === open) return;
    node._auto = true;
    node.open = open;
    window.setTimeout(function () { node._auto = false; }, 0);
  }

  function upsertReasoning(opts) {
    var live = !!opts.live;
    var node = nodeIndex[opts.key];
    if (!node) {
      node = el('details', 'reasoning');
      node.dataset.key = opts.key;
      node.dataset.seq = seqAttr(opts.seq);
      node.dataset.rank = '1';
      var summary = el('summary', '', '思考过程');
      var text = el('div', 'reasoning-text');
      node.appendChild(summary);
      node.appendChild(text);
      node._refs = { text: text };
      reasoningUserToggled(node);
      nodeIndex[opts.key] = node;
      insertOrdered(node, opts.seq, 1);
      setReasoningOpen(node, live);
    }
    node.dataset.live = live ? 'true' : 'false';
    node._refs.text.textContent = opts.text || '';
    if (live) {
      setReasoningOpen(node, true);
      node._refs.text.scrollTop = node._refs.text.scrollHeight;
    } else if (node.dataset.userToggled !== '1') {
      setReasoningOpen(node, false);
    }
    setChatEmpty(false);
    return node;
  }

  function statusLabel(status) {
    if (status === 'completed') return '完成';
    if (status === 'failed') return '失败';
    return '运行中';
  }

  function upsertTool(opts) {
    var node = nodeIndex[opts.key];
    if (!node) {
      node = el('div', 'msg-tool');
      node.dataset.key = opts.key;
      node.dataset.seq = seqAttr(opts.seq);
      node.dataset.rank = '3';
      node.dataset.open = 'false';

      var head = el('button', 'tool-head');
      head.type = 'button';
      var iconBox = el('span', 'tool-icon');
      iconBox.appendChild(icon('terminal', 'sm'));
      var main = el('span', 'tool-main');
      var name = el('span', 'tool-name', opts.name || 'tool');
      var desc = el('span', 'tool-desc', '');
      main.appendChild(name);
      main.appendChild(desc);
      var dot = el('span', 'tool-dot');
      var chev = el('span', 'tool-chev');
      chev.appendChild(icon('chevron-down', 'sm'));
      head.appendChild(iconBox);
      head.appendChild(main);
      head.appendChild(dot);
      head.appendChild(chev);
      node.appendChild(head);

      var body = el('div', 'tool-body');
      body.hidden = true;
      body.appendChild(el('div', 'tool-label', '参数'));
      var args = el('pre', 'tool-pre');
      body.appendChild(args);
      body.appendChild(el('div', 'tool-label', '输出'));
      var output = el('pre', 'tool-pre');
      body.appendChild(output);
      var actions = el('div', 'tool-actions');
      var copyBtn = el('button', 'tool-action', '复制输出');
      copyBtn.type = 'button';
      var fullBtn = el('button', 'tool-action', '完整输出');
      fullBtn.type = 'button';
      fullBtn.hidden = true;
      actions.appendChild(copyBtn);
      actions.appendChild(fullBtn);
      body.appendChild(actions);
      node.appendChild(body);

      node._refs = {
        head: head, name: name, desc: desc, dot: dot, body: body,
        args: args, output: output, copyBtn: copyBtn, fullBtn: fullBtn,
        onFetchFull: null, outputText: ''
      };

      head.addEventListener('click', function () {
        var open = node.dataset.open === 'true';
        node.dataset.open = open ? 'false' : 'true';
        node.dataset.userToggled = '1';
        body.hidden = open;
      });

      copyBtn.addEventListener('click', async function () {
        var ok = await util.copyText(node._refs.outputText);
        copyBtn.textContent = ok ? '已复制' : '复制失败';
        copyBtn.dataset.copied = ok ? 'true' : 'false';
        window.setTimeout(function () { copyBtn.textContent = '复制输出'; copyBtn.dataset.copied = 'false'; }, 1500);
      });

      fullBtn.addEventListener('click', function () {
        if (node._refs.onFetchFull) node._refs.onFetchFull();
      });

      nodeIndex[opts.key] = node;
      insertOrdered(node, opts.seq, 3);
    }

    var refs = node._refs;
    var status = opts.status || 'running';
    refs.name.textContent = opts.name || 'tool';
    refs.desc.textContent = util.oneLineArgs(opts.rawInput) || opts.title || '';
    refs.dot.dataset.status = status;
    refs.dot.title = statusLabel(status);
    refs.args.textContent = util.stringifyArgs(opts.rawInput) || '（无参数）';

    var outputText = typeof opts.output === 'string'
      ? opts.output
      : (Array.isArray(opts.output) ? opts.output.map(function (part) {
        return part && typeof part.text === 'string' ? part.text : '';
      }).join('') : '');
    if (!outputText && status === 'running') outputText = '等待工具返回…';
    if (!outputText) outputText = '（无输出）';
    refs.output.textContent = outputText;
    refs.outputText = outputText;

    refs.onFetchFull = typeof opts.onFetchFull === 'function' ? opts.onFetchFull : null;
    refs.fullBtn.hidden = !opts.truncated || !refs.onFetchFull;

    if (status === 'running' && node.dataset.userToggled !== '1') {
      node.dataset.open = 'true';
      refs.body.hidden = false;
    }
    setChatEmpty(false);
    return node;
  }

  function removeMessage(key) {
    var node = nodeIndex[key];
    if (!node) return;
    if (node.parentNode) node.parentNode.removeChild(node);
    delete nodeIndex[key];
  }

  function appendNote(text) {
    R.messageList.appendChild(el('div', 'msg-note', text));
    setChatEmpty(false);
  }

  function finishStreaming() {
    Object.keys(nodeIndex).forEach(function (key) {
      var node = nodeIndex[key];
      if (!node) return;
      if (node.classList && node.classList.contains('assistant-bubble')) {
        node.classList.remove('streaming');
      }
      if (node._refs && node._refs.bubble) node._refs.bubble.classList.remove('streaming');
      if (node.classList && node.classList.contains('reasoning')) {
        node.dataset.live = 'false';
        if (node.dataset.userToggled !== '1') setReasoningOpen(node, false);
      }
    });
  }

  function scrollToBottom(smooth) {
    var node = R.messageScroll;
    window.requestAnimationFrame(function () {
      try {
        node.scrollTo({ top: node.scrollHeight, behavior: smooth ? 'smooth' : 'auto' });
      } catch (err) {
        node.scrollTop = node.scrollHeight;
      }
    });
  }

  function isNearBottom(threshold) { return util.isNearBottom(R.messageScroll, threshold); }

  /* ----------------------------------------------------------------- markdown */

  function inlineHtml(text) {
    var safe = escapeHtml(text);
    var codes = [];
    safe = safe.replace(/`([^`\n]+)`/g, function (match, code) {
      codes.push(code);
      return '\u0000' + (codes.length - 1) + '\u0000';
    });
    safe = safe.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
    safe = safe.replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g, function (match, label, url) {
      return '<a href="' + url + '" target="_blank" rel="noopener noreferrer">' + label + '</a>';
    });
    safe = safe.replace(/\u0000(\d+)\u0000/g, function (match, index) {
      return '<code>' + codes[Number(index)] + '</code>';
    });
    return safe.replace(/\n/g, '<br>');
  }

  function makeCodeBlock(code, lang) {
    var block = el('div', 'code-block');
    var head = el('div', 'code-head');
    head.appendChild(el('span', '', lang || '代码'));
    var copy = el('button', 'copy-btn');
    copy.type = 'button';
    copy.appendChild(icon('copy', 'sm'));
    copy.appendChild(el('span', '', '复制'));
    copy.addEventListener('click', async function () {
      var ok = await util.copyText(code);
      copy.dataset.copied = ok ? 'true' : 'false';
      copy.lastChild.textContent = ok ? '已复制' : '复制失败';
      window.setTimeout(function () {
        copy.dataset.copied = 'false';
        copy.lastChild.textContent = '复制';
      }, 1500);
    });
    head.appendChild(copy);
    block.appendChild(head);
    var pre = el('pre');
    var codeEl = el('code', '', code);
    pre.appendChild(codeEl);
    block.appendChild(pre);
    return block;
  }

  function renderTextBlock(container, block) {
    var lines = block.split('\n');
    var first = lines[0] || '';
    var heading = first.match(/^(#{1,4})\s+(.*)$/);
    if (heading) {
      var level = heading[1].length;
      var h = el('h' + Math.min(4, level));
      h.innerHTML = inlineHtml(heading[2]);
      container.appendChild(h);
      return;
    }
    var isList = lines.every(function (line) { return !line.trim() || /^\s*[-*]\s+/.test(line); });
    if (isList && lines.some(function (line) { return /^\s*[-*]\s+/.test(line); })) {
      var ul = el('ul');
      lines.forEach(function (line) {
        if (!line.trim()) return;
        var li = el('li');
        li.innerHTML = inlineHtml(line.replace(/^\s*[-*]\s+/, ''));
        ul.appendChild(li);
      });
      container.appendChild(ul);
      return;
    }
    var isOrdered = lines.every(function (line) { return !line.trim() || /^\s*\d+[.)]\s+/.test(line); });
    if (isOrdered && lines.some(function (line) { return /^\s*\d+[.)]\s+/.test(line); })) {
      var ol = el('ol');
      lines.forEach(function (line) {
        if (!line.trim()) return;
        var li = el('li');
        li.innerHTML = inlineHtml(line.replace(/^\s*\d+[.)]\s+/, ''));
        ol.appendChild(li);
      });
      container.appendChild(ol);
      return;
    }
    if (/^\s*>\s?/.test(block)) {
      var quote = el('blockquote');
      quote.innerHTML = inlineHtml(block.replace(/^\s*>\s?/gm, ''));
      container.appendChild(quote);
      return;
    }
    var p = el('p');
    p.innerHTML = inlineHtml(block);
    container.appendChild(p);
  }

  function renderMarkdown(container, text) {
    container.textContent = '';
    var source = String(text || '');
    var fence = /```([^\n`]*)\n?([\s\S]*?)(?:```|$)/g;
    var lastIndex = 0;
    var match;
    while ((match = fence.exec(source)) !== null) {
      if (match.index > lastIndex) {
        renderTextBlocks(container, source.slice(lastIndex, match.index));
      }
      var code = match[2].replace(/\n$/, '');
      container.appendChild(makeCodeBlock(code, (match[1] || '').trim()));
      lastIndex = fence.lastIndex;
      if (match[0].indexOf('```') === -1) break;
    }
    if (lastIndex < source.length) {
      renderTextBlocks(container, source.slice(lastIndex));
    }
  }

  function renderTextBlocks(container, source) {
    source.split(/\n{2,}/).forEach(function (block) {
      var trimmed = block.replace(/\n+$/, '');
      if (!trimmed.trim()) return;
      renderTextBlock(container, trimmed);
    });
  }

  /* ----------------------------------------------------------------- composer */

  function autoGrow() {
    var input = R.composerInput;
    input.style.height = 'auto';
    input.style.height = Math.min(118, input.scrollHeight) + 'px';
  }

  function getComposerText() { return R.composerInput.value; }

  function setComposerText(text) {
    R.composerInput.value = text || '';
    autoGrow();
  }

  function clearComposer() { setComposerText(''); }

  function focusComposer() {
    try { R.composerInput.focus({ preventScroll: true }); } catch (err) { R.composerInput.focus(); }
  }

  function setComposerHint(text, tone) {
    R.composerHint.textContent = text || '';
    R.composerHint.dataset.tone = tone || '';
  }

  function setComposerMode(mode, options) {
    var opts = options || {};
    var input = R.composerInput;
    R.composer.dataset.mode = mode;

    input.disabled = mode === 'detached' || mode === 'awaiting_permission';
    R.btnSend.hidden = mode === 'streaming' || mode === 'cancelling' ||
      mode === 'detached' || mode === 'awaiting_permission';
    R.btnSend.disabled = mode === 'offline' || mode === 'sending';
    R.btnStop.hidden = mode !== 'streaming' && mode !== 'cancelling';
    R.btnStop.disabled = mode === 'cancelling';
    R.btnResume.hidden = mode !== 'detached';
    R.btnResume.disabled = !!opts.resumeBusy;
    R.btnResume.textContent = opts.resumeBusy ? '恢复中…' : '继续';

    if (mode === 'idle') {
      input.placeholder = '给 DSH 发消息…';
      setComposerHint('Enter 发送 · Shift+Enter 换行');
    } else if (mode === 'sending') {
      input.placeholder = '给 DSH 发消息…';
      setComposerHint('正在发送…');
    } else if (mode === 'streaming') {
      input.placeholder = '可以先输入下一条…';
      setComposerHint('DSH 正在回复，可随时停止');
    } else if (mode === 'cancelling') {
      input.placeholder = '正在停止…';
      setComposerHint('正在停止本次回复', 'warn');
    } else if (mode === 'detached') {
      input.placeholder = '会话未激活';
      setComposerHint(opts.reason || '点「继续」恢复会话', 'warn');
    } else if (mode === 'offline') {
      input.placeholder = '实时连接已断开…';
      setComposerHint('正在重连，发送按钮暂不可用', 'warn');
    } else if (mode === 'awaiting_permission') {
      input.placeholder = '等待工具授权';
      setComposerHint('请先处理工具权限请求', 'warn');
    } else if (mode === 'error') {
      input.placeholder = '给 DSH 发消息…';
      setComposerHint('上一步出错，可重试', 'error');
    }
  }

  /* ------------------------------------------------------------------ permission */

  function bindPermission(handler) {
    permissionDecision = handler;
    R.btnPermAllow.addEventListener('click', function () {
      if (permissionDecision) permissionDecision('allow-once');
    });
    R.btnPermReject.addEventListener('click', function () {
      if (permissionDecision) permissionDecision('reject-once');
    });
  }

  function stopPermissionTimer() {
    if (permissionTimer) {
      clearInterval(permissionTimer);
      permissionTimer = null;
    }
  }

  function tickPermission() {
    var remain = Math.max(0, permissionDeadline - Date.now());
    var seconds = Math.ceil(remain / 1000);
    var pct = permissionTotal > 0 ? Math.max(0, Math.min(100, remain / permissionTotal * 100)) : 0;
    R.permTimeText.textContent = remain > 0 ? '剩余 ' + seconds + ' 秒' : '已超时，等待服务端处理…';
    R.permTimeText.dataset.urgent = remain <= 20000 ? 'true' : 'false';
    if (remain <= 0) {
      stopPermissionTimer();
      R.btnPermAllow.disabled = true;
      R.btnPermReject.disabled = true;
      if (permissionOnTimeout) permissionOnTimeout();
    }
  }

  function startPermissionCountdown(totalMs, onTimeout) {
    stopPermissionTimer();
    permissionTotal = typeof totalMs === 'number' && totalMs > 0 ? totalMs : 120000;
    permissionDeadline = Date.now() + permissionTotal;
    permissionOnTimeout = onTimeout || null;
    tickPermission();
    permissionTimer = setInterval(tickPermission, 250);
  }

  function showPermission(request, options) {
    var opts = options || {};
    stopPermissionTimer();
    permissionTotal = 0;
    R.permissionOverlay.hidden = false;
    R.permTool.textContent = request.toolName || '工具';
    R.permArgs.textContent = request.argumentsPreview || '（没有参数预览）';

    var allow = null;
    var reject = null;
    (Array.isArray(request.options) ? request.options : []).forEach(function (option) {
      if (!option) return;
      if (option.kind === 'allow_once' || option.optionId === 'allow-once') allow = option;
      if (option.kind === 'reject_once' || option.optionId === 'reject-once') reject = option;
    });
    R.btnPermAllow.textContent = '允许一次';
    R.btnPermReject.textContent = '拒绝';
    R.btnPermAllow.disabled = false;
    R.btnPermReject.disabled = false;

    var queueLen = opts.queueLen || 1;
    R.permQueue.hidden = queueLen <= 1;
    R.permQueue.textContent = queueLen > 1 ? '还有 ' + (queueLen - 1) + ' 个待处理' : '';

    startPermissionCountdown(request.timeoutMs, opts.onTimeout);
  }

  function hidePermission() {
    stopPermissionTimer();
    permissionOnTimeout = null;
    R.permissionOverlay.hidden = true;
  }

  function setPermissionBusy(busy) {
    R.btnPermAllow.disabled = !!busy;
    R.btnPermReject.disabled = !!busy;
    if (busy) {
      stopPermissionTimer();
      R.permTimeText.textContent = '已提交，等待服务端确认…';
      return;
    }
    if (R.permissionOverlay.hidden) return;
    R.btnPermAllow.textContent = '允许一次';
    R.btnPermReject.textContent = '拒绝';
    if (permissionDeadline > Date.now()) {
      stopPermissionTimer();
      tickPermission();
      permissionTimer = setInterval(tickPermission, 250);
    }
  }

  function isPermissionOpen() { return !R.permissionOverlay.hidden; }

  /* --------------------------------------------------------------------- toast */

  function toast(message, kind) {
    if (!message) return;
    while (R.toastHost.children.length >= 3) R.toastHost.removeChild(R.toastHost.firstChild);
    var node = el('div', 'toast', message);
    node.dataset.kind = kind || 'info';
    R.toastHost.appendChild(node);
    window.setTimeout(function () {
      if (node.parentNode) node.parentNode.removeChild(node);
    }, 3400);
  }

  /* ---------------------------------------------------------------------- init */

  function init() {
    cacheRefs();
    R.btnDrawerClose.addEventListener('click', closeDrawer);
    R.drawerBackdrop.addEventListener('click', function (event) {
      if (event.target === R.drawerBackdrop) closeDrawer();
    });
    document.addEventListener('keydown', function (event) {
      if (event.key !== 'Escape') return;
      if (isPermissionOpen()) {
        if (permissionDecision && !R.btnPermReject.disabled) permissionDecision('reject-once');
        return;
      }
      if (isDrawerOpen()) closeDrawer();
    });
    R.composerInput.addEventListener('input', autoGrow);
    setConnection('connecting');
  }

  DSH.ui = {
    R: R,
    init: init,
    showBoot: showBoot,
    setBootText: setBootText,
    showBootError: showBootError,
    showHome: showHome,
    showChat: showChat,
    isChatOpen: isChatOpen,
    setWorkspace: setWorkspace,
    setModeLabel: setModeLabel,
    setConnection: setConnection,
    setSessionCount: setSessionCount,
    renderSessions: renderSessions,
    setListStatus: setListStatus,
    setListEmpty: setListEmpty,
    setListMore: setListMore,
    setRefreshBusy: setRefreshBusy,
    openDrawer: openDrawer,
    closeDrawer: closeDrawer,
    isDrawerOpen: isDrawerOpen,
    renderWorkspaces: renderWorkspaces,
    renderDrawerSessions: renderDrawerSessions,
    setBrowseOpen: setBrowseOpen,
    isBrowseOpen: isBrowseOpen,
    renderBrowse: renderBrowse,
    setBrowseError: setBrowseError,
    closeBrowseError: closeBrowseError,
    getBrowsePath: getBrowsePath,
    setChatHeader: setChatHeader,
    setStateChip: setStateChip,
    setUsage: setUsage,
    setBanner: setBanner,
    clearMessages: clearMessages,
    hasMessages: hasMessages,
    setChatLoading: setChatLoading,
    setChatEmpty: setChatEmpty,
    setOlderButton: setOlderButton,
    setNewBelow: setNewBelow,
    upsertUser: upsertUser,
    anchorUser: anchorUser,
    upsertAssistant: upsertAssistant,
    upsertReasoning: upsertReasoning,
    upsertTool: upsertTool,
    removeMessage: removeMessage,
    appendNote: appendNote,
    finishStreaming: finishStreaming,
    scrollToBottom: scrollToBottom,
    isNearBottom: isNearBottom,
    getComposerText: getComposerText,
    setComposerText: setComposerText,
    clearComposer: clearComposer,
    focusComposer: focusComposer,
    autoGrow: autoGrow,
    setComposerHint: setComposerHint,
    setComposerMode: setComposerMode,
    bindPermission: bindPermission,
    showPermission: showPermission,
    hidePermission: hidePermission,
    setPermissionBusy: setPermissionBusy,
    isPermissionOpen: isPermissionOpen,
    toast: toast
  };
})();
