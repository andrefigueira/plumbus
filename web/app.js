import { Viewer } from './viewer.js';

const $  = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];

const state = {
  backends: [],
  backend: null,
  sources: [],         // ordered; the selected engine consumes the first max_images
  imgProvider: null,
  jobs: [],
  selected: null,
  loadedUrl: null,
  thumbed: new Set(),
  profile: 'object',
  quality: 'detailed',
  step: 'define',
  resultTab: 'inspect',
  rig: null,           // { tree, joints, skins } for the loaded GLB, null when unrigged
  bone: null,          // selected bone uuid
  known: new Set(),    // job ids we have queued, so a poll race cannot unselect them
  viewerImages: [],    // what the centre pane is showing when it is not showing a mesh
  viewerSig: null,     // url signature of those images, so polling does not rebuild them
  drawing: false,      // a reference is being drawn; the viewer belongs to it
  match: new Map(),    // job id -> per-view reference match scores, computed on demand
  objectUrls: [],      // blob URLs held for the viewer, revoked on rebuild
};

const QUALITY = {
  preview: {
    label: 'Preview',
    note: 'A fast composition pass. Use it to catch silhouette and framing problems.',
    trellis: { pipeline_type: '512', texture_size: '512' },
    hunyuan: { octree_resolution: '128', steps: 25, max_faces: 40000 },
    fal: { model: 'fal-ai/trellis' },
    runpod: { engine: 'trellis', pipeline_type: '512', texture_size: '512', octree_resolution: '128', max_faces: 40000 },
  },
  detailed: {
    label: 'Detailed',
    note: 'High-resolution geometry and 2K materials—the best default for final assets.',
    trellis: { pipeline_type: '1024', texture_size: '2048' },
    hunyuan: { octree_resolution: '384', steps: 50, max_faces: 200000 },
    fal: { model: 'fal-ai/hunyuan3d/v2' },
    runpod: { engine: 'trellis', pipeline_type: '1024', texture_size: '2048', octree_resolution: '384', max_faces: 200000 },
  },
  hero: {
    label: 'Hero',
    note: 'Maximum geometry fidelity. Expect the longest run and a large source GLB.',
    trellis: { pipeline_type: '1024_cascade', texture_size: '2048' },
    hunyuan: { octree_resolution: '512', steps: 65, max_faces: 350000 },
    fal: { model: 'fal-ai/hyper3d/rodin' },
    runpod: { engine: 'trellis', pipeline_type: '1024_cascade', texture_size: '2048', octree_resolution: '512', max_faces: 350000 },
  },
};

const viewer = new Viewer($('#stage'));

function saveSetup() {
  try {
    localStorage.setItem('plumbus-setup', JSON.stringify({
      profile: state.profile,
      quality: state.quality,
      step: state.step,
      subject: $('#ref-subject')?.value || '',
    }));
  } catch {}
}

function loadSetup() {
  try {
    const saved = JSON.parse(localStorage.getItem('plumbus-setup') || '{}');
    if (['object', 'character'].includes(saved.profile)) state.profile = saved.profile;
    if (QUALITY[saved.quality]) state.quality = saved.quality;
    if (STEPS.includes(saved.step)) state.step = saved.step;
    if (saved.subject) $('#ref-subject').value = saved.subject;
  } catch {}
}

/* ── helpers ───────────────────────────────────────────────────────────── */

let toastTimer;
function toast(msg, bad = false) {
  const el = $('#toast');
  el.textContent = msg;
  el.classList.toggle('bad', bad);
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.hidden = true), bad ? 6000 : 3000);
}

const nf = new Intl.NumberFormat('en');

function ago(ts) {
  if (!ts) return '';
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return `${Math.round(s)}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

function dur(job) {
  if (!job.started_at) return '';
  const end = job.finished_at || Date.now() / 1000;
  const s = Math.round(end - job.started_at);
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m${String(s % 60).padStart(2, '0')}`;
}

async function api(url, opts) {
  const r = await fetch(url, opts);
  if (!r.ok) {
    let detail = r.statusText;
    try { detail = (await r.json()).detail ?? detail; } catch {}
    throw new Error(detail);
  }
  return r.json();
}

/* ── engine picker + dynamic params ────────────────────────────────────── */

async function loadBackends() {
  const previousId = state.backend?.id || null;
  let previousParams = null;
  if (previousId) {
    try { previousParams = collectParams($('#params'), { extras: false }); } catch {}
  }
  const data = await api('/api/backends');
  state.backends = data.backends;

  const wrap = $('#backends');
  wrap.innerHTML = '';

  for (const b of data.backends) {
    const el = document.createElement('label');
    el.className = 'backend' + (b.available ? '' : ' off');
    el.innerHTML = `
      <input type="radio" name="backend" value="${b.id}" ${b.available ? '' : 'disabled'}>
      <div class="backend-top">
        <span>${b.name}</span>
        <span class="kind ${b.kind}">${b.kind}</span>
      </div>
      <p>${b.blurb}</p>
      ${b.available ? '' : `<p class="why">${b.reason}</p>`}`;
    el.querySelector('input').addEventListener('change', () => selectBackend(b.id));
    wrap.appendChild(el);
  }

  const first =
    data.backends.find((b) => b.id === previousId && b.available) ||
    data.backends.find((b) => b.id === data.default && b.available) ||
    data.backends.find((b) => b.available);
  if (first) selectBackend(first.id, first.id === previousId ? previousParams : null);
  else {
    $('#params').innerHTML =
      '<div class="empty">No engine is usable yet.<br>Run <code>plumbus install trellis</code>, ' +
      'or set <code>FAL_KEY</code> in <code>.env</code> for the hosted fallback.</div>';
    $('#gen-note').textContent = '';
  }
}

function selectBackend(id, restore = null) {
  const b = state.backends.find((x) => x.id === id);
  if (!b || !b.available) return;
  state.backend = b;

  $$('.backend').forEach((el) => {
    const input = el.querySelector('input');
    const on = input.value === id;
    input.checked = on;
    el.classList.toggle('sel', on);
  });

  renderParams($('#params'), b.params);
  applyQualityPreset();
  if (restore) Object.entries(restore).forEach(([key, value]) => setParam(key, value));
  renderThumbs();
  syncGenerate();
}

