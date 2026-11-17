(function () {
  const config = document.currentScript;
  if (!config) return;

  let detected;
  try {
    detected = Intl.DateTimeFormat().resolvedOptions().timeZone;
  } catch (_) {
    return;
  }
  if (!detected || !config.dataset.timezoneUrl || !config.dataset.csrfToken) return;

  // The head tracker starts before forms can be edited, including before defer runs.
  fetch(config.dataset.timezoneUrl, {
    method: 'POST',
    mode: 'same-origin',
    credentials: 'same-origin',
    headers: { 'X-CSRFToken': config.dataset.csrfToken },
    body: new URLSearchParams({ timezone: detected, source: 'browser' }),
  }).then(response => {
    if (response.ok && document.documentElement.dataset.timezoneUntouched === 'true') {
      window.location.reload();
    }
  }).catch(() => {
    // The profile form remains available if detection or saving is unavailable.
  });
})();
