/* Keep loading and failure messages outside the content replaced by HTMX. */
(() => {
  const latestRequests = new WeakMap();
  function panelFor(event) {
    return event.detail.target?.closest('[data-remote-panel]');
  }
  document.addEventListener('htmx:beforeRequest', (event) => {
    const panel = panelFor(event);
    if (!panel) return;
    latestRequests.set(panel, event.detail.xhr);
    panel.setAttribute('aria-busy', 'true');
    const status = panel.querySelector('[data-panel-status]');
    status.hidden = false;
    status.textContent = panel.dataset.loadingMessage;
  });
  document.addEventListener('htmx:afterRequest', (event) => {
    const panel = panelFor(event);
    if (!panel || latestRequests.get(panel) !== event.detail.xhr) return;
    panel.setAttribute('aria-busy', 'false');
    const status = panel.querySelector('[data-panel-status]');
    status.hidden = event.detail.successful;
    if (!event.detail.successful) status.textContent = panel.dataset.errorMessage;
  });
})();