function renderParams(wrap, schema) {
  wrap.innerHTML = '';

  for (const p of schema) {
    const row = document.createElement('div');
    row.className = 'row';
    const id = `p-${p.key}`;
    let control = '';

    if (p.type === 'views') {
      row.innerHTML = `<span class="lbl">${p.label}</span>
        <div class="chips" id="${id}" data-key="${p.key}" data-views="1"></div>
        ${p.help ? `<div class="help">${p.help}</div>` : ''}`;
      const box = row.querySelector('.chips');
      for (const o of p.options) {
        const chip = document.createElement('button');
        chip.type = 'button';
        chip.className = 'chip';
        chip.dataset.value = o.value;
        chip.textContent = o.label;
        if ((p.default || []).includes(o.value)) chip.classList.add('on');
        chip.setAttribute('aria-pressed', String(chip.classList.contains('on')));
        chip.addEventListener('click', () => {
          chip.classList.toggle('on');
          if (!box.querySelector('.chip.on')) chip.classList.add('on'); // never zero
          box.querySelectorAll('.chip').forEach((c) => c.setAttribute('aria-pressed', String(c.classList.contains('on'))));
          markBaseView(box);
          refreshPromptPeek();
        });
        box.appendChild(chip);
      }
      markBaseView(box);
      wrap.appendChild(row);
      continue;
    }

    if (p.type === 'select') {
      control = `<select id="${id}" data-key="${p.key}">${p.options
        .map((o) => `<option value="${o.value}" ${String(o.value) === String(p.default) ? 'selected' : ''}>${o.label}</option>`)
        .join('')}</select>`;
    } else if (p.type === 'bool') {
      row.className = 'row check';
      row.innerHTML =
        `<input type="checkbox" id="${id}" data-key="${p.key}" aria-label="${p.label}" ${p.default ? 'checked' : ''}>` +
        `<span>${p.label}</span>`;
      if (p.help) row.insertAdjacentHTML('beforeend', `<div class="help">${p.help}</div>`);
      wrap.appendChild(row);
      continue;
    } else if (p.type === 'int' || p.type === 'float') {
      const step = p.step ?? (p.type === 'int' ? 1 : 0.1);
      const hasRange = p.min !== undefined && p.max !== undefined && (p.max - p.min) / step <= 2000;
      control = hasRange
        ? `<div class="range-row">
             <input type="range" id="${id}" data-key="${p.key}" data-num="${p.type}"
                    min="${p.min}" max="${p.max}" step="${step}" value="${p.default}">
             <output for="${id}">${p.default}</output>
           </div>`
        : `<input type="number" id="${id}" data-key="${p.key}" data-num="${p.type}"
                  value="${p.default}" ${p.min !== undefined ? `min="${p.min}"` : ''}
                  ${p.max !== undefined ? `max="${p.max}"` : ''} step="${step}">`;
    } else if (p.type === 'json') {
      control = `<textarea id="${id}" data-key="${p.key}" data-json="1"
                   placeholder="{}">${JSON.stringify(p.default ?? {}, null, 2)}</textarea>`;
    } else {
      control = `<input type="text" id="${id}" data-key="${p.key}" value="${p.default ?? ''}">`;
    }

    row.innerHTML =
      `<label class="lbl" for="${id}">${p.label}</label>${control}` +
      (p.help ? `<div class="help">${p.help}</div>` : '');
    wrap.appendChild(row);
  }

  wrap.querySelectorAll('input[type=range]').forEach((r) => {
    r.addEventListener('input', () => (r.parentElement.querySelector('output').value = r.value));
  });
  wrap.onchange = () => {
    if (wrap === $('#params')) {
      renderThumbs();
      syncGenerate();
    }
    if (wrap === $('#ref-params')) refreshPromptPeek();
  };
}

function setParam(key, value) {
  const el = $(`#params [data-key="${key}"]`);
  if (!el || value === undefined) return;
  if (el.type === 'checkbox') el.checked = !!value;
  else el.value = String(value);
  if (el.type === 'range') {
    const output = el.parentElement.querySelector('output');
    if (output) output.value = el.value;
  }
}

function applyQualityPreset() {
  if (!state.backend) return;
  const spec = QUALITY[state.quality];
  const values = spec?.[state.backend.id] || {};
  Object.entries(values).forEach(([key, value]) => setParam(key, value));
  $$('#quality-switch button').forEach((b) => {
    const on = b.dataset.quality === state.quality;
    b.classList.toggle('on', on);
    b.setAttribute('aria-pressed', String(on));
  });
  $('#quality-note').textContent = spec.note;
  syncGenerate();
}

function applyProfile() {
  $$('#profile-switch button').forEach((b) => {
    const on = b.dataset.profile === state.profile;
    b.classList.toggle('on', on);
    b.setAttribute('aria-pressed', String(on));
  });
  const character = state.profile === 'character';
  $('#preserve-deformation').checked = character;
  $('#repair-holes').checked = !character;
  $('#ref-subject').placeholder = character
    ? 'Full-body realistic character, age, facial identity, clothing and materials'
    : 'Cast iron fire hydrant, chipped red paint, hexagonal bolts';
  $('#source-hint').textContent = character
    ? 'Use a full-body neutral A-pose with visible hands and feet. Front, profile and back views give the best foundation for retopology and rigging.'
    : 'Use a clean three-quarter view, one subject, plain background and even light. Fine detail must be visible in the source to survive into geometry.';

  if (character && state.quality === 'preview') state.quality = 'detailed';
  const chips = [...$('#ref-params').querySelectorAll('[data-views] .chip')];
  if (chips.length) {
    const preferred = new Set(character ? ['front', 'left', 'back', 'three_quarter'] : ['three_quarter']);
    chips.forEach((c) => {
      const on = preferred.has(c.dataset.value);
      c.classList.toggle('on', on);
      c.setAttribute('aria-pressed', String(on));
      c.classList.remove('base');
    });
    const baseValue = character ? 'front' : 'three_quarter';
    chips.find((c) => c.dataset.value === baseValue)?.classList.add('base');
  }
  applyQualityPreset();
  refreshPromptPeek();
  syncGenerate();
  saveSetup();
}

$$('#profile-switch button').forEach((btn) => btn.addEventListener('click', () => {
  state.profile = btn.dataset.profile;
  applyProfile();
}));

$$('#quality-switch button').forEach((btn) => btn.addEventListener('click', () => {
  state.quality = btn.dataset.quality;
  applyQualityPreset();
  saveSetup();
}));

function markBaseView(box) {
  const on = [...box.querySelectorAll('.chip.on')];
  box.querySelectorAll('.chip').forEach((c) => c.classList.remove('base'));
  on[0]?.classList.add('base');
}

function collectParams(wrap = $('#params'), { extras = true } = {}) {
  const out = {};
  if (!wrap) return extras ? { web_optimize: $('#web-optimize')?.checked ?? false } : {};
  for (const el of wrap.querySelectorAll('[data-key]')) {
    const k = el.dataset.key;
    if (el.dataset.views) {
      const chips = [...el.querySelectorAll('.chip.on')];
      chips.sort((a, b) => Number(b.classList.contains('base')) - Number(a.classList.contains('base')));
      out[k] = chips.map((c) => c.dataset.value);
    }
    else if (el.type === 'checkbox') out[k] = el.checked;
    else if (el.dataset.json) {
      try { out[k] = JSON.parse(el.value || '{}'); }
      catch { throw new Error(`"${k}" is not valid JSON`); }
    } else if (el.dataset.num === 'int') out[k] = parseInt(el.value, 10);
    else if (el.dataset.num === 'float') out[k] = parseFloat(el.value);
    else out[k] = el.value;
  }
  if (extras) {
    out.web_optimize = $('#web-optimize').checked;
    out.repair_holes = $('#repair-holes').checked;
    out.preserve_deformation = $('#preserve-deformation').checked;
    out._profile = state.profile;
    out._quality = state.quality;
  }
  return out;
}

/* ── reference generator ───────────────────────────────────────────────── */

async function loadImagegen() {
  let data;
  try { data = await api('/api/imagegen'); } catch { return; }

  const prov = data.providers[0];
  if (!prov) return;
  state.imgProvider = prov;

  $('#ref-provider').innerHTML =
    `<b>${prov.name}</b> · ${prov.blurb}` +
    (prov.available ? '' : `<div class="why">${prov.reason}</div>`);

  renderParams($('#ref-params'), prov.params);
  $('#btn-reference').disabled = !prov.available;
  $('#ref-subject').disabled = !prov.available;
  refreshPromptPeek();
}

let peekTimer;
function refreshPromptPeek() {
  clearTimeout(peekTimer);
  peekTimer = setTimeout(async () => {
    const subject = $('#ref-subject').value.trim();
    const el = $('#ref-prompt');
    if (!subject) { el.textContent = 'Describe a subject to see the prompt.'; return; }

    const p = collectParams($('#ref-params'), { extras: false });
    if (p.raw_prompt) { el.textContent = subject; return; }

    const q = new URLSearchParams({
      subject,
      view: (p.views || ['three_quarter'])[0],
      transparent: String(p.transparent ?? true),
      profile: state.profile,
    });
    try {
      const r = await api(`/api/imagegen/preview?${q}`);
      el.textContent = r.prompt;
    } catch { /* preview is a nicety, not worth a toast */ }
  }, 300);
}

