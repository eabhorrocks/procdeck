'use strict';
// ---------------------------------------------------------------- basics
const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const GB = 1024 ** 3;
const gb = b => ((b || 0) / GB).toFixed(1) + ' GB';
const clamp = (x, a, b) => Math.max(a, Math.min(b, x));
function dur(s) { if (s == null || isNaN(s)) return '-'; s = Math.max(0, Math.round(s)); if (s < 60) return s + 's';
  if (s < 3600) return Math.floor(s / 60) + 'm' + String(s % 60).padStart(2, '0') + 's';
  return Math.floor(s / 3600) + 'h' + String(Math.floor(s % 3600 / 60)).padStart(2, '0') + 'm'; }
function hm(ts) { if (!ts) return '-'; const d = new Date(ts * 1000), n = new Date();
  const t = d.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'});
  return d.toDateString() === n.toDateString() ? t : d.toLocaleDateString([], {weekday: 'short', day: 'numeric', month: 'short'}) + ' ' + t; }
const ls = { get(k, d) { try { return JSON.parse(localStorage.getItem(k)) ?? d; } catch { return d; } },
             set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* ignore */ } } };
const PRI = {idle: 'Idle', below_normal: 'Below normal', normal: 'Normal', above_normal: 'Above normal', high: 'High', realtime: 'Realtime'};
const FLAG = {idle: ['warn', 'idle hog'], orphan: ['bad', 'parent exited'], hung: ['bad', 'not responding'], suspended: ['warn', 'suspended']};

// token: from ?token=… once, then remembered
const urlTok = new URLSearchParams(location.search).get('token');
if (urlTok) { ls.set('procdeck_token', urlTok); history.replaceState(null, '', location.pathname); }
const TOKEN = urlTok || ls.get('procdeck_token', '');

async function api(method, path, body) {
  const r = await fetch(path, {method, headers: {'x-procdeck-token': TOKEN, 'content-type': 'application/json'},
                               body: body === undefined ? undefined : JSON.stringify(body)});
  const data = await r.json().catch(() => ({}));
  if (!r.ok) { const e = new Error(data.detail || data.errors?.join('\n') || r.statusText); e.status = r.status; throw e; }
  return data;
}
let toastT;
function toast(msg) { const t = $('toast'); t.textContent = msg; t.classList.add('on'); clearTimeout(toastT); toastT = setTimeout(() => t.classList.remove('on'), 3500); }

// Live tables re-render every second, which swallows normal clicks; act on pointerdown instead.
function onPress(el, fn) { el.addEventListener('pointerdown', e => { if (e.button === 0) fn(e); }); }

// ---------------------------------------------------------------- state + polling
let S = null, tab = ls.get('procdeck_tab', 'runs'), selRun = ls.get('procdeck_run', null), runDetail = null;
let selLog = null, logText = '', drawerPid = null, drawerGid = null;
let dragKey = null, dropAt = null;        // step being dragged / {key, below} the row it would land next to
const selSteps = new Set();               // waiting steps ticked for "run in parallel"
const expanded = new Set();
let procView = 'all', procSort = 'rss';

async function poll() {
  try {
    S = await api('GET', '/api/state');
    $('liveDot').classList.remove('down');
    render();
  } catch (e) {
    $('liveDot').classList.add('down');
    $('hdaemon').textContent = e.status === 403 || !TOKEN ? 'access token missing or out of date: reopen with `procdeck open`'
      : 'daemon not reachable: run `procdeck start`';
  }
}
setInterval(poll, 1000);
setInterval(() => { if (tab === 'runs' && selRun) loadRun(); }, 2000);

function render() {
  renderHeader(); renderBanners();
  if (tab === 'runs') renderRuns();
  if (tab === 'procs') renderProcs();
  if (tab === 'system') renderSystem();
  if (drawerGid) updateDrawerLive();
}

// ---------------------------------------------------------------- header + banners
function spark(arr, w, h, color, max = 100) {
  if (!arr.length) return '';
  const pts = arr.map((v, i) => `${(i / Math.max(1, arr.length - 1) * w).toFixed(1)},${(h - 1 - clamp(v / max, 0, 1) * (h - 2)).toFixed(1)}`).join(' ');
  return `<polyline fill="none" stroke="${color}" stroke-width="1.3" points="${pts}"/>`;
}
function renderHeader() {
  const sy = S.system; if (!sy.ram_total) return;
  $('hcpu').textContent = sy.cpu.toFixed(0) + '%';
  $('hcpuSpark').innerHTML = spark(S.sys_hist.slice(-80).map(x => x[1]), 80, 22, 'var(--accent)');
  $('hram').textContent = `${(sy.ram_used / GB).toFixed(1)} / ${(sy.ram_total / GB).toFixed(1)} GB`;
  const rb = $('hramBar'); rb.firstElementChild.style.width = sy.ram_pct + '%';
  rb.className = 'bar ' + (S.alert.level === 'critical' ? 'bad' : S.alert.level === 'warn' ? 'warn' : '');
  $('hcommit').textContent = `${sy.commit_pct.toFixed(0)}%`;
  $('hcommit').title = `${gb(sy.commit_total)} of ${gb(sy.commit_limit)} (RAM + pagefile)`;
  const d = S.daemon;
  $('hdaemon').textContent = `daemon ${d.cpu.toFixed(1)}% · ${d.tick_ms.toFixed(0)} ms/tick` + (d.error ? ' · ERROR' : '');
  $('hdaemon').title = d.error || '';
}
function renderBanners() {
  const a = S.alert, ad = S.admission || {}, out = [];
  if (a.level && a.level !== 'ok') {
    const cls = a.level === 'critical' ? 'critical' : 'warn', b = cls === 'critical' ? 'c' : 'w';
    let txt = `<b class="${b}">${a.level === 'critical' ? '⛔' : '⚠'} RAM ${a.ram_pct.toFixed(0)}%</b>`;
    if (a.eta_s != null) txt += ` <span>At the current rate, full in <b class="${b}">~${Math.max(1, Math.round(a.eta_s / 60))} min</b>.</span>`;
    if (a.commit_pct > 85) txt += ` <span>Commit ${a.commit_pct.toFixed(0)}%.</span>`;
    if (a.grower) txt += ` <span>Fastest growing: <b>${esc(a.grower.label)}</b> <span class="num">+${a.grower.mb_per_min.toFixed(0)} MB/min</span>.</span>`;
    if (ad.alert_paused) txt += ` <span class="muted">New procdeck steps are held until RAM recovers.</span>`;
    out.push(`<div class="banner ${cls}">${txt}<div class="acts">${a.grower ? `<button data-steer="${a.grower.pid}">Steer grower</button>` : ''}</div></div>`);
  }
  if (ad.paused) out.push(`<div class="banner info"><b>Admission paused</b><span>No queued steps will start.</span><div class="acts"><button data-adm="resume">Resume admission</button></div></div>`);
  const html = out.join('');
  if ($('banners').innerHTML !== html) $('banners').innerHTML = html;
}
onPress($('banners'), async e => {
  const s = e.target.closest('[data-steer]'); if (s) { const g = S.groups.find(x => x.pid == s.dataset.steer); if (g) openDrawer(g.id); }
  const adm = e.target.closest('[data-adm]'); if (adm) { await api('POST', '/api/admission', {paused: false}); toast('Admission resumed'); }
});

