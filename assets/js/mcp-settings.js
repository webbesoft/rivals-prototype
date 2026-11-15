document.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-copy-target]");
  if (!button) return;
  const target = document.getElementById(button.dataset.copyTarget);
  const status = document.getElementById(button.dataset.copyStatus);
  if (!target || !status) return;
  try {
    await navigator.clipboard.writeText(target.value ?? target.textContent.trim());
    status.textContent = "Copied. You can paste it into your assistant.";
  } catch {
    status.textContent = "Copy did not work. Select the text above and copy it manually.";
    if (typeof target.select === "function") {
      target.focus();
      target.select();
    }
  }
});