$('#ref-subject').addEventListener('input', () => {
  refreshPromptPeek();
  saveSetup();
  syncGenerate();
});

/* ── wizard ────────────────────────────────────────────────────────────── */

const STEPS = ['define', 'reference', 'build', 'run'];

/** What each step still needs, or null when it is satisfied. */
function stepBlocker(step) {
  switch (step) {
    case 'define':
      return null;                                    // a profile is always chosen
    case 'reference':
      return staged() || state.backend?.accepts_text ? null : 'Add at least one reference image';
    case 'build':
      return state.backend ? null : 'Choose an engine';
    case 'run':
      return $('#btn-generate').disabled ? 'Not ready to run yet' : null;
    default:
      return null;
  }
}

function setStep(step, { focus = true } = {}) {
  if (!STEPS.includes(step)) return;
  state.step = step;

  $$('.wstage').forEach((el) => { el.hidden = el.dataset.stage !== step; });
  $$('.wstep').forEach((el) => {
    const on = el.dataset.step === step;
    el.classList.toggle('on', on);
    el.setAttribute('aria-selected', String(on));
  });

  const i = STEPS.indexOf(step);
  $('#w-back').disabled = i === 0;
  $('#w-next').hidden = i === STEPS.length - 1;
  $('.wizard-body').scrollTop = 0;
  if (focus) $(`.wstage[data-stage="${step}"] textarea, .wstage[data-stage="${step}"] input[type=text]`)?.focus?.();
  syncWizard();
}

/** Ticks off the steps that are satisfied and explains the one that is not. */
function syncWizard() {
  for (const step of STEPS) {
    const el = $(`.wstep[data-step="${step}"]`);
    if (!el) continue;
    const done = !stepBlocker(step) && (step !== 'define' || !!state.sources.length || !!$('#ref-subject').value.trim());
    el.classList.toggle('done', done && step !== state.step);
  }
  const blocker = stepBlocker(state.step);
  $('#w-hint').textContent = blocker || '';
  $('#w-next').classList.toggle('secondary', !blocker);
  $('#w-next').classList.toggle('ghost', !!blocker);
}

$$('.wstep').forEach((btn) => btn.addEventListener('click', () => setStep(btn.dataset.step)));
$('#w-back').addEventListener('click', () => setStep(STEPS[Math.max(0, STEPS.indexOf(state.step) - 1)]));
$('#w-next').addEventListener('click', () => setStep(STEPS[Math.min(STEPS.length - 1, STEPS.indexOf(state.step) + 1)]));

$('#btn-reference').addEventListener('click', async () => {
  const subject = $('#ref-subject').value.trim();
  if (!subject) { toast('Describe the subject first'); return; }

  const btn = $('#btn-reference');
  const was = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'Drawing…';

  const token = `t${Date.now().toString(36)}${Math.random().toString(36).slice(2, 8)}`;
  const refParams = collectParams($('#ref-params'), { extras: false });
  refParams.asset_profile = state.profile;
  const wanted = (refParams.views || ['three_quarter']).length;
  startRefProgress(token, wanted, Number(refParams.partials ?? 0) || 0);
  viewer.clear();
  state.loadedUrl = null;
  showViewerImages([], {
    kicker: 'Drawing',
    name: subject.slice(0, 60),
    pending: `drawing ${wanted} view${wanted === 1 ? '' : 's'}`,
  });

  try {
    const fd = new FormData();
    fd.append('provider', state.imgProvider.id);
    fd.append('subject', subject);
    fd.append('params', JSON.stringify(refParams));
    fd.append('token', token);

    const r = await api('/api/imagegen', { method: 'POST', body: fd });
    // Generated references replace whatever was staged; mixing sets rarely helps.
    state.sources = r.images.map((img) => ({ ...img, kind: 'reused' }));
    if (!$('#asset-name').value.trim()) {
      $('#asset-name').value = subject.toLowerCase().replace(/[^a-z0-9]+/g, '-').slice(0, 40).replace(/^-|-$/g, '');
    }
    renderThumbs();
    syncGenerate();
    setStep('reference', { focus: false });
    previewSources({ kicker: 'Reference', name: subject.slice(0, 60) });
    toast(`${r.images.length} reference${r.images.length > 1 ? 's' : ''} ready`);
  } catch (err) {
    toast(err.message, true);
  } finally {
    stopRefProgress();
    btn.disabled = false;
    btn.textContent = was;
  }
});

/* ── reference drawing progress ────────────────────────────────────────── */

// The draw is one blocking POST, so real progress comes from polling a token
// the server updates as each view lands.
let refPoll = null;

function startRefProgress(token, total, partials = 0) {
  state.drawing = true;
  const box = $('#ref-progress');
  box.hidden = false;
  paintRefProgress(0, total, 'starting');

  const started = Date.now();
  let seenRev = 0;
  const tick = async () => {
    try {
      const p = await api(`/api/imagegen/progress/${token}`);
      // Before the first view lands, creep the bar on elapsed time so it is
      // never frozen, but never let the guess overtake a real completion.
      const guess = Math.min(0.55, (Date.now() - started) / 42000);
      paintRefProgress(p.done || 0, p.total || total, p.stage || 'drawing', guess, {
        rev: p.rev || 0, partials,
      });

      // Progressive preview: the partial file keeps its name, so the revision
      // counter is what busts the cache.
      if (p.preview && p.rev > seenRev) {
        seenRev = p.rev;
        setViewerPreview(`${p.preview}?r=${p.rev}`);
      } else if (!p.preview && seenRev) {
        seenRev = 0;
      }
    } catch { /* the POST is the source of truth; a missed poll is harmless */ }
  };
  tick();
  refPoll = setInterval(tick, 700);
}

function paintRefProgress(done, total, stage, guess = 0, { rev = 0, partials = 0 } = {}) {
  const real = total ? done / total : 0;
  // Each partial is a real signal that the current view is part way there, so
  // the bar moves on evidence rather than dying at whatever the guess reached.
  const intra = partials && total && done < total
    ? Math.min(rev, partials) / (partials + 1) / total
    : 0;
  const frac = Math.max(real + intra, real === 0 ? guess : 0);
  $('#ref-progress-fill').style.width = `${Math.round(frac * 100)}%`;
  $('#ref-progress-pct').textContent = `${Math.round(frac * 100)}%`;
  $('#ref-progress-stage').textContent =
    total > 1 ? `${stage} · ${done} of ${total} views` : stage;
}

function stopRefProgress() {
  state.drawing = false;
  clearInterval(refPoll);
  refPoll = null;
  $('#viewer-image-main').classList.remove('previewing');
  // Land on 100 for a beat rather than vanishing from wherever the poll left it.
  $('#ref-progress-fill').style.width = '100%';
  $('#ref-progress-pct').textContent = '100%';
  setTimeout(() => {
    if (state.drawing) return;
    $('#ref-progress').hidden = true;
    $('#ref-progress-fill').style.width = '0%';
  }, 450);
}

/* ── source images ─────────────────────────────────────────────────────── */

const drop = $('#drop');
const fileInput = $('#file');

drop.addEventListener('click', () => fileInput.click());
drop.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); fileInput.click(); }
});
fileInput.addEventListener('change', () => addFiles([...fileInput.files]));

['dragenter', 'dragover'].forEach((ev) =>
  drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add('over'); }));
['dragleave', 'drop'].forEach((ev) =>
  drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove('over'); }));
drop.addEventListener('drop', (e) => addFiles([...e.dataTransfer.files]));

window.addEventListener('paste', (e) => {
  const imgs = [...(e.clipboardData?.files || [])].filter((f) => f.type.startsWith('image/'));
  if (imgs.length) addFiles(imgs);
});