// ---------------------------------------------------------------- runs
const STATE_COLOR = {done: 'var(--ok)', running: 'var(--run)', failed: 'var(--bad)', skipped: 'var(--queued)', cancelled: 'var(--queued)'};
function renderRuns() {
  const ad = S.admission || {}, runs = S.runs || [];
  const waiting = runs.filter(r => ['queued', 'running'].includes(r.state)).reduce((n, r) => n + (r.counts.waiting || 0), 0);
  $('kpis').innerHTML = `
    <div class="kpi"><div class="lbl">Running steps</div><div class="v num">${ad.running ?? 0}</div><div class="s">${runs.filter(r => r.state === 'running').length} active run(s)</div></div>
    <div class="kpi"><div class="lbl">Waiting steps</div><div class="v num">${waiting}</div><div class="s">${ad.paused || ad.alert_paused ? 'admission paused' : 'admission open'}</div></div>
    <div class="kpi"><div class="lbl">Free RAM for new steps</div><div class="v num">${(ad.free_gb ?? 0).toFixed(1)} GB</div><div class="s">after ${ad.headroom_gb ?? '-'} GB headroom${ad.outstanding_gb > 0.05 ? ` and ${ad.outstanding_gb.toFixed(1)} GB reserved for running steps` : ''}</div></div>
    <div class="kpi"><div class="lbl">Threads committed</div><div class="v num">${ad.threads_running ?? 0} / ${ad.threads_budget ?? '-'}</div><div class="s">${S.system.ncpu} logical CPUs</div></div>`;
  $('runsCount').textContent = runs.length ? `${runs.length} recent` : '';
  const finishedN = runs.filter(r => ['done', 'failed', 'cancelled'].includes(r.state)).length;
  $('clearRuns').style.display = finishedN ? '' : 'none';
  $('clearRuns').textContent = `Clear ${finishedN} finished`;
  if (!runs.length) {
    $('runList').innerHTML = `<div class="empty">No runs to show.<br><br>Ask Claude to set up an analysis run, or<br><code>procdeck submit jobs/my_run.yaml</code><br><br><span class="muted">Cleared runs: <code>procdeck status --all</code></span></div>`;
  } else {
    if (!selRun || !runs.some(r => r.id === selRun)) { selRun = runs[0].id; ls.set('procdeck_run', selRun); loadRun(); }
    $('runList').innerHTML = runs.map(r => {
      const c = r.counts, n = r.n_steps || 1;
      const bar = ['done', 'running', 'failed', 'skipped', 'cancelled'].map(k => c[k] ? `<i style="width:${c[k] / n * 100}%;background:${STATE_COLOR[k]}"></i>` : '').join('');
      const when = ['queued', 'running'].includes(r.state) ? (r.eta ? `ETA ${hm(r.eta)}` : '') : (r.finished_at ? `finished ${hm(r.finished_at)}` : '');
      return `<div class="row ${r.id === selRun ? 'sel' : ''}" data-run="${esc(r.id)}">
        <div class="t"><b class="ellip">${esc(r.name)}</b><span class="pill ${r.state}">${r.state}${r.paused ? ' · paused' : ''}</span></div>
        <div class="m">${c.done || 0}/${r.n_steps} done${c.running ? ` · ${c.running} running` : ''}${c.failed ? ` · ${c.failed} failed` : ''} · ${hm(r.submitted_at)}${r.submitted_by ? ' by ' + esc(r.submitted_by) : ''} ${when ? '· ' + when : ''}</div>
        <div class="progress">${bar}</div></div>`;
    }).join('');
  }
}
onPress($('runList'), e => { const row = e.target.closest('[data-run]'); if (!row) return;
  selRun = row.dataset.run; ls.set('procdeck_run', selRun); selLog = null; logText = ''; runDetail = null; renderRuns(); loadRun(); });
function afterForget(msg) { toast(msg); selRun = null; selLog = null; logText = ''; runDetail = null; $('runDetail').innerHTML = ''; setTimeout(poll, 300); }
$('clearRuns').addEventListener('click', async () => {
  const fin = (S?.runs || []).filter(r => ['done', 'failed', 'cancelled'].includes(r.state));
  if (!fin.length || !confirm(`Remove ${fin.length} finished run(s) (done, failed and cancelled) from the list?\n\n` +
      `Logs and timing history are kept; \`procdeck status --all\` still shows them.`)) return;
  try { const r = await api('POST', '/api/runs/forget', {states: ['done', 'failed', 'cancelled']}); afterForget(`${r.runs.length} run(s) cleared`); }
  catch (err) { toast('Error: ' + err.message); }
});

