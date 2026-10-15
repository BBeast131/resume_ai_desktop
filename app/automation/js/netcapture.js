/*
 * Resume AI: remember the JSON a job page loads.
 *
 * Installed by the embedded browser on every page at document creation, in
 * the page's own world. It wraps fetch and XMLHttpRequest so that JSON
 * responses (a job board's own API answers: title, company, full
 * description) can be read later by the extractor, which often finds the
 * whole posting there when the page only renders part of it.
 *
 * - Read-only: requests go out and come back exactly as before; the page gets
 *   the original response, the copy is read from a clone.
 * - Nothing leaves the page: the copies sit in window.__RAI_NET__ (a small
 *   ring buffer in this tab's memory) until the page is closed.
 * - Skipped entirely on ChatGPT and sign-in pages, and for analytics hosts.
 */
(function () {
  'use strict';
  if (window.__RAI_NET__) return;

  var host = '';
  try {
    host = String(location.hostname || '').toLowerCase();
  } catch (e) {
    return;
  }
  // Not on ChatGPT or sign-in pages, and not on jobright.ai: its Apply control opens the real
  // job site and must behave exactly as without this script (its data is read from the page).
  var SKIP_PAGE = /(^|\.)(chatgpt\.com|openai\.com|jobright\.ai|accounts\.google\.com|login\.microsoftonline\.com|appleid\.apple\.com)$/;
  if (SKIP_PAGE.test(host)) return;

  var SKIP_URL = /google-analytics|googletagmanager|doubleclick|segment\.(io|com)|sentry|datadoghq|hotjar|amplitude|mixpanel|intercom|fullstory|clarity\.ms|launchdarkly|optimizely|newrelic|bugsnag|facebook\.com\/tr/i;
  var MAX_ENTRIES = 25;
  var MAX_ENTRY_CHARS = 1500000;
  var MAX_TOTAL_CHARS = 8000000;

  var store = { version: 1, entries: [], total: 0 };
  try {
    Object.defineProperty(window, '__RAI_NET__', { value: store, enumerable: false });
  } catch (e) {
    window.__RAI_NET__ = store;
  }

  function looksJson(type, text) {
    if (type && /json/i.test(type)) return true;
    var start = text.slice(0, 1);
    return start === '{' || start === '[';
  }

  function remember(url, type, text) {
    try {
      if (typeof text !== 'string' || text.length < 40 || text.length > MAX_ENTRY_CHARS) return;
      if (!looksJson(type, text)) return;
      store.entries.push({ url: String(url || '').slice(0, 2000), text: text, at: Date.now() });
      store.total += text.length;
      while (store.entries.length > MAX_ENTRIES || store.total > MAX_TOTAL_CHARS) {
        var dropped = store.entries.shift();
        store.total -= dropped ? dropped.text.length : 0;
      }
    } catch (e) {
      /* never disturb the page */
    }
  }

  function skipped(url) {
    try {
      return SKIP_URL.test(String(url || ''));
    } catch (e) {
      return true;
    }
  }

  // fetch
  try {
    var originalFetch = window.fetch;
    if (typeof originalFetch === 'function') {
      var wrapped = function () {
        var args = arguments;
        var promise = originalFetch.apply(this === undefined ? window : this, args);
        try {
          promise
            .then(function (response) {
              try {
                var url = response && response.url ? response.url : String(args[0] && args[0].url ? args[0].url : args[0]);
                if (!response || !response.ok || skipped(url)) return;
                var type = response.headers && response.headers.get ? response.headers.get('content-type') || '' : '';
                if (type && !/json|text\/plain/i.test(type)) return;
                var length = Number(response.headers.get('content-length') || 0);
                if (length > MAX_ENTRY_CHARS) return;
                response
                  .clone()
                  .text()
                  .then(function (text) {
                    remember(url, type, text);
                  })
                  .catch(function () {});
              } catch (e) {
                /* ignore */
              }
            })
            .catch(function () {});
        } catch (e) {
          /* ignore */
        }
        return promise;
      };
      try {
        Object.defineProperty(wrapped, 'name', { value: 'fetch' });
      } catch (e) {
        /* ignore */
      }
      window.fetch = wrapped;
    }
  } catch (e) {
    /* ignore */
  }

  // XMLHttpRequest
  try {
    var proto = window.XMLHttpRequest && window.XMLHttpRequest.prototype;
    if (proto) {
      var open = proto.open;
      var send = proto.send;
      proto.open = function (method, url) {
        try {
          this.__raiUrl = url;
        } catch (e) {
          /* ignore */
        }
        return open.apply(this, arguments);
      };
      proto.send = function () {
        var xhr = this;
        try {
          xhr.addEventListener('load', function () {
            try {
              var url = xhr.responseURL || xhr.__raiUrl;
              if (xhr.status < 200 || xhr.status >= 300 || skipped(url)) return;
              var type = xhr.getResponseHeader('content-type') || '';
              if (type && !/json|text\/plain/i.test(type)) return;
              if (xhr.responseType === '' || xhr.responseType === 'text') {
                remember(url, type, xhr.responseText);
              } else if (xhr.responseType === 'json' && xhr.response) {
                remember(url, 'application/json', JSON.stringify(xhr.response));
              }
            } catch (e) {
              /* ignore */
            }
          });
        } catch (e) {
          /* ignore */
        }
        return send.apply(this, arguments);
      };
    }
  } catch (e) {
    /* ignore */
  }
})();