function sourceLimit() {
  if (!state.backend) return 1;
  if (state.backend.id === 'fal') {
    const model = $('#params [data-key="model"]')?.value;
    return ['fal-ai/hunyuan3d/v2/multi-view', 'fal-ai/hyper3d/rodin'].includes(model) ? 4 : 1;
  }
  return state.backend.max_images || 1;
}

function activeSources() {
  return state.sources.slice(0, sourceLimit());
}

function staged() {
  return activeSources().length;
}

function addFiles(files) {
  const imgs = files.filter((f) => f.type.startsWith('image/'));
  if (!imgs.length) return;
  const room = Math.max(0, 8 - state.sources.length);
  if (imgs.length > room) toast('Keep the strongest eight references; the engine will use its supported maximum.');
  state.sources.push(...imgs.slice(0, room).map((file) => ({ kind: 'file', file })));
  // A paste can land from any step; show the images that just arrived.
  if (state.step !== 'reference') setStep('reference', { focus: false });
  renderThumbs();
  syncGenerate();
  previewSources({ kicker: 'Reference', name: `${staged()} staged` });
}

function thumbFigure(src, { caption = null, generated = false, unused = false, onRemove, onPromote }) {
  const fig = document.createElement('figure');
  if (generated) fig.className = 'gen';
  if (unused) fig.classList.add('unused');
  fig.title = unused ? 'Click to make this a primary reference' : 'Primary reference';
  const img = document.createElement('img');
  img.src = src;
  const btn = document.createElement('button');
  btn.textContent = '×';
  btn.title = 'Remove';
  btn.onclick = (e) => { e.stopPropagation(); onRemove(); };
  fig.onclick = () => onPromote?.();
  fig.append(img, btn);
  if (caption) {
    const cap = document.createElement('figcaption');
    cap.textContent = caption;
    fig.appendChild(cap);
  }
  return fig;
}

function renderThumbs() {
  const wrap = $('#thumbs');
  wrap.innerHTML = '';
  const max = sourceLimit();

  state.sources.forEach((source, i) => {
    const url = source.kind === 'file' ? URL.createObjectURL(source.file) : source.url;
    const fig = thumbFigure(url, {
      caption: source.label,
      generated: source.kind === 'reused',
      unused: i >= max,
      onRemove: () => {
        state.sources.splice(i, 1);
        renderThumbs();
        syncGenerate();
        if (state.viewerImages.length) {
          if (staged()) previewSources({ kicker: 'Reference', name: `${staged()} staged` });
          else hideViewerImages();
        }
      },
      onPromote: () => {
        if (i === 0) return;
        const [picked] = state.sources.splice(i, 1);
        state.sources.unshift(picked);
        renderThumbs();
        syncGenerate();
        if (state.viewerImages.length) previewSources({ kicker: 'Reference', name: `${staged()} staged` });
      },
    });
    if (source.kind === 'file') fig.querySelector('img').onload = () => URL.revokeObjectURL(url);
    wrap.appendChild(fig);
  });

  const used = Math.min(max, state.sources.length);
  $('#source-count').textContent = state.sources.length ? `${used} of ${state.sources.length} used` : '';
}

function syncGenerate() {
  const b = state.backend;
  const ready = !!b && (staged() > 0 || b.accepts_text);
  $('#btn-generate').disabled = !ready;
  const profile = state.profile === 'character' ? 'character' : 'object';
  $('#generate-label').textContent = `${QUALITY[state.quality].label} ${profile}`;
  $('#generate-meta').textContent = !b
    ? 'Choose an engine'
    : !ready
      ? 'Add a reference to continue'
      : `${staged()} reference${staged() === 1 ? '' : 's'} · ${b.name}`;
  $('#btn-generate').firstChild.textContent = ready ? `Generate ${profile} ` : 'Generate asset ';
  $('#gen-note').textContent = b
    ? b.kind === 'hosted'
      ? 'Billed per generation by fal.ai.'
      : state.quality === 'hero'
        ? 'Maximum-detail local run. This can take several minutes.'
        : 'Runs locally. No per-generation cost.'
    : '';

  renderRunSummary();
  syncWizard();
}

/** Step 4 restates every choice, so nobody runs a nine-minute build blind. */
function renderRunSummary() {
  const dl = $('#run-summary');
  if (!dl) return;
  const b = state.backend;
  const rows = [
    ['Profile', state.profile === 'character' ? 'Character' : 'Object'],
    ['Quality', QUALITY[state.quality].label],
    ['Engine', b ? `${b.name} (${b.kind})` : 'none usable'],
    ['References', staged() ? `${staged()} image${staged() === 1 ? '' : 's'}` : b?.accepts_text ? 'text only' : 'none'],
    ['Topology', $('#preserve-deformation').checked ? 'deformation preserved' : 'static cleanup'],
    ['Web copy', $('#web-optimize').checked ? 'Draco + WebP' : 'source only'],
  ];
  dl.innerHTML = rows.map(([k, v]) => `<div><dt>${k}</dt><dd>${v}</dd></div>`).join('');
}

/* ── submit ────────────────────────────────────────────────────────────── */

$('#btn-generate').addEventListener('click', async () => {
  const btn = $('#btn-generate');
  btn.disabled = true;
  try {
    const engine =
      state.backend?.available
        ? state.backend
        : state.backends.find((b) => b.available);
    if (!engine) throw new Error('No engine is usable yet. Hunyuan is the local one that works without gated weights.');
    const fd = new FormData();
    fd.append('backend', engine.id);
    fd.append('params', JSON.stringify(collectParams($('#params'))));
    fd.append('name', $('#asset-name').value.trim());
    const sources = activeSources();
    fd.append('image_paths', JSON.stringify(sources.filter((s) => s.kind === 'reused').map((s) => s.name)));
    for (const source of sources.filter((s) => s.kind === 'file')) fd.append('files', source.file);

    const { id } = await api('/api/jobs', { method: 'POST', body: fd });
    toast(`Queued ${id}`);
    // Drop the previous asset now. Waiting for the poll leaves the last mesh
    // on screen looking like it is the thing being built.
    state.selected = id;
    state.known.add(id);
    clearResult(`queued ${id}`);
    await refresh();
  } catch (err) {
    toast(err.message, true);
  } finally {
    syncGenerate();
  }
});

/* ── queue ─────────────────────────────────────────────────────────────── */

function renderQueue() {
  const wrap = $('#queue');
  const jobs = state.jobs;

  $('#queue-count').textContent = jobs.length ? `${jobs.length}` : '';

  if (!jobs.length) {
    wrap.innerHTML = '<div class="empty">No assets yet.<br>Drop an image and generate one.</div>';
    return;
  }

  wrap.innerHTML = '';
  for (const j of jobs) {
    const card = document.createElement('div');
    card.className = 'card' + (j.id === state.selected ? ' sel' : '');
    card.dataset.id = j.id;

    const name = j.params?._name || j.id;
    const st = j.stats || {};
    const meta =
      j.status === 'done'
        ? `${st.faces ? nf.format(st.faces) + ' tris · ' : ''}${st.bytes ? Math.round(st.bytes / 1024) + 'KB' : ''} · ${dur(j)}`
        : j.status === 'running'
          ? `${j.stage || 'working'} · ${dur(j)}`
          : j.status === 'failed'
            ? (j.error || 'failed').slice(0, 48)
            : ago(j.created_at);

    const thumb = j.thumb_path
      ? `<img src="/outputs/${j.id}.png?v=${Math.round(j.finished_at || 0)}" alt="">`
      : j.status === 'done' ? '◍' : j.status === 'failed' ? '×' : '◌';

    card.innerHTML = `
      <div class="card-thumb">${thumb}</div>
      <div class="card-body">
        <div class="card-name">${name}</div>
        <div class="card-meta"><span class="st st-${j.status}">${j.status}</span> · ${meta}</div>
        ${j.status === 'running'
          ? `<div class="bar-track"><div class="bar-fill" style="width:${Math.round((j.progress || 0) * 100)}%"></div></div>`
          : ''}
      </div>`;

    card.addEventListener('click', () => select(j.id));
    wrap.appendChild(card);
  }
}

