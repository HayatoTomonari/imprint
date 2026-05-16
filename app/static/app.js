const _cfg        = document.getElementById('config');
let   API_KEY     = _cfg.dataset.apiKey;
const NETWORK     = _cfg.dataset.network;
const CHAIN_ID    = parseInt(_cfg.dataset.chainId, 10);
const STRIPE_ON   = _cfg.dataset.stripe === 'true';
const USER_PLAN   = _cfg.dataset.plan;

// ── API キーをサーバーから取得してダッシュボードに表示 ──
(async function loadApiKey() {
  const valueEl   = document.getElementById('apikeyValue');
  const noticeEl  = document.getElementById('apikeyFirstTime');
  try {
    const res  = await fetch('/auth/apikey', { credentials: 'same-origin' });
    if (!res.ok) { valueEl.textContent = '(取得失敗)'; return; }
    const data = await res.json();
    if (data.first_time && data.raw_key) {
      API_KEY = data.raw_key;
      valueEl.textContent = data.raw_key;
      noticeEl.classList.remove('hidden');
    } else if (data.key_id) {
      API_KEY = '';
      valueEl.textContent = `imp_${'*'.repeat(32)}  (IDのみ表示: ${data.key_id})`;
    } else {
      valueEl.textContent = '(APIキーなし)';
    }
  } catch {
    valueEl.textContent = '(取得失敗)';
  }
})();

// ── アップグレードボタン ──
(function initUpgradeBtn() {
  const btn = document.getElementById('upgradeBtn');
  if (!btn) return;
  if (STRIPE_ON && USER_PLAN === 'starter') {
    btn.classList.remove('hidden');
    btn.addEventListener('click', upgradeToB);
  }
})();

async function upgradeToB() {
  const btn = document.getElementById('upgradeBtn');
  btn.textContent = '処理中...';
  btn.style.pointerEvents = 'none';
  try {
    const res  = await fetch('/billing/checkout', { method: 'POST', credentials: 'same-origin' });
    const data = await res.json();
    if (data.url) {
      window.location.href = data.url;
    } else {
      alert(data.detail || 'エラーが発生しました');
      btn.textContent = 'Business にアップグレード（¥9,800/月）';
      btn.style.pointerEvents = '';
    }
  } catch {
    alert('サーバーへの接続に失敗しました');
    btn.textContent = 'Business にアップグレード（¥9,800/月）';
    btn.style.pointerEvents = '';
  }
}

// ネットワークバッジを初期表示
(function () {
  const badge = document.getElementById('networkBadge');
  if (!badge) return;
  const isMainnet = CHAIN_ID === 137;
  badge.textContent = NETWORK;
  badge.className = 'network-badge ' + (isMainnet ? 'network-mainnet' : 'network-testnet');
})();

const uploadZone  = document.getElementById('uploadZone');
const fileInput   = document.getElementById('fileInput');
const loadingEl   = document.getElementById('loading');
const errorBox    = document.getElementById('errorBox');
const resultsEl   = document.getElementById('results');

let currentFile = null;

// ── Upload events ──────────────────────────────────────────

uploadZone.addEventListener('click', () => fileInput.click());

uploadZone.addEventListener('dragover', e => {
  e.preventDefault();
  uploadZone.classList.add('drag-over');
});
uploadZone.addEventListener('dragleave', () => uploadZone.classList.remove('drag-over'));
uploadZone.addEventListener('drop', e => {
  e.preventDefault();
  uploadZone.classList.remove('drag-over');
  const f = e.dataTransfer.files[0];
  if (f) handleFile(f);
});
fileInput.addEventListener('change', () => {
  if (fileInput.files[0]) handleFile(fileInput.files[0]);
});

// ── Verify ────────────────────────────────────────────────

