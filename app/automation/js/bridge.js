/*
 * ChatGPT bridge: the functions that run INSIDE the ChatGPT page.
 *
 * Ported from resume_builder/apps/extension/src/lib/chatgpt-bridge.ts (compiled
 * from that file, not rewritten). Every behaviour in here was measured on the
 * live site; see the comments in the original before changing anything.
 *
 * Each function is self-contained. They are exposed on one object,
 * window.__RAI_BRIDGE__, so Python can call them with runJavaScript
 * (app/automation/chatgpt.py).
 *
 * Added for the desktop app, on top of the port:
 *   - submitPrompt reports where it is in window.__raiBridgeProgress
 *     ({ phase: 'pasting', chunk: 3, chunks: 14 }) so the app can show
 *     "Pasting prompt 3/14...";
 *   - submitPrompt stops at the next safe point (between chunks, and before
 *     sending) once window.__raiBridgeCancel is true, and answers
 *     { status: 'cancelled' };
 *   - submitPrompt(prompt, { send: false }) inserts without sending;
 *   - readBeyond also returns the numbers behind its progress line;
 *   - detectBlocked() and composerReady().
 */
(() => {
  async function submitPromptInPage(prompt, options) {
    const COMPOSER_SELECTORS = [
      'div#prompt-textarea[contenteditable="true"]',
      'div.ProseMirror[contenteditable="true"]',
      '[data-testid="prompt-textarea"]',
      "textarea#prompt-textarea",
      "form textarea",
      // Last resort: any editable box on the page. Guards against a renamed id
      // or testid, which is the most likely way this breaks.
      '[contenteditable="true"]'
    ];
    const PASTE_CHUNK_LIMIT = 8e3;
    const EXEC_COMMAND_SAFE_LIMIT = 2e4;
    const SEND_SELECTORS = [
      'button[data-testid="send-button"]',
      "#composer-submit-button",
      'button[aria-label*="Send prompt"]',
      'button[aria-label*="Send message"]',
      'button[aria-label*="Send"]',
      'form button[type="submit"]'
    ];
    const wait = (ms) => new Promise((r) => setTimeout(r, ms));
    // Page globals, not closures: the app polls the first and sets the second.
    const report = (phase, extra) => {
      try {
        window.__raiBridgeProgress = Object.assign({ phase }, extra || {});
      } catch {
      }
    };
    const cancelled = () => window.__raiBridgeCancel === true;
    async function waitFor(get, timeoutMs) {
      const deadline = Date.now() + timeoutMs;
      for (; ; ) {
        const value = get();
        if (value) return value;
        if (Date.now() > deadline || cancelled()) return null;
        await wait(150);
      }
    }
    report("composer");
    const composer = await waitFor(() => {
      for (const selector of COMPOSER_SELECTORS) {
        const element = document.querySelector(selector);
        if (element) return element;
      }
      return null;
    }, 15e3);
    if (cancelled()) return { status: "cancelled" };
    if (!composer) {
      const signedOut = document.querySelector(
        'a[href*="/auth/login"], button[data-testid="login-button"]'
      );
      return { status: signedOut ? "not-signed-in" : "composer-not-found" };
    }
    const hadFocus = document.hasFocus();
    await wait(700);
    if (cancelled()) return { status: "cancelled" };
    function placeCaret(el) {
      try {
        el.click();
      } catch {
      }
      el.focus();
      if (el instanceof HTMLTextAreaElement) {
        el.setSelectionRange(el.value.length, el.value.length);
        return document.activeElement === el;
      }
      try {
        const range = document.createRange();
        range.selectNodeContents(el);
        range.collapse(false);
        const selection = window.getSelection();
        selection?.removeAllRanges();
        selection?.addRange(range);
      } catch {
        return false;
      }
      return document.activeElement === el || el.contains(document.activeElement);
    }
    const caretPlaced = placeCaret(composer);
    let diagnostic = "";
    if (composer instanceof HTMLTextAreaElement) {
      const setter = Object.getOwnPropertyDescriptor(
        window.HTMLTextAreaElement.prototype,
        "value"
      )?.set;
      report("pasting", { chunk: 1, chunks: 1 });
      setter?.call(composer, prompt);
      composer.dispatchEvent(new Event("input", { bubbles: true }));
    } else {
      const attempts = [];
      const currentText = () => (composer.innerText ?? composer.textContent ?? "").trim();
      const findInlineChipControl = () => {
        const scope = composer.closest("form") ?? document.body;
        const controls = Array.from(
          scope.querySelectorAll('button, [role="button"]')
        );
        return controls.find(
          (control) => /show in text field|show text|expand text/i.test(
            `${control.textContent ?? ""} ${control.getAttribute("aria-label") ?? ""}`
          )
        ) ?? null;
      };
      const splitForPaste = (text, limit) => {
        const chunks = [];
        const lines = text.split("\n");
        let buffer = "";
        for (let index = 0; index < lines.length; index += 1) {
          const piece = (index === 0 ? "" : "\n") + lines[index];
          if (piece.length > limit) {
            if (buffer.length > 0) {
              chunks.push(buffer);
              buffer = "";
            }
            for (let offset = 0; offset < piece.length; offset += limit) {
              chunks.push(piece.slice(offset, offset + limit));
            }
            continue;
          }
          if (buffer.length + piece.length > limit) {
            chunks.push(buffer);
            buffer = piece;
          } else {
            buffer += piece;
          }
        }
        if (buffer.length > 0) chunks.push(buffer);
        return chunks;
      };
      const pasteChunk = (chunk) => {
        placeCaret(composer);
        const transfer = new DataTransfer();
        transfer.setData("text/plain", chunk);
        composer.dispatchEvent(
          new ClipboardEvent("paste", {
            clipboardData: transfer,
            bubbles: true,
            cancelable: true
          })
        );
      };
      // Only clear a draft when there is one. A paste sent right after
      // selectAll + delete on an EMPTY composer is silently dropped; that once
      // lost the first 8,000 characters of every prompt.
      if (currentText().length > 0) {
        report("clearing");
        try {
          document.execCommand("selectAll", false, void 0);
          document.execCommand("delete", false, void 0);
        } catch {
        }
        await wait(300);
      }
      const expected = prompt.trim().length;
      if (currentText().length === 0) {
        try {
          const chunks = splitForPaste(prompt, PASTE_CHUNK_LIMIT);
          let retries = 0;
          for (let index = 0; index < chunks.length; index += 1) {
            const chunk = chunks[index];
            // Safe point: a chunk is either pasted whole or not at all.
            if (cancelled()) {
              return { status: "cancelled", diagnostic: `chunked:${index}/${chunks.length}` };
            }
            report("pasting", { chunk: index + 1, chunks: chunks.length });
            const before = currentText().length;
            for (let attempt = 0; attempt < 4; attempt += 1) {
              pasteChunk(chunk);
              await wait(50);
              if (currentText().length > before || chunk.trim().length === 0) break;
              await wait(300);
              if (currentText().length > before || findInlineChipControl()) break;
              retries += 1;
            }
          }
          await wait(250);
          attempts.push(
            `chunked:${chunks.length}x:${currentText().length}/${expected}${retries ? `:retried${retries}` : ""}`
          );
        } catch (error) {
          attempts.push(`chunked:threw(${error?.name ?? "error"})`);
        }
      }
      if (currentText().length === 0 && prompt.length <= EXEC_COMMAND_SAFE_LIMIT) {
        placeCaret(composer);
        try {
          document.execCommand("insertText", false, prompt);
          await wait(250);
          attempts.push(`execCommand:${currentText().length}`);
        } catch (error) {
          attempts.push(`execCommand:threw(${error?.name ?? "error"})`);
        }
      }
      if (currentText().length === 0 && prompt.length <= EXEC_COMMAND_SAFE_LIMIT) {
        placeCaret(composer);
        try {
          composer.dispatchEvent(
            new InputEvent("beforeinput", {
              inputType: "insertText",
              data: prompt,
              bubbles: true,
              cancelable: true
            })
          );
          await wait(250);
          attempts.push(`beforeinput:${currentText().length}`);
        } catch (error) {
          attempts.push(`beforeinput:threw(${error?.name ?? "error"})`);
        }
      }
      if (currentText().length === 0) {
        try {
          pasteChunk(prompt);
          await wait(400);
          attempts.push(`paste:${currentText().length}`);
        } catch (error) {
          attempts.push(`paste:threw(${error?.name ?? "error"})`);
        }
      }
      if (currentText().length === 0) {
        const inlineControl = findInlineChipControl();
        if (inlineControl) {
          try {
            inlineControl.click();
            await wait(500);
            attempts.push(`unchip:${currentText().length}`);
          } catch (error) {
            attempts.push(`unchip:threw(${error?.name ?? "error"})`);
          }
        }
      }
      if (currentText().length === 0) {
        const chipPresent = Boolean(findInlineChipControl());
        if (!chipPresent) {
          return {
            status: "composer-not-found",
            diagnostic: `insertion failed; caret=${caretPlaced}; focus=${hadFocus}; active=${document.activeElement?.tagName ?? "none"}; tried ${attempts.join(", ")}`
          };
        }
        attempts.push("sending-as-attachment");
      }
      composer.dispatchEvent(new Event("input", { bubbles: true }));
      diagnostic = attempts.join(", ");
    }
    // Spike / dry-run: insert only, never send.
    if (options && options.send === false) {
      report("pasted");
      return { status: "ok", diagnostic, sent: false };
    }
    report("sending");
    await wait(400);
    const findEnabledSendButton = () => {
      for (const selector of SEND_SELECTORS) {
        const button = document.querySelector(selector);
        if (button && !button.disabled && button.getAttribute("aria-disabled") !== "true") {
          return button;
        }
      }
      return null;
    };
    const sendButton = await waitFor(findEnabledSendButton, 15e3);
    const pressEnter = () => {
      const target = document.activeElement instanceof HTMLElement && composer.contains(document.activeElement) ? document.activeElement : composer;
      for (const type of ["keydown", "keypress", "keyup"]) {
        target.dispatchEvent(
          new KeyboardEvent(type, {
            key: "Enter",
            code: "Enter",
            keyCode: 13,
            which: 13,
            bubbles: true,
            cancelable: true,
            composed: true,
            view: window
          })
        );
      }
    };
    const didSubmit = () => {
      const composerEmpty = composer instanceof HTMLTextAreaElement ? composer.value.trim().length === 0 : (composer.innerText ?? "").trim().length === 0;
      const streaming = document.querySelector(
        'button[data-testid="stop-button"], button[aria-label*="Stop"]'
      );
      const answered = document.querySelector(
        '[data-message-author-role="assistant"], [data-content-search-unit-key$=":assistant"]'
      );
      return composerEmpty || streaming || answered ? true : null;
    };
    // Last safe point: after this the prompt is sent and cannot be taken back.
    if (cancelled()) return { status: "cancelled", diagnostic };
    placeCaret(composer);
    pressEnter();
    let submitted = await waitFor(didSubmit, 4e3);
    if (!submitted) {
      const button = sendButton ?? findEnabledSendButton();
      if (button) {
        button.click();
        submitted = await waitFor(didSubmit, 6e3);
        diagnostic = diagnostic ? `${diagnostic}; clicked send` : "clicked send";
      }
    } else {
      diagnostic = diagnostic ? `${diagnostic}; sent with Enter` : "sent with Enter";
    }
    if (!submitted) {
      return {
        status: "send-failed",
        diagnostic: `${diagnostic || "inserted"}; enter did not submit; sendButton=${Boolean(
          sendButton
        )}`
      };
    }
    report("sent");
    return { status: "ok", diagnostic };
  }

  function countNonEmptyRepliesInPage() {
    try {
      const all = (selector) => Array.from(document.querySelectorAll(selector));
      const hasText = (el) => (el.textContent ?? "").trim().length > 0;
      const layouts = [
        () => all('[data-message-author-role="assistant"]'),
        () => all(
          '[data-content-search-unit-key$=":assistant"], [data-chatgpt-search-unit-key$=":assistant"]'
        ).filter(
          (unit, _index, units) => !units.some((other) => other !== unit && other.contains(unit))
        ),
        () => all('[data-markdown-text-style="assistant-message"]'),
        () => all('[data-turn="assistant"]')
      ];
      for (const layout of layouts) {
        const found = layout().filter(hasText);
        if (found.length > 0) return found.length;
      }
      return 0;
    } catch {
      return 0;
    }
  }

  function readReplyBeyondInPage(baseline) {
    try {
      const all = (selector) => Array.from(document.querySelectorAll(selector));
      const hasText = (el) => (el.textContent ?? "").trim().length > 0;
      const layouts = [
        () => all('[data-message-author-role="assistant"]'),
        () => all(
          '[data-content-search-unit-key$=":assistant"], [data-chatgpt-search-unit-key$=":assistant"]'
        ).filter(
          (unit, _index, units) => !units.some((other) => other !== unit && other.contains(unit))
        ),
        () => all('[data-markdown-text-style="assistant-message"]'),
        () => all('[data-turn="assistant"]')
      ];
      let nodes = layouts[0]();
      for (const layout of layouts) {
        const found = layout();
        if (found.some(hasText)) {
          nodes = found;
          break;
        }
      }
      const plainText = (root) => {
        let out = "";
        const walk = (node) => {
          if (node.nodeType === 3) {
            out += node.nodeValue ?? "";
            return;
          }
          if (node.nodeType !== 1) return;
          const el = node;
          if (el.getAttribute("data-markdown-copy") === "exclude" || el.tagName === "svg") return;
          if (el.tagName === "BR") {
            out += "\n";
            return;
          }
          const block = /^(P|DIV|LI|UL|OL|H[1-6]|PRE|BLOCKQUOTE|TABLE|TR)$/.test(el.tagName);
          if (block && out.length > 0 && !out.endsWith("\n")) out += "\n";
          el.childNodes.forEach(walk);
          if (block && !out.endsWith("\n")) out += "\n";
          if (el.tagName === "P" && !out.endsWith("\n\n")) out += "\n";
        };
        walk(root);
        return out.trim();
      };
      const textOf = (el) => {
        const blocks = Array.from(el.querySelectorAll('pre, [data-markdown-copy="code-block"]')).map((block) => ((block.querySelector("code")?.textContent || block.textContent) ?? "").trim()).filter((text) => text.length > 0).sort((a, b) => b.length - a.length);
        if (blocks[0]) return blocks[0];
        const bodies = Array.from(
          el.querySelectorAll('[data-markdown-text-style="assistant-message"]')
        );
        const body = bodies.map(plainText).filter((text) => text.length > 0).join("\n\n");
        return body || plainText(el);
      };
      const texts = nodes.map(textOf).filter((text) => text.length > 0);
      const stopButton = Array.from(document.querySelectorAll("button")).find(
        (button) => (button.getAttribute("data-testid") === "stop-button" || /^stop( (answering|streaming|generating|response))?$/i.test(
          (button.getAttribute("aria-label") ?? "").trim()
        )) && button.getClientRects().length > 0
      );
      const streamingAttr = nodes.some(
        (node) => node.getAttribute("data-message-is-streaming") === "true"
      );
      const diag = `${nodes.length} reply block(s), ${texts.length ? texts[texts.length - 1].length : 0} chars, Stop ${stopButton ? "shown" : "gone"}`;
      const blocks = nodes.length;
      const replies = texts.length;
      if (texts.length <= baseline) {
        return { status: "ok", text: "", streaming: true, stopVisible: Boolean(stopButton), diag, blocks, replies };
      }
      return {
        status: "ok",
        text: texts[texts.length - 1],
        streaming: Boolean(stopButton) || streamingAttr,
        stopVisible: Boolean(stopButton),
        diag,
        blocks,
        replies
      };
    } catch {
      return { status: "error" };
    }
  }

  function readLatestReplyInPage(baselineCount) {
    try {
      const all = (selector) => Array.from(document.querySelectorAll(selector));
      const hasText = (el) => (el.textContent ?? "").trim().length > 0;
      const layouts = [
        () => all('[data-message-author-role="assistant"]'),
        () => all(
          '[data-content-search-unit-key$=":assistant"], [data-chatgpt-search-unit-key$=":assistant"]'
        ).filter(
          (unit, _index, units) => !units.some((other) => other !== unit && other.contains(unit))
        ),
        () => all('[data-markdown-text-style="assistant-message"]'),
        () => all('[data-turn="assistant"]')
      ];
      let nodes = layouts[0]();
      for (const layout of layouts) {
        const found = layout();
        if (found.some(hasText)) {
          nodes = found;
          break;
        }
      }
      const plainText = (root) => {
        let out = "";
        const walk = (node) => {
          if (node.nodeType === 3) {
            out += node.nodeValue ?? "";
            return;
          }
          if (node.nodeType !== 1) return;
          const el = node;
          if (el.getAttribute("data-markdown-copy") === "exclude" || el.tagName === "svg") return;
          if (el.tagName === "BR") {
            out += "\n";
            return;
          }
          const block = /^(P|DIV|LI|UL|OL|H[1-6]|PRE|BLOCKQUOTE|TABLE|TR)$/.test(el.tagName);
          if (block && out.length > 0 && !out.endsWith("\n")) out += "\n";
          el.childNodes.forEach(walk);
          if (block && !out.endsWith("\n")) out += "\n";
          if (el.tagName === "P" && !out.endsWith("\n\n")) out += "\n";
        };
        walk(root);
        return out.trim();
      };
      const textOf = (el) => {
        const blocks = Array.from(el.querySelectorAll('pre, [data-markdown-copy="code-block"]')).map((block) => ((block.querySelector("code")?.textContent || block.textContent) ?? "").trim()).filter((text) => text.length > 0).sort((a, b) => b.length - a.length);
        if (blocks[0]) return blocks[0];
        const bodies = Array.from(
          el.querySelectorAll('[data-markdown-text-style="assistant-message"]')
        );
        const body = bodies.map(plainText).filter((text) => text.length > 0).join("\n\n");
        return body || plainText(el);
      };
      const texts = nodes.map(textOf).filter((text) => text.length > 0);
      const stopButton = Array.from(document.querySelectorAll("button")).find(
        (button) => (button.getAttribute("data-testid") === "stop-button" || /^stop( (answering|streaming|generating|response))?$/i.test(
          (button.getAttribute("aria-label") ?? "").trim()
        )) && button.getClientRects().length > 0
      );
      const streamingAttr = nodes.some(
        (node) => node.getAttribute("data-message-is-streaming") === "true"
      );
      const diag = `${nodes.length} reply block(s), ${texts.length ? texts[texts.length - 1].length : 0} chars, Stop ${stopButton ? "shown" : "gone"}`;
      if (nodes.length <= baselineCount && texts.length === 0) {
        return { status: "ok", text: "", streaming: true, stopVisible: Boolean(stopButton), diag };
      }
      const latestText = texts.length > 0 ? texts[texts.length - 1] : "";
      const streaming = Boolean(stopButton) || streamingAttr;
      return { status: "ok", text: latestText, streaming, stopVisible: Boolean(stopButton), diag };
    } catch {
      return { status: "error" };
    }
  }

  function looksLikeCompleteJson(text) {
    const start = text.indexOf("{");
    if (start < 0) return false;
    let depth = 0;
    let inString = false;
    let escaped = false;
    for (let i = start; i < text.length; i += 1) {
      const ch = text[i];
      if (inString) {
        if (escaped) escaped = false;
        else if (ch === "\\") escaped = true;
        else if (ch === '"') inString = false;
        continue;
      }
      if (ch === '"') inString = true;
      else if (ch === "{" || ch === "[") depth += 1;
      else if (ch === "}" || ch === "]") {
        depth -= 1;
        if (depth < 0) return false;
        if (depth === 0) return /^[\s`]*$/.test(text.slice(i + 1));
      }
    }
    return false;
  }

  /**
   * Is there a message box to type into, and is it on screen?
   * The same selectors submitPrompt uses, in the same order.
   */
  function composerReadyInPage() {
    try {
      const COMPOSER_SELECTORS = [
        'div#prompt-textarea[contenteditable="true"]',
        'div.ProseMirror[contenteditable="true"]',
        '[data-testid="prompt-textarea"]',
        "textarea#prompt-textarea",
        "form textarea",
        '[contenteditable="true"]'
      ];
      for (const selector of COMPOSER_SELECTORS) {
        const element = document.querySelector(selector);
        if (element) return element.getClientRects().length > 0;
      }
      return false;
    } catch {
      return false;
    }
  }

  /**
   * Does the page need a real person before a prompt can be sent?
   *
   *   'login'      ChatGPT is signed out: a sign-in page (auth.openai.com,
   *                /auth/login, a single-sign-on host), or a visible
   *                "Log in" / "Sign up" control on the page. Signed-out
   *                ChatGPT can still show a message box (anonymous chat), so
   *                the control alone is enough: a resume must never be sent
   *                from an anonymous session, which has no conversation link.
   *   'challenge'  a Cloudflare / "verify you are human" page.
   *   null         neither.
   *
   * Buttons inside a reply or the composer are ignored, and links count only
   * by their address, so a resume or a chat title that says "Log in" cannot
   * trip this.
   */
  function detectBlockedInPage() {
    try {
      const host = (location.hostname || "").toLowerCase();
      const path = (location.pathname || "").toLowerCase();
      const LOGIN_HOSTS = [
        "auth.openai.com",
        "auth0.openai.com",
        "accounts.google.com",
        "login.microsoftonline.com",
        "login.live.com",
        "appleid.apple.com"
      ];
      if (LOGIN_HOSTS.includes(host) || /^\/auth\/(login|error)\b/.test(path) || /^\/(log-in|login)(\/|$)/.test(path)) {
        return "login";
      }

      const visible = (el) => Boolean(el) && el.getClientRects().length > 0;
      const COMPOSER_SELECTORS = [
        'div#prompt-textarea[contenteditable="true"]',
        'div.ProseMirror[contenteditable="true"]',
        '[data-testid="prompt-textarea"]',
        "textarea#prompt-textarea",
        "form textarea"
      ];
      const hasComposer = COMPOSER_SELECTORS.some((selector) => visible(document.querySelector(selector)));

      // A challenge page replaces the whole app, so it never has a composer.
      // (The signed-in app itself loads Cloudflare scripts and a hidden
      // Turnstile frame, which is why those alone do not count.)
      if (!hasComposer) {
        const title = (document.title || "").trim();
        const marker = document.querySelector(
          '#challenge-form, #challenge-running, #challenge-stage, #challenge-error-text, #cf-challenge-running, #cf-chl-widget, .cf-challenge, .cf-turnstile, form[action*="__cf_chl"], iframe[src*="challenges.cloudflare.com"], iframe[title*="challenge" i]'
        );
        const bodyText = (document.body ? document.body.textContent || "" : "").replace(/\s+/g, " ").trim();
        const wording = bodyText.length < 3e3 && /verify(ing)? (that )?you are (a )?human|checking (if the site connection is secure|your browser)|needs to review the security of your connection|enable javascript and cookies to continue|unusual activity (has been detected )?from your (device|network)/i.test(bodyText);
        if (typeof window._cf_chl_opt !== "undefined" || /^(just a moment|attention required|one more step)/i.test(title) || marker || wording) {
          return "challenge";
        }
      }

      const IGNORED = '[data-message-author-role], [data-markdown-text-style], [data-content-search-unit-key], [data-chatgpt-search-unit-key], [data-turn], [contenteditable="true"]';
      const controls = Array.from(document.querySelectorAll('button, a, [role="button"]'));
      const loginControl = controls.find((control) => {
        if (!visible(control) || control.closest(IGNORED)) return false;
        const testId = control.getAttribute("data-testid") || "";
        if (testId === "login-button" || testId === "signup-button") return true;
        if (control.tagName === "A") {
          // Links count by where they go, never by their words: the sidebar
          // lists the user's chats as links, and a chat can be titled "Log in".
          return /\/auth\/login|\/log-?in\b|\/sign-?up\b|auth\.openai\.com/i.test(control.getAttribute("href") || "");
        }
        const label = (control.textContent || "").replace(/\s+/g, " ").trim();
        return /^(log ?in|sign in|sign up|sign up for free)$/i.test(label);
      });
      return loginControl ? "login" : null;
    } catch {
      return null;
    }
  }

  window.__RAI_BRIDGE__ = {
    submitPrompt: submitPromptInPage,
    countReplies: countNonEmptyRepliesInPage,
    readBeyond: readReplyBeyondInPage,
    readLatest: readLatestReplyInPage,
    looksLikeCompleteJson,
    detectBlocked: detectBlockedInPage,
    composerReady: composerReadyInPage,
  };
  return true;
})();