async function select(id) {
  if (id !== state.selected) {
    const job = state.jobs.find((j) => j.id === id);
    renderMatch(job, state.match.get(id) || null);
  }
  state.selected = id;
  renderQueue();
  await loadSelected();
}

/* ── detail pane ───────────────────────────────────────────────────────── */

async function loadSelected() {
  const id = state.selected;
  if (!id) return;

  let job;
  try { job = await api(`/api/jobs/${id}`); }
  catch { return; }

  const displayName = job.params?._name || job.id;
  if (!state.drawing) {
    $('#viewer-context').hidden = false;
    $('#viewer-kicker').textContent = job.status === 'done' ? 'Result' : job.status;
    $('#viewer-name').textContent = displayName;
  }

  $('#log-title').textContent = `Activity · ${displayName}`;
  const logEl = $('#log');
  const atBottom = logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight < 60;
  logEl.textContent = job.log || (job.status === 'queued' ? 'waiting for a slot…' : '');
  if (job.error) {
    const p = document.createElement('div');
    p.className = 'err';
    p.textContent = `\n${job.error}`;
    logEl.appendChild(p);
  }
  if (atBottom) logEl.scrollTop = logEl.scrollHeight;

  $('#btn-cancel').disabled = !['queued', 'running'].includes(job.status);
  $('#btn-download').disabled = job.status !== 'done';
  $('#btn-export').disabled = job.status !== 'done';

  if (state.drawing) return;   // the reference being drawn owns the viewer

  if (job.status === 'done') {
    const url = `/outputs/${job.id}.glb`;
    if (state.loadedUrl !== url) {
      state.loadedUrl = url;
      $('#viewer-empty').hidden = true;
      try {
        const info = await viewer.load(url);
        hideViewerImages();
        $('#viewer-empty').hidden = true;
        showStats(job, info);
        if (!job.thumb_path && !state.thumbed.has(job.id)) {
          state.thumbed.add(job.id);
          setTimeout(() => saveThumb(job.id, true), 350);
        }
      } catch (err) {
        toast(`Could not load mesh: ${err.message}`, true);
      }
    }
  } else {
    const pct = Math.round((job.progress || 0) * 100);
    clearResult(
      job.stage || job.status,
      job.status === 'queued'
        ? 'Waiting for the current build to finish.'
        : `${pct}% complete · live details are in Activity.`,
    );
    // Show what this job is being built from rather than an empty stage.
    const items = (job.source_images || []).map((path) => ({
      url: `/uploads/${path.split('/').pop()}`,
      label: '',
    }));
    if (items.length) {
      showViewerImages(items, {
        kicker: job.status,
        name: displayName,
        pending: job.status === 'queued' ? 'queued' : `${job.stage || 'building'} · ${pct}%`,
      });
    }
  }
}

/** Empties the viewer and every result panel. Used on queue and while running. */
function clearResult(headline = 'Nothing loaded', detail = 'Pick a finished asset, or generate one.') {
  viewer.clear();
  hideViewerImages();
  state.loadedUrl = null;
  state.bone = null;
  $('#viewer-empty').hidden = false;
  $('#viewer-stats').innerHTML = '';
  $('#inspect').className = 'inspect empty-inspect';
  $('#inspect').innerHTML = `<div class="inspect-empty"><b>${headline}</b><br>${detail}</div>`;
  $('#rig').className = 'rig rig-empty';
  $('#rig').innerHTML = '<div class="rig-none"><h3>Nothing to rig yet</h3><p>The skeleton browser opens once a build finishes.</p></div>';
  $('#tab-rig').classList.remove('has-rig');
  $('#match').className = 'match match-empty';
  $('#match').innerHTML = '<div class="match-intro"><h3>Nothing to score yet</h3><p>Reference matching opens once a build finishes.</p></div>';
  for (const tool of ['bones', 'joints']) {
    const btn = $(`[data-tool="${tool}"]`);
    btn.disabled = true;
    btn.classList.remove('on');
  }
}

function showStats(job, info) {
  const st = job.stats || {};
  const bits = [
    ['engine', st.engine || job.backend],
    ['tris', nf.format(info.triangles)],
    ['verts', nf.format(info.vertices)],
    ['mats', info.materials],
    ['pbr', st.pbr ? 'yes' : 'no'],
    ['size', `${Math.round((st.bytes || 0) / 1024)}KB`],
    st.web_bytes ? ['web', `${Math.round(st.web_bytes / 1024)}KB`] : null,
    ['time', `${st.seconds ?? '?'}s`],
    job.cost_usd ? ['cost', `$${job.cost_usd.toFixed(2)}`] : ['cost', 'free'],
  ].filter(Boolean);

  $('#viewer-stats').innerHTML = bits
    .map(([k, v]) => `<span>${k} <b>${v}</b></span>`)
    .join('');

  const joints = info.bones || st.joints || 0;
  const skins = info.skinnedMeshes || st.skins || 0;
  const morphs = info.morphTargets || st.morph_targets || 0;
  const animations = info.animations || st.animations || 0;
  const topology = st.topology || {};
  const pbr = st.pbr ? 'Complete' : 'Base material';
  const profile = job.params?._profile || 'object';
  let readiness;
  if (joints && skins) readiness = `${joints} joints are bound to ${skins} mesh${skins === 1 ? '' : 'es'}. Inspect deformation in your DCC before animation.`;
  else if (profile === 'character' && job.params?.preserve_deformation) readiness = 'High-detail source preserved. Retopology, skinning and a facial rig are still required before animation.';
  else readiness = 'This is a static mesh. Generate with the Character profile to preserve a cleaner foundation for downstream rigging.';

  $('#inspect').className = 'inspect';
  $('#inspect').innerHTML = `
    <section class="inspect-card">
      <h3>Fidelity</h3>
      <div class="signal"><span>Geometry</span><b>${nf.format(info.triangles)} tris</b></div>
      <div class="signal"><span>Materials</span><b class="${st.pbr ? 'good' : 'warn'}">${pbr}</b></div>
      <div class="signal"><span>Texture target</span><b>${st.texture_size ? `${st.texture_size}px` : 'engine default'}</b></div>
      <div class="signal"><span>Source quality</span><b>${job.params?._quality || 'custom'}</b></div>
    </section>
    <section class="inspect-card">
      <h3>Topology</h3>
      <div class="signal"><span>Mesh bodies</span><b>${topology.bodies ?? st.meshes ?? info.meshes ?? '—'}</b></div>
      <div class="signal"><span>Watertight</span><b class="${topology.watertight ? 'good' : 'warn'}">${topology.watertight === undefined ? 'not measured' : topology.watertight ? 'yes' : 'open edges'}</b></div>
      <div class="signal"><span>Boundary edges</span><b>${topology.boundary_edges ?? '—'}</b></div>
      <div class="signal"><span>Deformation pass</span><b>${job.params?.preserve_deformation ? 'preserved' : 'static cleanup'}</b></div>
    </section>
    <section class="inspect-card">
      <h3>Animation foundation</h3>
      <div class="signal"><span>Skeleton</span><b class="${joints ? 'good' : 'warn'}">${joints ? `${joints} joints` : 'unrigged'}</b></div>
      <div class="signal"><span>Blend shapes</span><b>${morphs || 'none'}</b></div>
      <div class="signal"><span>Animation clips</span><b>${animations || 'none'}</b></div>
      <div class="readiness">${readiness}</div>
    </section>`;

  for (const tool of ['bones', 'joints']) {
    const btn = $(`[data-tool="${tool}"]`);
    btn.disabled = !joints;
    if (!joints) btn.classList.remove('on');
  }
  if (!joints) { viewer.setBones(false); viewer.setJoints(false); }

  state.bone = null;
  renderRig(job, info);
  renderMatch(job, state.match.get(job.id) || null);
  $('#tab-rig').classList.toggle('has-rig', !!joints);
}