async function handleFile(file) {
  currentFile = file;

  show(loadingEl);
  hide(resultsEl);
  hide(errorBox);

  const fd = new FormData();
  fd.append('file', file);

  try {
    const res = await fetch('/verify', {
      method: 'POST',
      headers: { 'X-API-Key': API_KEY },
      body: fd,
    });
    if (!res.ok) {
      const err = await res.json();
      showError(err.detail || 'エラーが発生しました');
      return;
    }
    renderResults(await res.json());
  } catch {
    showError('サーバーへの接続に失敗しました');
  } finally {
    hide(loadingEl);
  }
}

// ── Render ────────────────────────────────────────────────

function renderResults(data) {
  const auth    = data.authenticity;
  const score   = auth.score;
  const verdict = auth.verdict;
  const COLOR   = { high: '#16a34a', medium: '#d97706', low: '#dc2626' };

  // Score gauge
  const C = 2 * Math.PI * 42; // circumference ≈ 263.9
  const gauge = document.getElementById('gaugeFill');
  gauge.style.strokeDashoffset = C - (score / 100) * C;
  gauge.style.stroke = COLOR[verdict] || '#9ca3af';

  const numEl = document.getElementById('scoreNum');
  numEl.textContent  = score;
  numEl.style.color  = COLOR[verdict] || '#9ca3af';

  // Verdict badge
  const badge = document.getElementById('verdictBadge');
  badge.textContent = auth.verdict_label;
  badge.className   = 'verdict-badge verdict-' + verdict;

  // Filename
  document.getElementById('resultFilename').textContent = data.file.filename;

  // Checklist — non-technical summary
  const checkItems = [];
  if (data.exif.has_exif) {
    checkItems.push({ cls: 'check-item-ok', text: '撮影情報あり（カメラ・日時などのデータが記録されています）' });
  } else {
    checkItems.push({ cls: 'check-item-bad', text: '撮影情報なし（EXIFデータが見つかりませんでした）' });
  }
  const ELA_CHK = {
    clean:         { cls: 'check-item-ok',   text: '加工痕なし（改ざんは検出されませんでした）' },
    suspicious:    { cls: 'check-item-warn', text: '加工の疑いあり（一部に通常と異なる痕跡があります）' },
    likely_edited: { cls: 'check-item-bad',  text: '加工・改ざんを検出（画像が編集された可能性が高いです）' },
    unknown:       { cls: 'check-item-info', text: '加工チェック：判定不能' },
    error:         { cls: 'check-item-info', text: '加工チェック：エラーが発生しました' },
  };
  checkItems.push(ELA_CHK[data.ela.ela_verdict] || { cls: 'check-item-info', text: '加工チェック：不明' });
  if (data.ai_detection) {
    const AI_CHK = {
      likely_real:  { cls: 'check-item-ok',   text: 'AI生成の可能性は低い（本物の写真と判定されました）' },
      suspicious:   { cls: 'check-item-warn', text: 'AI生成の疑いあり（念のご確認をお勧めします）' },
      ai_generated: { cls: 'check-item-bad',  text: 'AI生成画像の可能性が高いです' },
      unknown:      { cls: 'check-item-info', text: 'AI判定：判定不能' },
    };
    checkItems.push(AI_CHK[data.ai_detection.verdict] || { cls: 'check-item-info', text: 'AI判定：不明' });
  }
  if (data.timestamp) {
    checkItems.push({ cls: 'check-item-ok', text: `日時証明済み（${fmtDate(data.timestamp.tsa_time)} ・ RFC 3161 認定）` });
  } else {
    checkItems.push({ cls: 'check-item-info', text: '日時証明：未取得（取得するとスコアが上がります）' });
  }
  document.getElementById('checklist').innerHTML =
    checkItems.map(i => `<li class="check-item ${i.cls}">${i.text}</li>`).join('');

  // Deductions (technical detail, inside <details>)
  const ded = document.getElementById('deductions');
  ded.innerHTML =
    auth.deductions.map(d =>
      `<div class="deduction-item">▼${Math.abs(d.penalty)}点 ${d.factor} — ${d.reason}</div>`
    ).join('') +
    auth.details.map(d =>
      `<div class="detail-item">✓ ${d}</div>`
    ).join('');

  // File info table
  renderTable('fileInfo', [
    ['ファイル名', data.file.filename],
    ['フォーマット', `${data.file.format} (${data.file.content_type})`],
    ['サイズ', fmtBytes(data.file.size_bytes)],
    ['解像度', `${data.file.width} × ${data.file.height} px`],
    ['検証日時', fmtDate(data.verified_at)],
  ]);

  // Hash
  document.getElementById('hashValue').textContent = data.hash.value;

  // EXIF
  const exif = data.exif;
  renderTable('exifInfo', [
    ['EXIFデータ', exif.has_exif ? 'あり' : 'なし'],
    ['カメラ', [exif.camera_make, exif.camera_model].filter(Boolean).join(' ') || '—'],
    ['撮影日時', exif.datetime_original || '—'],
    ['焦点距離', exif.focal_length ? `${exif.focal_length} mm` : '—'],
    ['ISO', exif.iso ?? '—'],
    ['GPS', exif.gps_latitude != null
      ? `${exif.gps_latitude}, ${exif.gps_longitude}`
      : '未記録'],
    ['ソフトウェア', exif.software || '—'],
    ['ソフトウェア種別', exif.software_category || '—'],
  ]);

  const warnEl = document.getElementById('exifWarnings');
  warnEl.innerHTML = (exif.warnings || [])
    .map(w => `<div class="warning-item">${w}</div>`)
    .join('');

  // ELA
  const ela = data.ela;
  const ELA_LABEL = {
    clean: '加工なし', suspicious: '要注意',
    likely_edited: '加工の可能性あり', unknown: '不明', error: 'エラー',
  };
  const elaEl = document.getElementById('elaVerdict');
  elaEl.textContent = ELA_LABEL[ela.ela_verdict] || ela.ela_verdict;
  elaEl.className   = 'ela-verdict ela-' + ela.ela_verdict;

  renderTable('elaInfo', [
    ['不自然なピクセルの割合', `${(ela.ela_suspicious_ratio * 100).toFixed(2)}%`],
  ]);

  renderTable('elaRaw', [
    ['疑わしいピクセル比率', `${(ela.ela_suspicious_ratio * 100).toFixed(2)}%`],
    ['平均誤差（Mean diff）', ela.ela_mean_diff],
    ['最大誤差（Max diff）', ela.ela_max_diff],
  ]);

  // AI Detection
  const ai = data.ai_detection;
  if (ai) {
    const AI_LABEL = {
      ai_generated: '🤖 AI生成画像',
      suspicious:   '⚠️ AI生成の疑い',
      likely_real:  '✅ 本物の写真',
      unknown:      '— 不明',
    };
    const aiEl = document.getElementById('aiVerdict');
    aiEl.textContent = AI_LABEL[ai.verdict] || ai.verdict;
    aiEl.className   = 'ai-verdict ai-' + ai.verdict;

    const aiRows = [
      ['判定', AI_LABEL[ai.verdict] || ai.verdict],
      ['検出方法', ai.method || '—'],
      ['詳細', ai.detail || '—'],
    ];
    if (ai.ai_score != null) {
      aiRows.push(['AIスコア', `${(ai.ai_score * 100).toFixed(1)}%`]);
      aiRows.push(['実写スコア', `${(ai.real_score * 100).toFixed(1)}%`]);
    }
    if (ai.noise_std != null) {
      aiRows.push(['ノイズ標準偏差', ai.noise_std]);
    }
    renderTable('aiInfo', aiRows);
  }

  // Timestamp
  renderTimestamp(data.timestamp, data.hash.value);

  // PDF download button reset
  const dlBtn = document.getElementById('downloadBtn');
  dlBtn.disabled    = false;
  dlBtn.textContent = 'PDF証明書をダウンロード';

  // Blockchain reset
  const btn = document.getElementById('registerBtn');
  btn.disabled    = false;
  btn.textContent = 'ブロックチェーンに記録する';
  hide(document.getElementById('blockchainResult'));

  show(resultsEl);
}

