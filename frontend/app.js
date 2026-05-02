/* ─── State ─── */
let activeCampaignId = null;
let domains = [];

const ADMIN_PREFIX = window.ADMIN_PREFIX || '';
if (!window.ADMIN_PREFIX) {
  console.warn('[app] ADMIN_PREFIX not injected — falling back to empty string. Admin API calls may fail.');
}

/* ─── DOM refs ─── */
const app            = document.getElementById('app');
const logoutBtn      = document.getElementById('logout-btn');
const tabs           = document.querySelectorAll('.nav-tab');
const tabContents    = document.querySelectorAll('.tab-content');

// Upload
const dropZone       = document.getElementById('drop-zone');
const fileInput      = document.getElementById('file-input');
const fileInfo       = document.getElementById('file-info');
const campaignName   = document.getElementById('campaign-name');
const uploadBtn      = document.getElementById('upload-btn');
const uploadResult   = document.getElementById('upload-result');

// Campaigns
const refreshBtn      = document.getElementById('refresh-btn');
const campaignsList   = document.getElementById('campaigns-list');
const linksPanel      = document.getElementById('links-panel');
const linksTitle      = document.getElementById('links-title');
const closeLinksBtn   = document.getElementById('close-links-btn');
const domainSelect    = document.getElementById('domain-select');
const generateLinkBtn = document.getElementById('generate-link-btn');
const linksList       = document.getElementById('links-list');

/* ─── Init ─── */
(async () => {
  // Verify session is still valid; redirect to login if not
  const res = await apiFetch('/api/auth/me');
  if (!res.ok) {
    window.location.href = ADMIN_PREFIX + '/login';
    return;
  }
  const data = await res.json();
  const userNameEl = document.getElementById('user-name');
  if (userNameEl && data.username) userNameEl.textContent = data.username;
  loadDomains();
  loadCampaigns();
})();

/* ─── Logout ─── */
logoutBtn.addEventListener('click', async () => {
  await apiFetch('/api/auth/logout', 'POST');
  window.location.href = ADMIN_PREFIX + '/login';
});

/* ─── Tabs ─── */
tabs.forEach(tab => {
  tab.addEventListener('click', () => {
    tabs.forEach(t => t.classList.remove('active'));
    tabContents.forEach(tc => tc.classList.add('hidden'));
    tab.classList.add('active');
    const id = 'tab-' + tab.dataset.tab;
    document.getElementById(id).classList.remove('hidden');
    if (tab.dataset.tab === 'campaigns') loadCampaigns();
    if (tab.dataset.tab === 'bot') loadBotStatus();
  });
});

/* ─── Drop zone ─── */
let selectedFile = null;

dropZone.addEventListener('click', () => fileInput.click());
dropZone.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') fileInput.click(); });
fileInput.addEventListener('change', () => {
  if (fileInput.files[0]) setFile(fileInput.files[0]);
});

dropZone.addEventListener('dragover', e => { e.preventDefault(); dropZone.classList.add('drag-over'); });
dropZone.addEventListener('dragleave', () => dropZone.classList.remove('drag-over'));
dropZone.addEventListener('drop', e => {
  e.preventDefault();
  dropZone.classList.remove('drag-over');
  const f = e.dataTransfer.files[0];
  if (f && f.name.toLowerCase().endsWith('.zip')) setFile(f);
  else showUploadResult('error', 'Merci de sélectionner un fichier .zip');
});

function setFile(f) {
  selectedFile = f;
  fileInfo.textContent = `📦 ${f.name} (${(f.size / 1024 / 1024).toFixed(2)} Mo)`;
  fileInfo.classList.remove('hidden');
  updateUploadBtn();
}

campaignName.addEventListener('input', updateUploadBtn);

function updateUploadBtn() {
  uploadBtn.disabled = !(selectedFile && campaignName.value.trim());
}

/* ─── Upload ─── */
uploadBtn.addEventListener('click', async () => {
  const name = campaignName.value.trim();
  if (!selectedFile || !name) return;

  uploadBtn.disabled = true;
  uploadBtn.textContent = '⏳ Upload…';
  uploadResult.classList.add('hidden');

  const fd = new FormData();
  fd.append('file', selectedFile);
  fd.append('name', name);

  const res = await fetch(ADMIN_PREFIX + '/api/upload', {
    method: 'POST',
    credentials: 'include',
    body: fd,
  });

  if (res.ok) {
    const data = await res.json();
    showUploadResult('success', `✅ Campagne « ${data.name} » créée (ID: ${data.campaign_id})`);
    selectedFile = null;
    fileInput.value = '';
    campaignName.value = '';
    fileInfo.classList.add('hidden');
  } else {
    const err = await res.json().catch(() => ({}));
    showUploadResult('error', `❌ ${err.detail || res.statusText}`);
  }

  uploadBtn.textContent = 'Uploader';
  updateUploadBtn();
});

function showUploadResult(type, msg) {
  uploadResult.textContent = msg;
  uploadResult.className = 'result ' + type;
  uploadResult.classList.remove('hidden');
}