async function saveThumb(id, quiet = false) {
  const url = viewer.screenshot(512);
  if (!url) return;
  const fd = new FormData();
  fd.append('data_url', url);
  try {
    await api(`/api/jobs/${id}/thumb`, { method: 'POST', body: fd });
    if (!quiet) toast('Thumbnail saved');
    await refresh();
  } catch (err) {
    if (!quiet) toast(err.message, true);
  }
}

/* ── actions ───────────────────────────────────────────────────────────── */

$('#btn-download').addEventListener('click', () => {
  if (!state.selected) return;
  const a = document.createElement('a');
  a.href = `/outputs/${state.selected}.glb`;
  a.download = `${state.selected}.glb`;
  a.click();
});

$('#btn-cancel').addEventListener('click', async () => {
  try {
    await api(`/api/jobs/${state.selected}/cancel`, { method: 'POST' });
    toast('Cancelled');
    await refresh();
  } catch (err) { toast(err.message, true); }
});

$('#btn-export').addEventListener('click', async () => {
  const job = state.jobs.find((j) => j.id === state.selected);
  const suggested = job?.params?._name || state.selected;
  const name = prompt('Export into ../<category>/<name>/ as:', suggested);
  if (!name) return;
  const fd = new FormData();
  fd.append('name', name);
  fd.append('category', 'models');
  try {
    const r = await api(`/api/jobs/${state.selected}/export`, { method: 'POST', body: fd });
    toast(`Exported to ${r.exported}`);
  } catch (err) { toast(err.message, true); }
});

/* ── viewer tools ──────────────────────────────────────────────────────── */

$$('.tool').forEach((btn) => {
  btn.addEventListener('click', () => {
    const t = btn.dataset.tool;
    if (t === 'frame') return viewer.frame();
    if (t === 'shot') return saveThumb(state.selected);

    btn.classList.toggle('on');
    const on = btn.classList.contains('on');
    if (t === 'turntable') viewer.setTurntable(on);
    if (t === 'wire') viewer.setWireframe(on);
    if (t === 'bones') viewer.setBones(on);
    if (t === 'joints') viewer.setJoints(on);
    if (t === 'grid') viewer.setGrid(on);
    if (t === 'env') viewer.setEnv(on);
  });
});

/* ── reference images in the main viewer ───────────────────────────────── */

// The centre pane is "what am I looking at right now". Before a mesh exists
// that is the reference, not the previous asset still sitting in the scene.
function showViewerImages(items, { kicker = null, name = null, pending = null } = {}) {
  if (!items.length && !pending) return hideViewerImages();

  $('#viewer-image').hidden = false;
  $('#viewer-empty').hidden = true;
  $('#viewer-image-pending').hidden = pending === null;
  if (pending !== null) $('#viewer-image-pending-text').textContent = pending;

  // A running job re-polls every 1.5s. Rebuilding the same images would reset
  // the chosen view and flicker the main image, so only rebuild on a change.
  const sig = items.map((i) => i.url).join('|');
  if (sig === state.viewerSig) {
    if (kicker) $('#viewer-kicker').textContent = kicker;
    return;
  }
  state.viewerSig = sig;
  state.viewerImages = items;

  const main = $('#viewer-image-main');
  main.hidden = !items.length;
  if (items.length) {
    main.src = items[0].url;
    main.classList.remove('previewing');
  }

  const strip = $('#viewer-image-strip');
  strip.innerHTML = '';
  if (items.length > 1) {
    items.forEach((it, i) => {
      const b = document.createElement('button');
      b.className = 'vshot' + (i === 0 ? ' on' : '');
      b.dataset.i = String(i);
      b.dataset.tip = `Show the ${(it.label || 'reference').toLowerCase()} reference full size.`;
      b.innerHTML = `<img src="${it.url}" alt="">`;
      if (it.label) b.insertAdjacentHTML('beforeend', `<span>${it.label}</span>`);
      b.addEventListener('click', () => {
        main.src = it.url;
        $$('#viewer-image-strip .vshot').forEach((x) => x.classList.toggle('on', x === b));
      });
      strip.appendChild(b);
    });
  }

  if (kicker) {
    $('#viewer-context').hidden = false;
    $('#viewer-kicker').textContent = kicker;
    $('#viewer-name').textContent = name || '';
  }
}

/** Swap in a partial render while the image is still resolving. */
function setViewerPreview(url) {
  const main = $('#viewer-image-main');
  if ($('#viewer-image').hidden) return;
  const next = new Image();
  // Only swap once the bytes are decoded, so the pane never blanks mid-draw.
  next.onload = () => { main.src = url; main.hidden = false; main.classList.add('previewing'); };
  next.src = url;
}

function hideViewerImages() {
  state.viewerImages = [];
  state.viewerSig = null;
  $('#viewer-image').hidden = true;
  $('#viewer-image-strip').innerHTML = '';
  $('#viewer-image-pending').hidden = true;
}

function releaseObjectUrls() {
  for (const u of state.objectUrls) URL.revokeObjectURL(u);
  state.objectUrls = [];
}

/** The references currently staged, as things the viewer can display. */
function sourceItems() {
  releaseObjectUrls();
  return activeSources().map((src) => {
    if (src.kind !== 'file') return { url: src.url, label: src.label || '' };
    const url = URL.createObjectURL(src.file);
    state.objectUrls.push(url);
    return { url, label: src.file.name };
  });
}

/** Put the staged references on screen, replacing whatever mesh was there. */
function previewSources({ kicker = 'Reference', name = '' } = {}) {
  const items = sourceItems();
  if (!items.length) return;
  viewer.clear();
  state.loadedUrl = null;
  showViewerImages(items, { kicker, name });
}

/* ── reference match scoring ───────────────────────────────────────────── */

const MASK_N = 64;   // both silhouettes are normalised into this square

/** Alpha silhouette of an image, at its native resolution. */
async function maskFromUrl(url) {
  const img = await new Promise((resolve, reject) => {
    const el = new Image();
    el.crossOrigin = 'anonymous';
    el.onload = () => resolve(el);
    el.onerror = () => reject(new Error('could not load reference'));
    el.src = url;
  });
  const w = img.naturalWidth, h = img.naturalHeight;
  const c = document.createElement('canvas');
  c.width = w; c.height = h;
  const ctx = c.getContext('2d', { willReadFrequently: true });
  ctx.drawImage(img, 0, 0);
  const data = ctx.getImageData(0, 0, w, h).data;

  const mask = new Uint8Array(w * h);
  let covered = 0;
  for (let i = 0; i < w * h; i++) {
    const on = data[i * 4 + 3] > 128 ? 1 : 0;
    mask[i] = on;
    covered += on;
  }
  // A fully opaque reference has no silhouette to read; that is not a low
  // score, it is a measurement we cannot make.
  return { mask, w, h, opaque: covered > w * h * 0.985, empty: covered < w * h * 0.001 };
}

/**
 * Crop to the silhouette's bounding box and rescale into MASK_N, preserving
 * aspect. That removes framing and scale differences so the comparison is
 * about shape and proportion, which is what actually survives into geometry.
 */
