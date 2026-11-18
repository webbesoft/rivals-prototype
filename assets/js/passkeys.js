(() => {
  "use strict";

  document.querySelectorAll("[data-passkey-form]").forEach((form) => {
    const button = form.querySelector('button[type="submit"]');
    const status = form.querySelector("[data-passkey-status]");
    const registering = form.dataset.passkeyForm === "register";
    const label = button.textContent;

    if (!window.isSecureContext || !window.PublicKeyCredential || !window.webauthnJSON) {
      status.textContent = "Passkeys aren’t available in this browser. Use Google or an email link to sign in.";
      return;
    }
    button.disabled = false;
    let busy = false;

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (busy) return;
      busy = true;
      button.disabled = true;
      button.textContent = "Waiting for your device…";
      status.textContent = "Follow the prompt on your device to continue.";

      try {
        let credential;
        if (registering) {
          const options = JSON.parse(document.getElementById("passkey-options").textContent);
          form.elements.challenge_id.value = options.challenge_id;
          credential = await window.webauthnJSON.create(options.creation_options);
        } else {
          const response = await fetch(form.action, {
            headers: { Accept: "application/json" },
            credentials: "same-origin",
            cache: "no-store",
          });
          if (!response.ok) throw new Error("challenge-unavailable");
          const options = await response.json();
          form.elements.challenge_id.value = options.challenge_id;
          credential = await window.webauthnJSON.get(options.request_options);
        }
        form.elements.credential.value = JSON.stringify(credential);
        HTMLFormElement.prototype.submit.call(form);
      } catch (error) {
        if (error.name === "NotAllowedError" || error.name === "AbortError") {
          status.textContent = "The passkey prompt was cancelled or no matching passkey was found. You can try again.";
        } else if (error.name === "InvalidStateError") {
          status.textContent = "This passkey is already saved. Try another device or passkey.";
        } else {
          status.textContent = "Couldn’t use your passkey. Please try again, or sign in with an email link.";
        }
        busy = false;
        button.disabled = false;
        button.textContent = label;
      }
    });
  });
})();
