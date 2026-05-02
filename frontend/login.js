/* ─── Login form ─── */
const form        = document.getElementById('login-form');
const loginBtn    = document.getElementById('login-btn');
const loginError  = document.getElementById('login-error');
const rateLimitMsg = document.getElementById('rate-limit-msg');

const ADMIN_PREFIX = window.ADMIN_PREFIX || '';

form.addEventListener('submit', async e => {
  e.preventDefault();
  const username = document.getElementById('username').value.trim();
  const password = document.getElementById('password').value;

  if (!username || !password) return;

  loginBtn.disabled = true;
  loginBtn.textContent = '⏳ Vérification…';
  loginError.classList.add('hidden');
  rateLimitMsg.classList.add('hidden');

  try {
    const res = await fetch(ADMIN_PREFIX + '/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password }),
    });

    if (res.status === 429) {
      const retryAfter = res.headers.get('Retry-After') || '900';
      const minutes = Math.ceil(parseInt(retryAfter, 10) / 60);
      rateLimitMsg.textContent = `Trop de tentatives. Réessayez dans ${minutes} minute(s).`;
      rateLimitMsg.classList.remove('hidden');
      return;
    }

    let data;
    try {
      data = await res.json();
    } catch {
      const text = await res.text().catch(() => '');
      data = { detail: text || `HTTP ${res.status}` };
    }

    if (!res.ok) {
      loginError.textContent = data.detail || 'Identifiant ou mot de passe incorrect.';
      loginError.classList.remove('hidden');
      return;
    }

    // Login successful — redirect to dashboard
    window.location.href = ADMIN_PREFIX + '/dashboard';
  } catch (err) {
    console.error('[login] fetch failed:', err);
    loginError.textContent = `Erreur réseau : ${err.message || 'requête échouée'}. Vérifiez votre connexion ou les logs serveur.`;
    loginError.classList.remove('hidden');
  } finally {
    loginBtn.disabled = false;
    loginBtn.textContent = 'Se connecter';
  }
});

/* ─── Password toggle ─── */
const togglePassword = document.getElementById('toggle-password');
const passwordInput  = document.getElementById('password');

const eyeSVG = `<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>`;
const eyeOffSVG = `<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17.94 17.94A10.07 10.07 0 0112 20c-7 0-11-8-11-8a18.45 18.45 0 015.06-5.94M9.9 4.24A9.12 9.12 0 0112 4c7 0 11 8 11 8a18.5 18.5 0 01-2.16 3.19m-6.72-1.07a3 3 0 11-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg>`;

if (togglePassword) {
  togglePassword.addEventListener('click', () => {
    const isPassword = passwordInput.type === 'password';
    passwordInput.type = isPassword ? 'text' : 'password';
    togglePassword.setAttribute('aria-label', isPassword ? 'Masquer le mot de passe' : 'Afficher le mot de passe');
    togglePassword.innerHTML = isPassword ? eyeOffSVG : eyeSVG;
  });
}