// ── PDF Download ──────────────────────────────────────────

document.getElementById('downloadBtn').addEventListener('click', async () => {
  if (!currentFile) return;

  const btn = document.getElementById('downloadBtn');
  btn.disabled    = true;
  btn.textContent = 'PDF生成中...';

  const fd = new FormData();
  fd.append('file', currentFile);

  try {
    const res = await fetch('/certificate', {
      method: 'POST',
      headers: { 'X-API-Key': API_KEY },
      body: fd,
    });
    if (!res.ok) {
      const err = await res.json();
      showError(err.detail || 'PDF生成に失敗しました');
      btn.disabled    = false;
      btn.textContent = 'PDF証明書をダウンロード';
      return;
    }
    const blob     = await res.blob();
    const url      = URL.createObjectURL(blob);
    const hashPfx  = document.getElementById('hashValue').textContent.slice(0, 12);
    const a        = document.createElement('a');
    a.href         = url;
    a.download     = `imprint_certificate_${hashPfx}.pdf`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
    btn.textContent = 'ダウンロード完了';
  } catch {
    showError('サーバーへの接続に失敗しました');
    btn.disabled    = false;
    btn.textContent = 'PDF証明書をダウンロード';
  }
});

// ── Timestamp ─────────────────────────────────────────────

function renderTimestamp(ts, imageHash) {
  const badge  = document.getElementById('tsBadge');
  const note   = document.getElementById('tsNote');
  const tsBtn  = document.getElementById('tsBtn');

  hide(document.getElementById('tsResult'));

  const tsaSelector = document.getElementById('tsaSelector');

  if (ts) {
    badge.textContent = '✅ タイムスタンプ取得済み';
    badge.className   = 'ts-badge ts-certified';
    note.textContent  = 'RFC 3161 準拠のタイムスタンプにより、写真がこの時点に存在したことが第三者機関に認定されています。';
    hide(tsBtn);
    hide(tsaSelector);
    renderTable('tsInfo', [
      ['TSA', ts.tsa],
      ['認定日時', fmtDate(ts.tsa_time)],
      ['シリアル番号', ts.serial_no || '—'],
      ['取得日時', fmtDate(ts.requested_at)],
    ]);
  } else {
    badge.textContent = '未取得';
    badge.className   = 'ts-badge ts-none';
    note.textContent  = 'タイムスタンプを取得すると、写真がこの時点に存在したことを第三者機関が証明します。';
    tsBtn.disabled    = false;
    tsBtn.textContent = '日時証明を取得する';
    show(tsBtn);
    show(tsaSelector);
    document.getElementById('tsInfo').innerHTML = '';
  }
}