/* ─── Campaigns ─── */
refreshBtn.addEventListener('click', loadCampaigns);

async function loadCampaigns() {
  campaignsList.innerHTML = '<p class="text-muted">Chargement…</p>';
  const res = await apiFetch('/api/campaigns');
  if (!res.ok) {
    campaignsList.innerHTML = '<p class="error">Erreur lors du chargement.</p>';
    return;
  }
  const campaigns = await res.json();
  if (!campaigns.length) {
    campaignsList.innerHTML = '<p class="text-muted">Aucune campagne pour l\'instant. Upload ton premier zip !</p>';
    return;
  }

  const table = document.createElement('table');
  table.className = 'campaigns-table';
  table.innerHTML = `
    <thead>
      <tr>
        <th>Nom</th>
        <th>Date</th>
        <th>Liens actifs</th>
        <th>Actions</th>
      </tr>
    </thead>
    <tbody></tbody>
  `;
  const tbody = table.querySelector('tbody');

  campaigns.forEach(c => {
    const activeLinks = c.links.filter(l => l.is_active).length;
    const date = new Date(c.created_at).toLocaleDateString('fr-FR');
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td><strong>${esc(c.name)}</strong></td>
      <td>${date}</td>
      <td><span class="badge badge-active">${activeLinks}</span></td>
      <td>
        <div class="actions">
          <button class="btn-ghost btn-sm" data-action="links" data-id="${c.id}" data-name="${esc(c.name)}">🔗 Liens</button>
          <button class="btn-danger btn-sm" data-action="del-campaign" data-id="${c.id}" data-name="${esc(c.name)}">🗑</button>
        </div>
      </td>
    `;
    tbody.appendChild(tr);
  });

  table.addEventListener('click', async e => {
    const btn = e.target.closest('[data-action]');
    if (!btn) return;
    const action = btn.dataset.action;
    const id = parseInt(btn.dataset.id);
    const name = btn.dataset.name;

    if (action === 'links') {
      showLinks(id, name, campaigns.find(c => c.id === id)?.links || []);
    } else if (action === 'del-campaign') {
      if (!confirm(`Supprimer la campagne « ${name} » et tous ses fichiers ?`)) return;
      const r = await apiFetch(`/api/campaigns/${id}`, 'DELETE');
      if (r.ok) loadCampaigns();
      else alert('Erreur lors de la suppression.');
    }
  });

  campaignsList.innerHTML = '';
  campaignsList.appendChild(table);
}

/* ─── Links panel ─── */
function showLinks(campaignId, campaignName, links) {
  activeCampaignId = campaignId;
  linksTitle.textContent = `Liens — ${campaignName}`;
  linksPanel.classList.remove('hidden');
  renderLinks(links);
  linksPanel.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

closeLinksBtn.addEventListener('click', () => {
  linksPanel.classList.add('hidden');
  activeCampaignId = null;
});

async function loadDomains() {
  const res = await apiFetch('/api/domains');
  if (!res.ok) return;
  const data = await res.json();
  // Support both new structure (array of objects) and legacy (array of strings)
  const rawDomains = data.domains || [];
  domains = rawDomains.map(d => typeof d === 'string' ? { domain: d, is_default: false } : d);
  domainSelect.innerHTML = '';
  domains.forEach((d, i) => {
    const o = document.createElement('option');
    o.value = d.domain;
    o.textContent = d.is_default ? `🌟 ${d.domain} (par défaut)` : d.domain;
    if (i === 0) o.selected = true;
    domainSelect.appendChild(o);
  });
}

generateLinkBtn.addEventListener('click', async () => {
  if (!activeCampaignId) return;
  generateLinkBtn.disabled = true;
  const selectedDomain = domainSelect.value || null;
  const body = selectedDomain ? { domain: selectedDomain } : {};
  const res = await apiFetch(`/api/campaigns/${activeCampaignId}/links`, 'POST', body);
  generateLinkBtn.disabled = false;
  if (res.ok) {
    // Refresh campaign links
    const campaigns = await (await apiFetch('/api/campaigns')).json();
    const c = campaigns.find(c => c.id === activeCampaignId);
    if (c) renderLinks(c.links);
  } else {
    const err = await res.json().catch(() => ({}));
    alert(err.detail || 'Erreur lors de la génération du lien.');
  }
});

function renderLinks(links) {
  if (!links.length) {
    linksList.innerHTML = '<p class="text-muted mt-1">Aucun lien. Génère-en un ci-dessus !</p>';
    return;
  }

  const table = document.createElement('table');
  table.className = 'links-table';
  table.innerHTML = `
    <thead>
      <tr>
        <th>Slug</th>
        <th>URL</th>
        <th>Domaine</th>
        <th>Clics</th>
        <th>Statut</th>
        <th>Actions</th>
      </tr>
    </thead>
    <tbody></tbody>
  `;
  const tbody = table.querySelector('tbody');

  links.forEach(l => {
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td><span class="link-slug">${esc(l.slug)}</span></td>
      <td><span class="link-url" title="${esc(l.full_url)}">${esc(l.full_url)}</span></td>
      <td>${l.domain ? esc(l.domain) : '<span class="text-muted">—</span>'}</td>
      <td>${l.clicks}</td>
      <td>${l.is_active ? '<span class="badge badge-active">Actif</span>' : '<span class="tag-inactive">Inactif</span>'}</td>
      <td>
        <div class="actions">
          <button class="btn-ghost btn-sm" data-copy="${esc(l.full_url)}">📋</button>
          ${l.is_active ? `<button class="btn-danger btn-sm" data-deactivate="${esc(l.slug)}">Désactiver</button>` : ''}
        </div>
      </td>
    `;
    tbody.appendChild(tr);
  });

  table.addEventListener('click', async e => {
    const copyBtn = e.target.closest('[data-copy]');
    if (copyBtn) {
      navigator.clipboard.writeText(copyBtn.dataset.copy).then(() => {
        copyBtn.textContent = '✅';
        setTimeout(() => (copyBtn.textContent = '📋'), 1500);
      });
      return;
    }
    const deactivateBtn = e.target.closest('[data-deactivate]');
    if (deactivateBtn) {
      const slug = deactivateBtn.dataset.deactivate;
      const r = await apiFetch(`/api/links/${slug}`, 'DELETE');
      if (r.ok) {
        const campaigns = await (await apiFetch('/api/campaigns')).json();
        const c = campaigns.find(c => c.id === activeCampaignId);
        if (c) renderLinks(c.links);
      }
    }
  });

  linksList.innerHTML = '';
  linksList.appendChild(table);
}

