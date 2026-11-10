/* Keep loading and failure messages outside the content replaced by HTMX. */
(() => {
  const latestRequests = new WeakMap();
  const requestPanels = new WeakMap();
  function panelFor(event) {
    return (event.detail.target || event.detail.elt || event.target)?.closest('[data-remote-panel]');
  }
  document.addEventListener('htmx:beforeRequest', (event) => {
    const panel = panelFor(event);
    if (!panel) return;
    latestRequests.set(panel, event.detail.xhr);
    requestPanels.set(event.detail.xhr, panel);
    panel.setAttribute('aria-busy', 'true');
    const status = panel.querySelector('[data-panel-status]');
    if (!status) return;
    status.hidden = false;
    status.textContent = panel.dataset.loadingMessage;
  });
  function finishRequest(event, message) {
    const panel = requestPanels.get(event.detail.xhr) || panelFor(event);
    if (!panel || latestRequests.get(panel) !== event.detail.xhr) return;
    panel.setAttribute('aria-busy', 'false');
    const status = panel.querySelector('[data-panel-status]');
    if (!status) return;
    status.hidden = !message && event.detail.successful === true;
    if (!status.hidden) status.textContent = message || panel.dataset.errorMessage;
  }
  document.addEventListener('htmx:afterRequest', (event) => finishRequest(event));
  document.addEventListener('htmx:timeout', (event) => {
    const panel = requestPanels.get(event.detail.xhr) || panelFor(event);
    finishRequest(event, panel?.dataset.timeoutMessage || 'This is taking longer than expected. Please try again.');
  });
  for (const name of ['htmx:onLoadError', 'htmx:swapError']) {
    document.addEventListener(name, (event) => {
      const panel = requestPanels.get(event.detail.xhr) || panelFor(event);
      finishRequest(event, panel?.dataset.errorMessage);
    });
  }
})();