document.getElementById('tsBtn').addEventListener('click', async () => {
  if (!currentFile) return;

  const tsBtn    = document.getElementById('tsBtn');
  const resultEl = document.getElementById('tsResult');

  tsBtn.disabled    = true;
  tsBtn.textContent = '取得中...';

  const fd = new FormData();
  fd.append('file', currentFile);
  const selectedTsa = document.querySelector('input[name="tsa"]:checked')?.value;
  if (selectedTsa) fd.append('tsa', selectedTsa);

  try {
    const res  = await fetch('/timestamp/request', {
      method: 'POST',
      headers: { 'X-API-Key': API_KEY },
      body: fd,
    });
    const data = await res.json();

    if (!res.ok) {
      resultEl.className = 'blockchain-result bc-error';
      resultEl.innerHTML = `<strong>エラー:</strong> ${data.detail}`;
    } else {
      resultEl.className = 'blockchain-result bc-success';
      resultEl.innerHTML =
        `<strong>✅ タイムスタンプを取得しました</strong><br>` +
        `TSA: ${data.tsa}<br>` +
        `認定日時: ${fmtDate(data.tsa_time)}<br>` +
        `シリアル番号: ${data.serial_no || '—'}`;

      // バッジ・テーブルを更新
      renderTimestamp({
        tsa: data.tsa,
        tsa_time: data.tsa_time,
        serial_no: data.serial_no,
        requested_at: data.requested_at,
      }, null);
    }
    show(resultEl);
  } catch {
    resultEl.className = 'blockchain-result bc-error';
    resultEl.innerHTML = 'サーバーへの接続に失敗しました';
    show(resultEl);
    tsBtn.disabled    = false;
    tsBtn.textContent = '日時証明を取得する';
  }
});

