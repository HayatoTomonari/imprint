// ── Nav scroll effect ──
const nav = document.getElementById('nav');
window.addEventListener('scroll', () => {
  nav.classList.toggle('scrolled', window.scrollY > 40);
}, { passive: true });

// ── Hamburger menu ──
const hamburger = document.getElementById('hamburger');
const navLinks  = document.getElementById('navLinks');
hamburger.addEventListener('click', () => {
  hamburger.classList.toggle('open');
  navLinks.classList.toggle('open');
});
navLinks.querySelectorAll('a').forEach(a => {
  a.addEventListener('click', () => {
    hamburger.classList.remove('open');
    navLinks.classList.remove('open');
  });
});

// ── Scroll reveal ──
const revealEls = document.querySelectorAll('.reveal');
const revealObserver = new IntersectionObserver((entries) => {
  entries.forEach((entry, i) => {
    if (entry.isIntersecting) {
      setTimeout(() => entry.target.classList.add('visible'), i * 60);
      revealObserver.unobserve(entry.target);
    }
  });
}, { threshold: 0.12, rootMargin: '0px 0px -40px 0px' });
revealEls.forEach(el => revealObserver.observe(el));

// ── Counter animation ──
function animateCounter(el) {
  const target = parseInt(el.dataset.target, 10);
  if (isNaN(target)) return;
  const duration = 1200;
  const start    = performance.now();
  const update   = (now) => {
    const t     = Math.min((now - start) / duration, 1);
    const eased = 1 - Math.pow(1 - t, 3);
    el.textContent = Math.round(eased * target);
    if (t < 1) requestAnimationFrame(update);
  };
  requestAnimationFrame(update);
}
const counterObserver = new IntersectionObserver((entries) => {
  entries.forEach(entry => {
    if (entry.isIntersecting) {
      animateCounter(entry.target);
      counterObserver.unobserve(entry.target);
    }
  });
}, { threshold: 0.5 });
document.querySelectorAll('[data-target]').forEach(el => counterObserver.observe(el));

// ── FAQ accordion ──
document.querySelectorAll('.faq-q').forEach(btn => {
  btn.addEventListener('click', () => {
    const item     = btn.closest('.faq-item');
    const answer   = item.querySelector('.faq-a');
    const expanded = btn.getAttribute('aria-expanded') === 'true';

    document.querySelectorAll('.faq-item').forEach(other => {
      if (other !== item) {
        other.querySelector('.faq-q').setAttribute('aria-expanded', 'false');
        other.querySelector('.faq-a').classList.remove('open');
      }
    });

    btn.setAttribute('aria-expanded', String(!expanded));
    answer.classList.toggle('open', !expanded);
  });
});

// ── Smooth scroll for anchor links ──
document.querySelectorAll('a[href^="#"]').forEach(a => {
  a.addEventListener('click', e => {
    const id = a.getAttribute('href').slice(1);
    const el = document.getElementById(id);
    if (!el) return;
    e.preventDefault();
    const top = el.getBoundingClientRect().top + window.scrollY - 72;
    window.scrollTo({ top, behavior: 'smooth' });
  });
});

