/* Shared feedback for page navigation, native forms and HTMX actions. */
(() => {
  const pending = new Map();
  const requests = new WeakMap();
  const submitters = new WeakMap();
  const submitSelector = 'button:not([type]), button[type="submit"], input[type="submit"], input[type="image"]';
  const ajaxSelector = '[hx-get], [hx-post], [hx-put], [hx-patch], [hx-delete], [data-hx-get], [data-hx-post], [data-hx-put], [data-hx-patch], [data-hx-delete], [hx-boost="true"], [data-hx-boost="true"]';

  function announce() {
    const status = document.querySelector('[data-request-status]');
    if (!status) return;
    status.hidden = pending.size === 0;
    status.textContent = pending.size ? [...pending.values()].at(-1).message : '';
  }

  function begin(scope, trigger, deferDisable = false) {
    if (pending.has(scope)) return null;
    const controls = scope.tagName === 'FORM'
      ? [...scope.elements].filter(element => element.matches(submitSelector))
      : [scope];
    const state = {
      scope,
      message: trigger?.dataset.loadingLabel || scope.dataset.loadingLabel || 'Loading…',
      controls: controls.map(element => ({
        element,
        disabled: element.disabled,
        ariaDisabled: element.getAttribute('aria-disabled'),
        ariaBusy: element.getAttribute('aria-busy'),
      })),
    };
    pending.set(scope, state);
    for (const { element } of state.controls) {
      element.setAttribute('aria-disabled', 'true');
      element.setAttribute('aria-busy', 'true');
      element.classList.add('app-request-pending');
    }
    function disable() {
      if (pending.get(scope) !== state) return;
      for (const { element, disabled } of state.controls) {
        if (disabled !== undefined) element.disabled = true;
      }
    }
    // Native submission must serialize the clicked button's name/value first.
    if (deferDisable) setTimeout(disable, 0);
    else disable();
    announce();
    return state;
  }

  function finish(state) {
    if (!state || pending.get(state.scope) !== state) return;
    pending.delete(state.scope);
    for (const { element, disabled, ariaDisabled, ariaBusy } of state.controls) {
      if (disabled !== undefined) element.disabled = disabled;
      for (const [name, value] of [['aria-disabled', ariaDisabled], ['aria-busy', ariaBusy]]) {
        if (value === null) element.removeAttribute(name);
        else element.setAttribute(name, value);
      }
      element.classList.remove('app-request-pending');
    }
    announce();
  }

  function block(event) {
    event.preventDefault();
    event.stopImmediatePropagation();
  }

  document.addEventListener('click', event => {
    const control = event.target.closest('a, button, input[type="submit"], input[type="image"]');
    if (!control) return;
    if (control.tagName === 'A' && (event.button !== 0 || event.metaKey ||
        event.ctrlKey || event.shiftKey || event.altKey)) return;
    if (control.classList.contains('app-request-pending') ||
        (control.form && pending.has(control.form) && control.matches(submitSelector))) {
      block(event);
    }
  }, true);

  document.addEventListener('click', event => {
    const link = event.target.closest('a[href]');
    if (!link || event.defaultPrevented || event.button !== 0 || event.metaKey ||
        event.ctrlKey || event.shiftKey || event.altKey || link.hasAttribute('download') ||
        (link.target && link.target !== '_self') || link.getAttribute('href').startsWith('#') || link.closest(ajaxSelector) ||
        link.closest('[data-no-loading]')) return;
    const url = new URL(link.href, window.location.href);
    if (url.origin !== window.location.origin || !['http:', 'https:'].includes(url.protocol) ||
        (url.pathname === window.location.pathname && url.search === window.location.search && url.hash)) return;
    // Prevent the same destination being requested through another link too.
    for (const state of pending.values()) {
      if (state.destination === url.href) {
        block(event);
        return;
      }
    }
    const state = begin(link, link);
    if (state) {
      state.destination = url.href;
      setTimeout(() => { if (event.defaultPrevented) finish(state); }, 0);
    }
  });

  document.addEventListener('submit', event => {
    if (event.submitter) submitters.set(event.target, event.submitter);
    if (pending.has(event.target)) block(event);
  }, true);

  document.addEventListener('submit', event => {
    const form = event.target;
    if (event.defaultPrevented || form.method === 'dialog' || event.submitter?.formMethod === 'dialog' ||
        (form.target && form.target !== '_self') ||
        (event.submitter?.formTarget && event.submitter.formTarget !== '_self') ||
        form.closest(ajaxSelector) || form.closest('[data-no-loading]')) return;
    const state = begin(form, event.submitter, true);
    // A later handler may cancel submission (for example a confirmation dialog).
    setTimeout(() => { if (event.defaultPrevented) finish(state); }, 0);
  });

  document.addEventListener('htmx:beforeRequest', event => {
    const element = event.detail.elt;
    const scope = element?.matches('form, button, a') ? element : null;
    if (event.defaultPrevented || !scope || scope.closest('[data-no-loading]')) return;
    if (pending.has(scope)) {
      event.preventDefault();
      return;
    }
    const trigger = event.detail.requestConfig?.triggeringEvent?.submitter ||
      submitters.get(scope) || scope;
    const state = begin(scope, trigger);
    requests.set(event.detail.xhr, state);
    setTimeout(() => { if (event.defaultPrevented) finish(state); }, 0);
  });
  for (const name of ['afterRequest', 'timeout', 'sendError', 'sendAbort', 'onLoadError', 'swapError']) {
    document.addEventListener(`htmx:${name}`, event => finish(requests.get(event.detail.xhr)));
  }
  // Browsers restore the disabled DOM from their back/forward cache.
  window.addEventListener('pageshow', event => {
    if (!event.persisted) return;
    for (const state of [...pending.values()]) finish(state);
  });
})();