async function loadRun() {
  if (!selRun) { $('runDetail').innerHTML = ''; return; }
  try { runDetail = await api('GET', '/api/runs/' + encodeURIComponent(selRun)); } catch { return; }
  if (selLog) { try { logText = (await api('GET', `/api/runs/${encodeURIComponent(selRun)}/log?key=${encodeURIComponent(selLog)}&lines=300`)).text; } catch { /* keep old */ } }
  renderRunDetail();
}

function niceStep(span) { const c = [60, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400]; return c.find(x => span / x <= 8) || 86400; }
function renderRunDetail() {
  const r = runDetail; if (!r || dragKey) return;   // a re-render would cancel an in-progress drag
  const now = Date.now() / 1000, steps = r.steps;
  let t0 = Math.min(...steps.map(s => s.started_at || s.proj_start || now), r.submitted_at);
  let t1 = Math.max(...steps.map(s => s.ended_at || s.proj_end || now), now);
  if (t1 - t0 < 300) t1 = t0 + 300;
  const pad = (t1 - t0) * 0.03; t0 -= pad; t1 += pad;
  const pct = t => ((t - t0) / (t1 - t0) * 100).toFixed(2);
  const st = niceStep(t1 - t0); let ticks = '';
  for (let t = Math.ceil(t0 / st) * st; t < t1; t += st) ticks += `<span style="left:${pct(t)}%">${hm(t)}</span>`;
  let gantt = `<div></div><div class="axis">${ticks}</div>`;
  for (const s of steps) {
    let seg = '';
    if (s.state === 'waiting' && s.started_at && s.ended_at) {
      // previous attempt failed; it is queued for a retry
      seg += `<div class="seg failed" style="left:${pct(s.started_at)}%;width:${Math.max(0.3, pct(s.ended_at) - pct(s.started_at))}%" title="attempt ${s.attempt} failed: ${esc(s.error || '')}"></div>`;
      if (s.proj_start) seg += `<div class="seg proj" style="left:${pct(Math.max(s.proj_start, s.ended_at))}%;width:${Math.max(0.5, pct(s.proj_end) - pct(Math.max(s.proj_start, s.ended_at)))}%" title="retry projected ${hm(s.proj_start)}">retry</div>`;
    } else if (s.started_at) {
      const end = s.ended_at || now, cls = s.state === 'running' ? 'running' : s.state;
      seg += `<div class="seg ${cls}" style="left:${pct(s.started_at)}%;width:${Math.max(0.3, pct(end) - pct(s.started_at))}%" title="${esc(s.key)}: ${dur(end - s.started_at)}">${dur(end - s.started_at)}</div>`;
      if (s.state === 'running' && s.proj_end && s.proj_end > now) seg += `<div class="seg proj" style="left:${pct(now)}%;width:${pct(s.proj_end) - pct(now)}%" title="projected end ~${hm(s.proj_end)}"></div>`;
    } else if (s.proj_start && !['cancelled', 'skipped', 'failed'].includes(s.state)) {
      seg += `<div class="seg proj" style="left:${pct(s.proj_start)}%;width:${Math.max(0.5, pct(s.proj_end) - pct(s.proj_start))}%" title="projected ${hm(s.proj_start)}–${hm(s.proj_end)}">${esc(s.held_reason || '')}</div>`;
    }
    gantt += `<div class="lab" title="${esc(s.key)}">${esc(s.key)}</div><div class="track">${seg}${now > t0 && now < t1 ? `<div class="now" style="left:${pct(now)}%"></div>` : ''}</div>`;
  }
  const active = ['queued', 'running'].includes(r.state);
  const stateOf = Object.fromEntries(steps.map(s => [s.key, s.state]));
  for (const k of [...selSteps]) if (stateOf[k] !== 'waiting') selSteps.delete(k);
  const rows = steps.map(s => {
    const el = s.started_at ? (s.ended_at || now) - s.started_at : null;
    const canEdit = active && s.state === 'waiting';
    const open = canEdit ? s.needs.filter(n => stateOf[n] && stateOf[n] !== 'done') : [];
    const chips = open.slice(0, 3).map(n => `<span class="chip" title="${esc(n)}">${esc(n)}</span>`).join('') + (open.length > 3 ? ` <span class="muted">+${open.length - 3}</span>` : '');
    const waits = !canEdit ? '' : open.length ? `<span class="muted">after</span> ${chips}` : '<span class="muted">starts as soon as resources allow</span>';
    const lead = canEdit ? `<td class="grip" title="drag to reorder"><span class="chk ${selSteps.has(s.key) ? 'on' : ''}" data-sel="${esc(s.key)}" title="select, then “Run selected in parallel”"></span><span class="gripi">⠿</span></td>` : '<td class="grip"></td>';
    const wacts = canEdit ? ` <button class="sm" data-after="${esc(s.key)}" title="choose what this step waits for">after…</button>${open.length ? ` <button class="sm" data-release="${esc(s.key)}" title="stop waiting for anything: run as soon as resources allow">∥</button>` : ''}` : '';
    const g = s.state === 'running' ? S?.groups.find(x => x.kind === 'managed' && x.pid === s.pid) : null;
    const info = s.error || s.held_reason || '';
    const pill = s.state === 'waiting' && s.held_reason && /RAM|budget|paused|max_parallel/.test(s.held_reason) ? 'held' : s.state;
    const acts = [`<button class="sm" data-log="${esc(s.key)}">log</button>`];
    if (s.state === 'running') { acts.push(`<button class="sm" data-steer-pid="${s.pid}">steer</button>`); acts.push(`<button class="sm danger" data-step-cancel="${esc(s.key)}">kill</button>`); }
    if (['failed', 'cancelled', 'skipped'].includes(s.state)) acts.push(`<button class="sm" data-step-retry="${esc(s.key)}">retry</button>`);
    if (s.state === 'waiting') acts.push(`<button class="sm" data-step-cancel="${esc(s.key)}">skip</button>`);
    const hist = s.history ? `typical ${dur(s.history.median_s)}, peak ${s.history.peak_gb.toFixed(1)} GB (${s.history.n} runs)` : 'no history yet';
    return `<tr class="${selLog === s.key ? 'sel' : ''}"${canEdit ? ` draggable="true" data-wkey="${esc(s.key)}"` : ''}>${lead}<td title="${esc(s.cmd)}">${esc(s.key)}</td><td><span class="pill ${pill}">${s.state}${s.attempt > 1 ? ' · try ' + s.attempt : ''}</span></td>
      <td class="r num">${dur(el)}</td><td class="r num">${g ? g.cpu.toFixed(0) + '%' : '-'}</td>
      <td class="r num" title="${esc(hist)}">${g ? gb(g.rss) : s.peak_rss ? gb(s.peak_rss) : '-'} <span class="muted">/ ${s.mem_gb != null ? s.mem_gb.toFixed(1) : '-'}</span></td>
      <td>${esc(s.profile)}</td><td class="waits">${waits}${wacts}</td><td class="wrap muted" style="font-size:12px">${esc(info)}</td><td style="white-space:nowrap">${acts.join(' ')}</td></tr>`;
  }).join('');
  const nWaiting = steps.filter(s => s.state === 'waiting').length, nSel = selSteps.size;
  const queuebar = active && nWaiting ? `<div class="queuebar"><span class="muted">Drag <b>⠿</b> to reorder waiting steps · tick steps to run them together</span>
      <button class="sm" data-par-sel ${nSel ? '' : 'disabled'}>${nSel > 1 ? `Run ${nSel} selected in parallel` : nSel === 1 ? 'Start selected without waiting' : 'Run selected in parallel'}</button>
      <span style="margin-left:auto" class="muted">Max at once: <b class="num">${r.max_parallel || 'no limit'}</b> <button class="sm" data-maxpar title="how many steps of this run may run at the same time (RAM and CPU budgets still apply)">change</button></span></div>` : '';
  const failedN = steps.filter(s => ['failed', 'cancelled', 'skipped'].includes(s.state)).length;
  const html = `<div class="card">
    <div class="hd"><h2>${esc(r.name)}</h2><span class="pill ${r.state}">${r.state}${r.paused ? ' · paused' : ''}</span>
      <span class="muted" style="font-size:12px">${esc(r.id)} · ${r.spec_path ? esc(r.spec_path) + ' · ' : ''}submitted ${hm(r.submitted_at)}${r.submitted_by ? ' by ' + esc(r.submitted_by) : ''} · priority ${r.priority}${r.max_parallel ? ' · max ' + r.max_parallel + ' at once' : ''}${r.eta && active ? ' · ETA ' + hm(r.eta) : ''}</span>
      <div style="margin-left:auto" class="btnrow">
        ${active ? `<button data-run-act="${r.paused ? 'resume' : 'pause'}">${r.paused ? 'Resume' : 'Pause'}</button>` : ''}
        ${active ? `<button data-run-act="prio-up" title="higher priority runs get free slots first">Priority ↑</button><button data-run-act="prio-down">↓</button>` : ''}
        ${failedN ? `<button data-run-act="retry">Retry ${failedN} failed</button>` : ''}
        ${active ? `<button class="danger" data-run-act="cancel">Cancel run</button>` : ''}
        ${!active && !r.hidden ? `<button data-run-forget title="hide this run from the list (logs and timing history are kept)">Remove from list</button>` : ''}
      </div></div>
    ${r.description ? `<div class="bd muted" style="border-bottom:1px solid var(--line)">${esc(r.description)}</div>` : ''}
    <div class="bd"><div class="gantt">${gantt}</div>
      <div class="legend"><span><i style="background:var(--ok)"></i>done</span><span><i style="background:var(--run)"></i>running</span><span><i style="background:var(--bad)"></i>failed</span>
      <span><i style="background:repeating-linear-gradient(45deg,var(--queued) 0 3px,transparent 3px 5px);border:1px dashed var(--queued)"></i>projected (past runs, or 10 min if unknown)</span><span><i style="background:var(--bad);width:2px"></i>now</span></div></div>
    ${queuebar}
    <div class="tablewrap" style="border-top:1px solid var(--line)"><table id="stepTable"><thead><tr><th></th><th>Step</th><th>State</th><th class="r">Time</th><th class="r">CPU</th><th class="r">RAM / est</th><th>Profile</th><th>Waits for</th><th>Info</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>
  </div>
  ${selLog ? `<div class="card"><div class="hd"><h2>Log · ${esc(selLog)}</h2><button class="sm" data-log-close style="margin-left:auto">close</button></div><div class="bd"><pre class="log" id="logPre">${esc(logText) || '<span class="muted">(empty)</span>'}</pre></div></div>` : ''}
  <div class="card"><div class="hd"><h2>Spec</h2></div><div class="bd"><pre class="spec">${esc(r.spec_text || '')}</pre></div></div>`;
  const pre = $('logPre'), atBottom = pre ? pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 20 : true;
  // innerHTML below recreates every scrollable box, which would snap them back to the top on each refresh;
  // remember their scroll offsets (spec, log, step table) and put them back afterwards
  const SCROLLERS = ['pre.spec', 'pre.log', '.tablewrap'];
  const saved = SCROLLERS.map(sel => { const el = $('runDetail').querySelector(sel); return el ? [sel, el.scrollTop, el.scrollLeft] : null; }).filter(Boolean);
  $('runDetail').innerHTML = html;
  for (const [sel, top, left] of saved) { const el = $('runDetail').querySelector(sel); if (el) { el.scrollTop = top; el.scrollLeft = left; } }
  const npre = $('logPre'); if (npre && atBottom) npre.scrollTop = npre.scrollHeight;
}
const runAction = (body) => api('POST', `/api/runs/${encodeURIComponent(runDetail.id)}/action`, body);
const adjusted = r => { const n = Object.keys(r.needs_changed || {}).length; return n ? ` (${n} dependenc${n > 1 ? 'ies' : 'y'} rewired)` : ''; };

