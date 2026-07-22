// Login handler, external so it satisfies the strict CSP (script-src 'self').
document.getElementById('login-form').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const key = document.getElementById('k').value;
  const err = document.getElementById('e');
  try {
    const r = await fetch('/api/auth', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ key }),
    });
    if (r.ok) {
      location.reload();
    } else {
      err.textContent = r.status === 429 ? 'Too many attempts' : 'Invalid key';
    }
  } catch (_) {
    err.textContent = 'Network error — try again';
  }
});