// ── Blockchain ────────────────────────────────────────────

document.getElementById('registerBtn').addEventListener('click', async () => {
  if (!currentFile) return;

  const btn       = document.getElementById('registerBtn');
  const resultDiv = document.getElementById('blockchainResult');

  btn.disabled    = true;
  btn.textContent = '送信中...';

  const fd = new FormData();
  fd.append('file', currentFile);

  try {
    const res  = await fetch('/blockchain/register', {
      method: 'POST',
      headers: { 'X-API-Key': API_KEY },
      body: fd,
    });
    const data = await res.json();

    if (!res.ok) {
      resultDiv.className = 'blockchain-result bc-error';
      resultDiv.innerHTML = `<strong>エラー:</strong> ${data.detail}`;
    } else if (data.status === 'already_registered') {
      resultDiv.className = 'blockchain-result bc-already';
      resultDiv.innerHTML =
        `<strong>既に登録済みです</strong><br>` +
        `登録日時: ${fmtDate(data.registered_at)}<br>` +
        `登録者: <code>${data.registrar}</code>`;
      btn.textContent = '登録済み';
    } else {
      resultDiv.className = 'blockchain-result bc-success';
      resultDiv.innerHTML =
        `<strong>✅ ブロックチェーンに記録しました</strong><br>` +
        `ブロック: ${data.block_number.toLocaleString()}<br>` +
        `ガス使用量: ${data.gas_used.toLocaleString()}<br>` +
        `<a href="${data.explorer_url}" target="_blank" class="tx-link">` +
        `Polygonscanで確認 →</a>`;
      btn.textContent = '記録済み';
    }
    show(resultDiv);
  } catch {
    resultDiv.className = 'blockchain-result bc-error';
    resultDiv.innerHTML = 'サーバーへの接続に失敗しました';
    show(resultDiv);
    btn.disabled    = false;
    btn.textContent = 'ブロックチェーンに記録する';
  }
});

// ── Utilities ─────────────────────────────────────────────

function show(el) { el.classList.remove('hidden'); }
function hide(el) { el.classList.add('hidden'); }

function showError(msg) {
  errorBox.textContent = msg;
  show(errorBox);
  hide(loadingEl);
}

function renderTable(id, rows) {
  document.getElementById(id).innerHTML = rows
    .map(([k, v]) => `<tr><td>${k}</td><td>${v ?? '—'}</td></tr>`)
    .join('');
}

function fmtBytes(b) {
  if (b < 1024)       return b + ' B';
  if (b < 1024*1024)  return (b / 1024).toFixed(1) + ' KB';
  return (b / 1024 / 1024).toFixed(2) + ' MB';
}

function fmtDate(iso) {
  return new Date(iso).toLocaleString('ja-JP');
}

// ══════════════════════════════════════════════════════════════
//  FETCH HELPER（タイムアウト付き）
// ══════════════════════════════════════════════════════════════

async function apiFetch(url, opts = {}, timeoutMs = 20000) {
  const ctrl = new AbortController();
  const tid  = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const res = await fetch(url, { ...opts, signal: ctrl.signal, credentials: 'same-origin' });
    clearTimeout(tid);
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.detail || `サーバーエラー (HTTP ${res.status})`);
    }
    return res;
  } catch (err) {
    clearTimeout(tid);
    if (err.name === 'AbortError') throw new Error('応答がタイムアウトしました。再度お試しください。');
    throw err;
  }
}