// ---- drag to reorder waiting steps (native HTML5 drag and drop; the live re-render is paused while dragging)
function clearDrop() { document.querySelectorAll('#stepTable tr.drop-above, #stepTable tr.drop-below, #stepTable tr.dragging')
  .forEach(x => x.classList.remove('drop-above', 'drop-below', 'dragging')); }
$('runDetail').addEventListener('dragstart', e => {
  const tr = e.target.closest?.('tr[data-wkey]'); if (!tr) return;
  dragKey = tr.dataset.wkey; dropAt = null; tr.classList.add('dragging');
  e.dataTransfer.effectAllowed = 'move'; e.dataTransfer.setData('text/plain', dragKey);
});
$('runDetail').addEventListener('dragover', e => {
  if (!dragKey) return;
  const tr = e.target.closest?.('tr[data-wkey]'); if (!tr) return;
  e.preventDefault(); e.dataTransfer.dropEffect = 'move';
  const rect = tr.getBoundingClientRect(), below = e.clientY > rect.top + rect.height / 2;
  dropAt = {key: tr.dataset.wkey, below};
  document.querySelectorAll('#stepTable tr.drop-above, #stepTable tr.drop-below').forEach(x => x.classList.remove('drop-above', 'drop-below'));
  tr.classList.add(below ? 'drop-below' : 'drop-above');
});
$('runDetail').addEventListener('drop', async e => {
  if (!dragKey) return;
  e.preventDefault();
  const key = dragKey, at = dropAt;
  let before = null;
  if (at) {
    const waiting = runDetail.steps.filter(s => s.state === 'waiting').map(s => s.key);
    before = at.below ? (waiting[waiting.indexOf(at.key) + 1] ?? null) : at.key;
  }
  dragKey = null; dropAt = null; clearDrop();
  if (!at || before === key) { renderRunDetail(); return; }
  try { const r = await runAction({action: 'move', key, before}); toast(`moved ${key}${adjusted(r)}`); }
  catch (err) { toast('Error: ' + err.message); }
  loadRun();
});
$('runDetail').addEventListener('dragend', () => { dragKey = null; dropAt = null; clearDrop(); renderRunDetail(); });

