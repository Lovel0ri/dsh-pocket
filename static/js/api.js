/* dsh-pocket · REST client (personal LAN mode, no authentication).
   Contract: docs/api.md §3, with the T7 personal additions (cwd create). */
(function () {
  'use strict';

  var DSH = window.DSH;
  var API_BASE = '/api/v1';

  function ApiError(status, code, message, extra) {
    var opts = extra || {};
    this.name = 'ApiError';
    this.status = status || 0;
    this.code = code || 'E_INTERNAL';
    this.message = message || this.code;
    this.retryable = !!opts.retryable;
    this.details = opts.details === undefined ? null : opts.details;
    this.retryAfter = opts.retryAfter || null;
    if (Error.captureStackTrace) Error.captureStackTrace(this, ApiError);
  }
  ApiError.prototype = Object.create(Error.prototype);
  ApiError.prototype.constructor = ApiError;

  function buildQuery(query) {
    if (!query) return '';
    var params = new URLSearchParams();
    Object.keys(query).forEach(function (key) {
      var value = query[key];
      if (value === undefined || value === null || value === '') return;
      params.append(key, String(value));
    });
    var text = params.toString();
    return text ? '?' + text : '';
  }

  async function request(method, path, options) {
    var opts = options || {};
    var timeoutMs = typeof opts.timeoutMs === 'number' ? opts.timeoutMs : 15000;
    var url = API_BASE + path + buildQuery(opts.query);
    var headers = { Accept: 'application/json' };
    if (opts.body !== undefined) headers['Content-Type'] = 'application/json; charset=utf-8';

    var controller = typeof AbortController === 'function' ? new AbortController() : null;
    var timer = null;
    if (controller) timer = setTimeout(function () { controller.abort(); }, timeoutMs);

    var response;
    try {
      response = await fetch(url, {
        method: method,
        headers: headers,
        body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
        cache: 'no-store',
        credentials: 'same-origin',
        signal: controller ? controller.signal : undefined
      });
    } catch (err) {
      var aborted = err && err.name === 'AbortError';
      throw new ApiError(0, aborted ? 'E_TIMEOUT' : 'E_NETWORK',
        aborted ? '请求超时' : '无法连接到 dsh-pocket', { retryable: true });
    } finally {
      if (timer) clearTimeout(timer);
    }

    var text = '';
    try { text = await response.text(); } catch (err) { text = ''; }
    var data = null;
    if (text) {
      try { data = JSON.parse(text); } catch (err) { data = null; }
    }

    if (!response.ok) {
      var info = (data && data.error && typeof data.error === 'object') ? data.error : {};
      throw new ApiError(
        response.status,
        info.code || 'E_INTERNAL',
        info.message || ('HTTP ' + response.status),
        {
          retryable: info.retryable,
          details: info.details,
          retryAfter: response.headers.get('Retry-After')
        }
      );
    }
    return data;
  }

  var api = {
    ApiError: ApiError,

    health: function () {
      return request('GET', '/health', { timeoutMs: 6000 });
    },

    workspaces: function () {
      return request('GET', '/workspaces');
    },

    browse: function (path) {
      return request('GET', '/browse', { query: { path: path, showHidden: 'false' } });
    },

    sessions: function (params) {
      return request('GET', '/sessions', {
        query: {
          workspaceId: params.workspaceId,
          cursor: params.cursor,
          limit: params.limit || 30
        }
      });
    },

    session: function (sessionId) {
      return request('GET', '/sessions/' + encodeURIComponent(sessionId));
    },

    /** createSession({workspaceId}) or createSession({cwd: "/abs/path"}). */
    createSession: function (target) {
      var body = {};
      if (target && target.workspaceId) body.workspaceId = target.workspaceId;
      else if (target && target.cwd) body.cwd = target.cwd;
      return request('POST', '/sessions', { body: body, timeoutMs: 30000 });
    },

    resume: function (sessionId) {
      return request('POST', '/sessions/' + encodeURIComponent(sessionId) + '/resume',
        { body: {}, timeoutMs: 30000 });
    },

    transcript: function (sessionId, params) {
      var opts = params || {};
      return request('GET', '/sessions/' + encodeURIComponent(sessionId) + '/transcript', {
        query: {
          before: opts.before,
          limit: opts.limit || 30,
          includeReasoning: opts.includeReasoning === false ? 'false' : 'true',
          maxChars: opts.maxChars
        }
      });
    },

    toolCall: function (sessionId, callId) {
      return request('GET', '/sessions/' + encodeURIComponent(sessionId) +
        '/tool-calls/' + encodeURIComponent(callId));
    },

    sendMessage: function (sessionId, text, clientMessageId) {
      return request('POST', '/sessions/' + encodeURIComponent(sessionId) + '/messages', {
        body: { clientMessageId: clientMessageId, text: text },
        timeoutMs: 30000
      });
    },

    cancel: function (sessionId) {
      return request('POST', '/sessions/' + encodeURIComponent(sessionId) + '/cancel', { body: {} });
    },

    respondPermission: function (sessionId, requestId, decision) {
      return request('POST', '/sessions/' + encodeURIComponent(sessionId) +
        '/permissions/' + encodeURIComponent(requestId), { body: { decision: decision } });
    }
  };

  DSH.api = api;
})();