// ══════════════════════════════════════════════════════════════
//  SIDEBAR NAVIGATION & TAB SWITCHING
// ══════════════════════════════════════════════════════════════

const sidebar      = document.getElementById('sidebar');
const sidebarToggle = document.getElementById('sidebarToggle');

sidebarToggle.addEventListener('click', () => {
  sidebar.classList.toggle('open');
});

// close sidebar on overlay click (mobile)
document.addEventListener('click', e => {
  if (window.innerWidth <= 700 && !sidebar.contains(e.target) && e.target !== sidebarToggle && !sidebarToggle.contains(e.target)) {
    sidebar.classList.remove('open');
  }
});

let _historyLoaded = false;
let _statsLoaded   = false;
let _profileLoaded = false;
let _monthlyChart  = null;
let _verdictChart  = null;
let _historyPage   = 1;

document.querySelectorAll('.sidebar-link[data-tab]').forEach(link => {
  link.addEventListener('click', e => {
    e.preventDefault();
    const tabId = link.dataset.tab;

    document.querySelectorAll('.sidebar-link').forEach(l => l.classList.remove('active'));
    document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));

    link.classList.add('active');
    document.getElementById(tabId).classList.add('active');

    if (window.innerWidth <= 700) sidebar.classList.remove('open');

    if (tabId === 'tab-history' && !_historyLoaded) { loadHistory(1); _historyLoaded = true; }
    if (tabId === 'tab-stats'   && !_statsLoaded)   { loadStats();            _statsLoaded   = true; }
    if (tabId === 'tab-profile' && !_profileLoaded) { loadProfile();          _profileLoaded = true; }
  });
});

// ══════════════════════════════════════════════════════════════
//  HISTORY TAB
// ══════════════════════════════════════════════════════════════

const VERDICT_JP = { high: '真正性：高', medium: '真正性：中', low: '真正性：低' };
const ELA_JP     = { clean: 'クリーン', suspicious: '要注意', likely_edited: '加工あり', unknown: '不明', error: 'エラー' };
const AI_JP      = { likely_real: '本物', suspicious: '疑い', ai_generated: 'AI生成', unknown: '不明' };

