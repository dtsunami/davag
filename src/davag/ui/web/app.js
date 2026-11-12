(() => {
  const $ = (id) => document.getElementById(id);
  let token = sessionStorage.getItem('davag_token') || new URLSearchParams(location.search).get('token') || '';
  let es = null;
  let lastSeq = 0;
  let commands = [];
  let catalog = null;
  const approvalQueue = [];

  async function api(path, opts = {}) {
    const headers = { ...(opts.headers || {}), Authorization: `Bearer ${token}` };
    if (opts.body && typeof opts.body === 'object') {
      headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(opts.body);
    }
    const res = await fetch(path, { ...opts, headers });
    if (res.status === 401) throw new Error('Invalid session token');
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
    return data;
  }

  function appendEntry(kind, text) {
    const div = document.createElement('div');
    div.className = `entry ${kind}`;
    div.textContent = text;
    $('transcript').appendChild(div);
    $('scroll').scrollTop = $('scroll').scrollHeight;
  }

  function setBusy(busy, statusText = '') {
    $('btn-cancel').hidden = !busy;
    $('live-status').hidden = !busy && !statusText;
    if (statusText) $('live-status').textContent = `⠋ ${statusText}`;
  }

  function showNextApproval() {
    const box = $('approval-box');
    if (!approvalQueue.length) {
      box.hidden = true;
      return;
    }
    const cur = approvalQueue[0];
    box.hidden = false;
    $('ap-id').textContent = `(${cur.id})`;
    $('ap-detail').textContent = JSON.stringify(cur.op, null, 2);
  }

  async function answerApproval(allow, checkpointFirst = false) {
    if (!approvalQueue.length) return;
    const cur = approvalQueue.shift();
    showNextApproval();
    await api('/api/approve', { method: 'POST', body: { id: cur.id, allow, checkpoint_first: checkpointFirst } });
  }

  function updateHeader(info) {
    if (info.provider && info.model) {
      $('hdr-model').textContent = `${info.provider}/${info.model}${info.effort ? ' · ' + info.effort : ''}`;
    }
    if ('yolo' in info) $('hdr-yolo').hidden = !info.yolo;
    if ('resolve_connected' in info) {
      $('hdr-resolve').textContent = info.resolve_connected ? `resolve: ${info.resolve_mode}` : 'resolve: offline';
    }
    if (info.stats) {
      const s = info.stats;
      $('hdr-stats').textContent = `${s.turns || 0}t · ${s.ops || 0}ops · $${Number(s.cost || 0).toFixed(4)}`;
    }
  }

  function drawSpark(turns) {
    const c = $('hdr-spark');
    const ctx = c.getContext('2d');
    ctx.clearRect(0, 0, c.width, c.height);
    if (!turns || !turns.length) return;
    const vals = turns.slice(-16).map((t) => t.tps || 0);
    const max = Math.max(...vals, 1);
    const w = Math.max(3, Math.floor(c.width / vals.length) - 1);
    ctx.fillStyle = '#4cc9f0';
    vals.forEach((v, i) => {
      const h = Math.max(2, Math.round((v / max) * (c.height - 2)));
      ctx.fillRect(i * (w + 1), c.height - h, w, h);
    });
  }

  function connectSSE() {
    if (es) es.close();
    const url = `/api/events?token=${encodeURIComponent(token)}&since=${lastSeq}`;
    es = new EventSource(url);

    es.onopen = () => { $('conn-dot').classList.add('ok'); };
    es.onerror = () => { $('conn-dot').classList.remove('ok'); };

    const trackSeq = (e) => {
      if (e.lastEventId) lastSeq = Math.max(lastSeq, Number(e.lastEventId));
    };

    es.addEventListener('hello', (e) => {
      const d = JSON.parse(e.data);
      lastSeq = d.seq || lastSeq;
      updateHeader(d);
      if (d.state) {
        setBusy(d.state.busy, d.state.status || '');
        if (d.state.live) {
          $('live-text').hidden = false;
          $('live-text').textContent = d.state.live;
        }
      }
      approvalQueue.length = 0;
      (d.pending || []).forEach((p) => approvalQueue.push(p));
      showNextApproval();
      refreshResolveStrip();
    });

    es.addEventListener('transcript', (e) => {
      trackSeq(e);
      const d = JSON.parse(e.data);
      const txt = d.text || d.message || d.output || JSON.stringify(d);
      appendEntry(d.type || 'info', txt);
    });

    es.addEventListener('turn-start', (e) => {
      trackSeq(e);
      $('live-thinking').hidden = true;
      $('live-thinking').textContent = '';
      $('live-text').hidden = true;
      $('live-text').textContent = '';
      setBusy(true, 'thinking…');
    });

    es.addEventListener('thinking', (e) => {
      trackSeq(e);
      const delta = JSON.parse(e.data);
      $('live-thinking').hidden = false;
      $('live-thinking').textContent += delta;
      $('scroll').scrollTop = $('scroll').scrollHeight;
    });

    es.addEventListener('text', (e) => {
      trackSeq(e);
      const delta = JSON.parse(e.data);
      $('live-thinking').hidden = true;
      $('live-text').hidden = false;
      $('live-text').textContent += delta;
      $('scroll').scrollTop = $('scroll').scrollHeight;
    });

    es.addEventListener('ops-parsed', (e) => {
      trackSeq(e);
      const ops = JSON.parse(e.data);
      setBusy(true, `running ${ops.length} op(s)…`);
      appendEntry('ops', '· ' + ops.map((o) => o.op).join(', '));
    });

    es.addEventListener('ops-result', (e) => {
      trackSeq(e);
      const d = JSON.parse(e.data);
      if (d.checkpoint) appendEntry('notice', `checkpoint saved: ${d.checkpoint}`);
      refreshResolveStrip();
    });

    es.addEventListener('turn-end', (e) => {
      trackSeq(e);
      if (!$('live-text').hidden && $('live-text').textContent.trim()) {
        appendEntry('assistant', $('live-text').textContent);
      }
      $('live-text').hidden = true;
      $('live-thinking').hidden = true;
      setBusy(false);
      refreshPerf();
    });

    es.addEventListener('usage', (e) => {
      trackSeq(e);
      const u = JSON.parse(e.data);
      $('hdr-stats').textContent = `$${Number(u.total_cost || 0).toFixed(4)} · ctx ${u.context_in.toLocaleString()}`;
    });

    es.addEventListener('approval-request', (e) => {
      trackSeq(e);
      const req = JSON.parse(e.data);
      if (!approvalQueue.some((x) => x.id === req.id)) approvalQueue.push(req);
      showNextApproval();
    });

    es.addEventListener('approval-resolved', (e) => {
      trackSeq(e);
      const d = JSON.parse(e.data);
      const idx = approvalQueue.findIndex((x) => x.id === d.id);
      if (idx >= 0) approvalQueue.splice(idx, 1);
      showNextApproval();
    });

    function openMediaDrawer(kind, url, label) {
      const authedUrl = `${url}?token=${encodeURIComponent(token)}&t=${Date.now()}`;
      if (kind === 'video') {
        $('viewer-img').hidden = true;
        $('viewer-video').hidden = false;
        $('viewer-video').src = authedUrl;
      } else {
        $('viewer-video').pause();
        $('viewer-video').hidden = true;
        $('viewer-img').hidden = false;
        $('viewer-img').src = authedUrl;
      }
      $('viewer-tc').textContent = label || '';
      $('viewer-drawer').hidden = false;
    }

    es.addEventListener('viewer-still', (e) => {
      trackSeq(e);
      const d = JSON.parse(e.data);
      openMediaDrawer('image', d.url, `Playhead ${d.playhead_tc || ''}`);
    });

    es.addEventListener('viewer-media', (e) => {
      trackSeq(e);
      const d = JSON.parse(e.data);
      openMediaDrawer(d.kind, d.url, d.title || '');
    });

    es.addEventListener('model-changed', (e) => {
      trackSeq(e);
      const d = JSON.parse(e.data);
      updateHeader({ provider: d.provider, model: d.model.id, effort: d.effort });
    });

    es.addEventListener('yolo-changed', (e) => {
      trackSeq(e);
      const d = JSON.parse(e.data);
      updateHeader({ yolo: d.yolo });
    });

    es.addEventListener('warning', (e) => {
      trackSeq(e);
      const d = JSON.parse(e.data);
      appendEntry('warning', d.message || JSON.stringify(d));
    });

    es.addEventListener('error', (e) => {
      trackSeq(e);
      if (!e.data) return;
      const d = JSON.parse(e.data);
      appendEntry('error', d.message || JSON.stringify(d));
      setBusy(false);
    });

    es.addEventListener('done', (e) => { trackSeq(e); setBusy(false); });
    es.addEventListener('cancelled', (e) => { trackSeq(e); appendEntry('warning', 'Turn cancelled.'); setBusy(false); });
  }

  async function refreshResolveStrip() {
    try {
      const r = await api('/api/resolve');
      if (!r.connected) {
        $('strip-summary').textContent = `DaVinci Resolve: disconnected (${r.note || r.error || 'offline'})`;
        updateHeader({ resolve_connected: false });
        return;
      }
      const st = r.status;
      updateHeader({ resolve_connected: true, resolve_mode: st.mode });
      const proj = st.project ? st.project.name : 'none';
      const tl = st.timeline ? `${st.timeline.name} @ ${st.timeline.playhead_tc}` : 'no active timeline';
      $('strip-summary').textContent = `${st.product} ${st.version} [${st.mode}] · Project: ${proj} · Timeline: ${tl} · Page: ${st.page}`;
    } catch { /* ignore */ }
  }

  async function loadResolveInspector() {
    const r = await api('/api/resolve');
    const box = $('resolve-inspector');
    if (!r.connected) {
      box.innerHTML = `<div class="card"><h3>Not Connected</h3><p class="muted">${r.note || r.error || ''}</p></div>`;
      return;
    }
    const clipsHtml = (r.clips || []).filter((c) => c.file_path).map((c) => `
      <div class="card" style="background:#0d121c;">
        <div class="row-between">
          <strong>${c.name}</strong>
          <span class="muted">${c.resolution} @ ${c.fps}fps · ${c.frames.toLocaleString()}f</span>
        </div>
        <div class="muted" style="font-size:11px;">clip_id: ${c.clip_id}${c.proxy_path ? ' · <span style="color:#38b000">Proxy Linked</span>' : ''}</div>
        ${c.sheet_url ? `<img src="${c.sheet_url}?token=${encodeURIComponent(token)}" style="width:100%; border-radius:4px; border:1px solid #232b3d;" />` : ''}
        ${c.proxy_url ? `<video src="${c.proxy_url}?token=${encodeURIComponent(token)}" controls preload="metadata" style="width:100%; max-height:200px; border-radius:4px;"></video>` : ''}
        ${c.digest ? `<pre class="ap-code" style="font-size:11px;">${c.digest}</pre>` : '<span class="muted">No Scout Digest cached yet.</span>'}
        <div class="row-gap">
          <button class="btn small" data-cmd="/index ${c.clip_id}">🔍 Scout Index</button>
          <button class="btn small" data-cmd="/sheet ${c.clip_id}">🎞 Contact Sheet</button>
          <button class="btn small" data-cmd="/proxy ${c.clip_id}">⚡ Build & Link QSV Proxy</button>
        </div>
      </div>
    `).join('') || '<span class="muted">No file-backed clips in Media Pool.</span>';

    const tl = r.timeline ? JSON.stringify(r.timeline, null, 2) : 'No active timeline';
    box.innerHTML = `
      <div class="card"><h3>Media Pool Clips & Digests</h3><div class="stack">${clipsHtml}</div></div>
      <div class="card"><h3>Active Timeline State</h3><pre class="ap-code">${tl}</pre></div>
    `;
    box.querySelectorAll('[data-cmd]').forEach((btn) => {
      btn.onclick = async () => {
        const cmdText = btn.dataset.cmd;
        document.querySelector('[data-tab="chat"]').click();
        await api('/api/submit', { method: 'POST', body: { text: cmdText } });
      };
    });
  }

  let promptData = null;

  async function loadContextTab() {
    const [c, p] = await Promise.all([api('/api/context'), api('/api/prompts')]);
    promptData = p;
    const pct = (c.fraction * 100).toFixed(1);
    $('ctx-summary').textContent = `${c.total_tokens.toLocaleString()} / ${c.limit.toLocaleString()} tokens (${pct}%) · cached system prefix: ${c.system_tokens.toLocaleString()} tok`;
    $('ctx-bar').style.width = `${Math.min(100, c.fraction * 100)}%`;
    $('ctx-sys-preview').textContent = p.system || '';

    const sel = $('sel-prompt-key');
    const prevKey = sel.value || 'preamble';
    sel.innerHTML = (p.prompts || []).map((r) => `<option value="${r.key}">${r.key} (${r.layer}${r.dirty ? ' *' : ''})</option>`).join('');
    sel.value = prevKey;

    const syncPromptEditor = () => {
      const row = (promptData.prompts || []).find((x) => x.key === sel.value);
      if (!row) return;
      $('inp-prompt-body').value = row.disk;
      $('prompt-meta').textContent = `Source: ${row.source} ${row.placeholder ? '· required placeholder: ' + row.placeholder : ''}`;
    };
    sel.onchange = syncPromptEditor;
    syncPromptEditor();

    const rows = c.segments.map((s) => `
      <tr>
        <td>#${s.id}</td><td>${s.type}</td><td>${s.role}</td>
        <td>${s.tokens.toLocaleString()}</td><td>${s.label}</td>
        <td>${s.protected ? '' : `<button class="btn small" data-drop="${s.id}">drop</button>`}</td>
      </tr>`).join('');
    $('ctx-table').innerHTML = `<thead><tr><th>ID</th><th>Type</th><th>Role</th><th>Tokens</th><th>Label</th><th></th></tr></thead><tbody>${rows}</tbody>`;
    $('ctx-table').querySelectorAll('[data-drop]').forEach((b) => {
      b.onclick = async () => {
        await api('/api/context/drop', { method: 'POST', body: { id: Number(b.dataset.drop) } });
        loadContextTab();
      };
    });
  }

  async function refreshPerf() {
    const p = await api('/api/perf');
    drawSpark(p.turns || []);
    const canvas = $('perf-canvas');
    const ctx = canvas.getContext('2d');
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    const turns = p.turns || [];
    if (turns.length) {
      const maxTps = Math.max(...turns.map((t) => t.tps), 10);
      const bw = Math.min(48, Math.floor((canvas.width - 40) / turns.length) - 6);
      turns.forEach((t, i) => {
        const h = Math.round((t.tps / maxTps) * (canvas.height - 40));
        ctx.fillStyle = '#ff6b35';
        ctx.fillRect(24 + i * (bw + 6), canvas.height - 22 - h, Math.max(6, bw), h);
      });
    }
    $('perf-table').innerHTML = `<thead><tr><th>Turn</th><th>Model</th><th>tok/s</th><th>TTFT</th><th>Out</th><th>Context In</th></tr></thead><tbody>` +
      turns.map((t) => `<tr><td>#${t.turn}</td><td>${t.model}</td><td>${t.tps}</td><td>${t.ttft ?? '—'}s</td><td>${t.out_tokens}</td><td>${t.context_in.toLocaleString()}</td></tr>`).join('') +
      `</tbody>`;
  }

  async function loadModelsAndKeys() {
    catalog = await api('/api/models');
    const provSel = $('sel-provider');
    provSel.innerHTML = catalog.providers.map((p) => `<option value="${p.name}">${p.name}${p.has_key ? ' ✓' : ''}</option>`).join('');
    provSel.value = catalog.current_provider;

    const syncModels = () => {
      const pObj = catalog.providers.find((x) => x.name === provSel.value);
      $('sel-model').innerHTML = (pObj ? pObj.models : []).map((m) => `<option value="${m.id}">${m.label} (${m.id})${m.video ? ' 🎬' : ''}</option>`).join('');
      if (provSel.value === catalog.current_provider) $('sel-model').value = catalog.current_model.id;
    };
    provSel.onchange = syncModels;
    syncModels();

    $('sel-effort').innerHTML = catalog.efforts.map((e) => `<option value="${e}">${e}</option>`).join('');
    $('sel-effort').value = catalog.effort;

    const k = await api('/api/keys');
    $('keys-list').innerHTML = k.providers.map((p) => `<div><strong>${p.provider}</strong>: <span class="muted">${p.masked}</span></div>`).join('');

    const env = await api('/api/env');
    $('env-blocks-list').innerHTML = env.blocks.length
      ? env.blocks.map((b) => `<button class="btn small" data-blk="${b.name}">${b.enabled ? '● ' : '○ '}${b.name} ${b.note}</button>`).join('')
      : '<span class="muted">No #---# blocks in ~/.davag/.env</span>';
    $('env-blocks-list').querySelectorAll('[data-blk]').forEach((btn) => {
      btn.onclick = async () => {
        await api('/api/env', { method: 'POST', body: { block: btn.dataset.blk } });
        loadModelsAndKeys();
      };
    });
  }

  async function loadArtifacts() {
    const a = await api('/api/artifacts');
    const list = $('artifacts-list');
    if (!a.artifacts.length) {
      list.innerHTML = '<span class="muted">No artifacts emitted yet.</span>';
      return;
    }
    list.innerHTML = a.artifacts.map((it) => `<div class="row-between card"><span>#${it.id} [${it.lang}] ${it.title} (${it.lines} lines)</span><button class="btn small" data-art="${it.id}">View / Download</button></div>`).join('');
    list.querySelectorAll('[data-art]').forEach((b) => {
      b.onclick = async () => {
        const full = await api(`/api/artifact/${b.dataset.art}`);
        $('artifact-preview').hidden = false;
        $('artifact-preview').textContent = full.body;
      };
    });
  }

  async function boot() {
    const s = await api('/api/session');
    const c = await api('/api/commands');
    commands = c.commands || [];
    sessionStorage.setItem('davag_token', token);
    // Strip ?token=... from address bar after saving to sessionStorage so it isn't exposed on screen
    if (new URLSearchParams(location.search).has('token')) {
      const cleanUrl = new URL(location.href);
      cleanUrl.searchParams.delete('token');
      history.replaceState(null, '', cleanUrl.pathname + cleanUrl.search + cleanUrl.hash);
    }
    $('gate').hidden = true;
    $('app').hidden = false;
    updateHeader(s);
    connectSSE();
    refreshPerf();
  }

  // Event bindings
  $('gate-form').onsubmit = async (e) => {
    e.preventDefault();
    token = $('gate-token').value.trim();
    try {
      await boot();
    } catch (err) {
      $('gate-error').textContent = err.message;
    }
  };

  document.querySelectorAll('.tab').forEach((btn) => {
    btn.onclick = () => {
      document.querySelectorAll('.tab').forEach((t) => t.classList.remove('active'));
      btn.classList.add('active');
      const target = btn.dataset.tab;
      document.querySelectorAll('.view').forEach((v) => { v.hidden = v.id !== `view-${target}`; });
      if (target === 'resolve') loadResolveInspector();
      if (target === 'context') loadContextTab();
      if (target === 'perf') refreshPerf();
      if (target === 'models') loadModelsAndKeys();
      if (target === 'artifacts') loadArtifacts();
    };
  });

  $('composer-form').onsubmit = async (e) => {
    e.preventDefault();
    const inp = $('composer-input');
    const txt = inp.value.trim();
    if (!txt) return;
    inp.value = '';
    $('cmd-suggest').hidden = true;
    await api('/api/submit', { method: 'POST', body: { text: txt } });
  };

  $('composer-input').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      $('composer-form').requestSubmit();
    } else if (e.key === 'Escape') {
      api('/api/cancel', { method: 'POST' });
    }
  });

  $('composer-input').addEventListener('input', () => {
    const val = $('composer-input').value;
    const box = $('cmd-suggest');
    if (!val.startsWith('/') || val.includes(' ')) {
      box.hidden = true;
      return;
    }
    const matches = commands.filter((c) => c.name.startsWith(val.toLowerCase()));
    if (!matches.length) { box.hidden = true; return; }
    box.hidden = false;
    box.innerHTML = matches.map((m) => `<div class="cmd-item" data-cmd="${m.name}"><strong>${m.usage}</strong><span class="muted">${m.desc}</span></div>`).join('');
    box.querySelectorAll('[data-cmd]').forEach((el) => {
      el.onclick = () => {
        $('composer-input').value = el.dataset.cmd + ' ';
        box.hidden = true;
        $('composer-input').focus();
      };
    });
  });

  $('btn-cancel').onclick = () => api('/api/cancel', { method: 'POST' });
  $('ap-allow').onclick = () => answerApproval(true, false);
  $('ap-checkpoint').onclick = () => answerApproval(true, true);
  $('ap-deny').onclick = () => answerApproval(false, false);

  $('btn-grab-frame').onclick = () => api('/api/resolve/grab', { method: 'POST', body: { attach: true } });
  $('viewer-close').onclick = () => { $('viewer-drawer').hidden = true; };
  $('btn-undo-tl').onclick = () => api('/api/resolve/undo', { method: 'POST' });
  $('btn-res-refresh').onclick = loadResolveInspector;
  $('btn-res-direct').onclick = async () => { await api('/api/resolve/reconnect', { method: 'POST', body: { mode: 'direct' } }); loadResolveInspector(); };
  $('btn-res-bridge').onclick = async () => { await api('/api/resolve/reconnect', { method: 'POST', body: { mode: 'bridge' } }); loadResolveInspector(); };
  $('btn-ctx-compact').onclick = async () => { await api('/api/compact', { method: 'POST' }); loadContextTab(); };

  $('btn-apply-model').onclick = async () => {
    const custom = $('inp-custom-model').value.trim();
    await api('/api/model', {
      method: 'POST',
      body: {
        provider: $('sel-provider').value,
        model: custom || $('sel-model').value,
        effort: $('sel-effort').value,
        persist: $('chk-persist-model').checked,
      },
    });
    loadModelsAndKeys();
  };

  $('btn-save-key').onclick = async () => {
    const val = $('inp-api-key').value.trim();
    if (!val) return;
    await api('/api/keys', {
      method: 'POST',
      body: { provider: $('sel-key-prov').value, key: val, save: $('chk-save-key').checked },
    });
    $('inp-api-key').value = '';
    loadModelsAndKeys();
  };

  function flashBtn(btn, label) {
    const orig = btn.textContent;
    btn.textContent = label;
    setTimeout(() => { btn.textContent = orig; }, 1600);
  }

  $('btn-copy-url').onclick = async () => {
    const share = `${location.origin}/?token=${encodeURIComponent(token)}`;
    await navigator.clipboard.writeText(share);
    flashBtn($('btn-copy-url'), '✓ Copied URL!');
  };

  $('btn-copy-tok').onclick = async () => {
    await navigator.clipboard.writeText(token);
    flashBtn($('btn-copy-tok'), '✓ Copied Token!');
  };

  $('btn-toggle-sys').onclick = () => {
    $('ctx-sys-preview').hidden = !$('ctx-sys-preview').hidden;
  };

  $('btn-prompt-save').onclick = async () => {
    await api('/api/prompts/save', {
      method: 'POST',
      body: { key: $('sel-prompt-key').value, text: $('inp-prompt-body').value, layer: 'project' },
    });
    flashBtn($('btn-prompt-save'), '✓ Saved!');
    loadContextTab();
  };

  $('btn-prompt-revert').onclick = async () => {
    await api('/api/prompts/revert', {
      method: 'POST',
      body: { key: $('sel-prompt-key').value, layer: 'project' },
    });
    flashBtn($('btn-prompt-revert'), '✓ Reverted');
    loadContextTab();
  };

  $('btn-prompt-apply').onclick = async () => {
    await api('/api/prompts/apply', { method: 'POST', body: {} });
    flashBtn($('btn-prompt-apply'), '✓ Applied!');
    loadContextTab();
  };

  if (token) {
    boot().catch(() => { $('gate').hidden = false; });
  }
})();