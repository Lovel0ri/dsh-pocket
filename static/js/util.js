/* dsh-pocket · shared helpers. Classic script, no build step. */
(function () {
  'use strict';

  var DSH = (window.DSH = window.DSH || {});

  var ESCAPE_MAP = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };

  function escapeHtml(value) {
    return String(value === undefined || value === null ? '' : value).replace(
      /[&<>"']/g,
      function (ch) { return ESCAPE_MAP[ch]; }
    );
  }

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  var SVG_NS = 'http://www.w3.org/2000/svg';

  function icon(name, extraClass) {
    var svg = document.createElementNS(SVG_NS, 'svg');
    svg.setAttribute('class', 'icon' + (extraClass ? ' ' + extraClass : ''));
    svg.setAttribute('aria-hidden', 'true');
    var use = document.createElementNS(SVG_NS, 'use');
    use.setAttribute('href', '#i-' + name);
    svg.appendChild(use);
    return svg;
  }

  function timeMs(value) {
    if (typeof value === 'number' && isFinite(value)) return value;
    var parsed = Date.parse(String(value || ''));
    return isFinite(parsed) ? parsed : 0;
  }

  function pad2(n) { return (n < 10 ? '0' : '') + n; }

  function clockText(ms) {
    var d = new Date(ms);
    return pad2(d.getHours()) + ':' + pad2(d.getMinutes());
  }

  function sameDay(a, b) {
    return a.getFullYear() === b.getFullYear() &&
      a.getMonth() === b.getMonth() &&
      a.getDate() === b.getDate();
  }

  function relativeTime(value) {
    var ms = timeMs(value);
    if (!ms) return '';
    var now = Date.now();
    var diff = Math.max(0, now - ms);
    if (diff < 45 * 1000) return '刚刚';
    if (diff < 60 * 60 * 1000) return Math.floor(diff / 60000) + ' 分钟前';

    var d = new Date(ms);
    var n = new Date(now);
    if (sameDay(d, n)) return '今天 ' + clockText(ms);

    var yesterday = new Date(now);
    yesterday.setDate(yesterday.getDate() - 1);
    if (sameDay(d, yesterday)) return '昨天 ' + clockText(ms);

    if (d.getFullYear() === n.getFullYear()) {
      return (d.getMonth() + 1) + '月' + d.getDate() + '日';
    }
    return d.getFullYear() + '-' + pad2(d.getMonth() + 1) + '-' + pad2(d.getDate());
  }

  function shortTokens(n) {
    var value = Number(n);
    if (!isFinite(value) || value <= 0) return '0';
    if (value < 1000) return String(Math.round(value));
    if (value < 1000000) {
      var k = value / 1000;
      return (k < 10 ? k.toFixed(1) : String(Math.round(k))).replace(/\.0$/, '') + 'k';
    }
    return (value / 1000000).toFixed(1).replace(/\.0$/, '') + 'M';
  }

  function usageText(usage) {
    if (!usage || typeof usage.used !== 'number') return '';
    if (!usage.size) return shortTokens(usage.used) + ' tokens';
    return shortTokens(usage.used) + ' / ' + shortTokens(usage.size);
  }

  function randomHex(bytes) {
    var out = '';
    if (window.crypto && window.crypto.getRandomValues) {
      var buf = new Uint8Array(bytes);
      window.crypto.getRandomValues(buf);
      for (var i = 0; i < buf.length; i += 1) out += ('0' + buf[i].toString(16)).slice(-2);
      return out;
    }
    while (out.length < bytes * 2) out += Math.floor(Math.random() * 16).toString(16);
    return out.slice(0, bytes * 2);
  }

  function uid() {
    if (window.crypto && typeof window.crypto.randomUUID === 'function') {
      return window.crypto.randomUUID();
    }
    var hex = randomHex(16);
    return hex.slice(0, 8) + '-' + hex.slice(8, 12) + '-' + hex.slice(12, 16) + '-' +
      hex.slice(16, 20) + '-' + hex.slice(20, 32);
  }

  function turnId() { return 'turn_' + randomHex(6); }

  function tryParseJson(value) {
    if (typeof value !== 'string') return value;
    try { return JSON.parse(value); } catch (err) { return undefined; }
  }

  function stringifyArgs(raw) {
    if (raw === undefined || raw === null) return '';
    if (typeof raw === 'string') {
      var parsed = tryParseJson(raw);
      if (parsed === undefined) return raw;
      try { return JSON.stringify(parsed, null, 2); } catch (err) { return raw; }
    }
    try { return JSON.stringify(raw, null, 2); } catch (err) { return String(raw); }
  }

  function oneLineArgs(raw) {
    if (raw === undefined || raw === null) return '';
    var value = raw;
    if (typeof value === 'string') value = tryParseJson(value);
    if (value && typeof value === 'object' && !Array.isArray(value)) {
      if (typeof value.description === 'string' && value.description.trim()) return value.description.trim();
      if (typeof value.command === 'string' && value.command.trim()) return value.command.trim();
      if (typeof value.path === 'string' && value.path.trim()) return value.path.trim();
      try { return JSON.stringify(value); } catch (err) { return String(raw); }
    }
    if (typeof value === 'string') return value.replace(/\s+/g, ' ').trim();
    try { return JSON.stringify(value); } catch (err) { return String(raw); }
  }

  function truncate(text, max) {
    var value = String(text === undefined || text === null ? '' : text);
    if (value.length <= max) return value;
    return value.slice(0, Math.max(0, max - 1)) + '…';
  }

  async function copyText(text) {
    var value = String(text === undefined || text === null ? '' : text);
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(value);
        return true;
      }
    } catch (err) { /* fall through to execCommand */ }
    var area = document.createElement('textarea');
    area.value = value;
    area.setAttribute('readonly', '');
    area.style.position = 'fixed';
    area.style.top = '-2000px';
    area.style.opacity = '0';
    document.body.appendChild(area);
    area.select();
    var ok = false;
    try { ok = document.execCommand('copy'); } catch (err) { ok = false; }
    document.body.removeChild(area);
    return ok;
  }


  function cleanTitle(value) {
    var text = String(value === undefined || value === null ? '' : value)
      .replace(/```[\s\S]*?```/g, ' ')
      .replace(/`([^`]*)`/g, '$1')
      .replace(/\*\*([^*]+)\*\*/g, '$1')
      .replace(/^#{1,6}\s+/gm, '')
      .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
      .replace(/\s+/g, ' ')
      .trim();
    return truncate(text, 80) || '新会话';
  }

  function isNearBottom(node, threshold) {
    if (!node) return true;
    return node.scrollHeight - node.scrollTop - node.clientHeight < (threshold || 96);
  }

  var ERROR_TEXT = {
    E_RATE_LIMITED: '操作太频繁，请稍后再试',
    E_NOT_FOUND: '对象不存在或已被清理',
    E_WORKSPACE_UNKNOWN: '工作区不存在',
    E_WORKSPACE_MISSING: '工作区目录不存在或不可写',
    E_NOT_RESUMABLE: '会话不可恢复，请新建会话',
    E_PROMPT_IN_FLIGHT: '上一条消息还在处理中',
    E_ACP_INVALID: '参数无效',
    E_ACP_INTERNAL: '后端服务内部错误',
    E_ACP_UNAVAILABLE: 'ACP 服务不可用，请稍后重试',
    E_TIMEOUT: '请求超时，请重试',
    E_INVALID_REQUEST: '请求参数无效',
    E_PATH_FORBIDDEN: '路径越界，已拒绝访问',
    E_PATH_INVALID: '路径不存在或不是目录',
    E_INTERNAL: '服务内部错误',
    E_NETWORK: '网络连接失败',
    E_WS: '实时连接断开'
  };

  function errorText(code, fallback) {
    return ERROR_TEXT[code] || fallback || '操作失败，请稍后重试';
  }

  DSH.util = {
    escapeHtml: escapeHtml,
    el: el,
    icon: icon,
    timeMs: timeMs,
    clockText: clockText,
    relativeTime: relativeTime,
    shortTokens: shortTokens,
    usageText: usageText,
    uid: uid,
    turnId: turnId,
    tryParseJson: tryParseJson,
    stringifyArgs: stringifyArgs,
    oneLineArgs: oneLineArgs,
    truncate: truncate,
    cleanTitle: cleanTitle,
    copyText: copyText,
    isNearBottom: isNearBottom,
    errorText: errorText
  };
})();