async function loadHistory(page) {
  _historyPage = page;
  const loadEl    = document.getElementById('historyLoading');
  const errEl     = document.getElementById('historyError');
  const emptyEl   = document.getElementById('historyEmpty');
  const contentEl = document.getElementById('historyContent');

  show(loadEl); hide(errEl); hide(emptyEl); hide(contentEl);

  try {
    const res  = await apiFetch(`/dashboard/history?page=${page}`);
    const data = await res.json();

    hide(loadEl);
    if (!data.items || data.items.length === 0) {
      show(emptyEl);
      return;
    }
    show(contentEl);

    document.getElementById('historyTotal').textContent = `${data.total} 件`;

    const tbody = document.getElementById('historyBody');
    tbody.innerHTML = data.items.map(item => {
      const scoreColor = item.verdict === 'high' ? '#16a34a' : item.verdict === 'medium' ? '#d97706' : '#dc2626';
      const verdictBadge = item.verdict
        ? `<span class="badge-${item.verdict}">${VERDICT_JP[item.verdict] || item.verdict}</span>`
        : '—';
      const elaBadge = item.ela_verdict
        ? `<span class="badge-${item.ela_verdict === 'clean' ? 'yes' : item.ela_verdict === 'likely_edited' ? 'low' : 'medium'}">${ELA_JP[item.ela_verdict] || item.ela_verdict}</span>`
        : '—';
      const aiBadge = item.ai_verdict
        ? `<span class="badge-${item.ai_verdict === 'likely_real' ? 'yes' : item.ai_verdict === 'ai_generated' ? 'low' : 'medium'}">${AI_JP[item.ai_verdict] || item.ai_verdict}</span>`
        : '—';
      const tsBadge = item.has_timestamp
        ? '<span class="badge-yes">✓ 取得済</span>'
        : '<span class="badge-no">未取得</span>';
      const bcBadge = item.has_blockchain
        ? (item.explorer_url
            ? `<a href="${item.explorer_url}" target="_blank" class="badge-yes" style="text-decoration:none">✓ 記録済</a>`
            : '<span class="badge-yes">✓ 記録済</span>')
        : '<span class="badge-no">未記録</span>';
      const dt = item.created_at ? new Date(item.created_at).toLocaleDateString('ja-JP') : '—';

      return `<tr>
        <td><div class="hist-filename" title="${escHtml(item.filename)}">${escHtml(item.filename)}</div></td>
        <td class="score-cell" style="color:${scoreColor}">${item.score != null ? item.score : '—'}</td>
        <td>${verdictBadge}</td>
        <td>${elaBadge}</td>
        <td>${aiBadge}</td>
        <td>${tsBadge}</td>
        <td>${bcBadge}</td>
        <td>${dt}</td>
      </tr>`;
    }).join('');

    // pagination
    const totalPages = Math.ceil(data.total / data.per_page);
    const paginationEl = document.getElementById('historyPagination');
    if (totalPages <= 1) {
      paginationEl.innerHTML = '';
    } else {
      let html = '';
      for (let p = 1; p <= totalPages; p++) {
        html += `<button class="page-btn${p === page ? ' active' : ''}" onclick="loadHistory(${p})">${p}</button>`;
      }
      paginationEl.innerHTML = html;
    }
  } catch (err) {
    hide(loadEl);
    errEl.textContent = err.message || '読み込みに失敗しました';
    show(errEl);
  }
}

document.getElementById('historyRefresh')?.addEventListener('click', () => {
  _historyLoaded = false;
  loadHistory(_historyPage);
  _historyLoaded = true;
});

function escHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// ══════════════════════════════════════════════════════════════
//  STATS TAB
// ══════════════════════════════════════════════════════════════

async function loadStats() {
  const loadEl    = document.getElementById('statsLoading');
  const errEl     = document.getElementById('statsError');
  const contentEl = document.getElementById('statsContent');
  show(loadEl); hide(errEl); hide(contentEl);

  try {
    const res  = await apiFetch('/dashboard/stats');
    const data = await res.json();
    hide(loadEl);
    show(contentEl);

    document.getElementById('statTotal').textContent    = data.total_analyses ?? 0;
    document.getElementById('statAvgScore').textContent = data.avg_score != null ? `${data.avg_score}点` : '—';
    document.getElementById('statTsCount').textContent  = data.ts_count ?? 0;
    document.getElementById('statBcCount').textContent  = data.bc_count ?? 0;

    // Usage bar
    const cur = data.current_usage ?? 0;
    const lim = data.usage_limit;
    document.getElementById('usageBarCurrent').textContent = `${cur} 回`;
    document.getElementById('usageBarLimit').textContent   = lim != null ? `/ ${lim} 回` : '（無制限）';
    const pct = lim ? Math.min(cur / lim * 100, 100) : 0;
    document.getElementById('usageBarFill').style.width = `${pct}%`;

    // Monthly chart
    const monthly = data.monthly_usage || [];
    if (_monthlyChart) { _monthlyChart.destroy(); _monthlyChart = null; }
    if (monthly.length > 0) {
      const ctx = document.getElementById('monthlyChart').getContext('2d');
      _monthlyChart = new Chart(ctx, {
        type: 'bar',
        data: {
          labels: monthly.map(m => m.month),
          datasets: [{
            label: '解析回数',
            data: monthly.map(m => m.count),
            backgroundColor: '#818cf8',
            borderRadius: 5,
          }],
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: { legend: { display: false } },
          scales: {
            y: { beginAtZero: true, ticks: { stepSize: 1 }, grid: { color: '#f3f4f6' } },
            x: { grid: { display: false } },
          },
        },
      });
    }

    // Verdict doughnut
    const vdist = data.verdict_dist || [];
    if (_verdictChart) { _verdictChart.destroy(); _verdictChart = null; }
    if (vdist.length > 0) {
      const VCOL = { high: '#16a34a', medium: '#d97706', low: '#dc2626' };
      const ctx2 = document.getElementById('verdictChart').getContext('2d');
      _verdictChart = new Chart(ctx2, {
        type: 'doughnut',
        data: {
          labels: vdist.map(v => VERDICT_JP[v.verdict] || v.verdict || '不明'),
          datasets: [{
            data: vdist.map(v => v.count),
            backgroundColor: vdist.map(v => VCOL[v.verdict] || '#9ca3af'),
            borderWidth: 2,
            borderColor: '#fff',
          }],
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: {
            legend: { position: 'bottom', labels: { font: { size: 12 }, padding: 12 } },
          },
          cutout: '65%',
        },
      });
    }
  } catch (err) {
    hide(loadEl);
    errEl.textContent = err.message || '読み込みに失敗しました';
    show(errEl);
  }
}