// ---- "after…" dialog: choose exactly what a waiting step waits for
function dependents(key) {   // steps that (transitively) wait for `key`; they cannot also be something it waits for
  const out = new Set(), stack = [key];
  while (stack.length) { const k = stack.pop(); for (const s of runDetail.steps) if (s.needs.includes(k) && !out.has(s.key)) { out.add(s.key); stack.push(s.key); } }
  return out;
}
function openAfterDialog(key) {
  const step = runDetail.steps.find(s => s.key === key); if (!step) return;
  const blocked = dependents(key);
  $('needsTitle').textContent = key;
  $('needsList').innerHTML = runDetail.steps.filter(s => s.key !== key).map(s => {
    const dis = blocked.has(s.key);
    return `<label class="${dis ? 'dis' : ''}"><input type="checkbox" value="${esc(s.key)}" ${step.needs.includes(s.key) ? 'checked' : ''} ${dis ? 'disabled' : ''}>
      <span class="ellip">${esc(s.key)}</span><span class="pill ${s.state}">${s.state}</span>${dis ? '<span class="muted">waits for this step</span>' : ''}</label>`;
  }).join('');
  $('needsDlg').dataset.key = key; $('needsDlg').showModal();
}
$('needsList').addEventListener('input', () => { /* nothing live: Save reads the boxes */ });
$('needsAll').addEventListener('click', () => $('needsList').querySelectorAll('input:not(:disabled)').forEach(i => { i.checked = true; }));
$('needsNone').addEventListener('click', () => $('needsList').querySelectorAll('input').forEach(i => { i.checked = false; }));
$('needsCancel').addEventListener('click', () => $('needsDlg').close());
$('needsSave').addEventListener('click', async () => {
  const key = $('needsDlg').dataset.key, needs = [...$('needsList').querySelectorAll('input:checked')].map(i => i.value);
  $('needsDlg').close();
  try { await runAction({action: 'needs', key, needs}); toast(needs.length ? `${key} now waits for ${needs.length} step(s)` : `${key} starts as soon as resources allow`); loadRun(); }
  catch (err) { toast('Error: ' + err.message); }
});

onPress($('runDetail'), async e => {
  const sel = e.target.closest('[data-sel]');
  if (sel) { const k = sel.dataset.sel; selSteps.has(k) ? selSteps.delete(k) : selSteps.add(k); renderRunDetail(); return; }
  const t = e.target.closest('button'); if (!t || !runDetail) return;
  const rid = encodeURIComponent(runDetail.id);
  try {
    if (t.dataset.after) { openAfterDialog(t.dataset.after); }
    else if (t.dataset.release) { await runAction({action: 'needs', key: t.dataset.release, needs: []}); toast(`${t.dataset.release} no longer waits for anything`); loadRun(); }
    else if (t.dataset.parSel !== undefined) {
      const keys = [...selSteps]; if (!keys.length) return;
      const r = await runAction({action: 'parallel', keys}); selSteps.clear();
      const mp = r.max_parallel_raised ? `; max at once raised ${r.max_parallel_raised.from} → ${r.max_parallel_raised.to}` : '';
      toast((keys.length > 1 ? `${keys.length} steps can now run in parallel${adjusted(r)}` : `${keys[0]} no longer waits for anything`) + mp); loadRun();
    }
    else if (t.dataset.maxpar !== undefined) {
      const cur = runDetail.max_parallel || '';
      const v = prompt('How many steps of this run may run at the same time?\n(blank or 0 = no limit; the RAM and CPU budgets still apply)', cur);
      if (v === null) return;
      const n = parseInt(v, 10);
      await runAction({action: 'max_parallel', value: Number.isFinite(n) && n > 0 ? n : null});
      toast(n > 0 ? `max ${n} at once` : 'no per-run limit'); loadRun();
    }
    else if (t.dataset.log) { selLog = t.dataset.log; logText = ''; await loadRun(); }
    else if (t.dataset.logClose !== undefined) { selLog = null; renderRunDetail(); }
    else if (t.dataset.steerPid) { const g = S.groups.find(x => x.pid == t.dataset.steerPid); if (g) openDrawer(g.id); else toast('process not in the last sample yet'); }
    else if (t.dataset.stepCancel) { if (confirm(`Stop step ${t.dataset.stepCancel}?`)) { await api('POST', `/api/runs/${rid}/action`, {action: 'cancel', key: t.dataset.stepCancel}); toast('step cancelled'); loadRun(); } }
    else if (t.dataset.runForget !== undefined) { await api('POST', '/api/runs/forget', {runs: [runDetail.id]}); afterForget(`${runDetail.name} removed from the list`); }
    else if (t.dataset.stepRetry) { await api('POST', `/api/runs/${rid}/action`, {action: 'retry', key: t.dataset.stepRetry}); toast('step queued again'); loadRun(); }
    else if (t.dataset.runAct) {
      const a = t.dataset.runAct;
      if (a === 'cancel' && !confirm(`Cancel run ${runDetail.name}? Running steps are killed.`)) return;
      const body = a.startsWith('prio') ? {action: 'priority', value: runDetail.priority + (a === 'prio-up' ? 1 : -1)} : {action: a};
      await api('POST', `/api/runs/${rid}/action`, body); toast(`${runDetail.name}: ${a}`); loadRun();
    }
  } catch (err) { toast('Error: ' + err.message); }
});