function normaliseMask(mask, w, h) {
  let minX = w, minY = h, maxX = -1, maxY = -1;
  for (let y = 0; y < h; y++) {
    for (let x = 0; x < w; x++) {
      if (!mask[y * w + x]) continue;
      if (x < minX) minX = x;
      if (x > maxX) maxX = x;
      if (y < minY) minY = y;
      if (y > maxY) maxY = y;
    }
  }
  const out = new Uint8Array(MASK_N * MASK_N);
  if (maxX < 0) return out;

  const bw = maxX - minX + 1, bh = maxY - minY + 1;
  const scale = Math.max(bw, bh) / MASK_N;         // longer side fills the square
  const offX = (MASK_N - bw / scale) / 2;
  const offY = (MASK_N - bh / scale) / 2;

  for (let y = 0; y < MASK_N; y++) {
    for (let x = 0; x < MASK_N; x++) {
      const sx = Math.floor(minX + (x - offX) * scale);
      const sy = Math.floor(minY + (y - offY) * scale);
      if (sx < minX || sx > maxX || sy < minY || sy > maxY) continue;
      out[y * MASK_N + x] = mask[sy * w + sx];
    }
  }
  return out;
}

function iou(a, b) {
  let inter = 0, union = 0;
  for (let i = 0; i < a.length; i++) {
    const x = a[i], y = b[i];
    if (x | y) union++;
    if (x & y) inter++;
  }
  return union ? inter / union : 0;
}

/** The renderer's readback is bottom-up, so flip before normalising. */
function flipRows(mask, size) {
  const out = new Uint8Array(mask.length);
  for (let y = 0; y < size; y++) {
    out.set(mask.subarray(y * size, (y + 1) * size), (size - 1 - y) * size);
  }
  return out;
}

const MATCH_VERDICT = [
  [0.90, 'good', 'Matches the reference closely.'],
  [0.78, 'good', 'Close. Minor silhouette drift, usually fine.'],
  [0.62, 'warn', 'Loose. The engine reinterpreted part of the shape.'],
  [0.00, 'bad',  'Weak. This view is largely guessed rather than observed.'],
];

function verdictFor(score) {
  return MATCH_VERDICT.find(([floor]) => score >= floor);
}

async function scoreAgainstReferences(job) {
  const sources = job.source_images || [];
  if (!sources.length) throw new Error('this asset has no stored reference images');
  if (!viewer.model) throw new Error('load the mesh first');

  const wasTurntable = viewer.turntable;
  viewer.setTurntable(false);
  try {
    const sweep = viewer.silhouetteSweep({ azimuths: 48, elevations: [-10, 0, 12, 26], size: 128 });
    if (!sweep.length) throw new Error('the mesh produced no silhouette');
    const rendered = sweep.map((s) => ({
      ...s, norm: normaliseMask(flipRows(s.mask, s.size), s.size, s.size),
    }));

    const views = [];
    for (const path of sources) {
      const url = `/uploads/${path.split('/').pop()}`;
      let ref;
      try { ref = await maskFromUrl(url); }
      catch { views.push({ url, error: 'could not read this reference' }); continue; }

      if (ref.opaque) { views.push({ url, error: 'no transparency, so no silhouette to compare' }); continue; }
      if (ref.empty)  { views.push({ url, error: 'reference is empty' }); continue; }

      const refNorm = normaliseMask(ref.mask, ref.w, ref.h);
      let best = { score: -1 };
      for (const r of rendered) {
        const score = iou(refNorm, r.norm);
        if (score > best.score) best = { score, azimuth: r.azimuth, elevation: r.elevation };
      }
      views.push({ url, ...best, shot: viewer.shotAt(best.azimuth, best.elevation, 256) });
    }
    return views;
  } finally {
    viewer.setTurntable(wasTurntable);
  }
}

function renderMatch(job, result) {
  const wrap = $('#match');
  if (!result) {
    const sources = (job?.source_images || []).length;
    wrap.className = 'match match-empty';
    wrap.innerHTML = `
      <div class="match-intro">
        <h3>Score the mesh against its reference</h3>
        <p>Renders the finished mesh from every angle, compares each silhouette
           against the reference it was built from, and reports where the engine
           observed the shape and where it guessed.</p>
        <button class="secondary" id="btn-score" ${sources ? '' : 'disabled'}
                data-tip="Sweeps 192 angles and scores silhouette overlap per reference view. Runs locally, takes a second or two.">
          ${sources ? `Score against ${sources} reference${sources === 1 ? '' : 's'}` : 'No stored reference for this asset'}
        </button>
      </div>`;
    wrap.querySelector('#btn-score')?.addEventListener('click', runScore);
    return;
  }

  const scored = result.filter((v) => !v.error);
  const worst = scored.length ? scored.reduce((a, b) => (a.score < b.score ? a : b)) : null;
  const mean = scored.length ? scored.reduce((a, v) => a + v.score, 0) / scored.length : 0;

  let advice;
  if (!scored.length) advice = 'Nothing measurable. Generate references with a transparent background to score them.';
  else if (worst.score < 0.62) advice = `The weakest view scores ${(worst.score * 100).toFixed(0)}%. Draw that view as an extra reference and re-run at the same seed, so the engine observes it instead of inventing it.`;
  else if (mean < 0.8) advice = 'The mesh follows the reference loosely. A cleaner source, plainer background and even lighting usually buys more than a higher quality preset.';
  else advice = 'The mesh follows its references closely. Any remaining problems are in the source images, not the engine.';

  wrap.className = 'match';
  wrap.innerHTML = `
    <div class="match-head">
      <h3>Reference match</h3>
      <span class="muted small">${scored.length ? `mean ${(mean * 100).toFixed(0)}%` : 'not measurable'}</span>
      <button class="ghost" id="btn-rescore" data-tip="Run the sweep again.">Re-score</button>
    </div>
    <div class="match-views">
      ${result.map((v) => {
        if (v.error) {
          return `<div class="match-view">
            <div class="match-pair"><img src="${v.url}" alt=""></div>
            <div class="match-bar-row"><b class="warn">${v.error}</b></div>
          </div>`;
        }
        const [, tone, text] = verdictFor(v.score);
        const pct = (v.score * 100).toFixed(0);
        return `<div class="match-view">
          <div class="match-pair">
            <figure><img src="${v.url}" alt=""><figcaption>reference</figcaption></figure>
            <figure><img src="${v.shot}" alt=""><figcaption>mesh · ${v.azimuth}° / ${v.elevation}°</figcaption></figure>
          </div>
          <div class="match-bar-row">
            <div class="match-bar"><div class="match-fill ${tone}" style="width:${pct}%"></div></div>
            <b class="${tone}">${pct}%</b>
          </div>
          <p class="match-note">${text}</p>
        </div>`;
      }).join('')}
    </div>
    <p class="match-advice">${advice}</p>`;
  wrap.querySelector('#btn-rescore').addEventListener('click', runScore);
}

async function runScore() {
  const job = state.jobs.find((j) => j.id === state.selected);
  if (!job) return;
  const btn = $('#btn-score') || $('#btn-rescore');
  if (btn) { btn.disabled = true; btn.textContent = 'Scoring…'; }
  try {
    const result = await scoreAgainstReferences(job);
    state.match.set(job.id, result);
    renderMatch(job, result);
  } catch (err) {
    toast(err.message, true);
    renderMatch(job, null);
  }
}

/* ── rig panel ─────────────────────────────────────────────────────────── */

function selectBone(uuid, { focus = false } = {}) {
  state.bone = uuid;
  viewer.selectBone(uuid);
  if (focus) viewer.focusBone(uuid);

  // Turning joints on from the panel has to be reflected in the toolbar.
  const jointTool = $('[data-tool="joints"]');
  jointTool.classList.toggle('on', viewer.joints);

  $$('#rig .bone').forEach((el) => el.classList.toggle('sel', el.dataset.uuid === uuid));
  renderBoneDetail();
}