/* ─── API helper ─── */
function apiFetch(path, method = 'GET', body = null) {
  const opts = {
    method,
    credentials: 'include',
    headers: {},
  };
  if (body !== null) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  return fetch(ADMIN_PREFIX + path, opts);
}

/* ─── Bot Telegram tab ─── */
async function loadBotStatus() {
  const badge = document.getElementById('bot-status-badge');
  const details = document.getElementById('bot-status-details');
  const configDetails = document.getElementById('bot-config-details');
  if (!badge || !details) return;

  badge.textContent = 'Chargement…';
  badge.className = 'bot-badge bot-badge-loading';

  const res = await apiFetch('/api/bot/status');
  if (!res.ok) {
    badge.textContent = 'Erreur';
    badge.className = 'bot-badge bot-badge-error';
    details.innerHTML = '<p class="error">Impossible de récupérer le statut du bot.</p>';
    return;
  }

  const data = await res.json();
  if (data.configured) {
    badge.textContent = '🟢 Configuré';
    badge.className = 'bot-badge bot-badge-ok';
    const username = data.username ? `<a href="https://t.me/${data.username.replace('@','')}" target="_blank" rel="noopener" class="bot-link">${esc(data.username)}</a>` : '<span class="text-muted">—</span>';
    details.innerHTML = `
      <div class="bot-status-grid">
        <div class="bot-status-item"><span class="bot-status-label">Nom d'utilisateur</span><span>${username}</span></div>
        <div class="bot-status-item"><span class="bot-status-label">Admins configurés</span><span>${data.admin_ids_count}</span></div>
      </div>
    `;
  } else {
    badge.textContent = '🔴 Non configuré';
    badge.className = 'bot-badge bot-badge-error';
    details.innerHTML = `
      <p class="error">Le bot n'est pas configuré ou n'a pas démarré.</p>
      <p class="text-muted">Vérifie les variables <code>TELEGRAM_BOT_TOKEN</code> et <code>TELEGRAM_ADMIN_IDS</code> sur Render.</p>
    `;
  }

  if (configDetails) {
    configDetails.innerHTML = `
      <div class="bot-status-grid">
        <div class="bot-status-item">
          <span class="bot-status-label">Admins configurés</span>
          <span>${data.admin_ids_count} admin${data.admin_ids_count !== 1 ? 's' : ''}</span>
        </div>
      </div>
    `;
  }
}

const botTestBtn = document.getElementById('bot-test-btn');
const botTestResult = document.getElementById('bot-test-result');

if (botTestBtn) {
  botTestBtn.addEventListener('click', async () => {
    botTestBtn.disabled = true;
    botTestBtn.textContent = '⏳ Envoi…';
    if (botTestResult) {
      botTestResult.classList.add('hidden');
      botTestResult.className = 'bot-test-result hidden';
    }

    const res = await apiFetch('/api/bot/test', 'POST');
    botTestBtn.disabled = false;
    botTestBtn.textContent = '📨 Envoyer un message test';

    if (!botTestResult) return;
    const data = await res.json().catch(() => ({}));
    botTestResult.classList.remove('hidden');
    if (res.ok && data.success) {
      botTestResult.className = 'bot-test-result bot-test-ok';
      botTestResult.textContent = `✅ Message envoyé à ${data.sent_to} admin${data.sent_to !== 1 ? 's' : ''} !`;
    } else {
      botTestResult.className = 'bot-test-result bot-test-error';
      botTestResult.textContent = `❌ ${data.error || 'Erreur lors de l\'envoi.'}`;
    }
  });
}

/* ─── Utils ─── */
function esc(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}