// ---------------------------------------------------------------- processes
$('procFilter').addEventListener('input', () => renderProcs());
$('procSort').addEventListener('change', e => { procSort = e.target.value; renderProcs(); });
$('procView').addEventListener('click', e => { const b = e.target.closest('button'); if (!b) return; procView = b.dataset.v;
  [...$('procView').children].forEach(x => x.classList.toggle('on', x === b)); renderProcs(); });

function flagPills(flags) { return (flags || []).map(f => FLAG[f] ? `<span class="pill ${FLAG[f][0]}">${FLAG[f][1]}</span>` : '').join(' '); }
function renderProcs() {
  if (!S) return;
  const q = $('procFilter').value.toLowerCase();
  let gs = S.groups.filter(g => {
    if (procView === 'jobs' && !['job', 'managed'].includes(g.kind)) return false;
    if (procView === 'flagged' && !(g.flags.length || g.children.some(c => c.flags.length))) return false;
    if (q && !(g.label + ' ' + g.sub + ' ' + g.pid + ' ' + g.children.map(c => c.label + c.pid).join(' ')).toLowerCase().includes(q)) return false;
    return true;
  });
  gs.sort(procSort === 'cpu' ? (a, b) => b.cpu - a.cpu : procSort === 'label' ? (a, b) => a.label.localeCompare(b.label) : (a, b) => b.rss - a.rss);
  const flaggedN = S.groups.filter(g => g.flags.length).length;
  $('procView').querySelector('[data-v=flagged]').textContent = `Flagged (${flaggedN})`;
  $('procBody').innerHTML = gs.map(g => {
    const kids = g.children, ex = expanded.has(g.id);
    const tag = g.kind === 'managed' ? ' <span class="pill run">procdeck</span>' : '';
    let h = `<tr class="prow ${drawerGid === g.id ? 'sel' : ''}" data-gid="${esc(g.id)}">
      <td><span class="tw" data-tw="${esc(g.id)}">${kids.length ? (ex ? '▾' : '▸') : ''}</span><span class="lbl-main">${esc(g.label)}</span>${kids.length ? ` <span class="muted">+${kids.length}</span>` : ''}<div class="lbl-sub" style="margin-left:14px" title="${esc(g.sub)}">${esc(g.sub)}</div></td>
      <td class="r num muted">${g.pid}</td><td class="r num">${g.cpu.toFixed(1)}%</td><td><svg class="spark" width="90" height="20">${spark(g.cpu_hist, 90, 20, 'var(--accent)', Math.max(25, ...g.cpu_hist))}</svg></td>
      <td class="r num">${gb(g.rss)}</td><td class="r num muted">${g.nproc}</td><td>${PRI[g.priority] || esc(g.priority)}</td><td class="num">${esc(g.cores)}</td><td>${flagPills(g.flags)}${tag}</td></tr>`;
    if (ex) h += kids.map(k => `<tr class="child"><td style="padding-left:34px" title="${esc(k.cmd)}">${esc(k.label)} <span class="muted">${esc(k.name)}</span></td><td class="r num muted">${k.pid}</td><td class="r num">${k.cpu.toFixed(1)}%</td><td></td><td class="r num">${gb(k.rss)}</td><td class="r num muted">${k.threads} thr</td><td class="muted">${PRI[k.priority] || ''}</td><td></td><td>${flagPills(k.flags)}</td></tr>`).join('');
    return h;
  }).join('') || `<tr><td colspan="9" class="empty">nothing matches</td></tr>`;
}
onPress($('procBody'), e => {
  const tw = e.target.closest('[data-tw]');
  if (tw) { const id = tw.dataset.tw; expanded.has(id) ? expanded.delete(id) : expanded.add(id); renderProcs(); return; }
  const tr = e.target.closest('tr.prow'); if (tr) openDrawer(tr.dataset.gid);
});

