import DOMPurify from 'dompurify'

// Only our nonce-authorized navigation bridge runs; authored scripts are removed.
// No same-origin permission: this document cannot reach the Hub page or storage.
export const HTML_PREVIEW_SANDBOX = 'allow-scripts allow-popups allow-popups-to-escape-sandbox'

const POLICY = [
  "default-src 'none'",
  "style-src 'unsafe-inline'",
  'img-src data:',
  'font-src data:',
  "connect-src 'none'",
  "frame-src 'none'",
  "object-src 'none'",
  "form-action 'none'",
  'base-uri about:',
].join('; ')

/** Preserve document layout without granting active content or network access. */
export function htmlPreviewDocument(source: string): { srcdoc: string; channel: string } {
  const channel = Array.from(crypto.getRandomValues(new Uint8Array(16)), byte => byte.toString(16).padStart(2, '0')).join('')
  // Keep the default clobbering checks during sanitization, including for names.
  // A private instance lets us retain plain IDs without altering Markdown hooks.
  const purifier = DOMPurify(window)
  const ids = new WeakMap<Node, string>()
  purifier.addHook('uponSanitizeAttribute', (node, { attrName, attrValue }) => {
    if (attrName === 'id' && /^[A-Za-z_$][\w$]*$/.test(attrValue)) ids.set(node, attrValue)
  })
  const root = purifier.sanitize(source, {
    WHOLE_DOCUMENT: true,
    RETURN_DOM: true,
    USE_PROFILES: { html: true },
    ADD_TAGS: ['style'],
    FORBID_TAGS: [
      'script', 'noscript', 'iframe', 'object', 'embed', 'base', 'meta', 'link',
      'form', 'input', 'button', 'textarea', 'select', 'option', 'video', 'audio',
      'source', 'track', 'picture', 'portal', 'fencedframe',
    ],
    FORBID_ATTR: ['srcset', 'ping', 'download', 'action', 'formaction', 'target', 'srcdoc', 'autofocus', 'nonce'],
  })
  if (!(root instanceof HTMLElement)) throw new Error('Unable to parse the HTML document')
  root.querySelectorAll('[src]').forEach((element) => {
    const src = element.getAttribute('src') || ''
    if (element.localName !== 'img' || !/^data:image\/(?:png|jpeg|gif|webp);base64,[a-z\d+/=\s]+$/i.test(src)) {
      element.removeAttribute('src')
    }
  })
  root.querySelectorAll('[href]').forEach((element) => {
    const href = element.getAttribute('href') || ''
    if ((element.localName === 'a' || element.localName === 'area') && href.startsWith('#')) return
    // Relative links have no authorized sibling-file grant. They must not
    // accidentally navigate the frame to the Hub's own origin either.
    if (element.localName === 'a' || element.localName === 'area') {
      try {
        const url = new URL(href)
        if (/^https?:$/.test(url.protocol) && !url.username && !url.password) {
          element.setAttribute('href', url.href)
          element.setAttribute('target', '_blank')
          element.setAttribute('rel', 'noopener noreferrer')
          return
        }
      } catch { /* Non-URL links remain readable but cannot navigate. */ }
    }
    element.removeAttribute('href')
  })
  const head = root.querySelector('head') || root.insertBefore(document.createElement('head'), root.firstChild)
  const prefix = document.createElement('template')
  // A srcdoc fragment otherwise resolves against the embedding Hub URL.
  prefix.innerHTML = '<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
    + '<meta name="referrer" content="no-referrer"><base href="about:srcdoc">'
    + '<style>:root{color-scheme:light}html{overscroll-behavior:contain}body{margin:16px;overflow-wrap:anywhere}img{max-width:100%;height:auto}</style>'
  const policy = document.createElement('meta')
  policy.setAttribute('http-equiv', 'Content-Security-Policy')
  policy.setAttribute('content', `${POLICY}; script-src 'nonce-${channel}'`)
  head.prepend(policy, prefix.content)
  const bridge = document.createElement('script')
  // Serialize the attribute explicitly; the nonce property is not reflected in
  // every browser context used to build this detached document.
  bridge.setAttribute('nonce', channel)
  const parentOrigin = JSON.stringify(window.location.origin).replace(/</g, '\\u003c')
  // Only trusted origin data and the random hex channel are interpolated.
  // Fragment clicks scroll without adding iframe entries to joint session history.
  bridge.textContent = `(() => {
    const channel = '${channel}';
    const parentOrigin = ${parentOrigin};
    const owner = window.parent;
    // Restored IDs can shadow document methods through named HTML images.
    // Use native prototypes, not properties resolved through the document.
    const find = Document.prototype.getElementById.bind(document);
    const documentRoot = Document.prototype.querySelector.call(document, 'html');
    const listen = EventTarget.prototype.addEventListener;
    const scroll = (hash) => {
      if (typeof hash !== 'string' || !hash.startsWith('#')) return;
      let id; try { id = decodeURIComponent(hash.slice(1)); } catch { return; }
      if (!id) { Element.prototype.scrollTo.call(documentRoot, 0, 0); return; }
      const target = find(id);
      if (target) Element.prototype.scrollIntoView.call(target);
    };
    listen.call(document, 'click', event => {
      const link = event.target instanceof Element ? Element.prototype.closest.call(event.target, 'a[href], area[href]') : null;
      const href = link ? Element.prototype.getAttribute.call(link, 'href') : null;
      if (!href?.startsWith('#')) return;
      event.preventDefault(); scroll(href);
    });
    listen.call(document, 'keydown', event => {
      if (event.key !== 'Escape') return;
      event.preventDefault(); event.stopPropagation();
      owner.postMessage({ type: 'attachment-html-close', channel }, '*');
    }, true);
    listen.call(window, 'message', event => {
      if (event.source === owner && event.origin === parentOrigin && event.data?.type === 'attachment-html-init' && event.data.channel === channel) {
        scroll(event.data.hash);
      }
    });
  })();`
  root.querySelector('body')?.append(bridge)
  // Restore only plain IDs on retained elements after the complete cleanup.
  // This preserves fragment/CSS identity without restoring arbitrary rejected values.
  for (const element of [root, ...root.querySelectorAll('*')]) {
    const id = ids.get(element)
    if (id !== undefined && !element.hasAttribute('id')) element.setAttribute('id', id)
  }
  return { srcdoc: `<!doctype html>${root.outerHTML}`, channel }
}
