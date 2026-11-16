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

  let busyActive = false;
  let busyBaseLabel = '';
  let busyPhaseStarted = 0;
  let busyTimer = null;

  function renderBusyTick() {
    if (!busyActive) return;
    const elapsed = ((Date.now() - busyPhaseStarted) / 1000).toFixed(1);
    const full = `${busyBaseLabel || 'working…'} (${elapsed}s)`;
    $('live-status-text').textContent = full;
    $('hdr-activity-text').textContent = full;
    if ($('nle-ai-live')) $('nle-ai-live').textContent = `⚡ ${full}`;
  }

  function setBusy(busy, statusText = '') {
    busyActive = Boolean(busy);
    $('btn-cancel').hidden = !busyActive;
    $('live-status').hidden = !busyActive;
    $('hdr-activity').hidden = !busyActive;
    if (busyActive) {
      if (statusText && statusText !== busyBaseLabel) {
        busyBaseLabel = statusText;
        busyPhaseStarted = Date.now();
      } else if (!busyPhaseStarted) {
        busyPhaseStarted = Date.now();
      }
      renderBusyTick();
      if (!busyTimer) busyTimer = setInterval(renderBusyTick, 100);
      $('scroll').scrollTop = $('scroll').scrollHeight;
    } else {
      busyBaseLabel = '';
      busyPhaseStarted = 0;
      if (busyTimer) { clearInterval(busyTimer); busyTimer = null; }
      if ($('nle-ai-live')) $('nle-ai-live').textContent = '';
    }
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
    if (info.role) $('sel-hdr-role').value = info.role;
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
      setBusy(true, 'thinking…');
      $('live-thinking').hidden = false;
      $('live-thinking').textContent += delta;
      $('scroll').scrollTop = $('scroll').scrollHeight;
      const popSt = document.getElementById('pop-live-stream');
      if (popSt) {
        popSt.hidden = false;
        popSt.textContent = '🧠 ' + $('live-thinking').textContent.slice(-110).replace(/\s+/g, ' ');
      }
    });

    es.addEventListener('text', (e) => {
      trackSeq(e);
      const delta = JSON.parse(e.data);
      setBusy(true, 'streaming response…');
      $('live-thinking').hidden = true;
      $('live-text').hidden = false;
      $('live-text').textContent += delta;
      $('scroll').scrollTop = $('scroll').scrollHeight;

      const fullStream = $('live-text').textContent;
      const popSt = document.getElementById('pop-live-stream');
      if (popSt) {
        popSt.hidden = false;
        popSt.textContent = `✍️ streaming (${fullStream.length} chars)… ` + fullStream.slice(-90).replace(/\s+/g, ' ');
      }
      // Instant Realtime Hot-Swap: as soon as `--davag:end--` closes an HTML/SVG payload in the stream,
      // live-composite it onto the 16:9 Viewer & Popover BEFORE overlay `.mov` rendering even finishes!
      const payloadMatch = fullStream.match(/--davag:[^\n]+--\n([\s\S]*?(?:<style|<svg|<div|<script)[\s\S]*?)\n--davag:end--/);
      if (payloadMatch && payloadMatch[1]) {
        window.dispatchEvent(new CustomEvent('davag-live-html', { detail: payloadMatch[1] }));
      }
    });

    es.addEventListener('ops-parsed', (e) => {
      trackSeq(e);
      const ops = JSON.parse(e.data);
      const names = ops.slice(0, 2).map((o) => o.op).join(', ') + (ops.length > 2 ? ` (+${ops.length - 2})` : '');
      setBusy(true, `running ${names}…`);
      appendEntry('ops', '· ' + ops.map((o) => o.op).join(', '));
    });

    es.addEventListener('ops-result', (e) => {
      trackSeq(e);
      const d = JSON.parse(e.data);
      setBusy(true, 'preparing next turn…');
      if (d.checkpoint) appendEntry('notice', `checkpoint saved: ${d.checkpoint}`);
      for (const err of (d.errors || [])) {
        appendEntry('warning', `✗ [${err.index}] ${err.op}: ${err.message}`);
      }
      refreshResolveStrip();
      if (!$('view-timeline').hidden) loadTimelineStudio();
    });

    es.addEventListener('compact-start', (e) => {
      trackSeq(e);
      setBusy(true, 'compacting context…');
    });

    es.addEventListener('turn-end', (e) => {
      trackSeq(e);
      if (!$('live-text').hidden && $('live-text').textContent.trim()) {
        appendEntry('assistant', $('live-text').textContent);
      }
      $('live-text').hidden = true;
      $('live-thinking').hidden = true;
      // Do NOT clear busy here — if dr_ops were emitted, ops-parsed runs next; `done` clears busy!
      if (busyActive) setBusy(true, 'processing turn…');
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

    const clipsHtml = (r.clips || []).filter((c) => c.file_path).map((c) => {
      const trText = c.transcript
        ? c.transcript.segments.map((s) => `[${s.start_tc} · f${s.start_frame}] ${s.text}`).join('\n')
        : '';
      return `
      <div class="card clip-studio" data-cid="${c.clip_id}" style="background:#0d121c;">
        <div class="row-between" style="margin-bottom:4px;">
          <strong>${c.name}</strong>
          <span class="muted">${c.resolution} @ ${c.fps}fps · ${c.frames.toLocaleString()}f (${(c.frames / (Number(c.fps) || 30) / 60).toFixed(1)}m)</span>
        </div>
        <div class="muted" style="font-size:11px;">
          <code>${c.clip_id}</code> · ${c.file_path}
          ${c.proxy_path ? ' · <span style="color:#38b000; font-weight:bold;">✓ Linked in Resolve</span>' : ''}
          ${c.proxy_file_name ? ` · <span style="color:#4cc9f0;">Cached: ${c.proxy_file_name}</span>` : ''}
        </div>

        ${c.proxy_url ? `
          <div>
            <div class="muted" style="font-size:11px; margin-bottom:4px;">🎬 Proxy Video Player (${c.proxy_file_name || 'linked'}):</div>
            <video src="${c.proxy_url}?token=${encodeURIComponent(token)}&t=${Date.now()}" controls preload="metadata" style="width:100%; max-height:260px; border-radius:5px; background:#000; border:1px solid #232b3d;"></video>
          </div>` : ''}

        ${c.sheet_url ? `
          <div>
            <div class="muted" style="font-size:11px; margin-bottom:4px;">🎞 Mosaic Contact Sheet (click image for full-size zoom):</div>
            <a href="${c.sheet_url}?token=${encodeURIComponent(token)}" target="_blank" rel="noopener">
              <img src="${c.sheet_url}?token=${encodeURIComponent(token)}&t=${Date.now()}" style="width:100%; border-radius:5px; border:1px solid #232b3d;" />
            </a>
          </div>` : ''}

        <!-- Proxy Controls -->
        <div class="row-gap" style="background:#121927; padding:8px; border-radius:6px; border:1px solid #1f293d;">
          <strong style="font-size:12px;">Proxy:</strong>
          <select class="js-proxy-res">
            <option value="360:700">360p (700 kbps · ultra-fast)</option>
            <option value="540:1800">540p (1.8 Mbps · quarter-UHD)</option>
            <option value="720:3500" selected>720p (3.5 Mbps · smooth edit)</option>
            <option value="1080:6000">1080p (6.0 Mbps · sharp UI text)</option>
          </select>
          <label class="inline" style="font-size:12px;"><input type="checkbox" class="js-proxy-link" checked /> Link in Resolve</label>
          <button class="btn small primary js-act" data-action="proxy">⚡ Generate Proxy</button>
          ${c.proxy_path ? `<button class="btn small js-act" data-action="unlink">Unlink</button>` : ''}
        </div>

        <!-- Mosaic, Scout & Transcript Controls -->
        <div class="row-gap" style="background:#121927; padding:8px; border-radius:6px; border:1px solid #1f293d;">
          <strong style="font-size:12px;">Inspect:</strong>
          <select class="js-grid">
            <option value="4x4" selected>4×4 Mosaic (16f)</option>
            <option value="5x5">5×5 Mosaic (25f)</option>
            <option value="6x6">6×6 Mosaic (36f)</option>
          </select>
          <input class="js-start" type="number" placeholder="Start(s) all" style="width:92px;" />
          <input class="js-dur" type="number" placeholder="Dur(s) all" style="width:88px;" />
          <label class="inline" style="font-size:12px;"><input type="checkbox" class="js-attach" checked /> Attach to Chat</label>
          <button class="btn small js-act" data-action="sheet">🎞 Build Mosaic</button>
          <button class="btn small js-act" data-action="transcribe">📝 Extract Transcript</button>
          <button class="btn small js-act" data-action="index">🔍 Scout Digest</button>
        </div>

        <div class="muted js-clip-status" hidden></div>

        ${c.digest ? `<details open><summary style="cursor:pointer; font-weight:bold; font-size:12px;">Scout Digest (${c.scout_model || 'cached'})</summary><pre class="ap-code" style="font-size:11px;">${c.digest}</pre></details>` : ''}
        ${c.transcript ? `<details><summary style="cursor:pointer; font-weight:bold; font-size:12px;">Cached Transcript (${c.transcript.backend} · ${c.transcript.segment_count} segments)</summary><pre class="ap-code" style="font-size:11px;">${trText}</pre></details>` : ''}
      </div>`;
    }).join('') || '<span class="muted">No file-backed clips in Media Pool.</span>';

    const tl = r.timeline ? JSON.stringify(r.timeline, null, 2) : 'No active timeline';
    box.innerHTML = `
      <div class="card"><h3>Clip Proxy, Mosaic & Transcript Studio</h3><div class="stack">${clipsHtml}</div></div>
      <div class="card"><h3>Active Timeline State</h3><pre class="ap-code">${tl}</pre></div>
    `;

    box.querySelectorAll('.js-act').forEach((btn) => {
      btn.onclick = async () => {
        const card = btn.closest('.clip-studio');
        const cid = card.dataset.cid;
        const act = btn.dataset.action;
        const stEl = card.querySelector('.js-clip-status');
        const [cols, rows] = card.querySelector('.js-grid').value.split('x').map(Number);
        const [height, kbps] = card.querySelector('.js-proxy-res').value.split(':').map(Number);
        const startSec = card.querySelector('.js-start').value.trim();
        const durSec = card.querySelector('.js-dur').value.trim();
        const attach = card.querySelector('.js-attach').checked;
        const linkRes = card.querySelector('.js-proxy-link').checked;

        let body = { clip_id: cid, action: act };
        if (act === 'proxy') body = { clip_id: cid, action: 'proxy', height, video_kbps: kbps, link_in_resolve: linkRes };
        else if (act === 'unlink') body = { clip_id: cid, action: 'proxy', unlink: true };
        else if (act === 'sheet') body = { clip_id: cid, action: 'sheet', cols, rows, start_sec: startSec, duration_sec: durSec, attach };
        else if (act === 'transcribe') body = { clip_id: cid, action: 'transcribe', start_sec: startSec, duration_sec: durSec, attach };
        else if (act === 'index') body = { clip_id: cid, action: 'index', cols, rows, force_refresh: true, attach };

        btn.disabled = true;
        stEl.hidden = false;
        stEl.textContent = `⟳ Running ${act}… (large recordings may take 10–60s)`;
        setBusy(true, `clip ${act}…`);
        try {
          await api('/api/clip/action', { method: 'POST', body });
          await loadResolveInspector();
        } catch (err) {
          stEl.textContent = `✗ ${err.message}`;
        } finally {
          btn.disabled = false;
          setBusy(false);
        }
      };
    });
  }

  // Prompter & Overlays unified into NLE Timeline Studio below

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

  let promptHistory = [];
  let histCursor = -1;
  let draftBeforeHist = '';

  async function boot() {
    const [s, c, h] = await Promise.all([api('/api/session'), api('/api/commands'), api('/api/history')]);
    commands = c.commands || [];
    promptHistory = h.prompts || [];
    histCursor = promptHistory.length;
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
      if (target === 'timeline') loadTimelineStudio();
      else stopFollowPoll();
      if (target === 'resolve') loadResolveInspector();
      if (target === 'context') loadContextTab();
      if (target === 'perf') refreshPerf();
      if (target === 'models') loadModelsAndKeys();
      if (target === 'artifacts') loadArtifacts();
      if (target === 'help') loadDocsTab();
    };
  });

  $('composer-form').onsubmit = async (e) => {
    e.preventDefault();
    const inp = $('composer-input');
    const txt = inp.value.trim();
    if (!txt) return;
    if (!txt.startsWith('/') && txt !== 'sh' && !txt.startsWith('sh ')) {
      if (promptHistory[promptHistory.length - 1] !== txt) promptHistory.push(txt);
    }
    histCursor = promptHistory.length;
    draftBeforeHist = '';
    inp.value = '';
    $('cmd-suggest').hidden = true;
    if (!txt.startsWith('/')) setBusy(true, 'sending prompt…');
    await api('/api/submit', { method: 'POST', body: { text: txt } });
  };

  $('composer-input').addEventListener('keydown', (e) => {
    const inp = $('composer-input');
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      $('composer-form').requestSubmit();
    } else if (e.key === 'Escape') {
      api('/api/cancel', { method: 'POST' });
    } else if (e.key === 'ArrowUp' && !inp.value.slice(0, inp.selectionStart).includes('\n') && promptHistory.length) {
      if (histCursor === promptHistory.length) draftBeforeHist = inp.value;
      if (histCursor > 0) {
        e.preventDefault();
        histCursor -= 1;
        inp.value = promptHistory[histCursor];
      }
    } else if (e.key === 'ArrowDown' && !inp.value.slice(inp.selectionEnd).includes('\n') && histCursor < promptHistory.length) {
      e.preventDefault();
      histCursor += 1;
      inp.value = histCursor === promptHistory.length ? draftBeforeHist : promptHistory[histCursor];
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

  $('btn-prompter-push').onclick = async () => {
    const res = await api('/api/prompter/push', { method: 'POST', body: { import_srt_track: true } });
    flashBtn($('btn-prompter-push'), res.imported_onto_timeline ? '✓ Pushed SRT & ADR CSV!' : '✓ Saved CSV & SRT!');
  };

  $('btn-prompter-polish').onclick = async () => {
    document.querySelector('[data-tab="chat"]').click();
    const prompt = 'Write natural, conversational first-person YouTube voiceover lines for every Cyan chapter marker on the active timeline (paced to ~2.3 words/sec per beat duration), update marker notes, and call `timeline.vo_script` with `import_srt_track: true`!';
    setBusy(true, 'sending prompt…');
    await api('/api/submit', { method: 'POST', body: { text: prompt } });
  };

  $('sel-hdr-role').onchange = async (e) => {
    const role = e.target.value;
    try {
      await api('/api/role', { method: 'POST', body: { role } });
      appendEntry('notice', `Switched active capability role -> ${role}`);
    } catch (err) {
      appendEntry('warning', err.message);
    }
  };

  function escHtml(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  function inlineMd(s) {
    return escHtml(s)
      .replace(/`([^`]+)`/g, '<code>$1</code>')
      .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
      .replace(/\*([^*]+)\*/g, '<em>$1</em>');
  }

  function renderMarkdown(md) {
    const lines = String(md || '').replace(/\r\n/g, '\n').split('\n');
    const out = [];
    let i = 0;
    while (i < lines.length) {
      const ln = lines[i];
      if (ln.startsWith('```')) {
        const codeLines = [];
        i++;
        while (i < lines.length && !lines[i].startsWith('```')) { codeLines.push(lines[i]); i++; }
        i++;
        out.push(`<pre><code>${escHtml(codeLines.join('\n'))}</code></pre>`);
        continue;
      }
      if (ln.startsWith('|') && i + 1 < lines.length && /^\|[\s:|-]+\|/.test(lines[i + 1])) {
        const splitCells = (r) => r.trim().replace(/^\||\|$/g, '').replace(/\\\|/g, '&#124;').split('|').map((c) => inlineMd(c.trim()).replace(/&#124;/g, '|'));
        const headers = splitCells(ln);
        i += 2;
        const rows = [];
        while (i < lines.length && lines[i].trim().startsWith('|')) { rows.push(splitCells(lines[i])); i++; }
        out.push(`<table><thead><tr>${headers.map((h) => `<th>${h}</th>`).join('')}</tr></thead><tbody>${rows.map((r) => `<tr>${r.map((c) => `<td>${c}</td>`).join('')}</tr>`).join('')}</tbody></table>`);
        continue;
      }
      if (/^#{1,4}\s/.test(ln)) {
        const lvl = ln.match(/^#+/)[0].length;
        out.push(`<h${lvl}>${inlineMd(ln.slice(lvl).trim())}</h${lvl}>`);
        i++;
        continue;
      }
      if (/^---+$/.test(ln.trim())) { out.push('<hr />'); i++; continue; }
      if (/^\s*[-*]\s+/.test(ln)) {
        const items = [];
        while (i < lines.length && /^\s*[-*]\s+/.test(lines[i])) {
          items.push(`<li>${inlineMd(lines[i].replace(/^\s*[-*]\s+/, ''))}</li>`);
          i++;
        }
        out.push(`<ul>${items.join('')}</ul>`);
        continue;
      }
      if (/^\s*\d+\.\s+/.test(ln)) {
        const items = [];
        while (i < lines.length && /^\s*\d+\.\s+/.test(lines[i])) {
          items.push(`<li>${inlineMd(lines[i].replace(/^\s*\d+\.\s+/, ''))}</li>`);
          i++;
        }
        out.push(`<ol>${items.join('')}</ol>`);
        continue;
      }
      if (!ln.trim()) { i++; continue; }
      out.push(`<p>${inlineMd(ln)}</p>`);
      i++;
    }
    return out.join('\n');
  }

  // --- Unified DaVinci-Resolve-Style NLE Timeline, Markers, VO Prompter & Overlays Studio ---
  const RESOLVE_COLORS = [
    { name: 'Cyan', hex: '#00b4d8', role: 'Chapter & VO Beat' },
    { name: 'Purple', hex: '#9d4edd', role: 'QR Link Callout' },
    { name: 'Mint', hex: '#2ec4b6', role: 'JS/SVG Motion Overlay' },
    { name: 'Yellow', hex: '#ffbe0b', role: 'Editorial Note' },
    { name: 'Red', hex: '#ef233c', role: 'Cut / Fix Note' },
    { name: 'Blue', hex: '#3a86ff', role: 'B-Roll / Ref' },
    { name: 'Green', hex: '#38b000', role: 'Approved' },
    { name: 'Pink', hex: '#ff70a6', role: 'Highlight' },
  ];
  const COLOR_HEX = Object.fromEntries(RESOLVE_COLORS.map((c) => [c.name.toLowerCase(), c.hex]));

  const DEFAULT_OV_SAMPLE = `<div id="card" style="position:absolute;bottom:80px;right:80px;padding:32px 44px;background:rgba(12,17,28,0.94);border-left:10px solid #2ec4b6;border-radius:18px;color:#fff;box-shadow:0 24px 60px rgba(0,0,0,0.65);font-family:system-ui,sans-serif;">
  <div style="font-size:22px;letter-spacing:0.1em;color:#2ec4b6;font-weight:800;">KEY METRIC</div>
  <div id="metric" style="font-size:64px;font-weight:900;margin-top:6px;">0.0x</div>
</div>
<script>
  window.renderFrame = (f, t, dur) => {
    const p = Math.min(1, t / Math.max(0.2, dur * 0.7));
    const ease = 1 - Math.pow(1 - p, 3);
    document.getElementById('card').style.opacity = String(Math.min(1, t / 0.3));
    document.getElementById('metric').textContent = (ease * 90).toFixed(1) + 'x Faster';
  };
</script>`;

  const CLIP_LABEL_COLORS = [
    { name: 'Teal', hex: '#168aad' },
    { name: 'Orange', hex: '#fb8500' },
    { name: 'Lime', hex: '#70e000' },
    { name: 'Yellow', hex: '#ffbe0b' },
    { name: 'Purple', hex: '#7209b7' },
    { name: 'Pink', hex: '#f72585' },
    { name: 'Blue', hex: '#3a86ff' },
    { name: 'Violet', hex: '#560bad' },
    { name: '', hex: '#334155', label: 'Clear' },
  ];
  const CLIP_HEX = Object.fromEntries(CLIP_LABEL_COLORS.filter((c) => c.name).map((c) => [c.name.toLowerCase(), c.hex]));

  let nleData = null;
  let nleTimeline = null;
  let nleSelected = null;
  let nleCombinedItems = [];
  let nleClipTarget = null;
  let nleFilter = 'all';
  let nlePlayheadFrame = 108000;
  let nleHoverFrame = null; // non-null while mouse is over #ov-nle-stage!
  let tlZoom = 1.0;        // 1.0x .. 20.0x mouse-wheel zoom!
  let tlViewStartF = 0;    // visible window start frame
  let tlViewDurF = 1800;   // visible window duration in frames
  let hierScale = 1.0;     // Ctrl+Wheel zoom scale inside Hierarchy Browser
  let activeOvHtml = DEFAULT_OV_SAMPLE;
  let activeOvDur = 5.5;
  let lastQuantFrame = -1;
  let hiResTimer = null;
  let hiResReqSeq = 0;
  let followTimer = null;
  let ttAnimTimer = null;

  function stopFollowPoll() {
    if (followTimer) { clearInterval(followTimer); followTimer = null; }
  }

  function framesToSmpte(frame, fps) {
    const fInt = Math.max(0, Math.round(frame));
    const rate = Math.max(1, Math.round(fps || 30));
    const ff = fInt % rate;
    const totalSec = Math.floor(fInt / rate);
    const p2 = (v) => String(v).padStart(2, '0');
    return `${p2(Math.floor(totalSec / 3600))}:${p2(Math.floor(totalSec / 60) % 60)}:${p2(totalSec % 60)}:${p2(ff)}`;
  }

  function framesToYtTs(frame, startFrame, fps) {
    const sec = Math.max(0, Math.floor((frame - startFrame) / Math.max(1, fps || 30)));
    return `${String(Math.floor(sec / 60)).padStart(2, '0')}:${String(sec % 60).padStart(2, '0')}`;
  }

  function fitOverlayIframe() {
    const stage = $('nle-viewer-stage');
    const ifr = $('ifr-ov-preview');
    if (!stage || !ifr || !stage.clientWidth) return;
    ifr.style.transform = `scale(${stage.clientWidth / 1920})`;
  }
  window.addEventListener('resize', fitOverlayIframe);

  let sandboxDebounce = null;
  function pushSandboxPreview() {
    const ifr = $('ifr-ov-preview');
    const hud = $('nle-ov-debug-hud');
    const isOvSelected = nleSelected && (nleSelected.kind === 'overlay' || String(nleSelected.color || '').toLowerCase() === 'mint');
    hud.hidden = !isOvSelected;
    const showOverlay = isOvSelected && $('chk-ov-live-canvas').checked && Boolean(activeOvHtml);
    ifr.hidden = !showOverlay;
    if (!showOverlay) {
      ifr.removeAttribute('srcdoc');
      return;
    }
    const dur = Math.max(0.5, Number(activeOvDur) || 5.5);
    $('rng-ov-scrub').max = String(dur);
    const t = Math.min(dur, Number($('rng-ov-scrub').value) || 0);
    $('lbl-ov-scrub').textContent = `t=${t.toFixed(1)}s`;
    if (sandboxDebounce) clearTimeout(sandboxDebounce);
    sandboxDebounce = setTimeout(() => {
      ifr.srcdoc = `<!doctype html><html><head><meta charset="utf-8"><style>html,body{margin:0;width:1920px;height:1080px;overflow:hidden;background:transparent;}</style></head><body>${activeOvHtml}<script>window.addEventListener('load',()=>{if(typeof window.renderFrame==='function')window.renderFrame(${Math.round(t * 30)},${t},${dur},30);});<\/script></body></html>`;
      fitOverlayIframe();
    }, 90);
  }

  function updateViewerAtFrame(frame, labelSuffix = '', forceLive = false) {
    if (!nleTimeline) return;
    const tc = framesToSmpte(frame, nleTimeline.fps);
    const yt = framesToYtTs(frame, nleTimeline.start_frame, nleTimeline.fps);
    $('nle-viewer-badge').textContent = `${tc} · YT ${yt}${labelSuffix ? ' · ' + labelSuffix : ''}`;

    const stepF = Math.max(1, Math.round((nleTimeline.fps || 30) * 0.35));
    const qFrame = forceLive ? Math.round(frame) : Math.round(frame / stepF) * stepF;
    if (qFrame !== lastQuantFrame && !forceLive) {
      lastQuantFrame = qFrame;
      $('nle-viewer-img').src = `/api/overlays/thumb?frame=${qFrame}&w=540&live=0&token=${encodeURIComponent(token)}`;
    }

    // IMPORTANT: Never call `live=1` (`timeline.grab_still`) automatically on hover!
    // Hovering stays 100% read-only via FFmpeg (`live=0`) so Resolve's C++ viewer is NEVER hammered or crashed!
    if (hiResTimer) clearTimeout(hiResTimer);
    const mySeq = ++hiResReqSeq;
    hiResTimer = setTimeout(() => {
      const exactF = Math.round(frame);
      const hdUrl = `/api/overlays/thumb?frame=${exactF}&w=1600&live=${forceLive ? 1 : 0}&token=${encodeURIComponent(token)}${forceLive ? '&t=' + Date.now() : ''}`;
      const pre = new Image();
      pre.onload = () => {
        if (mySeq !== hiResReqSeq) return;
        $('nle-viewer-img').src = hdUrl;
        $('nle-viewer-badge').textContent = `${tc} · YT ${yt} · ${forceLive ? '✨ HD Composite' : '✨ HD 1600p'}`;
      };
      pre.src = hdUrl;
    }, forceLive ? 0 : 170);

    // Marker Progress Bar & VO Teleprompter HUD over bottom of Program Viewer
    const voHud = $('nle-vo-hud');
    voHud.hidden = !$('chk-show-vo-hud').checked;
    const chMarkers = (nleTimeline.markers || []).filter((m) => String(m.color || '').toLowerCase() !== 'purple');
    if (chMarkers.length) {
      let curIdx = 0;
      for (let i = 0; i < chMarkers.length; i++) {
        if (frame >= chMarkers[i].frame) curIdx = i;
      }
      const curM = chMarkers[curIdx];
      const nextF = curIdx + 1 < chMarkers.length ? chMarkers[curIdx + 1].frame : nleTimeline.end_frame;
      const remSec = Math.max(0, (nextF - frame) / (nleTimeline.fps || 30));
      const pct = Math.min(100, Math.max(0, ((frame - curM.frame) / Math.max(1, nextF - curM.frame)) * 100));
      $('nle-vo-beat').textContent = `#${String(curIdx + 1).padStart(2, '0')} · ${curM.name}`;
      $('nle-vo-rem').textContent = `${remSec.toFixed(1)}s left`;
      $('nle-vo-text').textContent = curM.note || curM.name;
      $('nle-vo-fill').style.width = `${pct.toFixed(1)}%`;
    }
  }

  function syncTimelineViewportBounds() {
    if (!nleTimeline) return;
    const fullDur = Math.max(1, nleTimeline.duration_frames || 1);
    const fullStart = nleTimeline.start_frame || 0;
    tlZoom = Math.max(1.0, Math.min(24.0, tlZoom));
    tlViewDurF = Math.max(Math.round((nleTimeline.fps || 30) * 4), Math.round(fullDur / tlZoom));
    if (tlViewDurF >= fullDur) {
      tlViewDurF = fullDur;
      tlViewStartF = fullStart;
    } else {
      tlViewStartF = Math.max(fullStart, Math.min(fullStart + fullDur - tlViewDurF, Math.round(tlViewStartF)));
    }
    $('btn-tl-zoom-reset').textContent = tlZoom > 1.02 ? `🔍 ${tlZoom.toFixed(1)}× (Reset)` : '🔍 1.0×';
    const thumbLeftPct = ((tlViewStartF - fullStart) / fullDur) * 100;
    const thumbWidthPct = Math.max(3, (tlViewDurF / fullDur) * 100);
    $('ov-pan-thumb').style.left = `${thumbLeftPct}%`;
    $('ov-pan-thumb').style.width = `${Math.min(100 - thumbLeftPct, thumbWidthPct)}%`;
  }

  function setPlayheadVisual(frame) {
    if (!nleTimeline) return;
    nlePlayheadFrame = frame;
    const vDur = Math.max(1, tlViewDurF || nleTimeline.duration_frames);
    const pct = ((frame - tlViewStartF) / vDur) * 100;
    const phEl = $('ov-nle-playhead');
    phEl.hidden = pct < -0.5 || pct > 100.5;
    phEl.style.left = `${Math.max(0, Math.min(100, pct))}%`;
    $('ov-nle-tc').textContent = `${framesToSmpte(frame, nleTimeline.fps)} · ${framesToYtTs(frame, nleTimeline.start_frame, nleTimeline.fps)}`;
  }

  function setClipLabelTarget(clipObj) {
    nleClipTarget = clipObj;
    $('insp-clip-label-row').hidden = !clipObj;
    if (!clipObj) return;
    $('lbl-clip-target').textContent = `V${clipObj.track_index}: ${clipObj.name}`;
    const curC = String(clipObj.clip_color || '').toLowerCase();
    $('insp-clip-swatches').querySelectorAll('.swatch-btn').forEach((b) => {
      b.classList.toggle('active', b.dataset.color.toLowerCase() === curC);
    });
  }
  $('btn-close-clip-lbl').onclick = () => setClipLabelTarget(null);

  $('insp-clip-swatches').innerHTML = CLIP_LABEL_COLORS.map((c) =>
    `<button type="button" class="swatch-btn" data-color="${c.name}" style="background:${c.hex};" title="${c.label || c.name}"></button>`
  ).join('');
  $('insp-clip-swatches').querySelectorAll('.swatch-btn').forEach((b) => {
    b.onclick = async () => {
      if (!nleClipTarget || !nleClipTarget.item_id) return;
      $('ov-nle-status').textContent = 'Labeling clip…';
      await api('/api/overlays/adjust', {
        method: 'POST',
        body: { action: 'set_clip_color', item_id: nleClipTarget.item_id, color: b.dataset.color },
      });
      nleClipTarget.clip_color = b.dataset.color;
      setClipLabelTarget(nleClipTarget);
      $('ov-nle-status').textContent = b.dataset.color ? `✓ Clip labeled ${b.dataset.color}` : '✓ Cleared clip label';
      await loadTimelineStudio();
    };
  });

  // Update Contextual AI Co-Director Scope & Starter Chips whenever selection changes!
  function updateAiBarForSelection() {
    const scopeEl = $('nle-ai-scope');
    const chipsEl = $('nle-ai-chips');
    const fps = nleTimeline ? nleTimeline.fps : 30;
    const curTc = framesToSmpte(nleHoverFrame ?? nlePlayheadFrame, fps);

    if (!nleSelected) {
      scopeEl.textContent = `🤖 Target: Playhead ${curTc}`;
      chipsEl.innerHTML = `<button type="button" class="ai-chip" data-p="Design and render a sleek animated JS/SVG callout overlay at ${curTc} (5.5s) highlighting the key metric on screen">➕ New JS Overlay @ ${curTc}</button>`;
    } else if (nleSelected.kind === 'overlay' || String(nleSelected.color || '').toLowerCase() === 'mint') {
      scopeEl.textContent = `🤖 Target: 🟢 Overlay "${nleSelected.name}" @ ${nleSelected.tc} (${nleSelected.duration_sec}s)`;
      chipsEl.innerHTML = [
        `<button type="button" class="ai-chip" data-p="Rewrite this JS/SVG animation with smoother spring motion, bolder typography, and higher contrast, then re-render onto Resolve at ${nleSelected.tc}: ">✨ Rewrite JS Animation…</button>`,
        `<button type="button" class="ai-chip" data-p="Grab a live composite frame at ${nleSelected.tc}, check if this overlay collides with any UI text or webcam PiP, and adjust its position/sizing cleanly">🔍 Visual Debug & Reposition</button>`,
      ].join('');
    } else if (String(nleSelected.color || '').toLowerCase() === 'purple') {
      scopeEl.textContent = `🤖 Target: 🟣 QR "${nleSelected.name}" @ ${nleSelected.tc}`;
      chipsEl.innerHTML = `<button type="button" class="ai-chip" data-p="Build a custom animated QR + 3-bullet summary card for ${nleSelected.note || 'this URL'} at ${nleSelected.tc}">✨ Custom Animated QR Card</button>`;
    } else {
      scopeEl.textContent = `🤖 Target: 🔵 Beat "${nleSelected.name}" @ ${nleSelected.tc}`;
      chipsEl.innerHTML = [
        `<button type="button" class="ai-chip" data-p="Rewrite the spoken voiceover cue for marker '${nleSelected.name}' at ${nleSelected.tc} so it sounds natural, conversational, and punchy: ">🎙 Rewrite VO Cue…</button>`,
        `<button type="button" class="ai-chip" data-p="Create a 6-second animated JS/SVG motion graphic callout at ${nleSelected.tc} to visually illustrate '${nleSelected.name}'">➕ Add JS Graphic for Beat</button>`,
      ].join('');
    }

    chipsEl.querySelectorAll('.ai-chip').forEach((c) => {
      c.onclick = () => {
        $('inp-nle-ai').value = c.dataset.p;
        $('inp-nle-ai').focus();
      };
    });
  }

  $('btn-nle-open-chat').onclick = () => document.querySelector('[data-tab="chat"]').click();
  $('nle-ai-form').onsubmit = async (ev) => {
    ev.preventDefault();
    const rawPrompt = $('inp-nle-ai').value.trim();
    if (!rawPrompt) return;

    let ctxPrefix = `[Timeline Studio Context: playhead=${framesToSmpte(nlePlayheadFrame, nleTimeline ? nleTimeline.fps : 30)}`;
    if (nleSelected) {
      ctxPrefix += ` | selected ${nleSelected.kind} "${nleSelected.name}" @ tc=${nleSelected.tc} duration=${nleSelected.duration_sec}s color=${nleSelected.color}`;
      if (nleSelected.item_id) ctxPrefix += ` item_id=${nleSelected.item_id} track=V${nleSelected.track_index}`;
      if (nleSelected.note) ctxPrefix += ` note="${nleSelected.note}"`;
      ctxPrefix += `]`;
      if ((nleSelected.kind === 'overlay' || String(nleSelected.color || '').toLowerCase() === 'mint') && activeOvHtml) {
        ctxPrefix += `\nCurrent Overlay HTML/JS (\`${nleSelected.name}\`):\n\`\`\`html\n${activeOvHtml}\n\`\`\``;
      }
    } else {
      ctxPrefix += `]`;
    }

    $('inp-nle-ai').value = '';
    setBusy(true, 'sending to agent…');
    await api('/api/submit', { method: 'POST', body: { text: `${ctxPrefix}\n\n${rawPrompt}` } });
  };

  // Forgiving, Sticky Interactive Popsicle Popover + Instant Realtime AI Hot-Preview!
  let popCloseTimer = null;
  let popPinned = false;
  let popCurrentKey = '';

  function cancelPopClose() {
    if (popCloseTimer) { clearTimeout(popCloseTimer); popCloseTimer = null; }
  }

  function schedulePopClose(delayMs = 340) {
    if (popPinned) return;
    cancelPopClose();
    popCloseTimer = setTimeout(() => {
      if (!popPinned) forceClosePopover();
    }, delayMs);
  }

  function forceClosePopover() {
    popPinned = false;
    popCurrentKey = '';
    cancelPopClose();
    if (ttAnimTimer) { clearInterval(ttAnimTimer); ttAnimTimer = null; }
    $('nle-flag-tooltip').hidden = true;
  }

  const popRoot = $('nle-flag-tooltip');
  popRoot.onmouseenter = () => cancelPopClose();
  popRoot.onmouseleave = () => schedulePopClose(320);

  // Close pinned popover on Escape or outside click
  document.addEventListener('mousedown', (ev) => {
    if (popRoot.hidden) return;
    if (!popRoot.contains(ev.target) && !ev.target.closest('.nle-pop')) {
      forceClosePopover();
    }
  });

  // Live SSE Hot-Swap Listener: activates Live JS Sandbox on 16:9 Viewer while overlay renders!
  window.addEventListener('davag-live-html', (ev) => {
    activeOvHtml = ev.detail;
    $('chk-ov-live-canvas').checked = true;
    pushSandboxPreview();
    const popSt = document.getElementById('pop-live-stream');
    if (popSt) {
      popSt.hidden = false;
      popSt.textContent = '✨ New JS animation live on Viewer! Rendering transparent .mov…';
    }
  });

  async function submitScopedAiPrompt(item, promptText) {
    if (!promptText.trim()) return;
    popPinned = true;
    const popSt = document.getElementById('pop-live-stream');
    if (popSt) { popSt.hidden = false; popSt.textContent = '⚡ Sending to agent…'; }

    let ctxPrefix = `[Timeline Studio Context: selected ${item.kind} "${item.name}" @ tc=${item.tc} duration=${item.duration_sec}s color=${item.color}`;
    if (item.item_id) ctxPrefix += ` item_id=${item.item_id} track=V${item.track_index}`;
    if (item.note) ctxPrefix += ` note="${item.note}"`;
    ctxPrefix += `]`;
    const htmlCode = item.html_source || ((item.kind === 'overlay' || String(item.color || '').toLowerCase() === 'mint') ? activeOvHtml : '');
    if (htmlCode) {
      ctxPrefix += `\nCurrent Overlay HTML/JS (\`${item.name.replace(/\.mov$/i, '')}\`):\n\`\`\`html\n${htmlCode}\n\`\`\`\nRender the updated overlay onto Resolve at \`record_tc: "${item.tc}"\` with \`motion.render_overlay\`.`;
    }
    setBusy(true, 'agent working…');
    await api('/api/submit', { method: 'POST', body: { text: `${ctxPrefix}\n\n${promptText.trim()}` } });
  }

  async function openInteractivePopover(pct, item, pinOpen = false) {
    cancelPopClose();
    if (pinOpen) popPinned = true;

    // Sync Left Browser & Big 16:9 Viewer immediately on hover!
    updateViewerAtFrame(item.frame, item.name);
    selectTimelineItem(item, true);

    const key = `${item.kind}:${item.frame}:${item.name}`;
    const tt = $('nle-flag-tooltip');
    const clampedPct = Math.max(16, Math.min(84, pct * 100));
    tt.style.left = `${clampedPct}%`;
    tt.hidden = false;
    if (popCurrentKey === key && !tt.hidden && document.activeElement && tt.contains(document.activeElement)) {
      return; // Don't clobber an input the human is actively typing in!
    }
    popCurrentKey = key;
    if (ttAnimTimer) { clearInterval(ttAnimTimer); ttAnimTimer = null; }

    const cLow = String(item.color || 'Cyan').toLowerCase();
    const fps = nleTimeline ? nleTimeline.fps : 30;

    if (item.kind === 'overlay' || cLow === 'mint') {
      const dur = Math.max(1, item.duration_sec || 5.5);
      const movFileName = item.name.endsWith('.mov') ? item.name : `${item.name}.mov`;
      const videoUrl = `/api/overlays/render-video/${encodeURIComponent(movFileName)}?frame=${item.frame}&token=${encodeURIComponent(token)}`;
      tt.innerHTML = `<div class="pop-card-inner" style="border-color:${item.hex || '#10b981'};">
        <div class="row-between">
          <strong style="color:#34d399;">🎬 Rendered Animation: ${escHtml(item.name)}</strong>
          <div class="row-gap"><code>${escHtml(item.tc)} (${dur}s)</code><button type="button" class="btn small js-pop-close">✕</button></div>
        </div>
        <div class="tt-media">
          <video src="${videoUrl}" autoplay loop muted playsinline poster="/api/overlays/thumb?frame=${item.frame}&w=540&token=${encodeURIComponent(token)}"></video>
        </div>
        <form class="row-gap js-pop-ai-form">
          <input class="js-pop-ai-inp" style="flex:1;" placeholder="Prompt AI to fix/rewrite this animation… (Enter)" />
          <button type="submit" class="btn primary small">✨ Fix</button>
        </form>
        <div id="pop-live-stream" class="pop-live-stream" hidden></div>
      </div>`;
    } else if (cLow === 'purple') {
      tt.innerHTML = `<div class="pop-card-inner" style="border-color:#c77dff;">
        <div class="row-between">
          <strong style="color:#e0aaff;">🔗 QR Link · ${escHtml(item.yt)}</strong>
          <button type="button" class="btn small js-pop-close">✕</button>
        </div>
        <div class="row-gap" style="align-items:center;">
          <div id="tt-qr-svg" class="qr-live-preview" style="width:92px;height:92px;"></div>
          <div class="stack" style="flex:1;">
            <input class="js-pop-qr-title" value="${escHtml(item.name)}" placeholder="Card Title" />
            <input class="js-pop-qr-url" value="${escHtml(item.note || 'https://github.com/')}" placeholder="https://github.com/..." />
          </div>
        </div>
        <div class="row-between">
          <button type="button" class="btn small js-pop-qr-save">💾 Save Link</button>
          <button type="button" class="btn primary small js-pop-qr-render">🎬 Render QR to Video</button>
        </div>
        <div id="pop-live-stream" class="pop-live-stream" hidden></div>
      </div>`;

      const tInp = tt.querySelector('.js-pop-qr-title');
      const uInp = tt.querySelector('.js-pop-qr-url');
      const refreshTtQr = async () => {
        try {
          const r = await api(`/api/overlays/qr-preview?url=${encodeURIComponent(uInp.value.trim())}&title=${encodeURIComponent(tInp.value.trim())}`);
          const box = document.getElementById('tt-qr-svg');
          if (box) box.innerHTML = r.svg || '';
        } catch { /* ignore */ }
      };
      refreshTtQr();
      tInp.oninput = () => { popPinned = true; refreshTtQr(); };
      uInp.oninput = () => { popPinned = true; refreshTtQr(); };

      tt.querySelector('.js-pop-qr-save').onclick = async (ev) => {
        await api('/api/overlays/adjust', {
          method: 'POST',
          body: { action: 'move_marker', old_frame: item.old_frame ?? item.frame, new_tc: item.tc, duration: Math.round((item.duration_sec || 6.5) * fps), color: 'Purple', name: tInp.value.trim(), note: uInp.value.trim() },
        });
        flashBtn(ev.target, '✓ Saved!');
        await loadTimelineStudio();
      };
      tt.querySelector('.js-pop-qr-render').onclick = async (ev) => {
        const b = ev.target;
        b.disabled = true; b.textContent = '⏳ Rendering…';
        try {
          await api('/api/overlays/render', {
            method: 'POST',
            body: { kind: 'qr', title: tInp.value.trim(), url: uInp.value.trim(), record_tc: item.tc, duration_sec: item.duration_sec || 6.5, add_marker: true },
          });
          flashBtn(b, '✓ On Video!');
          await loadTimelineStudio();
        } finally { b.disabled = false; }
      };
    } else {
      tt.innerHTML = `<div class="pop-card-inner" style="border-color:${item.hex || '#00b4d8'};">
        <div class="row-between">
          <strong style="color:${item.hex};">🎙 ${escHtml(item.yt)} · ${escHtml(item.tc)}</strong>
          <button type="button" class="btn small js-pop-close">✕</button>
        </div>
        <input class="js-pop-vo-name" value="${escHtml(item.name)}" placeholder="Chapter Title" />
        <textarea rows="2" class="mono-area js-pop-vo-note" placeholder="Spoken voiceover cue or note…">${escHtml(item.note || '')}</textarea>
        <div class="row-between">
          <button type="button" class="btn primary small js-pop-vo-save">💾 Save Beat</button>
          <form class="row-gap js-pop-ai-form" style="flex:1;margin-left:6px;">
            <input class="js-pop-ai-inp" style="flex:1;" placeholder="Ask AI (e.g. add JS graphic here)…" />
            <button type="submit" class="btn small">✨ AI</button>
          </form>
        </div>
        <div id="pop-live-stream" class="pop-live-stream" hidden></div>
      </div>`;

      const nInp = tt.querySelector('.js-pop-vo-name');
      const vTa = tt.querySelector('.js-pop-vo-note');
      nInp.onfocus = () => { popPinned = true; };
      vTa.onfocus = () => { popPinned = true; };
      tt.querySelector('.js-pop-vo-save').onclick = async (ev) => {
        await api('/api/overlays/adjust', {
          method: 'POST',
          body: { action: 'move_marker', old_frame: item.old_frame ?? item.frame, new_tc: item.tc, duration: Math.round((item.duration_sec || 1) * fps), color: item.color || 'Cyan', name: nInp.value.trim(), note: vTa.value.trim() },
        });
        flashBtn(ev.target, '✓ Saved!');
        await loadTimelineStudio();
      };
    }

    // Wire close & AI form inside popover
    const closeBtn = tt.querySelector('.js-pop-close');
    if (closeBtn) closeBtn.onclick = () => forceClosePopover();
    const aiForm = tt.querySelector('.js-pop-ai-form');
    if (aiForm) {
      const aiInp = aiForm.querySelector('.js-pop-ai-inp');
      aiInp.onfocus = () => { popPinned = true; };
      aiForm.onsubmit = async (ev) => {
        ev.preventDefault();
        const txt = aiInp.value.trim();
        if (!txt) return;
        aiInp.value = '';
        await submitScopedAiPrompt(item, txt);
      };
    }
  }

  function itemKey(it) {
    return it ? `${it.kind}:${it.frame}:${it.name}` : '';
  }

  function findClosestTimelineItem(frame) {
    if (!nleCombinedItems.length) return null;
    const fps = nleTimeline ? nleTimeline.fps : 30;
    // 1. If cursor is directly inside an active QR Link or JS Overlay span, prefer that child item!
    const coveringChild = nleCombinedItems.find((it) => {
      const isChild = it.kind === 'overlay' || ['purple', 'mint'].includes(String(it.color || '').toLowerCase());
      if (!isChild) return false;
      const spanF = Math.max(fps * 2.5, (it.duration_sec || 5) * fps);
      return frame >= it.frame - fps * 0.5 && frame <= it.frame + spanF;
    });
    if (coveringChild) return coveringChild;

    // 2. Otherwise pick the closest marker/overlay by absolute frame distance!
    let best = nleCombinedItems[0];
    let bestDist = Math.abs(best.frame - frame);
    for (let i = 1; i < nleCombinedItems.length; i++) {
      const d = Math.abs(nleCombinedItems[i].frame - frame);
      if (d < bestDist) {
        best = nleCombinedItems[i];
        bestDist = d;
      }
    }
    return best;
  }

  function isEditingLeftBrowser() {
    const ae = document.activeElement;
    if (!ae) return false;
    const listEl = $('nle-index-list');
    return Boolean(listEl && listEl.contains(ae) && (ae.tagName === 'INPUT' || ae.tagName === 'TEXTAREA' || ae.tagName === 'SELECT'));
  }

  function selectTimelineItem(sel, scrollIntoBrowser = true) {
    if (!sel) return;
    if (itemKey(nleSelected) === itemKey(sel)) {
      if (scrollIntoBrowser) {
        const actEl = document.getElementById('acc-active-card');
        if (actEl) actEl.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
      }
      return;
    }
    nleSelected = sel;
    activeOvDur = sel.duration_sec || 5.5;
    if (sel.html_source) activeOvHtml = sel.html_source;
    else if (!activeOvHtml) activeOvHtml = DEFAULT_OV_SAMPLE;

    document.querySelectorAll('.nle-pop').forEach((p) => {
      p.classList.toggle('selected', Number(p.dataset.frame) === sel.frame && p.dataset.kind === sel.kind);
    });
    renderLeftAccordion(scrollIntoBrowser);
    pushSandboxPreview();
    updateAiBarForSelection();
  }

  // Left-Hand Synced Hierarchy Browser (Chapters -> indented QR Links & JS Overlays, with inline edit!)
  function renderLeftAccordion(scrollActive = false) {
    if (!nleData || !nleTimeline) return;
    const fps = nleTimeline.fps || 30;
    const startF = nleTimeline.start_frame || 0;

    if (nleFilter === 'yt') {
      const pkgs = nleData.youtube_packages || [];
      $('nle-index-list').innerHTML = pkgs.length ? pkgs.map((p) => `
        <div class="acc-card" style="padding:9px 11px;">
          <strong>${escHtml(p.title)}</strong>
          <div class="muted" style="font-size:11px;margin:4px 0;">${escHtml(p.privacy_status)} · ${p.chapters_count} ch · ${p.links_count} QR${p.end_screen ? ` · ${p.end_screen.duration_sec}s end-slate` : ''}</div>
          ${p.studio_url ? `<a class="btn small primary" href="${escHtml(p.studio_url)}" target="_blank" rel="noopener">Open YouTube Studio ↗</a>` : `<code>${escHtml(p.manifest_path)}</code>`}
        </div>`).join('') : '<span class="muted" style="padding:8px;">No staged YouTube bundles yet.</span>';
      return;
    }

    const combined = [];
    for (const m of (nleTimeline.markers || [])) {
      const cLow = String(m.color || 'Cyan').toLowerCase();
      if (nleFilter === 'vo' && cLow === 'purple') continue;
      if (nleFilter === 'qr' && cLow !== 'purple') continue;
      if (nleFilter === 'ov' && cLow !== 'mint') continue;
      const isChild = cLow === 'purple' || cLow === 'mint';
      combined.push({
        sortF: m.frame,
        isChild: nleFilter === 'all' && isChild,
        kind: 'marker',
        frame: m.frame,
        old_frame: m.frame,
        tc: m.tc,
        yt: framesToYtTs(m.frame, startF, fps),
        duration_sec: Math.max(0.5, Number(((m.duration || 1) / fps).toFixed(1))),
        color: m.color || 'Cyan',
        hex: COLOR_HEX[cLow] || '#00b4d8',
        glyph: cLow === 'purple' ? '🔗' : cLow === 'mint' ? '✨' : '🎙',
        name: m.name || 'Marker',
        note: m.note || '',
      });
    }
    if (nleFilter === 'all' || nleFilter === 'ov') {
      for (const it of (nleData.timeline_overlays || [])) {
        if (it.duration > nleTimeline.duration_frames * 0.65) continue;
        combined.push({
          sortF: it.record_start,
          isChild: nleFilter === 'all',
          kind: 'overlay',
          frame: it.record_start,
          tc: it.record_start_tc,
          yt: framesToYtTs(it.record_start, startF, fps),
          duration_sec: Number((it.duration / fps).toFixed(1)),
          color: 'Mint',
          hex: '#10b981',
          glyph: '✨',
          name: it.name,
          note: '',
          item_id: it.item_id,
          track_index: it.track_index,
          html_source: it.html_source || null,
          preview_url: it.preview_url || null,
        });
      }
    }
    combined.sort((a, b) => a.sortF - b.sortF);
    nleCombinedItems = combined;

    // Identify parent chapter if a child QR/Overlay is currently selected
    let activeParentIdx = -1;
    if (nleSelected && nleSelected.isChild) {
      for (let i = 0; i < combined.length; i++) {
        if (!combined[i].isChild && combined[i].frame <= nleSelected.frame) activeParentIdx = i;
      }
    }

    const vEndF = tlViewStartF + tlViewDurF;
    $('nle-index-list').style.zoom = String(hierScale);
    $('nle-index-list').innerHTML = combined.length ? combined.map((r, idx) => {
      const isAct = itemKey(nleSelected) === itemKey(r);
      const childCls = r.isChild ? 'child-node' : '';
      const parentCls = idx === activeParentIdx ? 'parent-active' : '';
      const outZoomCls = (tlZoom > 1.05 && (r.frame < tlViewStartF || r.frame > vEndF)) ? 'out-of-zoom' : '';
      const subSummary = r.kind === 'overlay'
        ? `🎬 Rendered overlay (${r.duration_sec}s on V${r.track_index})`
        : (r.note ? r.note : '');

      if (!isAct) {
        return `<div class="acc-card ${childCls} ${parentCls} ${outZoomCls}" data-aidx="${idx}">
          <div class="acc-hdr">
            <span class="color-dot" style="background:${r.hex};"></span>
            <code style="color:#90e0ef;font-size:11px;">${escHtml(r.yt)}</code>
            <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:${r.isChild ? '500' : '700'};">${r.isChild ? '↳ ' : ''}${r.glyph} ${escHtml(r.name)}</span>
            <span class="muted" style="font-size:10px;">${r.kind === 'overlay' ? 'V' + r.track_index : r.color}</span>
          </div>
          ${subSummary ? `<div class="acc-sub-preview">${escHtml(subSummary)}</div>` : ''}
        </div>`;
      }

      const cLow = String(r.color || 'Cyan').toLowerCase();
      const isQr = r.kind === 'marker' && cLow === 'purple';
      const isOv = r.kind === 'overlay' || cLow === 'mint';
      const swatchesHtml = r.kind === 'marker' ? `<div class="color-swatches">${RESOLVE_COLORS.map((c) =>
        `<button type="button" class="swatch-btn js-acc-color ${c.name.toLowerCase() === cLow ? 'active' : ''}" data-color="${c.name}" style="background:${c.hex};" title="${c.name} — ${c.role}"></button>`
      ).join('')}</div>` : '';

      let modeBody = '';
      if (isQr) {
        modeBody = `
          <label>Target URL (` + '`https://...`' + `) <input class="js-acc-url" value="${escHtml(r.note || 'https://github.com/')}" /></label>
          <div class="row-between" style="align-items:center;">
            <div class="stack" style="flex:1;">
              <label>Corner
                <select class="js-acc-pos">
                  <option value="bottom-right" selected>bottom-right</option>
                  <option value="bottom-left">bottom-left</option>
                  <option value="top-right">top-right</option>
                  <option value="top-left">top-left</option>
                </select>
              </label>
              <button type="button" class="btn primary small js-acc-render-qr" style="margin-top:6px;">🎬 Render QR Overlay to V3</button>
            </div>
            <div class="qr-live-preview js-acc-qr-box" style="width:94px;height:94px;"></div>
          </div>`;
      } else if (isOv) {
        const movFileName = r.name.endsWith('.mov') ? r.name : `${r.name}.mov`;
        const videoUrl = `/api/overlays/render-video/${encodeURIComponent(movFileName)}?frame=${r.frame}&token=${encodeURIComponent(token)}`;
        modeBody = `
          <div class="tt-media"><video src="${videoUrl}" autoplay loop muted playsinline></video></div>
          <div class="muted" style="font-size:11px;">HTML / CSS / SVG / JS (\`window.renderFrame(f, t, dur)\`)</div>
          <textarea rows="6" class="mono-area js-acc-html" spellcheck="false">${escHtml(activeOvHtml)}</textarea>
          <button type="button" class="btn primary small js-acc-render-ov">🎬 Render Overlay to Timeline</button>`;
      } else {
        modeBody = `
          <div class="muted" style="font-size:11px;">Marker Contents (Spoken Voiceover Cue / Note)</div>
          <textarea rows="4" class="mono-area js-acc-note" placeholder="Spoken voiceover script or editorial note…">${escHtml(r.note)}</textarea>`;
      }

      return `<div class="acc-card active ${childCls}" data-aidx="${idx}" id="acc-active-card">
        <div class="acc-hdr">
          <span class="color-dot" style="background:${r.hex};"></span>
          <code style="color:#00e5ff;font-size:11px;font-weight:800;">${escHtml(r.yt)}</code>
          <strong style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${r.glyph} ${escHtml(r.name)}</strong>
          <span class="badge">${r.kind === 'overlay' ? 'V' + r.track_index : r.color}</span>
        </div>
        <div class="acc-body">
          ${swatchesHtml}
          <div class="row-gap">
            <label style="flex:1;">Timecode <input class="js-acc-tc" value="${escHtml(r.tc)}" style="font-family:monospace;" /></label>
            <label style="width:82px;">Dur (s) <input type="number" step="0.5" class="js-acc-dur" value="${r.duration_sec}" /></label>
          </div>
          <label>Title / Name <input class="js-acc-name" value="${escHtml(r.name)}" /></label>
          ${modeBody}
          <div class="row-between" style="margin-top:2px;">
            <button type="button" class="btn primary small js-acc-save">💾 Save</button>
            <button type="button" class="btn small js-acc-ai">🤖 Ask AI</button>
            <button type="button" class="btn danger small js-acc-del">🗑 Delete</button>
          </div>
        </div>
      </div>`;
    }).join('') : '<span class="muted" style="padding:8px;">No matching items. Hover timeline & press M / Q / O!</span>';

    $('nle-index-list').querySelectorAll('.acc-card').forEach((card) => {
      const idx = Number(card.dataset.aidx);
      const item = combined[idx];
      const hdr = card.querySelector('.acc-hdr');
      hdr.onclick = async () => {
        // If zoomed in and clicked item is outside current zoom window, recenter zoom window on it!
        if (tlZoom > 1.02 && (item.frame < tlViewStartF || item.frame > tlViewStartF + tlViewDurF)) {
          tlViewStartF = item.frame - Math.round(tlViewDurF * 0.5);
          renderTimelineViewportOnly();
        }
        setPlayheadVisual(item.frame);
        updateViewerAtFrame(item.frame, item.name);
        selectTimelineItem(item, false);
        await api('/api/prompter/seek', { method: 'POST', body: { tc: item.tc } });
      };
    });

    const actCard = document.getElementById('acc-active-card');
    if (actCard && nleSelected) {
      if (scrollActive) actCard.scrollIntoView({ behavior: 'smooth', block: 'center' });

      actCard.querySelectorAll('.js-acc-color').forEach((b) => {
        b.onclick = () => {
          nleSelected.color = b.dataset.color;
          nleSelected.hex = COLOR_HEX[b.dataset.color.toLowerCase()] || '#00b4d8';
          renderLeftAccordion(false);
          pushSandboxPreview();
          updateAiBarForSelection();
        };
      });

      const qrBox = actCard.querySelector('.js-acc-qr-box');
      const urlInp = actCard.querySelector('.js-acc-url');
      const nameInp = actCard.querySelector('.js-acc-name');
      const tcInp = actCard.querySelector('.js-acc-tc');
      const durInp = actCard.querySelector('.js-acc-dur');
      const posSel = actCard.querySelector('.js-acc-pos');
      const htmlTa = actCard.querySelector('.js-acc-html');
      const noteTa = actCard.querySelector('.js-acc-note');

      const updateQrBox = async () => {
        if (!qrBox || !urlInp) return;
        try {
          const r = await api(`/api/overlays/qr-preview?url=${encodeURIComponent(urlInp.value.trim())}&title=${encodeURIComponent(nameInp.value.trim())}`);
          qrBox.innerHTML = r.svg || '';
        } catch { qrBox.innerHTML = ''; }
      };
      if (qrBox) {
        updateQrBox();
        urlInp.oninput = updateQrBox;
        nameInp.oninput = updateQrBox;
      }

      if (htmlTa) {
        htmlTa.oninput = () => { activeOvHtml = htmlTa.value; $('chk-ov-live-canvas').checked = true; pushSandboxPreview(); };
      }
      if (durInp) {
        durInp.oninput = () => { activeOvDur = Number(durInp.value) || 5.5; pushSandboxPreview(); };
      }

      const btnAi = actCard.querySelector('.js-acc-ai');
      if (btnAi) btnAi.onclick = () => $('inp-nle-ai').focus();

      const btnSave = actCard.querySelector('.js-acc-save');
      btnSave.onclick = async () => {
        const durFrames = Math.max(1, Math.round((Number(durInp.value) || 1) * fps));
        if (nleSelected.kind === 'overlay' && nleSelected.item_id) {
          await api('/api/overlays/adjust', {
            method: 'POST',
            body: { action: 'move_overlay', item_id: nleSelected.item_id, new_tc: tcInp.value.trim() },
          });
        } else {
          const noteVal = urlInp ? urlInp.value.trim() : (noteTa ? noteTa.value.trim() : '');
          await api('/api/overlays/adjust', {
            method: 'POST',
            body: {
              action: nleSelected.old_frame !== undefined ? 'move_marker' : 'add_marker',
              old_frame: nleSelected.old_frame ?? null,
              new_tc: tcInp.value.trim(),
              duration: durFrames,
              color: nleSelected.color || 'Cyan',
              name: nameInp.value.trim() || 'Marker',
              note: noteVal,
            },
          });
        }
        flashBtn(btnSave, '✓ Saved!');
        await loadTimelineStudio();
      };

      const btnDel = actCard.querySelector('.js-acc-del');
      btnDel.onclick = async () => {
        if (nleSelected.kind === 'overlay' && nleSelected.item_id) {
          await api('/api/overlays/adjust', { method: 'POST', body: { action: 'delete_overlay', item_id: nleSelected.item_id } });
        } else if (nleSelected.old_frame !== undefined || nleSelected.frame !== undefined) {
          await api('/api/overlays/adjust', { method: 'POST', body: { action: 'delete_marker', old_frame: nleSelected.old_frame ?? nleSelected.frame } });
        }
        nleSelected = null;
        await loadTimelineStudio();
      };

      const btnQr = actCard.querySelector('.js-acc-render-qr');
      if (btnQr) {
        btnQr.onclick = async () => {
          btnQr.disabled = true;
          btnQr.textContent = '⏳ Rendering QR…';
          try {
            await api('/api/overlays/render', {
              method: 'POST',
              body: {
                kind: 'qr',
                title: nameInp.value.trim() || 'Explore Code',
                url: urlInp.value.trim(),
                record_tc: tcInp.value.trim() || null,
                duration_sec: Number(durInp.value) || 6.5,
                position: posSel ? posSel.value : 'bottom-right',
                add_marker: true,
              },
            });
            await loadTimelineStudio();
          } finally { btnQr.disabled = false; }
        };
      }

      const btnOv = actCard.querySelector('.js-acc-render-ov');
      if (btnOv) {
        btnOv.onclick = async () => {
          btnOv.disabled = true;
          btnOv.textContent = '⏳ Rendering Overlay…';
          try {
            await api('/api/overlays/render', {
              method: 'POST',
              body: {
                kind: 'html',
                name: nameInp.value.trim().replace(/\.mov$/i, '') || 'custom_overlay',
                html: activeOvHtml,
                record_tc: tcInp.value.trim() || null,
                duration_sec: Number(durInp.value) || 5.5,
              },
            });
            await loadTimelineStudio();
          } finally { btnOv.disabled = false; }
        };
      }
    }
  }

  // Build crisp SVG marker head matching the operator's sketch:
  // - Cyan Chapter/VO: Tall hollow/glowing Diamond (`◇`)
  // - Green JS Overlay: Double-outlined Vertical Hexagon Gem (`⬡`)
  // - Purple QR Link: Crisp Circle Ring (`○`)
  // - Yellow/Red Note: Crisp Triangle (`△`)
  function buildSketchPinSvg(shape, hex, isSelected) {
    const fillAlpha = isSelected ? '38' : '14';
    if (shape === 'hex') {
      return `<svg class="nle-pop-svg" viewBox="0 0 28 28" style="color:${hex};">
        <polygon points="14,1.5 23.5,7.5 23.5,20.5 14,26.5 4.5,20.5 4.5,7.5" fill="${hex}${fillAlpha}" stroke="${hex}" stroke-width="2.2"/>
        <polygon points="14,5.2 20,9.3 20,18.7 14,22.8 8,18.7 8,9.3" fill="none" stroke="${hex}" stroke-width="1.4"/>
      </svg>`;
    }
    if (shape === 'circle') {
      return `<svg class="nle-pop-svg" viewBox="0 0 28 28" style="color:${hex};">
        <circle cx="14" cy="14" r="9.5" fill="${hex}${fillAlpha}" stroke="${hex}" stroke-width="2.4"/>
      </svg>`;
    }
    if (shape === 'tri') {
      return `<svg class="nle-pop-svg" viewBox="0 0 28 28" style="color:${hex};">
        <polygon points="14,3 25,24 3,24" fill="${hex}${fillAlpha}" stroke="${hex}" stroke-width="2.3"/>
      </svg>`;
    }
    // Default: Tall Cyan Diamond (`◇`)
    return `<svg class="nle-pop-svg" viewBox="0 0 28 28" style="color:${hex};">
      <polygon points="14,1.5 25.5,14 14,26.5 2.5,14" fill="${hex}${fillAlpha}" stroke="${hex}" stroke-width="2.4"/>
    </svg>`;
  }

  let cachedPopsicles = [];

  function renderTimelineViewportOnly() {
    if (!nleTimeline) return;
    syncTimelineViewportBounds();
    const tlStart = nleTimeline.start_frame || 0;
    const fps = nleTimeline.fps || 30;
    const vStart = tlViewStartF;
    const vDur = Math.max(1, tlViewDurF);
    const vSec = vDur / Math.max(1, fps);

    // 1. Tall (92%) & Medium (54%) Vertical Graduation Bars Rising from Bottom Baseline (like user's sketch!)
    const niceSteps = [2, 5, 10, 15, 30, 60, 120, 300];
    const majorSec = niceSteps.find((s) => vSec / s <= 9) || 300;
    const medSec = majorSec / 2;
    const minorSec = majorSec / 6;

    const firstRelSec = Math.max(0, Math.floor(((vStart - tlStart) / fps) / minorSec) * minorSec);
    const lastRelSec = ((vStart + vDur - tlStart) / fps) + minorSec;
    const gradParts = [];
    for (let s = firstRelSec; s <= lastRelSec; s += minorSec) {
      const f = tlStart + s * fps;
      const pct = ((f - vStart) / vDur) * 100;
      if (pct < -0.5 || pct > 100.5) continue;
      const remMaj = Math.abs(s % majorSec);
      const isMajor = remMaj < 0.04 || Math.abs(remMaj - majorSec) < 0.04;
      const remMed = Math.abs(s % medSec);
      const isMed = !isMajor && (remMed < 0.04 || Math.abs(remMed - medSec) < 0.04);
      const cls = isMajor ? 'major' : (isMed ? 'med' : 'minor');
      const mm = Math.floor(Math.round(s) / 60);
      const ss = Math.round(s) % 60;
      const lbl = isMajor ? `<span class="grad-lbl">${String(mm).padStart(2, '0')}:${String(ss).padStart(2, '0')}</span>` : '';
      gradParts.push(`<div class="grad-bar ${cls}" style="left:${pct}%;">${lbl}</div>`);
    }

    // 2. Geometric Stemmed Pins Rising from Bottom Baseline (`tier-tall` ◇ Cyan, `tier-mid` ⬡ Green & ○ Purple)
    const stageWidthPx = $('ov-nle-stage').clientWidth || 1200;
    const pinsHtml = cachedPopsicles.map((p, idx) => {
      const pct = ((p.frame - vStart) / vDur) * 100;
      if (pct < -2 || pct > 102) return '';
      const spanPx = p.durF > fps * 0.5 ? Math.max(10, Math.round((p.durF / vDur) * stageWidthPx)) : 0;
      const isSel = nleSelected && nleSelected.kind === p.kind && nleSelected.frame === p.frame;
      return `<div class="nle-pop ${p.tierCls} ${isSel ? 'selected' : ''}" data-pidx="${idx}" data-frame="${p.frame}" data-kind="${p.kind}" style="left:${pct}%;color:${p.hex};" title="${escHtml(p.name)} (${escHtml(p.yt)})">
        ${buildSketchPinSvg(p.shape, p.hex, isSel)}
        <span class="nle-pop-stem" style="background:${p.hex};"></span>
        ${spanPx ? `<span class="nle-pop-span" style="width:${spanPx}px;background:${p.hex};"></span>` : ''}
      </div>`;
    }).join('');

    $('ov-nle-markers').innerHTML = `<div class="grad-clip-box">${gradParts.join('')}</div>${pinsHtml}`;

    $('ov-nle-markers').querySelectorAll('.nle-pop').forEach((pin) => {
      const itemObj = cachedPopsicles[Number(pin.dataset.pidx)];
      pin.onmouseenter = () => openInteractivePopover((itemObj.frame - vStart) / vDur, itemObj, false);
      pin.onmouseleave = () => schedulePopClose(340);

      pin.onmousedown = (ev) => {
        ev.stopPropagation();
        const stageRect = $('ov-nle-stage').getBoundingClientRect();
        let moved = false;
        let targetFrame = itemObj.frame;

        const onMove = (me) => {
          moved = true;
          forceClosePopover();
          const pct = Math.max(0.005, Math.min(0.995, (me.clientX - stageRect.left) / stageRect.width));
          targetFrame = Math.round(vStart + pct * vDur);
          pin.style.left = `${pct * 100}%`;
          updateViewerAtFrame(targetFrame, `Move "${itemObj.name}"`);
        };
        const onUp = async () => {
          window.removeEventListener('mousemove', onMove);
          window.removeEventListener('mouseup', onUp);
          if (!moved) {
            openInteractivePopover((itemObj.frame - vStart) / vDur, itemObj, true);
            setPlayheadVisual(itemObj.frame);
            await api('/api/prompter/seek', { method: 'POST', body: { tc: itemObj.tc } });
            return;
          }
          if (Math.abs(targetFrame - itemObj.frame) >= 2) {
            $('ov-nle-status').textContent = 'Saving position…';
            if (itemObj.kind === 'overlay') {
              await api('/api/overlays/adjust', { method: 'POST', body: { action: 'move_overlay', item_id: itemObj.item_id, new_frame: targetFrame } });
            } else {
              await api('/api/overlays/adjust', {
                method: 'POST',
                body: { action: 'move_marker', old_frame: itemObj.frame, new_frame: targetFrame, duration: itemObj.durF || 1, color: itemObj.color || 'Cyan', name: itemObj.name, note: itemObj.note || '' },
              });
            }
            $('ov-nle-status').textContent = '✓ Moved';
            await loadTimelineStudio();
          }
        };
        window.addEventListener('mousemove', onMove);
        window.addEventListener('mouseup', onUp);
      };
    });

    // Update optional raw V1/V2/V3 tracks to match current zoom window too
    const vTracks = [...(nleTimeline.tracks || [])].sort((a, b) => b.track_index - a.track_index);
    $('ov-nle-tracks').innerHTML = vTracks.map((tr) => {
      const tidx = tr.track_index;
      const clipsHtml = (tr.items || []).map((it) => {
        const leftPct = ((it.record_start - vStart) / vDur) * 100;
        const widthPct = Math.max(0.4, ((it.record_end - it.record_start) / vDur) * 100);
        if (leftPct + widthPct < 0 || leftPct > 100) return '';
        const isOv = tidx >= 3 || (tidx >= 2 && it.duration < nleTimeline.duration_frames * 0.65);
        const cls = tidx === 1 ? 'v1' : (isOv ? 'ov' : 'pip');
        const cHex = CLIP_HEX[String(it.clip_color || '').toLowerCase()];
        const customBg = cHex ? `background:linear-gradient(180deg, ${cHex}dd, ${cHex}88);border-color:${cHex};` : '';
        return `<div class="nle-clip ${cls}" data-iid="${escHtml(it.item_id)}" data-tidx="${tidx}" data-cc="${escHtml(it.clip_color || '')}" data-name="${escHtml(it.name)}" style="left:${leftPct}%;width:${widthPct}%;${customBg}">${escHtml(it.name)}</div>`;
      }).join('');
      return `<div class="nle-track-row"><span class="nle-track-lbl">V${tidx} ${escHtml(tr.name || '')}</span>${clipsHtml}</div>`;
    }).join('');
    $('ov-nle-tracks').querySelectorAll('.nle-clip').forEach((el) => {
      el.onclick = (ev) => {
        ev.stopPropagation();
        setClipLabelTarget({ item_id: el.dataset.iid, track_index: Number(el.dataset.tidx), name: el.dataset.name, clip_color: el.dataset.cc || '' });
      };
    });

    setPlayheadVisual(nlePlayheadFrame);
  }

  function renderVisualTimeline(tl) {
    const prevTlName = nleTimeline ? nleTimeline.name : null;
    nleTimeline = tl;
    $('nle-tl-name').textContent = tl.connected && tl.name ? `Timeline: ${tl.name} (${tl.fps}fps)` : 'Timeline: offline';
    if (prevTlName !== tl.name) {
      tlZoom = 1.0;
      tlViewStartF = tl.start_frame || 0;
      tlViewDurF = Math.max(1, tl.duration_frames || 1);
    }

    const dur = Math.max(1, tl.duration_frames || 1);
    const startF = tl.start_frame || 0;
    const fps = tl.fps || 30;

    const pops = [];
    for (const m of (tl.markers || [])) {
      const cLow = String(m.color || 'Cyan').toLowerCase();
      const isQr = cLow === 'purple';
      const isOv = cLow === 'mint';
      const isNote = cLow === 'yellow' || cLow === 'red';
      pops.push({
        kind: 'marker',
        old_frame: m.frame,
        frame: m.frame,
        durF: Number(m.duration || 1),
        tc: m.tc,
        yt: framesToYtTs(m.frame, startF, fps),
        duration_sec: Math.max(0.5, Number(((m.duration || 1) / fps).toFixed(1))),
        color: m.color || 'Cyan',
        hex: isQr ? '#a855f7' : isOv ? '#10b981' : isNote ? '#f59e0b' : '#00e5ff',
        shape: isQr ? 'circle' : isOv ? 'hex' : isNote ? 'tri' : 'diamond',
        tierCls: (isQr || isOv || isNote) ? 'tier-mid' : 'tier-tall',
        glyph: isQr ? '○' : isOv ? '⬡' : isNote ? '△' : '◇',
        name: m.name || '',
        note: m.note || '',
      });
    }
    for (const o of (nleData && nleData.timeline_overlays ? nleData.timeline_overlays : [])) {
      if (o.duration > dur * 0.65) continue;
      pops.push({
        kind: 'overlay',
        item_id: o.item_id,
        track_index: o.track_index,
        frame: o.record_start,
        durF: o.duration,
        tc: o.record_start_tc,
        yt: framesToYtTs(o.record_start, startF, fps),
        duration_sec: Number((o.duration / fps).toFixed(1)),
        color: 'Mint',
        hex: '#10b981',
        shape: 'hex',
        tierCls: 'tier-mid',
        glyph: '⬡',
        name: o.name,
        note: '',
        html_source: o.html_source || null,
        preview_url: o.preview_url || null,
      });
    }
    pops.sort((a, b) => a.frame - b.frame);
    cachedPopsicles = pops;

    nlePlayheadFrame = tl.playhead_frame || tl.start_frame;
    renderTimelineViewportOnly();
    updateViewerAtFrame(nlePlayheadFrame);
  }

  $('btn-toggle-tracks').onclick = () => {
    const trEl = $('ov-nle-tracks');
    trEl.hidden = !trEl.hidden;
    $('btn-toggle-tracks').textContent = trEl.hidden ? '▾ Tracks' : '▴ Tracks';
  };

  $('btn-tl-zoom-reset').onclick = () => {
    if (!nleTimeline) return;
    tlZoom = 1.0;
    tlViewStartF = nleTimeline.start_frame || 0;
    tlViewDurF = Math.max(1, nleTimeline.duration_frames || 1);
    renderTimelineViewportOnly();
    renderLeftAccordion(false);
  };

  // Dragging / clicking the Pan Scrollbar Strip beneath the Timeline
  (() => {
    const track = $('ov-pan-track');
    track.addEventListener('mousedown', (ev) => {
      ev.stopPropagation();
      if (!nleTimeline || tlZoom <= 1.01) return;
      const fullDur = Math.max(1, nleTimeline.duration_frames);
      const fullStart = nleTimeline.start_frame || 0;
      const movePan = (me) => {
        const rect = track.getBoundingClientRect();
        const pct = Math.max(0, Math.min(1, (me.clientX - rect.left) / Math.max(1, rect.width)));
        tlViewStartF = Math.round(fullStart + pct * fullDur - tlViewDurF * 0.5);
        renderTimelineViewportOnly();
      };
      movePan(ev);
      const upPan = () => {
        window.removeEventListener('mousemove', movePan);
        window.removeEventListener('mouseup', upPan);
        renderLeftAccordion(false);
      };
      window.addEventListener('mousemove', movePan);
      window.addEventListener('mouseup', upPan);
    });
  })();

  // Ctrl/Alt + Wheel over Hierarchy Browser scales browser density; normal Wheel scrolls smoothly!
  $('nle-index-list').addEventListener('wheel', (ev) => {
    if (ev.ctrlKey || ev.altKey) {
      ev.preventDefault();
      hierScale = Math.max(0.8, Math.min(1.35, hierScale * (ev.deltaY < 0 ? 1.08 : 0.92)));
      $('nle-index-list').style.zoom = String(hierScale);
    }
  }, { passive: false });

  // Mouse-Wheel Zoom + Shift-Wheel Pan + Continuous Closest-Item Hover Sync on Timeline!
  (() => {
    const stage = $('ov-nle-stage');
    const hLine = $('ov-hover-line');
    const hPill = $('ov-hover-pill');
    let scrubbing = false;

    const posAtEvent = (ev) => {
      const rect = stage.getBoundingClientRect();
      const pct = Math.max(0, Math.min(1, (ev.clientX - rect.left) / Math.max(1, rect.width)));
      const frame = nleTimeline ? Math.round(tlViewStartF + pct * tlViewDurF) : 108000;
      return { pct, frame };
    };

    stage.addEventListener('wheel', (ev) => {
      if (!nleTimeline) return;
      if (popRoot.contains(ev.target)) return;
      ev.preventDefault();
      forceClosePopover();

      // Horizontal trackpad pan OR Shift+Wheel pan!
      if (ev.shiftKey || Math.abs(ev.deltaX) > Math.abs(ev.deltaY)) {
        const delta = Math.abs(ev.deltaX) > Math.abs(ev.deltaY) ? ev.deltaX : ev.deltaY;
        tlViewStartF += Math.round((delta / 450) * tlViewDurF);
        renderTimelineViewportOnly();
        renderLeftAccordion(false);
        return;
      }

      // Mouse Wheel Up/Down = Cursor-Anchored Zoom In / Out!
      const { pct, frame: anchorFrame } = posAtEvent(ev);
      const factor = ev.deltaY < 0 ? 1.25 : (1 / 1.25);
      tlZoom = Math.max(1.0, Math.min(24.0, tlZoom * factor));
      const fullDur = Math.max(1, nleTimeline.duration_frames);
      const newDur = Math.max(Math.round((nleTimeline.fps || 30) * 4), Math.round(fullDur / tlZoom));
      tlViewDurF = newDur;
      tlViewStartF = Math.round(anchorFrame - pct * newDur);
      renderTimelineViewportOnly();
      renderLeftAccordion(false);
    }, { passive: false });

    stage.addEventListener('mousemove', (ev) => {
      if (!nleTimeline) return;
      if (popRoot.contains(ev.target)) return;
      const { pct, frame } = posAtEvent(ev);
      nleHoverFrame = frame;
      hLine.hidden = false;
      hLine.style.left = `${pct * 100}%`;
      hPill.hidden = false;
      hPill.style.left = `${Math.max(4, Math.min(96, pct * 100))}%`;
      hPill.textContent = `${framesToYtTs(frame, nleTimeline.start_frame, nleTimeline.fps)} · ${framesToSmpte(frame, nleTimeline.fps)}`;

      if (!popPinned) {
        updateViewerAtFrame(frame, scrubbing ? 'Scrubbing' : 'Hover');
        if (!isEditingLeftBrowser()) {
          const closest = findClosestTimelineItem(frame);
          if (closest) selectTimelineItem(closest, true);
        }
      }
      if (scrubbing) setPlayheadVisual(frame);
    });

    stage.addEventListener('mouseleave', () => {
      nleHoverFrame = null;
      hLine.hidden = true;
      hPill.hidden = true;
      schedulePopClose(320);
      if (!scrubbing && nleTimeline && popRoot.hidden) updateViewerAtFrame(nlePlayheadFrame);
    });

    stage.addEventListener('mousedown', (ev) => {
      if (!nleTimeline) return;
      if (popRoot.contains(ev.target) || ev.target.closest('.nle-pop') || ev.target.closest('#ov-pan-track')) return;
      forceClosePopover();
      scrubbing = true;
      const { frame } = posAtEvent(ev);
      setPlayheadVisual(frame);
      updateViewerAtFrame(frame, 'Scrubbing');
      const onUp = async (ue) => {
        window.removeEventListener('mouseup', onUp);
        scrubbing = false;
        const fin = posAtEvent(ue);
        const tc = framesToSmpte(fin.frame, nleTimeline.fps);
        setPlayheadVisual(fin.frame);
        await api('/api/prompter/seek', { method: 'POST', body: { tc } });
        updateViewerAtFrame(fin.frame, 'Playhead');
        updateAiBarForSelection();
      };
      window.addEventListener('mouseup', onUp);
    });
  })();

  async function loadTimelineStudio() {
    nleData = await api('/api/overlays');
    if (nleData.timeline) renderVisualTimeline(nleData.timeline);
    renderLeftAccordion(false);
    if (!nleSelected && nleCombinedItems.length) {
      selectTimelineItem(nleCombinedItems[0], false);
    } else {
      updateAiBarForSelection();
      pushSandboxPreview();
    }
    fitOverlayIframe();
  }

  $('nle-filter-bar').querySelectorAll('.pill').forEach((b) => {
    b.onclick = () => {
      $('nle-filter-bar').querySelectorAll('.pill').forEach((p) => p.classList.remove('active'));
      b.classList.add('active');
      nleFilter = b.dataset.flt;
      renderLeftAccordion(false);
    };
  });

  const stepPlayheadFrames = async (deltaF) => {
    if (!nleTimeline) return;
    const nextF = Math.max(nleTimeline.start_frame, Math.min(nleTimeline.end_frame, nlePlayheadFrame + deltaF));
    const tc = framesToSmpte(nextF, nleTimeline.fps);
    setPlayheadVisual(nextF);
    updateViewerAtFrame(nextF);
    await api('/api/prompter/seek', { method: 'POST', body: { tc } });
    updateAiBarForSelection();
  };
  $('btn-nle-back').onclick = () => stepPlayheadFrames(-Math.round(nleTimeline ? nleTimeline.fps : 30));
  $('btn-nle-fwd').onclick = () => stepPlayheadFrames(Math.round(nleTimeline ? nleTimeline.fps : 30));
  $('btn-nle-prev').onclick = () => {
    if (!nleTimeline) return;
    const prev = [...(nleTimeline.markers || [])].reverse().find((m) => m.frame < nlePlayheadFrame - 2);
    if (prev) stepPlayheadFrames(prev.frame - nlePlayheadFrame);
  };
  $('btn-nle-next').onclick = () => {
    if (!nleTimeline) return;
    const nxt = (nleTimeline.markers || []).find((m) => m.frame > nlePlayheadFrame + 2);
    if (nxt) stepPlayheadFrames(nxt.frame - nlePlayheadFrame);
  };

  $('chk-nle-follow').onchange = () => {
    stopFollowPoll();
    if ($('chk-nle-follow').checked) {
      followTimer = setInterval(async () => {
        if ($('view-timeline').hidden || !$('chk-nle-follow').checked) return;
        try {
          const ph = await api('/api/playhead');
          if (ph.connected && ph.playhead_frame !== undefined) {
            setPlayheadVisual(ph.playhead_frame);
            updateViewerAtFrame(ph.playhead_frame, 'Live Follow');
          }
        } catch { /* ignore */ }
      }, 250);
    }
  };

  // Drop Flag at HOVER location (if mouse is over timeline) or Playhead!
  async function dropFlagAtTarget(color, defaultName, durSec, defaultNote = '') {
    const fps = nleTimeline ? nleTimeline.fps : 30;
    const targetF = nleHoverFrame !== null ? nleHoverFrame : nlePlayheadFrame;
    const tc = framesToSmpte(targetF, fps);
    const durFrames = Math.max(1, Math.round(durSec * fps));
    $('ov-nle-status').textContent = `Dropping ${color} flag @ ${tc}…`;
    await api('/api/overlays/adjust', {
      method: 'POST',
      body: { action: 'add_marker', new_tc: tc, duration: durFrames, color, name: defaultName, note: defaultNote },
    });
    setPlayheadVisual(targetF);
    await loadTimelineStudio();
    selectTimelineItem({
      kind: 'marker',
      old_frame: targetF,
      frame: targetF,
      tc,
      yt: framesToYtTs(targetF, nleTimeline ? nleTimeline.start_frame : 0, fps),
      duration_sec: durSec,
      color,
      hex: COLOR_HEX[color.toLowerCase()] || '#00b4d8',
      name: defaultName,
      note: defaultNote,
    }, true);
    $('ov-nle-status').textContent = `✓ Flag @ ${tc}`;
    const nameInput = document.querySelector('#acc-active-card .js-acc-name');
    if (nameInput) { nameInput.focus(); nameInput.select(); }
  }

  $('btn-add-vo').onclick = () => dropFlagAtTarget('Cyan', 'New Chapter', 1.0, '');
  $('btn-add-qr').onclick = () => dropFlagAtTarget('Purple', 'Explore Code & Demo', 6.5, 'https://github.com/');
  $('btn-add-ov').onclick = () => dropFlagAtTarget('Mint', 'callout_card', 5.5, '');
  $('btn-add-note').onclick = () => dropFlagAtTarget('Yellow', 'Review Note', 1.0, '');

  const openClipLabelAtTarget = () => {
    if (!nleTimeline) return;
    const targetF = nleHoverFrame !== null ? nleHoverFrame : nlePlayheadFrame;
    let hit = null;
    for (const tr of (nleTimeline.tracks || [])) {
      const found = (tr.items || []).find((it) => targetF >= it.record_start && targetF < it.record_end);
      if (found) hit = { item_id: found.item_id, track_index: tr.track_index, name: found.name, clip_color: found.clip_color || '' };
    }
    if (hit) setClipLabelTarget(hit);
  };
  $('btn-label-clip').onclick = openClipLabelAtTarget;

  // Hover-Location Hotkeys: M (VO/Chapter), Q (QR), O (JS Overlay), N (Note), L (Clip Label)
  document.addEventListener('keydown', (ev) => {
    if ($('view-timeline').hidden || ev.ctrlKey || ev.metaKey || ev.altKey) return;
    const tag = (ev.target && ev.target.tagName) || '';
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
    const k = ev.key.toLowerCase();
    if (k === 'm') { ev.preventDefault(); dropFlagAtTarget('Cyan', 'New Chapter', 1.0, ''); }
    else if (k === 'q') { ev.preventDefault(); dropFlagAtTarget('Purple', 'Explore Code & Demo', 6.5, 'https://github.com/'); }
    else if (k === 'o') { ev.preventDefault(); dropFlagAtTarget('Mint', 'callout_card', 5.5, ''); }
    else if (k === 'n') { ev.preventDefault(); dropFlagAtTarget('Yellow', 'Review Note', 1.0, ''); }
    else if (k === 'l') { ev.preventDefault(); openClipLabelAtTarget(); }
  });

  $('btn-ov-live-comp').onclick = () => updateViewerAtFrame(nlePlayheadFrame, 'Resolve Composite', true);
  $('btn-ov-refresh').onclick = () => loadTimelineStudio();
  $('chk-show-vo-hud').onchange = () => updateViewerAtFrame(nlePlayheadFrame);
  $('chk-ov-live-canvas').onchange = () => pushSandboxPreview();
  $('rng-ov-scrub').oninput = () => pushSandboxPreview();

  async function loadDocsTab() {
    const r = await api('/api/docs');
    const docsCache = r.docs || [];
    const nav = $('docs-nav');
    const body = $('docs-body');
    if (!docsCache.length) {
      body.innerHTML = '<p class="muted">No documentation files found.</p>';
      return;
    }
    const selectDoc = (idx) => {
      nav.querySelectorAll('.doc-nav-btn').forEach((b, bi) => b.classList.toggle('active', bi === idx));
      body.innerHTML = renderMarkdown(docsCache[idx].markdown);
      body.scrollTop = 0;
    };
    nav.innerHTML = docsCache.map((d, idx) => `<button class="doc-nav-btn" data-didx="${idx}">${escHtml(d.title)}</button>`).join('');
    nav.querySelectorAll('.doc-nav-btn').forEach((b) => { b.onclick = () => selectDoc(Number(b.dataset.didx)); });
    selectDoc(0);
  }

  if (token) {
    boot().catch(() => { $('gate').hidden = false; });
  }
})();