// ---------------------------------------------------------------- drawer (steering)
let dMode = 'none';
function openDrawer(gid) {
  const g = S.groups.find(x => x.id === gid); if (!g) return;
  drawerGid = gid; drawerPid = g.pid;
  $('dTitle').textContent = g.label; $('dExe').textContent = g.sub;
  $('dPri').value = PRI[g.priority] ? g.priority : 'normal';
  const ncpu = S.system.ncpu, aff = g.affinity && g.affinity.length ? g.affinity : [...Array(ncpu).keys()];
  $('dCores').innerHTML = [...Array(ncpu).keys()].map(i => `<button class="${aff.includes(i) ? 'on' : ''}" data-c="${i}">${i}</button>`).join('');
  $('dProfiles').innerHTML = Object.keys(S.profiles || {}).map(p => `<button class="sm" data-prof="${esc(p)}">${esc(p)}</button>`).join('');
  $('dMem').value = ''; $('dResult').style.display = 'none';
  setMode('none');
  const sys = g.kind === 'system', app = g.kind === 'app';
  const daemon = g.kind_root === 'daemon';
  for (const id of ['dApply', 'dSuspend', 'dResume', 'dKill']) $(id).disabled = sys;
  for (const id of ['dSuspend', 'dKill']) {
    $(id).disabled = sys || daemon;
    $(id).title = daemon ? 'This is procdeck itself: stop it with `procdeck stop` (running steps keep going)' : '';
  }
  $('dApply').textContent = app && g.nproc > 1 ? 'Apply to oldest process tree' : 'Apply to tree';
  updateDrawerLive();
  $('drawer').classList.add('on'); renderProcs();
  api('GET', `/api/procs/${g.pid}/info`).then(info => { const cs = info.cpu_share || {};
    setMode(cs.mode || 'none', cs.value); }).catch(() => {});
}
function updateDrawerLive() {
  const g = S.groups.find(x => x.id === drawerGid);
  if (!g) { $('dKv').innerHTML = '<div>Status</div><div>exited</div>'; return; }
  const kv = [['PID', g.pid], ['Kind', g.kind === 'managed' ? `procdeck step (${g.managed?.run_id || ''})` : g.kind], ['Folder', g.cwd || '-'],
    ['Started', hm(g.started)], ['Tree', `${g.nproc} processes · ${gb(g.rss)} · ${g.threads} threads`], ['CPU', g.cpu.toFixed(1) + '% of machine'],
    ['Priority', PRI[g.priority] || g.priority], ['Cores', g.cores]];
  if (g.flags.includes('idle')) kv.push(['Idle', dur(g.idle_for) + ' at <1% CPU']);
  if (g.flags.includes('orphan')) kv.push(['Parent', 'exited (orphaned process)']);
  if (g.flags.includes('hung')) kv.push(['Window', 'not responding']);
  $('dKv').innerHTML = kv.map(([k, v]) => `<div>${k}</div><div>${esc(v)}</div>`).join('');
}
function setMode(m, value) {
  dMode = m; [...$('dMode').children].forEach(b => b.classList.toggle('on', b.dataset.m === m));
  const s = $('dShare'); s.disabled = m === 'none';
  if (m === 'cap') { s.min = 5; s.max = 100; s.step = 5; s.value = value || 50; } else { s.min = 1; s.max = 9; s.step = 1; s.value = value || 5; }
  shareLabel();
}
function shareLabel() {
  const v = $('dShare').value;
  $('dShareV').textContent = dMode === 'weight' ? `${v} / 9` : dMode === 'cap' ? `${v}%` : '-';
  $('dShareNote').textContent = dMode === 'weight' ? 'Relative share when CPU is contended: weight 8 gets ~4× the CPU of weight 2. Idle CPU is still used.'
    : dMode === 'cap' ? 'Hard ceiling as % of the whole machine, even when the CPU is otherwise idle.' : 'No CPU share limit (applies "unlimited" if a limit was set before).';
}
$('dMode').addEventListener('click', e => { const b = e.target.closest('button'); if (b) setMode(b.dataset.m); });
$('dShare').addEventListener('input', shareLabel);
$('dCores').addEventListener('click', e => { const b = e.target.closest('button'); if (b) b.classList.toggle('on'); });
$('dClose').onclick = () => { $('drawer').classList.remove('on'); drawerGid = null; drawerPid = null; renderProcs(); };
function showResult(r) { const el = $('dResult'); el.style.display = 'block';
  el.textContent = (r.applied?.length ? 'Applied: ' + r.applied.join(', ') : 'Nothing changed') + (r.warnings?.length ? '\nWarnings:\n- ' + r.warnings.join('\n- ') : ''); }
$('dApply').onclick = async () => {
  const cores = [...$('dCores').querySelectorAll('button.on')].map(b => +b.dataset.c);
  if (!cores.length) { toast('select at least one core'); return; }
  const body = {priority: $('dPri').value, cores: cores.length === S.system.ncpu ? 'all' : cores.join(','), cpu_mode: dMode};
  if (dMode !== 'none') body.cpu_value = +$('dShare').value;
  if ($('dMem').value !== '') body.mem_cap_gb = +$('dMem').value;
  try { showResult(await api('POST', `/api/procs/${drawerPid}/steer`, body)); } catch (e) { toast('Error: ' + e.message); }
};
$('dProfiles').addEventListener('click', async e => { const b = e.target.closest('[data-prof]'); if (!b) return;
  try { showResult(await api('POST', `/api/procs/${drawerPid}/steer`, {profile: b.dataset.prof})); } catch (err) { toast('Error: ' + err.message); } });
for (const [id, act] of [['dSuspend', 'suspend'], ['dResume', 'resume'], ['dKill', 'kill']]) {
  $(id).onclick = async () => {
    const g = S.groups.find(x => x.id === drawerGid);
    if (act === 'kill' && !confirm(`Kill ${g ? g.label : drawerPid} and all ${g ? g.nproc : ''} processes in its tree?`)) return;
    try { showResult(await api('POST', `/api/procs/${drawerPid}/signal`, {action: act})); } catch (e) { toast('Error: ' + e.message); }
  };
}