// ══════════════════════════════════════════════════════════════
//  PROFILE TAB
// ══════════════════════════════════════════════════════════════

async function loadProfile() {
  const loadEl    = document.getElementById('profileLoading');
  const contentEl = document.getElementById('profileContent');
  show(loadEl); hide(contentEl);

  try {
    const res  = await apiFetch('/auth/profile');
    const data = await res.json();
    hide(loadEl);
    show(contentEl);

    document.getElementById('profileEmail').textContent   = data.email;
    document.getElementById('profilePlan').innerHTML      =
      `<span class="plan-badge plan-${data.plan}">${data.plan_label}</span>`;
    document.getElementById('profileCreated').textContent =
      data.created_at ? new Date(data.created_at).toLocaleDateString('ja-JP') : '—';
    document.getElementById('profileTotal').textContent   = `${data.total_analyses} 回`;
    document.getElementById('profileKeyId').textContent   = data.api_key_id ? `…${data.api_key_id}` : '—';
    document.getElementById('profileKeyCreated').textContent =
      data.api_key_created ? new Date(data.api_key_created).toLocaleDateString('ja-JP') : '—';

    // upgrade button
    const upWrap = document.getElementById('profileUpgradeWrap');
    if (STRIPE_ON && data.plan === 'starter') {
      upWrap.classList.remove('hidden');
      document.getElementById('profileUpgradeBtn').addEventListener('click', upgradeToB);
    }
  } catch {
    hide(loadEl);
    show(contentEl);
  }
}

// Password change
document.getElementById('pwForm')?.addEventListener('submit', async e => {
  e.preventDefault();
  const msg      = document.getElementById('pwMsg');
  const current  = document.getElementById('pwCurrent').value;
  const newPw    = document.getElementById('pwNew').value;
  const confirm  = document.getElementById('pwConfirm').value;

  msg.classList.remove('ok', 'err');
  hide(msg);

  if (newPw !== confirm) {
    msg.textContent = '新しいパスワードが一致しません';
    msg.classList.add('err');
    show(msg);
    return;
  }

  const fd = new FormData();
  fd.append('current_password', current);
  fd.append('new_password', newPw);

  try {
    const res  = await fetch('/auth/profile/password', {
      method: 'POST',
      credentials: 'same-origin',
      body: fd,
    });
    const data = await res.json();
    if (res.ok) {
      msg.textContent = 'パスワードを変更しました';
      msg.classList.add('ok');
      document.getElementById('pwForm').reset();
    } else {
      msg.textContent = data.detail || 'エラーが発生しました';
      msg.classList.add('err');
    }
  } catch {
    msg.textContent = 'サーバーへの接続に失敗しました';
    msg.classList.add('err');
  }
  show(msg);
});