function renderBoneDetail() {
  const box = $('#bone-detail');
  if (!box) return;
  const s = state.bone && viewer.boneStats(state.bone);
  if (!s) {
    box.innerHTML = '<div class="bone-empty">Select a joint to read its pivot, parent and local transform.</div>';
    return;
  }
  box.innerHTML = `
    <div class="bone-name">${s.name}</div>
    <div class="signal"><span>Parent</span><b>${s.parent}</b></div>
    <div class="signal"><span>Children</span><b>${s.children || 'leaf'}</b></div>
    <div class="signal"><span>Bone length</span><b>${s.length ? s.length : '—'}</b></div>
    <div class="signal"><span>Local position</span><b class="mono">${s.position.join(', ')}</b></div>
    <div class="signal"><span>Local rotation</span><b class="mono">${s.rotation.join(', ')}°</b></div>
    <button class="ghost bone-focus" data-tip="Move the camera to this joint so you can see how it sits inside the mesh.">Focus in viewer</button>`;
  box.querySelector('.bone-focus').addEventListener('click', () => viewer.focusBone(state.bone));
}

function renderRig(job, info) {
  const wrap = $('#rig');
  const joints = info?.bones || 0;
  const skins = info?.skinnedMeshes || 0;
  const profile = job?.params?._profile || 'object';

  if (!joints) {
    const preserved = !!job?.params?.preserve_deformation;
    wrap.className = 'rig rig-empty';
    wrap.innerHTML = `
      <div class="rig-none">
        <h3>No skeleton in this GLB</h3>
        <p>Image-to-3D engines return a static shell. Nothing here is bound to
           joints, so the skeleton and joint-marker tools stay switched off.</p>
        <div class="rig-checklist">
          <div class="signal"><span>Deformation topology</span>
            <b class="${preserved ? 'good' : 'warn'}">${preserved ? 'preserved' : 'static cleanup applied'}</b></div>
          <div class="signal"><span>Profile used</span>
            <b class="${profile === 'character' ? 'good' : 'warn'}">${profile === 'character' ? 'character' : 'object'}</b></div>
          <div class="signal"><span>Triangles</span><b>${nf.format(info?.triangles || 0)}</b></div>
        </div>
        <p class="rig-next">${profile === 'character' && preserved
          ? 'This is a usable rigging source. It still needs quad retopology, a skeleton, skin weights and blend shapes before it will deform.'
          : 'Re-run with the <b>Character</b> profile and <b>Preserve deformation topology</b> on to get a mesh worth rigging.'}</p>
        <button class="secondary" id="btn-autorig" disabled
                data-tip="No auto-rig engine is wired up yet. Once one is installed this fits a skeleton and skin weights to the mesh in place.">Auto-rig this mesh</button>
        <p class="muted small">No rig engine installed.</p>
      </div>`;
    return;
  }

  wrap.className = 'rig';
  const tree = viewer.boneTree();
  const rows = [];
  const walk = (nodes) => {
    for (const n of nodes) {
      rows.push(`<button class="bone" data-uuid="${n.uuid}" style="--depth:${n.depth}">
        <span class="bone-tick"></span><span class="bone-label">${n.name}</span>
        ${n.children.length ? `<span class="bone-kids">${n.children.length}</span>` : ''}
      </button>`);
      walk(n.children);
    }
  };
  walk(tree);

  wrap.innerHTML = `
    <section class="rig-tree">
      <div class="rig-head">
        <h3>Hierarchy</h3>
        <span class="muted small">${joints} joints · ${skins} skinned mesh${skins === 1 ? '' : 'es'}</span>
      </div>
      <div class="bone-list">${rows.join('')}</div>
    </section>
    <section class="rig-detail">
      <div class="rig-head"><h3>Joint</h3></div>
      <div id="bone-detail"></div>
    </section>`;

  wrap.querySelectorAll('.bone').forEach((el) => {
    el.addEventListener('click', () => selectBone(el.dataset.uuid, { focus: false }));
    el.addEventListener('dblclick', () => selectBone(el.dataset.uuid, { focus: true }));
  });
  renderBoneDetail();
}

// Clicking a joint marker in the 3D view drives the same selection.
viewer.onBoneSelect = (uuid) => {
  setResultTab('rig');
  selectBone(uuid);
  $(`#rig .bone[data-uuid="${uuid}"]`)?.scrollIntoView({ block: 'nearest' });
};

function setResultTab(tab) {
  state.resultTab = tab;
  for (const name of ['inspect', 'match', 'rig', 'activity']) {
    $(`#${name}`).hidden = tab !== name;
    $(`#tab-${name}`).classList.toggle('on', tab === name);
  }
}
for (const name of ['inspect', 'match', 'rig', 'activity']) {
  $(`#tab-${name}`).addEventListener('click', () => setResultTab(name));
}

/* ── tooltips ──────────────────────────────────────────────────────────── */

// One shared, fixed-position bubble: the viewer and result panes both clip
// their overflow, so a CSS-only tooltip would be cut off at the pane edge.
const tip = $('#tip');
let tipTimer;

function showTip(el) {
  const text = el.dataset.tip;
  if (!text) return;
  tip.textContent = text;
  tip.hidden = false;
  tip.style.visibility = 'hidden';

  const r = el.getBoundingClientRect();
  const t = tip.getBoundingClientRect();
  const gap = 8;
  // Prefer the left of a right-docked control, otherwise sit underneath it.
  let x = r.left + r.width / 2 - t.width / 2;
  let y = r.bottom + gap;
  if (x + t.width > innerWidth - 10) x = r.left - t.width - gap;
  if (x < 10) x = 10;
  if (y + t.height > innerHeight - 10) y = r.top - t.height - gap;

  tip.style.left = `${Math.round(x)}px`;
  tip.style.top = `${Math.round(Math.max(10, y))}px`;
  tip.style.visibility = 'visible';
}

function hideTip() {
  clearTimeout(tipTimer);
  tip.hidden = true;
}

document.addEventListener('pointerover', (e) => {
  const el = e.target.closest?.('[data-tip]');
  if (!el) return;
  clearTimeout(tipTimer);
  tipTimer = setTimeout(() => showTip(el), 320);
});
document.addEventListener('pointerout', (e) => {
  if (e.target.closest?.('[data-tip]')) hideTip();
});
document.addEventListener('pointerdown', hideTip);
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') hideTip(); });
// Keyboard users get the same copy on focus, without the hover delay.
document.addEventListener('focusin', (e) => {
  const el = e.target.closest?.('[data-tip]');
  if (el) showTip(el); else hideTip();
});
document.addEventListener('focusout', hideTip);

/* ── theme ─────────────────────────────────────────────────────────────── */

const savedTheme = (() => { try { return localStorage.getItem('plumbus-theme'); } catch { return null; } })();
if (savedTheme) document.documentElement.dataset.theme = savedTheme;

$('#btn-theme').addEventListener('click', () => {
  const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem('plumbus-theme', next); } catch {}
  viewer.scene.background.set(next === 'dark' ? 0x0a0a09 : 0x1a1917);
});

/* ── poll ──────────────────────────────────────────────────────────────── */

async function refresh() {
  try {
    const data = await api('/api/jobs');
    state.jobs = data.jobs;

    const busy = !!data.running;
    const pill = $('#worker-state');
    pill.textContent = busy ? 'generating' : 'idle';
    pill.className = 'pill ' + (busy ? 'pill-busy' : 'pill-idle');

    const current = data.jobs.find((j) => j.id === state.selected);
    if (!current && !state.known.has(state.selected)) {
      const running = data.jobs.find((j) => j.status === 'running');
      const done = data.jobs.find((j) => j.status === 'done');
      state.selected = running?.id || done?.id || data.jobs[0]?.id || null;
    }
    if (current) state.known.delete(state.selected);
    renderQueue();
    await loadSelected();
  } catch { /* server restarting; the next tick will catch up */ }
}

loadSetup();
applyProfile();
setStep('define', { focus: false });
await loadBackends();
await loadImagegen();
applyProfile();
setStep(state.step, { focus: false });
await refresh();
setInterval(refresh, 1500);
setInterval(loadBackends, 30000);
setInterval(loadImagegen, 60000);
