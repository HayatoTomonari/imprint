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
