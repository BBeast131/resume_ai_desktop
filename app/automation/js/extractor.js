/*
 * Resume AI: job page extractor.
 *
 * Injected into arbitrary job pages with runJavaScript. It only READS the
 * page, with three narrow exceptions that are each a single deliberate click
 * or scroll: expandDescription() (a "Show more" toggle next to the job
 * description), scrollForLazy() and clickApplyControl() (the jobright
 * "Apply" button). It never clicks consent, login, CAPTCHA or submit
 * controls, and it never evaluates page data as code.
 *
 * Ported from the browser extension (`extraction/{index,adapters,generic}.ts`)
 * and kept as one self-contained script: no imports, no build step.
 *
 * Every public function catches its own errors and returns a safe default.
 */
(function () {
  'use strict';

  var VERSION = 1;
  if (window.__RAI_EXTRACT__ && window.__RAI_EXTRACT__.version === VERSION) return;

  // -------------------------------------------------------------------------
  // Limits. FORM_ONLY_* and MIN_JD_LENGTH mirror app/automation/job_rules.py.
  // -------------------------------------------------------------------------

  const MIN_DESCRIPTION_LENGTH = 200;
  const MIN_JD_LENGTH = 100;
  const MAX_JD_LENGTH = 50000;
  const MAX_FIELD_LENGTH = 160;
  const FORM_ONLY_PROSE_MAX = 600;
  const FORM_ONLY_MIN_FIELDS = 3;
  /** A line shorter than this is a label or a menu item, not prose. */
  const PROSE_LINE_MIN = 30;
  const MAX_APPLY_LINKS = 20;
  const MAX_IFRAMES = 10;

  // -------------------------------------------------------------------------
  // Text helpers
  // -------------------------------------------------------------------------

  function cleanText(value) {
    return String(value == null ? '' : value)
      .replace(/\r\n?/g, '\n')
      .replace(/[ \t\u00a0\u200b]+/g, ' ')
      .replace(/ *\n */g, '\n')
      .replace(/\n{3,}/g, '\n\n')
      .trim();
  }

  function oneLine(value) {
    return String(value == null ? '' : value)
      .replace(/\s+/g, ' ')
      .trim();
  }

  function clean(value, maxLength) {
    const cleaned = oneLine(value);
    return cleaned.length > maxLength ? cleaned.slice(0, maxLength) : cleaned;
  }

  function safe(fn, fallback) {
    try {
      const value = fn();
      return value === undefined ? fallback : value;
    } catch (error) {
      return fallback;
    }
  }

  function tagOf(el) {
    return String(el.tagName || '').toUpperCase();
  }

  function classAndId(el) {
    return (el.getAttribute('id') || '') + ' ' + (el.getAttribute('class') || '');
  }

  function hostMatches(url, domains) {
    try {
      const host = new URL(url).hostname.toLowerCase().replace(/^www\./, '');
      return domains.some((domain) => host === domain || host.endsWith('.' + domain));
    } catch (error) {
      return false;
    }
  }

  function isHttpUrl(value) {
    return typeof value === 'string' && /^https?:\/\//i.test(value);
  }

  // -------------------------------------------------------------------------
  // What counts as page chrome rather than job content
  // -------------------------------------------------------------------------

  // Same list as the extension's visibleText(), plus form controls and labels
  // so a field's caption never counts as description.
  const STRIP_TAGS = new Set([
    'SCRIPT',
    'STYLE',
    'NOSCRIPT',
    'SVG',
    'NAV',
    'HEADER',
    'FOOTER',
    'FORM',
    'BUTTON',
    'IFRAME',
    'TEMPLATE',
    'SELECT',
    'TEXTAREA',
    'INPUT',
    'OPTION',
    'LABEL',
    'LEGEND',
  ]);

  const STRIP_ROLES = new Set(['navigation', 'banner', 'contentinfo', 'search']);

  const BLOCK_TAGS = new Set([
    'P',
    'DIV',
    'SECTION',
    'ARTICLE',
    'MAIN',
    'ASIDE',
    'LI',
    'UL',
    'OL',
    'DL',
    'DT',
    'DD',
    'H1',
    'H2',
    'H3',
    'H4',
    'H5',
    'H6',
    'TR',
    'TABLE',
    'BLOCKQUOTE',
    'PRE',
    'HR',
    'DETAILS',
    'SUMMARY',
    'FIGURE',
    'ADDRESS',
  ]);

  const PARAGRAPH_TAGS = new Set(['P', 'H1', 'H2', 'H3', 'H4', 'H5', 'H6', 'UL', 'OL', 'TABLE', 'BLOCKQUOTE']);

  /** Cookie and consent managers, by the hooks they put on their container. */
  const CONSENT_HOOK_RE =
    /cookie|consent|gdpr|onetrust|cookiebot|didomi|usercentrics|truste|qc-cmp|sp_message|privacy-(banner|notice|wall|modal)/i;

  const CONSENT_SELECTOR = [
    '#onetrust-banner-sdk',
    '#onetrust-consent-sdk',
    '#onetrust-pc-sdk',
    '#CybotCookiebotDialog',
    '#didomi-host',
    '#usercentrics-root',
    '#truste-consent-track',
    '.truste_box_overlay',
    '.qc-cmp2-container',
    '[id^="sp_message_container"]',
    '.cc-window',
    '[id*="cookie" i]',
    '[class*="cookie" i]',
    '[id*="consent" i]',
    '[class*="consent" i]',
    '[id*="gdpr" i]',
    '[class*="gdpr" i]',
    '[aria-label*="cookie" i]',
    '[aria-label*="consent" i]',
  ].join(', ');

  const SIMILAR_HOOK_RE =
    /(similar|related|recommended|suggested|more|other)[-_ ]?(jobs?|roles|positions|openings|opportunities|listings|vacancies)/i;

  const SIMILAR_HEADING_RE = new RegExp(
    [
      '^(similar|related|recommended|suggested|featured|other|more|recent|latest|new)\\s+' +
        '(jobs?|roles?|positions?|openings?|opportunities|listings?|vacancies|searches)\\b',
      '^(people|others) also (viewed|applied)',
      '^jobs? you (may|might) (also )?(like|be interested in)',
      '^you (may|might) also (like|be interested in)',
      '^explore (more|other|similar) (jobs|roles|positions)',
      '^(view|see|browse) (more|other|similar|all) (jobs|roles|positions)',
    ].join('|'),
    'i'
  );

  function newContext() {
    return { skip: new WeakMap(), noise: new WeakMap(), excluded: new WeakMap() };
  }

  /** An ASP.NET-style form that wraps the whole page is layout, not a form. */
  function isWrapperForm(form) {
    return safe(
      () =>
        form.getAttribute('id') === 'aspnetForm' ||
        Boolean(form.querySelector('input[name="__VIEWSTATE"]')) ||
        Boolean(form.querySelector('main, article, [role="main"]')),
      false
    );
  }

  /**
   * A consent/cookie container. The guards stop a page wrapper that merely
   * carries a state class ("cookie-banner-open") from being treated as one.
   */
  function isConsentNode(el) {
    const tag = tagOf(el);
    if (tag === 'HTML' || tag === 'BODY' || tag === 'MAIN' || tag === 'ARTICLE') return false;
    const label = el.getAttribute('aria-label') || '';
    if (!CONSENT_HOOK_RE.test(classAndId(el)) && !/cookie|consent/i.test(label)) return false;
    if (el.querySelector('main, article, [role="main"]')) return false;
    return (el.textContent || '').length <= 8000;
  }

  function isSimilarJobsNode(el) {
    const tag = tagOf(el);
    if (tag === 'HTML' || tag === 'BODY' || tag === 'MAIN') return false;
    return SIMILAR_HOOK_RE.test(classAndId(el));
  }

  function isNoiseNode(el, ctx) {
    const cached = ctx.noise.get(el);
    if (cached !== undefined) return cached;
    const noise = safe(() => isConsentNode(el) || isSimilarJobsNode(el), false);
    ctx.noise.set(el, noise);
    return noise;
  }

  function cssHidden(el) {
    const view = el.ownerDocument && el.ownerDocument.defaultView;
    if (!view) return false;
    const style = view.getComputedStyle(el);
    if (!style) return false;
    return style.display === 'none' || style.visibility === 'hidden' || style.visibility === 'collapse';
  }

  function shouldSkip(el, ctx, styled) {
    const cached = ctx.skip.get(el);
    if (cached !== undefined) return cached;

    let skip = false;
    const tag = tagOf(el);
    if (STRIP_TAGS.has(tag)) {
      skip = !(tag === 'FORM' && isWrapperForm(el));
    } else if (el.hasAttribute('hidden')) {
      skip = true;
    } else if (el.getAttribute('aria-hidden') === 'true') {
      // Icons and decoration. But an aria-hidden subtree that is large, or that
      // holds the page's landmarks, is the page itself, marked inert while a
      // dialog is open; its text is still the content.
      skip = (el.textContent || '').length <= 500 && !el.querySelector('main, article, h1, [role="main"]');
    } else if (STRIP_ROLES.has(String(el.getAttribute('role') || '').toLowerCase())) {
      skip = true;
    } else if (isNoiseNode(el, ctx)) {
      skip = true;
    } else if (styled && safe(() => cssHidden(el), false)) {
      skip = true;
    }

    ctx.skip.set(el, skip);
    return skip;
  }

  function walk(node, out, ctx, styled, pre) {
    for (let child = node.firstChild; child; child = child.nextSibling) {
      if (child.nodeType === 3) {
        out.push(pre ? child.nodeValue : String(child.nodeValue || '').replace(/\s+/g, ' '));
        continue;
      }
      if (child.nodeType !== 1) continue;

      const tag = tagOf(child);
      const isHeading = /^H[1-6]$/.test(tag) || child.getAttribute('role') === 'heading';
      if (isHeading) {
        const heading = oneLine(child.textContent);
        // "Similar jobs" and everything after it in this container is not the posting.
        if (heading.length <= 80 && SIMILAR_HEADING_RE.test(heading)) break;
      }

      if (shouldSkip(child, ctx, styled)) continue;
      if (tag === 'BR') {
        out.push('\n');
        continue;
      }

      const block = BLOCK_TAGS.has(tag);
      if (block) out.push('\n');
      if (child.shadowRoot) walk(child.shadowRoot, out, ctx, styled, pre || tag === 'PRE');
      walk(child, out, ctx, styled, pre || tag === 'PRE');
      if (block) out.push(PARAGRAPH_TAGS.has(tag) ? '\n\n' : '\n');
    }
  }

  /** Is the element itself, or anything above it, hidden or page chrome? */
  function isExcluded(el, ctx) {
    const chain = [];
    let excluded = false;
    for (let node = el; node && node.nodeType === 1; node = node.parentElement) {
      const cached = ctx.excluded.get(node);
      if (cached !== undefined) {
        excluded = cached;
        break;
      }
      chain.push(node);
    }
    // Resolve from the top down, so every ancestor's answer is remembered.
    for (let index = chain.length - 1; index >= 0; index -= 1) {
      const node = chain[index];
      const view = node.ownerDocument && node.ownerDocument.defaultView;
      excluded = excluded || shouldSkip(node, ctx, Boolean(view));
      ctx.excluded.set(node, excluded);
    }
    return excluded;
  }

  /** Text a sighted user would actually see: hidden and chrome elements excluded. */
  function visibleText(element, ctx) {
    if (!element) return '';
    // A block inside a footer, a cookie banner or a hidden panel is not content.
    if (isExcluded(element, ctx)) return '';
    const out = [];
    walk(element, out, ctx, true, false);
    return cleanText(out.join(''));
  }

  function stripHtml(html) {
    // The description in JSON-LD is frequently HTML, sometimes entity-encoded
    // twice. Parsing it in a detached document means no script in it can run.
    const toText = (markup) => {
      const parsed = new DOMParser().parseFromString(String(markup), 'text/html');
      const out = [];
      walk(parsed.body, out, newContext(), false, false);
      return cleanText(out.join(''));
    };
    let text = toText(html);
    if (/<\/?(p|br|li|ul|ol|div|span|strong|em|b|h[1-6])\b[^>]*>/i.test(text)) text = toText(text);
    return text;
  }

  /** Is this control something a person could see and use right now? */
  function isShown(el) {
    return safe(() => {
      if (!el || !el.isConnected) return false;
      if (typeof el.checkVisibility === 'function') {
        return el.checkVisibility({ checkOpacity: false, checkVisibilityCSS: true });
      }
      for (let node = el; node && node.nodeType === 1; node = node.parentElement) {
        if (cssHidden(node)) return false;
      }
      return true;
    }, false);
  }

  function inChrome(el) {
    return Boolean(
      el.closest('nav, footer, [role="navigation"], [role="contentinfo"], [role="search"], [role="banner"]')
    );
  }

  function inConsent(el, ctx) {
    for (let node = el; node && node.nodeType === 1; node = node.parentElement) {
      if (isNoiseNode(node, ctx) && isConsentNode(node)) return true;
    }
    return false;
  }

  // -------------------------------------------------------------------------
  // Title / company clean-up
  // -------------------------------------------------------------------------

  const SITE_SUFFIX_RE = new RegExp(
    '\\s*[|\u00b7\\-\u2013\u2014]\\s*(LinkedIn|Indeed|Glassdoor|Greenhouse|Lever|Ashby|Built ?In|Jobright(\\.ai)?|' +
      'Hiring ?Cafe|hiring\\.cafe|Workday|Workable|SmartRecruiters|iCIMS|ZipRecruiter)\\s*$',
    'i'
  );

  /**
   * Split the common "Role at Company" / "Role - Company" title patterns.
   * Conservative: an ambiguous title yields only a role.
   */
  function splitTitleAndCompany(value) {
    if (!value) return {};
    const cleaned = oneLine(value);

    // Strip common trailing site names, and Greenhouse's title prefix.
    const withoutSuffix = cleaned
      .replace(SITE_SUFFIX_RE, '')
      .replace(/^job application for\s+/i, '')
      .trim();
    if (!withoutSuffix) return {};

    const atMatch = withoutSuffix.match(/^(.+?)\s+(?:at|@)\s+(.+?)$/i);
    if (atMatch && atMatch[1] && atMatch[2]) {
      return { title: atMatch[1].trim(), company: atMatch[2].trim() };
    }

    const dashMatch = withoutSuffix.match(/^(.+?)\s+[-\u2013|]\s+(.+?)$/);
    if (dashMatch && dashMatch[1] && dashMatch[2] && dashMatch[2].length < 60) {
      return { title: dashMatch[1].trim(), company: dashMatch[2].trim() };
    }

    return { title: withoutSuffix };
  }

  // The company is the HIRING company, never the board the job is listed on.
  const BOARD_NAMES = new Set([
    'jobright',
    'jobrightai',
    'hiringcafe',
    'greenhouse',
    'lever',
    'ashby',
    'ashbyhq',
    'workday',
    'myworkdayjobs',
    'linkedin',
    'indeed',
    'glassdoor',
    'ziprecruiter',
    'builtin',
    'smartrecruiters',
    'icims',
    'workable',
    'monster',
    'dice',
    'careers',
    'jobs',
  ]);

  function cleanCompany(value) {
    let company = clean(value || '', MAX_FIELD_LENGTH)
      .replace(/^(at|@)\s+/i, '')
      .replace(/\s+(logo|careers|jobs)$/i, '')
      .trim();
    if (!company) return '';
    const key = company.toLowerCase().replace(/[^a-z0-9]/g, '');
    if (!key || BOARD_NAMES.has(key)) return '';
    return company;
  }

  function firstCompany(candidates) {
    for (const candidate of candidates) {
      const company = cleanCompany(candidate);
      if (company) return company;
    }
    return '';
  }

  // -------------------------------------------------------------------------
  // 1. JSON-LD JobPosting
  // -------------------------------------------------------------------------

  /** Walk a JSON-LD document, including @graph arrays, for a JobPosting node. */
  function findJobPosting(node, depth) {
    if (depth > 6 || node === null || typeof node !== 'object') return null;

    if (Array.isArray(node)) {
      for (const item of node) {
        const found = findJobPosting(item, depth + 1);
        if (found) return found;
      }
      return null;
    }

    const type = node['@type'];
    const typeMatches = Array.isArray(type)
      ? type.some((value) => String(value).toLowerCase() === 'jobposting')
      : String(type == null ? '' : type).toLowerCase() === 'jobposting';
    if (typeMatches) return node;

    for (const key of ['@graph', 'mainEntity', 'mainEntityOfPage', 'itemListElement', 'item']) {
      if (key in node) {
        const found = findJobPosting(node[key], depth + 1);
        if (found) return found;
      }
    }
    return null;
  }

  function parseJson(raw) {
    try {
      return JSON.parse(raw);
    } catch (error) {
      // Malformed structured data is common in the wild. The usual fault is a
      // raw line break inside a string; one repair attempt, then give up.
      try {
        return JSON.parse(String(raw).replace(/[\u0000-\u001f]+/g, ' '));
      } catch (again) {
        return undefined;
      }
    }
  }

  function fromJsonLd(doc) {
    const scripts = Array.from(doc.querySelectorAll('script[type="application/ld+json"]'));
    for (const script of scripts) {
      const raw = script.textContent;
      if (!raw || raw.length > 2000000) continue;
      const parsed = parseJson(raw);
      if (parsed === undefined) continue;

      const posting = findJobPosting(parsed, 0);
      if (!posting) continue;

      let hiring = posting.hiringOrganization;
      if (Array.isArray(hiring)) hiring = hiring[0];
      const company =
        typeof hiring === 'string'
          ? hiring
          : typeof hiring === 'object' && hiring !== null
            ? String(hiring.name == null ? '' : hiring.name)
            : '';
      const title =
        typeof posting.title === 'string' ? posting.title : typeof posting.name === 'string' ? posting.name : '';
      const rawDescription = typeof posting.description === 'string' ? posting.description : '';

      return {
        jobTitle: clean(title, MAX_FIELD_LENGTH),
        company: clean(company, MAX_FIELD_LENGTH),
        jobDescription: rawDescription ? safe(() => stripHtml(rawDescription), '') : '',
        htmlLength: rawDescription.length,
      };
    }
    return null;
  }

  // -------------------------------------------------------------------------
  // 2. Site adapters (hints, never dependencies: each may return nothing and
  //    the generic extractor fills whatever is left empty)
  // -------------------------------------------------------------------------

  function text(doc, selectors) {
    for (const selector of selectors) {
      const element = safe(() => doc.querySelector(selector), null);
      if (!element) continue;
      const value =
        tagOf(element) === 'IMG'
          ? oneLine(element.getAttribute('alt'))
          : tagOf(element) === 'META'
            ? oneLine(element.getAttribute('content'))
            : oneLine(element.textContent);
      if (value) return value;
    }
    return '';
  }

  function block(doc, selectors, ctx) {
    for (const selector of selectors) {
      const element = safe(() => doc.querySelector(selector), null);
      if (!element) continue;
      const value = visibleText(element, ctx);
      if (value.length > MIN_DESCRIPTION_LENGTH) return { text: value, element: element };
    }
    return null;
  }

  const JOB_KEYWORDS = [
    'responsibilities',
    'requirements',
    'qualification',
    'about the role',
    'about the job',
    'what you will do',
    "what you'll do",
    'who you are',
    'experience with',
    'we are looking for',
    'benefits',
    'job description',
    'minimum qualifications',
    'preferred qualifications',
    "what you'll bring",
    'what we offer',
    'about you',
    'nice to have',
  ];

  function jobScore(value) {
    const lower = value.toLowerCase();
    const hits = JOB_KEYWORDS.filter((keyword) => lower.includes(keyword)).length;
    // Favour keyword density over raw length so a whole-page match does not
    // beat the actual description block.
    return { hits: hits, score: hits * 1000 + Math.min(value.length, 20000) / 100 };
  }

  /**
   * For sites whose markup we cannot rely on: of everything the selectors
   * match, the block that reads most like a job description.
   */
  function bestBlock(doc, selectors, ctx) {
    let best = null;
    for (const selector of selectors) {
      const elements = safe(() => Array.from(doc.querySelectorAll(selector)).slice(0, 40), []);
      for (const element of elements) {
        if ((element.textContent || '').length < MIN_DESCRIPTION_LENGTH) continue;
        const value = visibleText(element, ctx);
        if (value.length <= MIN_DESCRIPTION_LENGTH || value.length > 60000) continue;
        const score = jobScore(value).score;
        if (!best || score > best.score) best = { text: value, element: element, score: score };
      }
    }
    return best;
  }

  /**
   * Next.js sites (jobright, hiring.cafe) ship the page's data as JSON. It is
   * parsed, never evaluated, and only a few well-named keys are read.
   */
  const NEXT_TITLE_KEYS = new Set(['jobtitle']);
  const NEXT_COMPANY_KEYS = new Set(['companyname']);
  const NEXT_DESCRIPTION_KEYS = new Set(['jobdescription', 'description', 'descriptionhtml', 'jobsummary']);
  const NEXT_APPLY_KEYS = new Set([
    'applylink',
    'applyurl',
    'applicationurl',
    'originalurl',
    'sourceurl',
    'externalurl',
    'joburl',
    'applyredirecturl',
  ]);
  const NEXT_SOURCE_KEYS = new Set(['source', 'jobsource', 'sourcename', 'publisher', 'jobboard', 'sourcetype']);

  function nextDataHints(doc) {
    const hints = { title: '', company: '', description: '', applyUrls: [], linkedinSource: false };
    const script = doc.querySelector('script#__NEXT_DATA__');
    const raw = script && script.textContent;
    if (!raw || raw.length > 3000000) return hints;
    const data = parseJson(raw);
    if (!data || typeof data !== 'object') return hints;

    let budget = 20000;
    const visit = (node, depth) => {
      if (budget-- <= 0 || depth > 14 || node === null || typeof node !== 'object') return;
      if (Array.isArray(node)) {
        for (const item of node.slice(0, 50)) visit(item, depth + 1);
        return;
      }
      for (const key of Object.keys(node)) {
        const value = node[key];
        if (typeof value === 'string') {
          const name = key.toLowerCase().replace(/[_-]/g, '');
          if (NEXT_TITLE_KEYS.has(name) && !hints.title) hints.title = clean(value, MAX_FIELD_LENGTH);
          else if (NEXT_COMPANY_KEYS.has(name) && !hints.company) hints.company = clean(value, MAX_FIELD_LENGTH);
          else if (NEXT_DESCRIPTION_KEYS.has(name) && value.length > hints.description.length) {
            hints.description = value;
          } else if (NEXT_APPLY_KEYS.has(name) && isHttpUrl(value) && hints.applyUrls.length < 5) {
            hints.applyUrls.push(value);
          } else if (NEXT_SOURCE_KEYS.has(name) && /^linkedin(\.com)?$/i.test(value.trim())) {
            hints.linkedinSource = true;
          }
        } else if (value && typeof value === 'object') {
          visit(value, depth + 1);
        }
      }
    };
    visit(data.props || data, 0);
    if (hints.description) hints.description = safe(() => stripHtml(hints.description), '');
    return hints;
  }

  const ADAPTERS = [
    {
      name: 'linkedin',
      hosts: ['linkedin.com'],
      extract: (doc, url, ctx) => ({
        jobTitle: text(doc, [
          '.job-details-jobs-unified-top-card__job-title',
          '.top-card-layout__title',
          'h1.topcard__title',
          'h1',
        ]),
        company: text(doc, [
          '.job-details-jobs-unified-top-card__company-name',
          '.topcard__org-name-link',
          '.topcard__flavor',
        ]),
        description: block(
          doc,
          [
            '.jobs-description__content',
            '.jobs-box__html-content',
            '.description__text',
            '.show-more-less-html__markup',
          ],
          ctx
        ),
      }),
    },
    {
      name: 'indeed',
      hosts: ['indeed.com'],
      extract: (doc, url, ctx) => ({
        jobTitle: text(doc, [
          '[data-testid="jobsearch-JobInfoHeader-title"]',
          '.jobsearch-JobInfoHeader-title',
          'h1',
        ]),
        company: text(doc, [
          '[data-testid="inlineHeader-companyName"]',
          '[data-company-name="true"]',
          '.jobsearch-InlineCompanyRating div:first-child',
        ]),
        description: block(doc, ['#jobDescriptionText', '.jobsearch-jobDescriptionText'], ctx),
      }),
    },
    {
      name: 'greenhouse',
      hosts: ['greenhouse.io'],
      extract: (doc, url, ctx) => ({
        jobTitle: text(doc, ['.app-title', '.job__title h1', 'h1.section-header', 'h1']),
        company: text(doc, ['.company-name', '#header .company-name', '.logo img[alt]']),
        description: block(doc, ['.job__description', '#content', '.content'], ctx),
      }),
    },
    {
      name: 'lever',
      hosts: ['lever.co'],
      extract: (doc, url, ctx) => ({
        jobTitle: text(doc, ['.posting-headline h2', 'h2']),
        company: text(doc, ['.main-header-logo img[alt]']),
        description: block(doc, ['.posting-page', '.section-wrapper.page-full-width', '.content'], ctx),
      }),
    },
    {
      name: 'workday',
      hosts: ['workday.com', 'myworkdayjobs.com', 'myworkdaysite.com'],
      extract: (doc, url, ctx) => ({
        jobTitle: text(doc, ['[data-automation-id="jobPostingHeader"]', 'h1', 'h2']),
        company: '',
        description: block(
          doc,
          ['[data-automation-id="jobPostingDescription"]', '[data-automation-id="job-posting-details"]'],
          ctx
        ),
      }),
    },
    {
      name: 'ashby',
      hosts: ['ashbyhq.com'],
      extract: (doc, url, ctx) => ({
        jobTitle: text(doc, ['h1', '[class*="jobPostingHeader"] h1']),
        company: text(doc, ['[class*="companyName"]', 'header img[alt]']),
        description: block(doc, ['[class*="jobPostingDescription"]', '[class*="descriptionText"]', 'main'], ctx),
      }),
    },
    {
      name: 'smartrecruiters',
      hosts: ['smartrecruiters.com'],
      extract: (doc, url, ctx) => ({
        jobTitle: text(doc, ['h1.job-title', '[itemprop="title"]', 'h1']),
        company: text(doc, [
          '[itemprop="hiringOrganization"] [itemprop="name"]',
          'meta[itemprop="hiringOrganization"]',
          '.header-logo img[alt]',
        ]),
        description: block(
          doc,
          ['[itemprop="description"]', '.job-sections', '#st-jobDescription', '.jobad-main', 'main'],
          ctx
        ),
      }),
    },
    {
      name: 'icims',
      hosts: ['icims.com'],
      extract: (doc, url, ctx) => ({
        jobTitle: text(doc, ['.iCIMS_Header h1', 'h1.iCIMS_Header', '.iCIMS_JobHeader h1', 'h1']),
        company: '',
        description: block(doc, ['.iCIMS_JobContent', '.iCIMS_InfoMsg_Job', '#jobcontent', '.iCIMS_MainWrapper'], ctx),
      }),
    },
    {
      name: 'workable',
      hosts: ['workable.com'],
      extract: (doc, url, ctx) => ({
        jobTitle: text(doc, ['[data-ui="job-title"]', 'h1']),
        company: text(doc, ['[data-ui="company-name"]', 'header img[alt]', 'a[data-ui="company-logo"] img[alt]']),
        description: block(
          doc,
          ['[data-ui="job-description"]', '[data-ui="job-breakdown"]', 'main section', 'main'],
          ctx
        ),
      }),
    },
    {
      name: 'builtin',
      hosts: ['builtin.com'],
      extract: (doc, url, ctx) => ({
        jobTitle: text(doc, ['h1', '[data-id="job-title"]']),
        company: text(doc, ['[data-id="company-title"]', '.company-title']),
        description: block(doc, ['[data-id="job-description"]', '.job-description'], ctx),
      }),
    },
    {
      name: 'ziprecruiter',
      hosts: ['ziprecruiter.com'],
      extract: (doc, url, ctx) => ({
        jobTitle: text(doc, ['h1.job_title', 'h1']),
        company: text(doc, ['a.hiring_company_text', '.hiring_company_text']),
        description: block(doc, ['.job_description', '[class*="jobDescription"]'], ctx),
      }),
    },
    {
      // jobright.ai job detail page. Written from DOM heuristics, not from the
      // live markup: headings, "job"-style class hooks and the page's own data
      // blob. Whatever it misses, the generic extractor fills in.
      name: 'jobright',
      hosts: ['jobright.ai'],
      extract: (doc, url, ctx) => {
        const hints = safe(() => nextDataHints(doc), null) || {};
        const description = bestBlock(
          doc,
          [
            '[class*="job-detail" i]',
            '[class*="jobdetail" i]',
            '[class*="job_detail" i]',
            '[class*="job-description" i]',
            '[class*="jobdescription" i]',
            '[class*="job-content" i]',
            '[class*="job-info" i]',
            '[id*="job-detail" i]',
            '[class*="description" i]',
            'main',
            '[role="main"]',
            'article',
          ],
          ctx
        );
        return {
          jobTitle:
            text(doc, ['h1']) ||
            hints.title ||
            text(doc, ['[class*="job-title" i]', '[class*="jobtitle" i]', '[class*="job_title" i]']),
          company:
            cleanCompany(hints.company) ||
            text(doc, [
              '[class*="company-name" i]',
              '[class*="companyname" i]',
              '[class*="company_name" i]',
              '[class*="company-title" i]',
              '[class*="company" i] h2',
              '[class*="company" i] a',
            ]),
          description:
            description ||
            (hints.description && hints.description.length > MIN_DESCRIPTION_LENGTH
              ? { text: hints.description, element: null }
              : null),
        };
      },
    },
    {
      // hiring.cafe job view. Utility-class markup with no stable hooks, so:
      // the typography ("prose") block or the block that reads most like a JD.
      name: 'hiring_cafe',
      hosts: ['hiring.cafe', 'hiringcafe.com'],
      extract: (doc, url, ctx) => {
        const hints = safe(() => nextDataHints(doc), null) || {};
        const description = bestBlock(
          doc,
          [
            '[class*="prose" i]',
            'article',
            '[class*="job-description" i]',
            '[class*="jobdescription" i]',
            '[class*="description" i]',
            '[role="dialog"]',
            'main',
            '[role="main"]',
          ],
          ctx
        );
        return {
          jobTitle: text(doc, ['h1', '[role="dialog"] h2', 'main h2']) || hints.title,
          company:
            cleanCompany(hints.company) ||
            text(doc, ['[class*="company-name" i]', '[class*="companyname" i]', '[class*="company" i] a']),
          description:
            description ||
            (hints.description && hints.description.length > MIN_DESCRIPTION_LENGTH
              ? { text: hints.description, element: null }
              : null),
        };
      },
    },
    {
      // Generic careers page: matches no hostname. It exists so a company site
      // with an obvious job-content container is handled a little better than
      // the pure heuristic scan.
      name: 'careers-page',
      test: (url) => /\/(careers?|jobs?|opportunities|openings|vacancies)\b/i.test(url),
      extract: (doc, url, ctx) => {
        const description = block(
          doc,
          [
            '[class*="job-description"]',
            '[class*="jobDescription"]',
            '[id*="job-description"]',
            '[class*="posting-content"]',
            '[class*="vacancy"]',
          ],
          ctx
        );
        if (!description) return null;
        const split = splitTitleAndCompany(doc.title);
        return {
          jobTitle: text(doc, ['h1']) || split.title || '',
          company: split.company || '',
          description: description,
        };
      },
    },
  ];

  function findAdapter(url) {
    for (const adapter of ADAPTERS) {
      const matches = adapter.hosts ? hostMatches(url, adapter.hosts) : adapter.test(url);
      if (matches) return adapter;
    }
    return null;
  }

  // -------------------------------------------------------------------------
  // 3. Generic extraction: meta tags, semantic landmarks, heuristic scan,
  //    document title, URL
  // -------------------------------------------------------------------------

  function fromMetaTags(doc) {
    const meta = (selector) => {
      const element = doc.querySelector(selector);
      const content = element && element.getAttribute('content');
      return (content && content.trim()) || '';
    };

    const ogTitle = meta('meta[property="og:title"]') || meta('meta[name="twitter:title"]');
    const ogSite = meta('meta[property="og:site_name"]');
    const description = meta('meta[property="og:description"]') || meta('meta[name="description"]');
    if (!ogTitle && !ogSite && !description) return null;

    const split = splitTitleAndCompany(ogTitle);
    return {
      jobTitle: split.title || '',
      company: split.company || ogSite,
      // A meta description is a summary, not the posting. Only used when
      // nothing better turns up.
      description: description && description.length > 400 ? { text: cleanText(description), element: null } : null,
    };
  }

  function fromSemanticHtml(doc, ctx) {
    const containers = Array.from(doc.querySelectorAll('[itemtype*="JobPosting"]')).concat(
      Array.from(doc.querySelectorAll('main, [role="main"], article'))
    );

    let best = null;
    for (const element of containers.slice(0, 60)) {
      const value = visibleText(element, ctx);
      if (value.length < MIN_DESCRIPTION_LENGTH) continue;
      if (!best || value.length > best.text.length) best = { text: value, element: element };
    }
    if (!best) return null;

    const heading = best.element.querySelector('h1') || doc.querySelector('h1');
    return { jobTitle: heading ? oneLine(heading.textContent) : '', description: best };
  }

  /**
   * Score candidate blocks by length and job-vocabulary density, and take the
   * best. This is what rescues pages that use no semantic landmarks at all.
   */
  function fromHeuristicScan(doc, ctx) {
    const candidates = Array.from(doc.querySelectorAll('div, section, article, td'));
    let best = null;

    for (const element of candidates.slice(0, 6000)) {
      // Skip containers that are mostly other containers: their text is
      // already covered by a more specific child.
      if (element.children.length > 60) continue;
      const rawLength = (element.textContent || '').length;
      if (rawLength < MIN_DESCRIPTION_LENGTH || rawLength > 400000) continue;

      const value = visibleText(element, ctx);
      if (value.length < MIN_DESCRIPTION_LENGTH || value.length > 60000) continue;

      const scored = jobScore(value);
      if (scored.hits === 0) continue;
      if (!best || scored.score > best.score) best = { score: scored.score, text: value, element: element };
    }

    return best ? { description: { text: best.text, element: best.element } } : null;
  }

  function fromHeading(doc) {
    const heading = doc.querySelector('h1');
    const value = heading ? oneLine(heading.textContent) : '';
    return value ? { jobTitle: value } : null;
  }

  function fromDocumentTitle(doc) {
    const title = (doc.title || '').trim();
    if (!title) return null;
    const split = splitTitleAndCompany(title);
    return { jobTitle: split.title || '', company: split.company || '' };
  }

  const GENERIC_SUBDOMAINS = new Set([
    'www',
    'careers',
    'career',
    'jobs',
    'job',
    'apply',
    'boards',
    'work',
    'join',
    'hire',
    'hiring',
    'recruiting',
    'talent',
    'app',
    'en',
  ]);

  function prettySlug(slug) {
    return String(slug)
      .split(/[-_.]+/)
      .filter(Boolean)
      .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
      .join(' ');
  }

  function fromUrl(url) {
    try {
      const parsed = new URL(url);
      if (!/^https?:$/.test(parsed.protocol)) return null;
      const host = parsed.hostname.toLowerCase().replace(/^www\./, '');
      const segments = parsed.pathname.split('/').filter(Boolean);

      // On a hosted board the employer is a path or subdomain slug.
      if (/(^|\.)greenhouse\.io$|^jobs\.(eu\.)?lever\.co$|^jobs\.ashbyhq\.com$|^apply\.workable\.com$/.test(host)) {
        const slug = segments[0];
        if (slug && !/^(embed|j|jobs?)$/i.test(slug) && /^[A-Za-z0-9._-]{2,60}$/.test(slug)) {
          return { company: prettySlug(slug) };
        }
        return null;
      }
      if (/\.(myworkdayjobs\.com|myworkdaysite\.com|icims\.com|workable\.com|bamboohr\.com)$/.test(host)) {
        const slug = host.split('.')[0].replace(/^(careers?|jobs?)-/, '');
        return slug && slug.length >= 2 && !GENERIC_SUBDOMAINS.has(slug) ? { company: prettySlug(slug) } : null;
      }

      // Only a company-owned domain tells us anything; a job board's domain is
      // the board, not the employer.
      const boards = [
        'linkedin.com',
        'indeed.com',
        'greenhouse.io',
        'lever.co',
        'ashbyhq.com',
        'workday.com',
        'myworkdayjobs.com',
        'ziprecruiter.com',
        'builtin.com',
        'glassdoor.com',
        'monster.com',
        'dice.com',
        'smartrecruiters.com',
        'jobright.ai',
        'hiring.cafe',
        'hiringcafe.com',
      ];
      if (boards.some((board) => host === board || host.endsWith('.' + board))) return null;

      const labels = host.split('.');
      while (labels.length > 2 && GENERIC_SUBDOMAINS.has(labels[0])) labels.shift();
      const name = labels[0];
      if (!name || name.length < 2) return null;
      return { company: name.charAt(0).toUpperCase() + name.slice(1) };
    } catch (error) {
      return null;
    }
  }

  function extractGeneric(doc, url, ctx, ld) {
    const result = { jobTitle: '', titleSource: '', companies: [], description: null, descriptionKind: '' };

    // Fill only what is still empty, so earlier (better) sources win.
    const merge = (source, kind) => {
      if (!source) return;
      if (!result.jobTitle && source.jobTitle) {
        result.jobTitle = clean(source.jobTitle, MAX_FIELD_LENGTH);
        result.titleSource = kind;
      }
      if (source.company) result.companies.push(source.company);
      const current = result.description ? result.description.text.length : 0;
      if (
        current < MIN_DESCRIPTION_LENGTH &&
        source.description &&
        source.description.text &&
        source.description.text.length > current
      ) {
        result.description = source.description;
        result.descriptionKind = kind;
      }
    };

    if (ld) {
      merge(
        {
          jobTitle: ld.jobTitle,
          company: ld.company,
          description: ld.jobDescription ? { text: ld.jobDescription, element: null } : null,
        },
        'ld'
      );
    }
    merge(
      safe(() => fromMetaTags(doc), null),
      'meta'
    );
    merge(
      safe(() => fromSemanticHtml(doc, ctx), null),
      'h1'
    );
    merge(
      safe(() => fromHeuristicScan(doc, ctx), null),
      'scan'
    );
    merge(
      safe(() => fromHeading(doc), null),
      'h1'
    );
    merge(
      safe(() => fromDocumentTitle(doc), null),
      'title'
    );
    merge(
      safe(() => fromUrl(url), null),
      'url'
    );
    return result;
  }

  // -------------------------------------------------------------------------
  // One document: LD-JSON, then the adapter, then generic
  // -------------------------------------------------------------------------

  function extractDocument(doc, url, ctx) {
    const ld = safe(() => fromJsonLd(doc), null);
    const adapter = findAdapter(url);

    let adapted = null;
    if (adapter) {
      // An adapter throwing on unexpected markup must not break extraction.
      adapted = safe(() => adapter.extract(doc, url, ctx), null);
    }
    const generic = extractGeneric(doc, url, ctx, ld);

    let role = '';
    let roleSource = '';
    if (ld && ld.jobTitle) {
      role = ld.jobTitle;
      roleSource = 'ld';
    } else if (adapted && adapted.jobTitle) {
      role = clean(adapted.jobTitle, MAX_FIELD_LENGTH);
      roleSource = 'adapter';
    } else {
      role = generic.jobTitle;
      roleSource = generic.titleSource;
    }

    const company = firstCompany(
      [ld ? ld.company : '', adapted ? adapted.company : ''].concat(generic.companies)
    );

    const adapterText = adapted && adapted.description ? adapted.description.text : '';
    const genericText = generic.description ? generic.description.text : '';
    const ldText = ld ? ld.jobDescription : '';

    // The DOM description. A known selector names the description itself, where
    // the generic scan tends to take the block around it (title, location and
    // all). But an adapter selector can match a near-empty container on a page
    // variant, so it only wins while it holds a fair share of what generic found.
    const domFromGeneric = generic.descriptionKind !== 'ld';
    let dom = null;
    if (adapterText && adapterText.length >= (domFromGeneric ? genericText.length * 0.5 : 0)) {
      dom = { text: adapterText, element: adapted.description.element, source: 'adapter:' + adapter.name };
    } else if (domFromGeneric && genericText) {
      dom = { text: genericText, element: generic.description.element, source: 'generic' };
    }

    // A schema.org JobPosting is the site telling us the description directly,
    // so it outranks layout inference, unless it is only a teaser of a much
    // longer description that is on the page.
    let chosen = null;
    const ldIsTeaser = ldText.length < 600 && dom && dom.text.length > ldText.length * 2;
    if (ldText.length >= MIN_DESCRIPTION_LENGTH && !ldIsTeaser) {
      chosen = { text: ldText, element: null, source: 'ldjson', htmlLength: ld.htmlLength };
    } else if (dom && dom.text.length >= ldText.length) {
      chosen = dom;
    } else if (ldText) {
      chosen = { text: ldText, element: null, source: 'ldjson', htmlLength: ld.htmlLength };
    }

    const jdText = chosen ? cleanText(chosen.text).slice(0, MAX_JD_LENGTH) : '';
    let htmlLength = 0;
    if (chosen) {
      htmlLength = chosen.element
        ? safe(() => chosen.element.innerHTML.length, jdText.length)
        : chosen.htmlLength || jdText.length;
    }

    return {
      role: role,
      roleSource: roleSource,
      // A role named by the site's data, a known selector or the page's own
      // heading. The tab title and the address alone are not evidence that
      // the posting rendered.
      strongRole: Boolean(role) && (roleSource === 'ld' || roleSource === 'adapter' || hasHeading(doc)),
      company: company,
      jdText: jdText,
      jdElement: chosen ? chosen.element : null,
      jdHtmlLength: htmlLength,
      hasLd: Boolean(ld),
      source: chosen ? chosen.source : ld && ld.jobTitle ? 'ldjson' : 'generic',
    };
  }

  function hasHeading(doc) {
    return safe(() => Boolean(oneLine((doc.querySelector('h1') || {}).textContent)), false);
  }

  function sameOriginFrames(doc) {
    const frames = [];
    for (const frame of Array.from(doc.querySelectorAll('iframe, frame')).slice(0, MAX_IFRAMES)) {
      // contentDocument is null (or throws) for a cross-origin frame.
      const inner = safe(() => frame.contentDocument, null);
      if (inner && inner.body) frames.push(inner);
    }
    return frames;
  }

  // -------------------------------------------------------------------------
  // Form fields and prose
  // -------------------------------------------------------------------------

  const NON_FIELD_INPUT_TYPES = new Set(['hidden', 'submit', 'button', 'image', 'reset', 'search']);

  function countFormFields(doc, ctx) {
    const seen = new Set();
    const groups = new Set();
    let count = 0;
    const controls = Array.from(
      doc.querySelectorAll('input, select, textarea, [contenteditable="true"], [role="textbox"], [role="combobox"]')
    ).slice(0, 2000);

    for (const control of controls) {
      if (seen.has(control)) continue;
      seen.add(control);
      const tag = tagOf(control);
      const type = String(control.getAttribute('type') || 'text').toLowerCase();
      if (tag === 'INPUT' && NON_FIELD_INPUT_TYPES.has(type)) continue;
      if (inChrome(control) || inConsent(control, ctx)) continue;
      // File inputs are routinely hidden behind a styled "Attach" button.
      if (!(tag === 'INPUT' && type === 'file') && !isShown(control)) continue;
      if (tag === 'INPUT' && (type === 'radio' || type === 'checkbox')) {
        const name = control.getAttribute('name');
        if (name) {
          if (groups.has(type + ':' + name)) continue;
          groups.add(type + ':' + name);
        }
      }
      count += 1;
    }
    return count;
  }

  /** Characters of descriptive prose outside form controls, labels and chrome. */
  function proseLength(doc, ctx) {
    if (!doc.body) return 0;
    const lines = visibleText(doc.body, ctx).split('\n');
    let total = 0;
    for (const line of lines) {
      if (line.length >= PROSE_LINE_MIN) total += line.length;
    }
    return total;
  }

  // -------------------------------------------------------------------------
  // Apply links and the LinkedIn source
  // -------------------------------------------------------------------------

  const APPLY_TEXT_RE = /\bapply\b/i;

  function controlText(el) {
    const tag = tagOf(el);
    let value = '';
    if (tag === 'INPUT') value = el.value || '';
    else value = typeof el.innerText === 'string' && el.innerText.trim() ? el.innerText : el.textContent || '';
    value = oneLine(value);
    if (!value) value = oneLine(el.getAttribute('aria-label') || el.getAttribute('title') || '');
    return value;
  }

  function collectApplyLinks(doc, ctx) {
    const links = [];
    for (const anchor of Array.from(doc.querySelectorAll('a[href]')).slice(0, 3000)) {
      const href = safe(() => anchor.href, '');
      if (!isHttpUrl(href)) continue;
      if (inConsent(anchor, ctx)) continue;
      const label = controlText(anchor);
      const hooks = classAndId(anchor) + ' ' + (anchor.getAttribute('data-testid') || '');
      const byText = label.length <= 60 && APPLY_TEXT_RE.test(label);
      const byHook = /(^|[^a-z])apply/i.test(hooks) && label.length <= 60;
      if (!byText && !byHook) continue;
      if (links.indexOf(href) === -1) links.push(href);
      if (links.length >= MAX_APPLY_LINKS) break;
    }
    return links;
  }

  /** A link INTO a LinkedIn job, not a share button or "Apply with LinkedIn" sign-in. */
  function isLinkedInJobUrl(href) {
    try {
      const url = new URL(href);
      const host = url.hostname.toLowerCase();
      if (host === 'lnkd.in') return true;
      if (host !== 'linkedin.com' && !host.endsWith('.linkedin.com')) return false;
      return !/^\/(oauth|sharing|sharearticle|share|uas|login|signup|company|in|school|feed)(\/|$)/i.test(
        url.pathname
      );
    } catch (error) {
      return false;
    }
  }

  const LINKEDIN_LABEL_RE =
    /^(job )?(source|via|from|posted (on|via|from)|originally (posted )?(on|from)|found on|sourced from)\s*:?\s*linkedin(\.com)?$/i;

  function hasLinkedInSourceLabel(doc) {
    const candidates = Array.from(doc.querySelectorAll('span, a, p, div, small, li, label, dd, td, b, strong')).slice(
      0,
      8000
    );
    for (const el of candidates) {
      const raw = el.textContent || '';
      if (raw.length > 60 || !/linkedin/i.test(raw)) continue;
      if (!LINKEDIN_LABEL_RE.test(oneLine(raw))) continue;
      if (inChrome(el) || !isShown(el)) continue;
      return true;
    }
    // A "source" slot holding only the LinkedIn name or logo.
    const slots = Array.from(
      doc.querySelectorAll(
        '[class*="source" i], [class*="origin" i], [class*="publisher" i], [data-source], [class*="posted-from" i]'
      )
    ).slice(0, 200);
    for (const slot of slots) {
      if (inChrome(slot) || !isShown(slot)) continue;
      if (/^linkedin(\.com)?$/i.test(oneLine(slot.getAttribute('data-source') || ''))) return true;
      const label = oneLine(slot.textContent);
      if (label.length <= 40 && /^(source\s*:?\s*)?linkedin(\.com)?$/i.test(label)) return true;
      const logo = slot.querySelector('img[alt]');
      if (logo && label.length <= 40 && /^linkedin( logo)?$/i.test(oneLine(logo.getAttribute('alt')))) return true;
    }
    return false;
  }

  // -------------------------------------------------------------------------
  // extract()
  // -------------------------------------------------------------------------

  let lastJdElement = null;

  function emptyExtraction() {
    return {
      url: safe(() => String(window.location.href), ''),
      title: safe(() => clean(document.title, 300), ''),
      role: '',
      company: '',
      jd_text: '',
      jd_html_len: 0,
      form_field_count: 0,
      has_jobposting_ldjson: false,
      apply_links: [],
      is_linkedin: false,
      linkedin_source: false,
      prose_len: 0,
      source: 'generic',
    };
  }

  function extractWithDetails() {
    const result = emptyExtraction();
    const details = { strongRole: false };
    const url = result.url;
    const ctx = newContext();

    const top = extractDocument(document, url, ctx);
    let best = top;
    const frames = safe(() => sameOriginFrames(document), []);

    // The posting is sometimes rendered in a same-origin frame (iCIMS, embedded
    // boards). Only looked at when the top document has no description.
    if (top.jdText.length < MIN_DESCRIPTION_LENGTH) {
      for (const frame of frames) {
        const frameUrl = safe(() => String(frame.location.href), '');
        const inner = safe(() => extractDocument(frame, isHttpUrl(frameUrl) ? frameUrl : url, ctx), null);
        if (inner && inner.jdText.length > best.jdText.length) best = inner;
      }
    }

    // The top document names the job unless only the frame really does.
    const roleOwner = best !== top && best.role && (!top.role || (best.strongRole && !top.strongRole)) ? best : top;
    result.role = clean(roleOwner.role, MAX_FIELD_LENGTH);
    details.strongRole = roleOwner.strongRole;
    result.company = clean(top.company || best.company, MAX_FIELD_LENGTH);
    result.jd_text = best.jdText;
    result.jd_html_len = best.jdHtmlLength;
    result.source = best.source;
    result.has_jobposting_ldjson = top.hasLd || best.hasLd;
    lastJdElement = best.jdElement;

    let fields = safe(() => countFormFields(document, ctx), 0);
    let prose = safe(() => proseLength(document, ctx), 0);
    for (const frame of frames) {
      fields += safe(() => countFormFields(frame, ctx), 0);
      prose += safe(() => proseLength(frame, ctx), 0);
    }
    result.form_field_count = fields;
    result.prose_len = prose;

    const links = safe(() => collectApplyLinks(document, ctx), []);
    let linkedinSource = links.some(isLinkedInJobUrl);
    result.is_linkedin = hostMatches(url, ['linkedin.com']);

    if (hostMatches(url, ['jobright.ai', 'hiring.cafe', 'hiringcafe.com'])) {
      const hints = safe(() => nextDataHints(document), null);
      if (hints) {
        for (const href of hints.applyUrls) {
          if (links.indexOf(href) === -1 && links.length < MAX_APPLY_LINKS) links.push(href);
          if (isLinkedInJobUrl(href)) linkedinSource = true;
        }
        if (hints.linkedinSource) linkedinSource = true;
      }
    }
    if (!linkedinSource) linkedinSource = safe(() => hasLinkedInSourceLabel(document), false);

    result.apply_links = links;
    result.linkedin_source = Boolean(linkedinSource);
    return { result: result, details: details };
  }

  function extract() {
    try {
      return extractWithDetails().result;
    } catch (error) {
      return emptyExtraction();
    }
  }

  // -------------------------------------------------------------------------
  // classify()
  // -------------------------------------------------------------------------

  const CAPTCHA_TITLE_RE = new RegExp(
    [
      'just a moment',
      'attention required',
      'access denied',
      'security check',
      'are you (a )?(human|robot)',
      'robot check',
      'human verification',
      "verify (that )?you('re| are) (a )?human",
      'pardon our interruption',
      'bot verification',
      'unusual traffic',
      'you have been blocked',
      'request blocked',
      'checking your browser',
    ].join('|'),
    'i'
  );

  const CAPTCHA_TEXT_RE = new RegExp(
    [
      "verif(y|ying) (that )?you('re| are) (a |not a )?(human|robot)",
      'unusual traffic',
      'are you a robot',
      "i('m| am) not a robot",
      'access (to this (page|site) )?(has been |is |was )?denied',
      'checking (if the site connection is secure|your browser)',
      'complete the (security check|captcha)',
      'press (and|&) hold',
      'you have been blocked',
      'enable javascript and cookies to continue',
      'performing security verification',
      'confirm (that )?you are (a )?human',
      "prove (that )?you('re| are) (a )?human",
    ].join('|'),
    'i'
  );

  const CAPTCHA_PAGE_SELECTOR = [
    '#challenge-form',
    '#challenge-running',
    '#challenge-stage',
    '#cf-challenge-running',
    '.cf-browser-verification',
    'iframe[src*="challenges.cloudflare.com"]',
    '#px-captcha',
    '.px-captcha-container',
    '#distilCaptchaForm',
    'form[action*="captcha" i]',
    'iframe[src*="captcha-delivery.com"]',
  ].join(', ');

  const CAPTCHA_WIDGET_SELECTOR =
    '.g-recaptcha, .h-captcha, iframe[src*="recaptcha"], iframe[src*="hcaptcha.com"], iframe[title*="captcha" i]';

  const AUTH_PATH_RE =
    /(^|\/)(login|log-in|signin|sign-in|sign_in|sso|oauth2?|authwall|authorize|register|signup|sign-up|sign_up|uas)(\/|\.|$)/i;

  const AUTH_HOSTS = [
    'accounts.google.com',
    'login.microsoftonline.com',
    'login.live.com',
    'appleid.apple.com',
    'okta.com',
    'auth0.com',
    'onelogin.com',
  ];

  const AUTH_HEADING_RE =
    /^((sign|log) ?in\b|login\b|sign ?up\b|register\b|create (an |your )?account|join (now|linkedin)|welcome back)/i;

  const AUTH_WALL_RE = new RegExp(
    [
      '(sign|log) ?in (or (sign up|register|join|create an account) )?to (view|see|continue|apply|access|read)',
      'please (sign|log) ?in',
      'you (must|need to) (be )?(sign|log)(ged)? ?in',
      '(create an account|sign up|register|join) to (view|see|continue|apply|access|read)',
    ].join('|'),
    'i'
  );

  const JOB_NOUN = '(job|position|posting|role|listing|vacancy|opening|requisition|opportunity)';

  /** The posting is gone. Strong enough to hold even when other text is on the page. */
  const JOB_GONE_RE = new RegExp(
    [
      '\\b' +
        JOB_NOUN +
        '\\b[^.\\n]{0,60}\\b(no longer (available|open|active|posted|exists?|listed|accepting)|' +
        'has (been )?(filled|closed|removed|expired)|is (closed|expired|filled|unavailable|not available)|' +
        "was (removed|closed|filled)|not found|(does not|doesn't) exist|(could not|couldn't) be found)",
      'no longer accepting applications',
      '\\b' + JOB_NOUN + ' not found\\b',
    ].join('|'),
    'i'
  );

  const HTTP_ERROR_RE = new RegExp(
    [
      '(^|\\b(error|status|http)\\s*:?\\s*)(403|404|410|50[0234])\\b',
      '\\b(403|404|410|50[0234])\\s*[-\u2013:|]?\\s*(error|not found|forbidden|gone|page|bad gateway|unavailable)',
      "\\b(page|url|link)\\b[^.\\n]{0,40}\\b(not found|(could not|couldn't|can't|cannot) be found|" +
        "(does not|doesn't) exist|no longer exists|(is not|isn't) available)",
      '\\b(page not found|not found|forbidden|bad gateway|service unavailable|internal server error)\\b',
      '\\b(gateway time-?out|temporarily unavailable|something went wrong)\\b',
      "this site can(not|'t|\u2019t) be reached",
    ].join('|'),
    'im'
  );

  const CONSENT_TEXT_RE = /cookies?|consent|privacy (preferences|choices|settings)|your privacy|personal data/i;
  const CONSENT_ACCEPT_RE = /\b(accept|agree|allow|got it|i understand|ok(ay)?)\b/i;

  function prominentText(doc) {
    const parts = [doc.title || ''];
    const nodes = Array.from(
      doc.querySelectorAll(
        'h1, h2, h3, [role="alert"], [role="status"], .flash, .alert, [class*="error" i], [class*="notice" i]'
      )
    ).slice(0, 30);
    for (const node of nodes) {
      const value = oneLine(node.textContent);
      if (value && value.length <= 300 && isShown(node)) parts.push(value);
    }
    return parts.join('\n');
  }

  function isFullScreen(el) {
    return safe(() => {
      const view = el.ownerDocument.defaultView;
      const style = view.getComputedStyle(el);
      const positioned = style.position === 'fixed' || style.position === 'absolute';
      if (!positioned) return false;
      // Pinned to all four edges covers the viewport whatever its size is.
      const zero = (value) => parseFloat(value) === 0;
      if (zero(style.top) && zero(style.left) && zero(style.right) && zero(style.bottom)) return true;
      const width = view.innerWidth;
      const height = view.innerHeight;
      if (!width || !height) return false;
      const rect = el.getBoundingClientRect();
      return rect.width * rect.height >= width * height * 0.6;
    }, false);
  }

  /** The visible consent/cookie containers on the page (outermost only). */
  function findConsentNodes(doc, ctx) {
    const found = [];
    const candidates = safe(() => Array.from(doc.querySelectorAll(CONSENT_SELECTOR)).slice(0, 200), []);
    const dialogs = safe(
      () => Array.from(doc.querySelectorAll('[role="dialog"], [role="alertdialog"], [aria-modal="true"], dialog[open]')),
      []
    );
    for (const el of candidates.concat(dialogs.slice(0, 20))) {
      if (found.some((other) => other === el || other.contains(el))) continue;
      const label = oneLine(el.textContent);
      const hooked = safe(() => isConsentNode(el), false);
      const dialogLike = dialogs.indexOf(el) !== -1;
      if (!hooked && !(dialogLike && CONSENT_TEXT_RE.test(label) && label.length <= 8000)) continue;
      if (!CONSENT_TEXT_RE.test(label + ' ' + classAndId(el))) continue;
      if (!isShown(el)) continue;
      // A consent prompt has a way to answer it; a privacy-policy paragraph does not.
      const controls = Array.from(el.querySelectorAll('button, a, [role="button"], input[type="button"]'));
      if (!controls.some((control) => CONSENT_ACCEPT_RE.test(controlText(control)))) continue;
      found.push(el);
    }
    return found;
  }

  function verdict(reason, detail) {
    return { blocked: Boolean(reason), reason: reason || null, detail: detail || '' };
  }

  function classifyWith(extraction, details) {
    const doc = document;
    const ctx = newContext();
    const jdLength = (extraction.jd_text || '').length;
    const hasJd = jdLength >= MIN_JD_LENGTH;

    // The site handed us the posting as data: nothing on screen can hide it.
    if (extraction.has_jobposting_ldjson && extraction.source === 'ldjson' && hasJd) return verdict(null, '');

    const protocol = safe(() => String(window.location.protocol), '');
    if (protocol === 'chrome-error:') return verdict('http_error', 'the page failed to load');

    const title = doc.title || '';
    const bodyText = cleanText(safe(() => doc.body.innerText || doc.body.textContent || '', '')).slice(0, 20000);
    const bodyLength = bodyText.length;
    const prominent = safe(() => prominentText(doc), title);
    const fields = extraction.form_field_count || 0;

    // --- CAPTCHA / bot check -------------------------------------------------
    if (CAPTCHA_TITLE_RE.test(title)) return verdict('captcha', 'the page title is a security check');
    if (bodyLength < 3000 && safe(() => Boolean(doc.querySelector(CAPTCHA_PAGE_SELECTOR)), false)) {
      return verdict('captcha', 'a security challenge is on the page');
    }
    if (bodyLength < 1500 && CAPTCHA_TEXT_RE.test(bodyText)) {
      return verdict('captcha', 'the page asks to verify you are human');
    }
    if (
      !hasJd &&
      bodyLength < 600 &&
      fields < FORM_ONLY_MIN_FIELDS &&
      safe(() => Boolean(doc.querySelector(CAPTCHA_WIDGET_SELECTOR)), false)
    ) {
      return verdict('captcha', 'a CAPTCHA is the only content on the page');
    }

    // --- Job gone / error page ----------------------------------------------
    if (JOB_GONE_RE.test(prominent) || (bodyLength < 1500 && JOB_GONE_RE.test(bodyText))) {
      return verdict('http_error', 'the page says the job is no longer available');
    }
    if (!hasJd && (HTTP_ERROR_RE.test(prominent) || (bodyLength < 600 && HTTP_ERROR_RE.test(bodyText)))) {
      return verdict('http_error', 'the page is an error page');
    }

    // --- Full-screen consent wall -------------------------------------------
    // A banner that merely overlays a readable page is not a wall.
    const consent = hasJd ? [] : safe(() => findConsentNodes(doc, ctx), []);
    if (consent.some(isFullScreen)) return verdict('consent', 'a consent wall hides the page');

    // --- Login / sign-up wall -----------------------------------------------
    const passwordShown = safe(
      () => Array.from(doc.querySelectorAll('input[type="password"]')).some(isShown),
      false
    );
    const authUrl = safe(() => {
      const location = window.location;
      return AUTH_PATH_RE.test(location.pathname) || hostMatches(location.href, AUTH_HOSTS);
    }, false);
    const headings = safe(
      () =>
        [title].concat(
          Array.from(doc.querySelectorAll('h1, h2'))
            .slice(0, 10)
            .map((node) => oneLine(node.textContent))
        ),
      [title]
    );
    const authHeading = headings.some((heading) => AUTH_HEADING_RE.test(heading.trim()));
    const authWall = bodyLength < 3000 && AUTH_WALL_RE.test(bodyText);

    if ((passwordShown || authUrl) && (!hasJd || authHeading || (passwordShown && jdLength < 600))) {
      return verdict('login', passwordShown ? 'the page shows a sign-in form' : 'the page is a sign-in page');
    }
    if (!hasJd && ((authHeading && bodyLength < 3000) || authWall)) {
      return verdict('login', 'the page asks to sign in');
    }

    // --- Nothing to read -----------------------------------------------------
    const formOnly =
      !extraction.has_jobposting_ldjson &&
      (extraction.prose_len || 0) < FORM_ONLY_PROSE_MAX &&
      fields >= FORM_ONLY_MIN_FIELDS;
    // A role that only comes from the tab title or the address says nothing
    // about whether the page rendered.
    const weakRole = !extraction.role || !details.strongRole;
    if (!hasJd && !formOnly) {
      if (consent.length > 0 && (extraction.prose_len || 0) < MIN_DESCRIPTION_LENGTH) {
        return verdict('consent', 'a consent prompt is the only content on the page');
      }
      if (weakRole) return verdict('empty', 'no role and no job description were found');
    }

    return verdict(null, '');
  }

  function classify(extraction) {
    try {
      // A caller-supplied extraction is used as-is; the role's origin is then
      // re-derived only when it is needed.
      const fresh = extractWithDetails();
      const subject = extraction && typeof extraction === 'object' ? extraction : fresh.result;
      return classifyWith(subject, fresh.details);
    } catch (error) {
      return { blocked: false, reason: null, detail: '' };
    }
  }

  /** extract() and classify() in one pass, for the Python side. */
  function read() {
    try {
      const fresh = extractWithDetails();
      const classification = safe(() => classifyWith(fresh.result, fresh.details), {
        blocked: false,
        reason: null,
        detail: '',
      });
      return { extraction: fresh.result, classification: classification };
    } catch (error) {
      return { extraction: emptyExtraction(), classification: { blocked: false, reason: null, detail: '' } };
    }
  }

  // -------------------------------------------------------------------------
  // expandDescription()
  // -------------------------------------------------------------------------

  const EXPAND_RE = new RegExp(
    [
      '^(show|read|see|view)\\s+(more|full|the full|entire|the entire|whole)' +
        '(\\s+(job\\s+)?(description|details|posting|text))?$',
      '^(show|read|see|view)\\s+(the\\s+)?(job\\s+)?description$',
      '^\\+?\\s*more$',
      '^expand(\\s+(job\\s+)?description)?$',
      '^continue reading$',
    ].join('|'),
    'i'
  );

  /** Anything that is a decision, a navigation or a submission is never clicked. */
  const NEVER_CLICK_RE = new RegExp(
    'accept|agree|consent|cookie|allow|log ?in|sign ?(in|up)|register|apply|submit|send|save|share|' +
      'jobs|roles|positions|results|reviews|comments|companies|subscribe|upload|delete|remove',
    'i'
  );

  const DESCRIPTION_SELECTOR = [
    '[class*="description" i]',
    '[id*="description" i]',
    '[class*="job-detail" i]',
    '[class*="jobdetail" i]',
    '[class*="job-content" i]',
    '[class*="posting" i]',
    '[itemprop="description"]',
    '[data-automation-id="jobPostingDescription"]',
    '[data-ui="job-description"]',
    '[class*="prose" i]',
    'article',
    'main',
    '[role="main"]',
  ].join(', ');

  function toggleLabel(el) {
    return oneLine(controlText(el))
      .replace(/[\s.\u2026\u203a\u00bb+\u25be\u25bc\u2193\u2304]+$/g, '')
      .replace(/^[\s.\u2026]+/g, '')
      .trim();
  }

  function isNextTo(toggle, container) {
    if (container.contains(toggle)) return true;
    // "Next to": the toggle, or a wrapper up to three levels above it, sits
    // beside the description inside the same parent.
    let node = toggle;
    for (let level = 0; level < 4 && node; level += 1) {
      if (node.parentElement && node.parentElement === container.parentElement) {
        return tagOf(node.parentElement) !== 'BODY' || level <= 1;
      }
      node = node.parentElement;
    }
    return false;
  }

  function inSimilarJobs(el, ctx) {
    for (let node = el; node && node.nodeType === 1; node = node.parentElement) {
      if (isNoiseNode(node, ctx) && isSimilarJobsNode(node)) return true;
    }
    return false;
  }

  function findDescriptionToggles() {
    const ctx = newContext();
    const containers = [];
    if (lastJdElement && lastJdElement.isConnected) containers.push(lastJdElement);
    for (const el of safe(() => Array.from(document.querySelectorAll(DESCRIPTION_SELECTOR)).slice(0, 200), [])) {
      if (containers.indexOf(el) === -1) containers.push(el);
    }
    if (containers.length === 0) return [];

    const toggles = [];
    const controls = Array.from(
      document.querySelectorAll(
        'button, a, [role="button"], summary, [aria-expanded], [tabindex="0"], ' +
          '[class*="show-more" i], [class*="showmore" i], [class*="read-more" i], [class*="readmore" i], ' +
          '[class*="see-more" i], [class*="expand" i]'
      )
    ).slice(0, 4000);

    for (const el of controls) {
      const tag = tagOf(el);
      const label = toggleLabel(el);
      if (!label || label.length > 40) continue;
      if (!EXPAND_RE.test(label) || NEVER_CLICK_RE.test(label)) continue;
      if (el.getAttribute('aria-expanded') === 'true') continue;
      if (el.disabled || el.getAttribute('aria-disabled') === 'true') continue;
      if (tag === 'INPUT' || String(el.getAttribute('type') || '').toLowerCase() === 'submit') continue;
      if (tag === 'A') {
        // A link that goes somewhere is a navigation, not a toggle.
        const href = (el.getAttribute('href') || '').trim();
        if (href && href !== '#' && !/^#/.test(href) && !/^javascript:\s*(void\(0\))?;?\s*$/i.test(href)) continue;
      }
      if (!isShown(el)) continue;
      if (inChrome(el) || inConsent(el, ctx) || inSimilarJobs(el, ctx)) continue;
      if (el.closest('form') && !isWrapperForm(el.closest('form'))) continue;
      if (!containers.some((container) => isNextTo(el, container))) continue;
      if (toggles.some((other) => other.contains(el) || el.contains(other))) continue;
      toggles.push(el);
      if (toggles.length >= 5) break;
    }
    return toggles;
  }

  function expandDescription() {
    try {
      let clicked = 0;
      for (const toggle of findDescriptionToggles()) {
        try {
          toggle.click();
          clicked += 1;
        } catch (error) {
          // One toggle failing must not stop the others.
        }
      }
      return clicked;
    } catch (error) {
      return 0;
    }
  }

  // -------------------------------------------------------------------------
  // scrollForLazy()
  // -------------------------------------------------------------------------

  function scrollTargets() {
    const targets = [];
    const root = document.scrollingElement || document.documentElement;
    if (root) targets.push(root);
    // Single-page sites often scroll an inner panel instead of the window.
    const inner = [];
    for (const el of Array.from(document.querySelectorAll('main, div, section, article')).slice(0, 3000)) {
      if (el.scrollHeight <= el.clientHeight + 200 || el.clientHeight < 200) continue;
      const overflow = window.getComputedStyle(el).overflowY;
      if (overflow === 'auto' || overflow === 'scroll') inner.push(el);
    }
    inner.sort((a, b) => b.scrollHeight - a.scrollHeight);
    return targets.concat(inner.slice(0, 2));
  }

  function scrollForLazy() {
    return new Promise((resolve) => {
      try {
        const targets = safe(scrollTargets, []).map((el) => ({ el: el, start: el.scrollTop }));
        if (targets.length === 0) {
          resolve(false);
          return;
        }
        // A hidden page runs no lazy loaders and throttles its timers to one a
        // second, so stepping through it would only waste time: one pass, no waits.
        if (document.visibilityState === 'hidden') {
          for (const target of targets) {
            target.el.scrollTop = Math.max(0, target.el.scrollHeight - target.el.clientHeight);
          }
          for (const target of targets) target.el.scrollTop = target.start;
          resolve(true);
          return;
        }
        const steps = 5;
        let step = 0;
        const tick = () => {
          try {
            step += 1;
            for (const target of targets) {
              const max = Math.max(0, target.el.scrollHeight - target.el.clientHeight);
              target.el.scrollTop = Math.round((max * step) / steps);
            }
            if (step < steps) {
              setTimeout(tick, 120);
              return;
            }
            setTimeout(() => {
              try {
                for (const target of targets) target.el.scrollTop = target.start;
              } catch (error) {
                // Restoring the position is a courtesy.
              }
              resolve(true);
            }, 200);
          } catch (error) {
            resolve(false);
          }
        };
        tick();
      } catch (error) {
        resolve(false);
      }
    });
  }

  // -------------------------------------------------------------------------
  // findApplyControl() / clickApplyControl(): jobright detail pages only
  // -------------------------------------------------------------------------

  const APPLY_CONTROL_SELECTOR = 'a, button, [role="button"], [role="link"], input[type="button"]';

  function applyTier(label) {
    if (!label || label.length > 60) return 0;
    if (/submit/i.test(label)) return 0;
    if (/apply with autofill/i.test(label)) return 1;
    if (/apply now/i.test(label)) return 2;
    // Last resort: a plain "Apply". Not "Applied", not a filter, not a
    // sign-in shortcut such as "Apply with LinkedIn".
    if (label.length <= 30 && /^(quick |easy )?apply\b/i.test(label) && !/linkedin|filter|later|saved/i.test(label)) {
      return 3;
    }
    return 0;
  }

  function findApplyElement() {
    const ctx = newContext();
    let best = null;
    const controls = Array.from(document.querySelectorAll(APPLY_CONTROL_SELECTOR)).slice(0, 5000);
    for (const el of controls) {
      const label = controlText(el);
      const tier = applyTier(label);
      if (!tier) continue;
      if (String(el.getAttribute('type') || '').toLowerCase() === 'submit') continue;
      if (el.disabled || el.getAttribute('aria-disabled') === 'true') continue;
      if (!isShown(el) || inConsent(el, ctx)) continue;
      // Prefer the innermost control: its click reaches every handler above it.
      const inner = Array.from(el.querySelectorAll(APPLY_CONTROL_SELECTOR)).some(
        (child) => applyTier(controlText(child)) === tier && isShown(child)
      );
      if (inner) continue;
      if (!best || tier < best.tier) best = { el: el, tier: tier, label: label };
      if (tier === 1) break;
    }
    return best;
  }

  function findApplyControl() {
    try {
      const found = findApplyElement();
      if (!found) return null;
      const anchor = found.el.closest('a[href]');
      const href = anchor ? safe(() => anchor.href, '') : '';
      return {
        text: clean(found.label, 80),
        href: isHttpUrl(href) ? href : null,
        tag: tagOf(found.el).toLowerCase(),
      };
    } catch (error) {
      return null;
    }
  }

  function clickApplyControl() {
    try {
      const found = findApplyElement();
      if (!found) return false;
      if (/submit/i.test(found.label)) return false;
      found.el.click();
      return true;
    } catch (error) {
      return false;
    }
  }

  window.__RAI_EXTRACT__ = {
    version: VERSION,
    extract: extract,
    classify: classify,
    read: read,
    expandDescription: expandDescription,
    scrollForLazy: scrollForLazy,
    findApplyControl: findApplyControl,
    clickApplyControl: clickApplyControl,
  };
})();
