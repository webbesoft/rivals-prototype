// Keep redemption as a CSRF-protected POST: fetching an email link alone
// must not consume it. Returning users can continue without another click.
document.querySelector('form[data-auto-sign-in]')?.requestSubmit();
