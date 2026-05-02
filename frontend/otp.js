/* ─── OTP page ─── */
const params      = new URLSearchParams(window.location.search);
const challengeId = params.get('challenge');

if (!challengeId) {
  window.location.href = '/login';
}

const digits      = Array.from(document.querySelectorAll('.otp-digit'));
const validateBtn = document.getElementById('validate-btn');
const otpError    = document.getElementById('otp-error');
const attemptsMsg = document.getElementById('attempts-msg');
const backLink    = document.getElementById('back-link');
const countdownEl = document.getElementById('countdown');

/* ─── Countdown ─── */
const OTP_TTL = 5 * 60; // 5 minutes in seconds
let secondsLeft = OTP_TTL;

function updateCountdown() {
  const m = Math.floor(secondsLeft / 60);
  const s = secondsLeft % 60;
  countdownEl.textContent = `${m}:${s.toString().padStart(2, '0')}`;

  if (secondsLeft <= 0) {
    clearInterval(countdownInterval);
    countdownEl.textContent = '0:00';
    otpError.textContent = 'Le code a expiré. Retournez à la page de login.';
    otpError.classList.remove('hidden');
    validateBtn.disabled = true;
    backLink.classList.remove('hidden');
    digits.forEach(d => (d.disabled = true));
  }
  secondsLeft--;
}

updateCountdown();
const countdownInterval = setInterval(updateCountdown, 1000);

/* ─── Digit inputs ─── */
digits.forEach((input, idx) => {
  input.addEventListener('input', () => {
    // Keep only digits
    input.value = input.value.replace(/\D/g, '').slice(-1);
    if (input.value && idx < digits.length - 1) {
      digits[idx + 1].focus();
    }
    checkComplete();
  });

  input.addEventListener('keydown', e => {
    if (e.key === 'Backspace' && !input.value && idx > 0) {
      digits[idx - 1].focus();
      digits[idx - 1].value = '';
      checkComplete();
    }
    if (e.key === 'ArrowLeft' && idx > 0) digits[idx - 1].focus();
    if (e.key === 'ArrowRight' && idx < digits.length - 1) digits[idx + 1].focus();
  });

  // Handle paste on any digit
  input.addEventListener('paste', e => {
    e.preventDefault();
    const pasted = (e.clipboardData || window.clipboardData)
      .getData('text')
      .replace(/\D/g, '')
      .slice(0, digits.length);
    pasted.split('').forEach((ch, i) => {
      if (digits[i]) digits[i].value = ch;
    });
    const nextEmpty = digits.findIndex(d => !d.value);
    if (nextEmpty !== -1) digits[nextEmpty].focus();
    else digits[digits.length - 1].focus();
    checkComplete();
  });
});

// Auto-focus first digit
digits[0].focus();

function getCode() {
  return digits.map(d => d.value).join('');
}

function checkComplete() {
  const code = getCode();
  validateBtn.disabled = code.length !== digits.length;
  if (code.length === digits.length) {
    submitOTP(code);
  }
}

/* ─── Submit ─── */
validateBtn.addEventListener('click', () => {
  const code = getCode();
  if (code.length === digits.length) submitOTP(code);
});

async function submitOTP(code) {
  validateBtn.disabled = true;
  validateBtn.textContent = '⏳ Validation…';
  otpError.classList.add('hidden');
  attemptsMsg.classList.add('hidden');

  try {
    const res = await fetch('/api/auth/verify-otp', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'include',
      body: JSON.stringify({ challenge_id: challengeId, code }),
    });

    const data = await res.json().catch(() => ({}));

    if (res.ok) {
      clearInterval(countdownInterval);
      window.location.href = '/';
      return;
    }

    if (res.status === 410) {
      // Expired or consumed
      clearInterval(countdownInterval);
      otpError.textContent = data.detail || 'Code expiré. Retournez au login.';
      otpError.classList.remove('hidden');
      backLink.classList.remove('hidden');
      validateBtn.disabled = true;
      digits.forEach(d => (d.disabled = true));
      return;
    }

    if (res.status === 429) {
      // Exhausted
      clearInterval(countdownInterval);
      otpError.textContent = data.detail || 'Trop de tentatives. Recommencez le login.';
      otpError.classList.remove('hidden');
      backLink.classList.remove('hidden');
      validateBtn.disabled = true;
      digits.forEach(d => (d.disabled = true));
      return;
    }

    // Wrong code
    if (data.attempts_left !== undefined) {
      attemptsMsg.textContent = `Tentatives restantes : ${data.attempts_left}`;
      attemptsMsg.classList.remove('hidden');
    }
    otpError.textContent = data.detail || 'Code incorrect.';
    otpError.classList.remove('hidden');

    // Clear digits and re-focus
    digits.forEach(d => (d.value = ''));
    digits[0].focus();
    validateBtn.disabled = true;
  } catch (err) {
    otpError.textContent = 'Erreur réseau. Réessayez.';
    otpError.classList.remove('hidden');
  } finally {
    if (!validateBtn.disabled || validateBtn.textContent === '⏳ Validation…') {
      validateBtn.textContent = 'Valider';
    }
  }
}
