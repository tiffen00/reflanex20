/* ─── Login form ─── */
const form        = document.getElementById('login-form');
const loginBtn    = document.getElementById('login-btn');
const loginError  = document.getElementById('login-error');
const rateLimitMsg = document.getElementById('rate-limit-msg');

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
    const res = await fetch('/api/auth/login', {
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

    const data = await res.json();

    if (!res.ok) {
      loginError.textContent = data.detail || 'Identifiant ou mot de passe incorrect.';
      loginError.classList.remove('hidden');
      return;
    }

    // Redirect to OTP page with challenge_id
    sessionStorage.setItem('otpExpiresIn', data.expires_in || 300);
    window.location.href = `/login/otp?challenge=${encodeURIComponent(data.challenge_id)}`;
  } catch (err) {
    loginError.textContent = 'Erreur réseau. Réessayez.';
    loginError.classList.remove('hidden');
  } finally {
    loginBtn.disabled = false;
    loginBtn.textContent = 'Continuer →';
  }
});