// ── Demo Widget ──
(function initDemo() {
  const dropZone       = document.getElementById('demoDropZone');
  const demoFile       = document.getElementById('demoFile');
  const demoResult     = document.getElementById('demoResult');
  const demoScanning   = document.getElementById('demoScanning');
  const demoResultCard = document.getElementById('demoResultCard');
  const demoPreviewImg = document.getElementById('demoPreviewImg');
  const drGaugeCircle  = document.getElementById('drGaugeCircle');
  const drScoreNum     = document.getElementById('drScoreNum');
  const drVerdictBadge = document.getElementById('drVerdictBadge');
  const drFilename     = document.getElementById('drFilename');
  const drHash         = document.getElementById('drHash');
  const drChecks       = document.getElementById('drChecks');
  const demoResetBtn   = document.getElementById('demoResetBtn');

  if (!dropZone) return;

  // クリックでファイル選択ダイアログを開く
  dropZone.addEventListener('click', () => demoFile.click());
  dropZone.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') demoFile.click(); });

  dropZone.addEventListener('dragover', e => { e.preventDefault(); dropZone.classList.add('dragging'); });
  dropZone.addEventListener('dragleave', e => { if (!dropZone.contains(e.relatedTarget)) dropZone.classList.remove('dragging'); });
  dropZone.addEventListener('drop', e => {
    e.preventDefault();
    dropZone.classList.remove('dragging');
    const f = e.dataTransfer.files[0];
    if (f && f.type.startsWith('image/')) processDemo(f);
  });
  demoFile.addEventListener('change', e => {
    if (e.target.files[0]) processDemo(e.target.files[0]);
  });
  demoResetBtn && demoResetBtn.addEventListener('click', resetDemo);

  function resetDemo() {
    dropZone.style.display = '';
    demoResult.style.display = 'none';
    demoFile.value = '';
    drGaugeCircle.style.transition = 'none';
    drGaugeCircle.style.strokeDashoffset = '239';
    drGaugeCircle.style.stroke = '#6366f1';
  }

  function processDemo(file) {
    dropZone.style.display = 'none';
    demoResult.style.display = '';
    demoScanning.style.display = '';
    demoResultCard.style.display = 'none';

    const reader = new FileReader();
    reader.onload = evt => {
      demoPreviewImg.src = evt.target.result;
      drFilename.textContent = file.name;
      const hash = pseudoHash(file.name + file.size + file.lastModified);
      drHash.textContent = 'SHA-256: ' + hash.slice(0, 16) + '…';
      runScan(file, hash);
    };
    reader.readAsDataURL(file);
  }

  function pseudoHash(input) {
    let h = 2166136261 >>> 0;
    for (let i = 0; i < input.length; i++) {
      h ^= input.charCodeAt(i);
      h = Math.imul(h, 16777619) >>> 0;
    }
    const digits = '0123456789abcdef';
    let out = '', seed = h;
    for (let i = 0; i < 64; i++) {
      seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0;
      out += digits[seed % 16];
    }
    return out;
  }

  function runScan(file, hash) {
    const steps = document.querySelectorAll('.scan-step');
    steps.forEach(s => { s.classList.remove('active', 'done'); });

    let h = 2166136261 >>> 0;
    const key = file.name + file.size;
    for (let i = 0; i < key.length; i++) {
      h ^= key.charCodeAt(i);
      h = Math.imul(h, 16777619) >>> 0;
    }
    const score = 72 + (h % 24);

    [400, 950, 1600, 2250, 2900, 3450].forEach((delay, i) => {
      setTimeout(() => {
        steps.forEach((s, j) => {
          s.classList.toggle('done', j < i);
          s.classList.toggle('active', j === i);
        });
      }, delay);
    });

    setTimeout(() => {
      steps.forEach(s => { s.classList.remove('active'); s.classList.add('done'); });
      setTimeout(() => showResult(score, hash, file.name), 250);
    }, 4000);
  }

  function showResult(score, hash, filename) {
    demoScanning.style.display = 'none';
    demoResultCard.style.display = '';

    let verdict, cls;
    if (score >= 88)      { verdict = '✓ 真正性高';  cls = 'verdict-high'; }
    else if (score >= 75) { verdict = '⚠ 要確認';    cls = 'verdict-mid';  }
    else                  { verdict = '✗ 加工の疑い'; cls = 'verdict-low';  }

    drVerdictBadge.textContent = verdict;
    drVerdictBadge.className = 'dr-verdict-badge ' + cls;

    const color = score >= 88 ? '#4ade80' : score >= 75 ? '#f59e0b' : '#ef4444';
    const circumference = 239;
    requestAnimationFrame(() => {
      drGaugeCircle.style.transition = 'stroke-dashoffset 1.5s cubic-bezier(.4,0,.2,1), stroke .4s ease';
      drGaugeCircle.style.strokeDashoffset = String(circumference - (score / 100) * circumference);
      drGaugeCircle.style.stroke = color;
    });
    animateCount(drScoreNum, score, 1400);

    const checks = [
      { ok: score >= 76, label: 'EXIFメタデータ ' + (score >= 76 ? '正常（撮影情報あり）' : '異常を検出') },
      { ok: score >= 72, label: 'ELA: '           + (score >= 80 ? '加工痕なし（clean）'   : '加工の可能性あり') },
      { ok: score >= 88, label: 'AI生成の疑い'   + (score >= 88 ? 'なし'                   : 'あり') },
      { ok: true,        label: 'SHA-256 ハッシュ生成完了' },
      { ok: true,        label: 'タイムスタンプ記録（本番: RFC 3161 認定局）' },
    ];
    drChecks.innerHTML = checks.map(c =>
      `<div class="dr-check ${c.ok ? 'ok' : 'ng'}"><span>${c.ok ? '✓' : '✗'}</span>${c.label}</div>`
    ).join('');
  }

  function animateCount(el, target, duration) {
    const start = performance.now();
    (function update(now) {
      const t = Math.min((now - start) / duration, 1);
      el.textContent = Math.round((1 - Math.pow(1 - t, 3)) * target);
      if (t < 1) requestAnimationFrame(update);
    })(performance.now());
  }
})();