// ---------------------------------------------------------------- system
function lineChart(svg, series, max = 100, warnAt = null) {
  const W = 600, H = 170, pad = 4;
  let h = '';
  for (const y of [25, 50, 75]) h += `<line x1="0" x2="${W}" y1="${H - y / max * (H - pad)}" y2="${H - y / max * (H - pad)}" stroke="var(--line)" vector-effect="non-scaling-stroke"/>`;
  if (warnAt != null) h += `<line x1="0" x2="${W}" y1="${H - warnAt / max * (H - pad)}" y2="${H - warnAt / max * (H - pad)}" stroke="var(--warn)" stroke-dasharray="4 4" opacity=".7" vector-effect="non-scaling-stroke"/>`;
  for (const [pts, color] of series) {
    if (pts.length < 2) continue;
    h += `<polyline fill="none" stroke="${color}" stroke-width="2" vector-effect="non-scaling-stroke" points="${pts.map((v, i) => `${(i / (pts.length - 1) * W).toFixed(1)},${(H - clamp(v, 0, max) / max * (H - pad)).toFixed(1)}`).join(' ')}"/>`;
  }
  svg.innerHTML = h;
}
function renderSystem() {
  const sy = S.system; if (!sy.percpu) return;
  const n = sy.percpu.length;
  $('coregrid').style.setProperty('--n', n); $('corelbl').style.setProperty('--n', n);
  $('cpuInfo').textContent = `${n} logical CPUs · ${sy.cpu.toFixed(0)}% total · ${sy.nproc} processes, ${sy.nthreads} threads`;
  $('coregrid').innerHTML = sy.percpu.map(c => `<div class="corebar"><i style="height:${c}%"></i></div>`).join('');
  $('corelbl').innerHTML = sy.percpu.map((c, i) => `<div>${i}<br><span class="num">${c.toFixed(0)}%</span></div>`).join('');
  const used = sy.ram_used, cache = Math.min(sy.cache, sy.ram_avail), free = sy.ram_total - used - cache;
  $('memTotal').textContent = gb(sy.ram_total);
  $('membar').innerHTML = `<div style="width:${used / sy.ram_total * 100}%;background:${S.alert.level === 'critical' ? 'var(--bad)' : S.alert.level === 'warn' ? 'var(--warn)' : 'var(--accent)'}" title="in use"></div>
    <div style="width:${cache / sy.ram_total * 100}%;background:color-mix(in srgb,var(--accent) 35%,transparent)" title="cache (reclaimable)"></div><div style="flex:1;background:var(--panel2)"></div>`;
  $('memLegend').innerHTML = `<span><i style="background:var(--accent)"></i>in use ${gb(used)}</span><span><i style="background:color-mix(in srgb,var(--accent) 35%,transparent)"></i>cache ${gb(cache)} (reusable)</span><span><i style="background:var(--panel2);border:1px solid var(--line)"></i>free ${gb(Math.max(0, free))}</span><span title="RAM + pagefile promised to programs">commit ${gb(sy.commit_total)} / ${gb(sy.commit_limit)}</span>`;
  const top = S.groups.slice(0, 8), mx = Math.max(...top.map(g => g.rss), 1);
  $('topram').innerHTML = top.map(g => `<div class="r"><span class="ellip" title="${esc(g.label)}">${esc(g.label)}</span><div class="t"><i style="width:${g.rss / mx * 100}%"></i></div><span class="num" style="text-align:right">${gb(g.rss)}</span></div>`).join('');
  lineChart($('sysChart'), [[S.sys_hist.map(x => x[1]), 'var(--accent)'], [S.sys_hist.map(x => x[2]), 'var(--ram)']], 100, 85);
}

// ---------------------------------------------------------------- history
let histMin = 60;
$('histRange').addEventListener('click', e => { const b = e.target.closest('button'); if (!b) return; histMin = +b.dataset.m;
  [...$('histRange').children].forEach(x => x.classList.toggle('on', x === b)); loadHistory(); });
async function loadHistory() {
  try {
    const h = await api('GET', '/api/history?minutes=' + histMin);
    lineChart($('histChart'), [[h.system.map(x => x.cpu), 'var(--accent)'], [h.system.map(x => x.ram_pct), 'var(--ram)']], 100, 85);
    $('histSpan').textContent = h.system.length ? `${hm(h.system[0].ts)} – ${hm(h.system[h.system.length - 1].ts)}` : 'no samples yet (written every 5 s)';
    $('histJobs').innerHTML = h.jobs.map(j => `<tr><td class="ellip" style="max-width:380px" title="${esc(j.label)}">${esc(j.label)}</td><td class="r num">${gb(j.peak_rss)}</td><td class="r num">${j.mean_cpu.toFixed(1)}%</td><td class="muted">${hm(j.first)}–${hm(j.last)}</td></tr>`).join('') || '<tr><td colspan="4" class="muted">none</td></tr>';
    $('histScripts').innerHTML = h.scripts.map(s => `<tr><td>${esc(s.script_key)}</td><td class="r num">${s.runs}</td><td class="r num">${dur(s.median_s)}</td><td class="r num">${gb(s.peak_rss)}</td><td class="r num">${s.cpu_hours.toFixed(1)}</td></tr>`).join('') || '<tr><td colspan="5" class="muted">no finished procdeck steps yet</td></tr>';
  } catch (e) { toast('history: ' + e.message); }
}

// ---------------------------------------------------------------- config
async function loadConfig() {
  for (const n of ['profiles', 'rules', 'settings']) {
    try { $('cfg-' + n).value = (await api('GET', '/api/config/' + n)).text; $('err-' + n).textContent = ''; } catch (e) { $('err-' + n).textContent = e.message; }
  }
  try { const lg = await api('GET', '/api/rules/log');
    $('rulesLog').innerHTML = lg.length ? 'Recently applied: ' + lg.slice(-8).reverse().map(x => `${esc(x.label || x.name)} (${x.pid}): ${esc(x.applied.join(', '))}`).join(' · ') : 'No rules applied yet.'; } catch { /* ignore */ }
}
document.querySelectorAll('[data-save]').forEach(b => b.addEventListener('click', async () => {
  const n = b.dataset.save;
  try { await api('PUT', '/api/config/' + n, {text: $('cfg-' + n).value}); $('err-' + n).textContent = ''; toast(n + '.yaml saved and reloaded'); }
  catch (e) { $('err-' + n).textContent = e.message; }
}));

// ---------------------------------------------------------------- tabs
function showTab(t) {
  tab = t; ls.set('procdeck_tab', t);
  document.querySelectorAll('#nav button').forEach(x => x.classList.toggle('on', x.dataset.tab === t));
  document.querySelectorAll('.tab').forEach(x => x.classList.toggle('on', x.id === 'tab-' + t));
  if (t === 'history') loadHistory();
  if (t === 'config') loadConfig();
  if (t === 'runs') loadRun();
  if (S) render();
}
$('nav').addEventListener('click', e => { const b = e.target.closest('button'); if (b) showTab(b.dataset.tab); });
showTab(['runs', 'procs', 'system', 'history', 'config'].includes(tab) ? tab : 'runs');
poll();
