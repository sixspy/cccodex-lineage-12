import { api } from '../api.js';

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

export async function renderPromo(view, setMsg) {
  const [state, pkgs, receipts, targets, runs, locks] = await Promise.all([
    api('/api/promo/state'),
    api('/api/promo/packages'),
    api('/api/promo/receipts'),
    api('/api/promo/target-edges'),
    api('/api/promo/ingest'),
    api('/api/promo/locks'),
  ]);

  const sameSource = (state.same_source_counts || []).map((r) => `
    <tr><td>${esc(r.source_repo)}</td><td><span class="pill mode">${esc(r.receive_mode)}</span></td><td>${r.edge_count}</td></tr>`).join('');

  view.innerHTML = `
    <section class="card">
      <h2>跨仓晋升放行</h2>
      <p class="hint">两种接收方式二选一、不可同时打开：快照表封存整包边指纹与条数（半包直接拒写）；文件凭证把整包物化成文件、旁路凭证记一行。切换方式只影响后续放行，已入边表内容不变。</p>
      <div class="row">
        <label>接收方式
          <select id="mode">
            <option value="快照表"${state.receive_mode === '快照表' ? ' selected' : ''}>快照表</option>
            <option value="文件凭证"${state.receive_mode === '文件凭证' ? ' selected' : ''}>文件凭证</option>
          </select>
        </label>
        <button id="saveMode" class="primary" type="button">保存接收方式</button>
      </div>
      <div class="row" style="margin-top:.75rem">
        <label>来源仓<input id="src" value="源仓" /></label>
        <label>目标仓<input id="dst" value="目标仓" /></label>
        <button id="pack" class="primary" type="button">封晋升包（整包指纹+条数）</button>
      </div>
    </section>

    <section class="card" style="margin-top:1rem">
      <h3>晋升总览（页面标记）</h3>
      <div class="kv">
        <b>当前接收方式</b><td><span class="pill mode">${esc(state.receive_mode)}</span></td>
        <b>晋升包进度</b><td>已接收 ${state.package_received} / 共 ${state.package_total} 个</td>
        <b>最近晋升来源</b><td>${state.last_receipt ? esc(state.last_receipt.source_repo) + ' → ' + esc(state.last_receipt.target_repo) + '（' + esc(state.last_receipt.package_code) + '）' : '暂无'}</td>
        <b>目标仓边表总数</b><td>${state.target_edge_total}</td>
        <b>最近全量入库</b><td>${state.last_ingest ? `<span class="pill ${state.last_ingest.status === '已通' ? 'ok' : 'bad'}">${esc(state.last_ingest.status)}</span> <span class="hint">${esc(state.last_ingest.reason)}</span>` : '未执行'}</td>
        <b>当前排他锁</b><td>${(state.locks || []).length ? state.locks.map((l) => `<span class="pill bad">${esc(l.package_code)} · ${esc(l.holder)}</span>`).join(' ') : '<span class="pill">无锁</span>'}</td>
      </div>
      <h3 style="margin-top:1rem">边表同源条数</h3>
      <table class="tbl">
        <thead><tr><th>来源仓</th><th>接收方式</th><th>同源条数</th></tr></thead>
        <tbody>${sameSource || '<tr><td colspan="3" class="hint">目标仓边表暂无数据</td></tr>'}</tbody>
      </table>
      <div class="row" style="margin-top:.75rem">
        <button id="ingest" class="primary" type="button">全量抓取入库（先核指纹/条数）</button>
      </div>
    </section>

    <section class="promo-grid" style="margin-top:1rem">
      <article class="card">
        <h3>晋升包（源仓）</h3>
        ${(pkgs.items || []).map((p) => `
          <div style="border-bottom:1px solid var(--line); padding:.5rem 0">
            <div class="row" style="justify-content:space-between">
              <strong>${esc(p.package_code)}</strong>
              <span class="pill ${p.status === '已接收' ? 'ok' : p.status === '已拒写' ? 'bad' : ''}">${esc(p.status)}</span>
            </div>
            <p class="hint">${esc(p.source_repo)} → ${esc(p.target_repo)} · ${p.edge_count} 条</p>
            <p class="mono">指纹 ${esc(p.edge_fingerprint.slice(0, 24))}…</p>
            <p class="hint">快照 ${p.snapshot_count} 条 · 入目标边表 ${p.target_count} 条</p>
            <div class="row">
              <button data-rel="${p.id}" class="primary" type="button" ${p.status === '已接收' ? 'disabled' : ''}>放行（单次写入）</button>
              <button data-det="${p.id}" type="button">详情</button>
            </div>
            <pre class="mono" id="det${p.id}"></pre>
          </div>`).join('') || '<p class="hint">暂无晋升包</p>'}
      </article>

      <article class="card">
        <h3>旁路凭证（单次写入）</h3>
        ${(receipts.items || []).map((r) => `
          <div style="border-bottom:1px solid var(--line); padding:.5rem 0">
            <strong>${esc(r.receipt_code)}</strong>
            <span class="pill mode">${esc(r.receive_mode)}</span>
            <p class="hint">${esc(r.package_code)} · ${esc(r.source_repo)} → ${esc(r.target_repo)}</p>
            <p class="hint">凭证条数 ${r.edge_count} · 持锁人 ${esc(r.holder)}</p>
            <p class="mono">${esc(r.edge_fingerprint.slice(0, 24))}…</p>
            ${r.payload_file ? `<p class="hint">物化文件：<span class="mono">${esc(r.payload_file)}</span></p>` : '<p class="hint">快照表封存（无物化文件）</p>'}
          </div>`).join('') || '<p class="hint">暂无凭证</p>'}
      </article>

      <article class="card">
        <h3>目标仓边表（接收内容）</h3>
        ${(targets.items || []).map((t) => `
          <div style="border-bottom:1px solid var(--line); padding:.4rem 0">
            <strong>${esc(t.edge_code)}</strong>
            <span class="pill mode">${esc(t.receive_mode)}</span>
            <p class="hint">${esc(t.upstream_code)} → ${esc(t.downstream_code)} · ${esc(t.transform_kind)}</p>
            <p class="hint">来源 ${esc(t.source_repo)}</p>
          </div>`).join('') || '<p class="hint">目标仓边表为空</p>'}
      </article>

      <article class="card">
        <h3>全量抓取入库记录</h3>
        ${(runs.items || []).map((r) => `
          <div style="border-bottom:1px solid var(--line); padding:.4rem 0">
            <span class="pill ${r.status === '已通' ? 'ok' : 'bad'}">${esc(r.status)}</span>
            <p class="hint">期望 ${r.expected_count} 条 / 实际 ${r.actual_count} 条</p>
            <p class="hint">${esc(r.reason)}</p>
          </div>`).join('') || '<p class="hint">暂无入库记录</p>'}
        <h3 style="margin-top:1rem">排他锁监视</h3>
        ${(locks.items || []).map((l) => `<p><span class="pill bad">${esc(l.package_code)} · ${esc(l.holder)}</span></p>`).join('') || '<p class="hint">当前无锁，放行结束即释放</p>'}
      </article>
    </section>`;

  document.getElementById('saveMode').onclick = async () => {
    try {
      const r = await api('/api/promo/mode', {
        method: 'POST',
        body: JSON.stringify({ receive_mode: document.getElementById('mode').value }),
      });
      setMsg('接收方式已切为：' + r.item.value);
      await renderPromo(view, setMsg);
    } catch (e) { setMsg(e.message, true); }
  };

  document.getElementById('pack').onclick = async () => {
    try {
      const r = await api('/api/promo/packages', {
        method: 'POST',
        body: JSON.stringify({
          source_repo: document.getElementById('src').value,
          target_repo: document.getElementById('dst').value,
        }),
      });
      setMsg(`已封 ${r.item.package_code}：${r.item.edge_count} 条边，指纹已封存`);
      await renderPromo(view, setMsg);
    } catch (e) { setMsg(e.message, true); }
  };

  document.getElementById('ingest').onclick = async () => {
    try {
      const r = await api('/api/promo/ingest', { method: 'POST', body: '{}' });
      setMsg('全量抓取入库：' + r.run.status + ' · ' + r.run.reason);
      await renderPromo(view, setMsg);
    } catch (e) { setMsg(e.message, true); await renderPromo(view, setMsg); }
  };

  view.querySelectorAll('[data-rel]').forEach((btn) => {
    btn.onclick = async () => {
      try {
        const r = await api('/api/promo/packages/' + btn.dataset.rel + '/release', {
          method: 'POST',
          body: JSON.stringify({ holder: '值班' }),
        });
        setMsg('放行成功，接收方式：' + r.receive_mode + '，凭证单次写入');
        await renderPromo(view, setMsg);
      } catch (e) { setMsg(e.message, true); await renderPromo(view, setMsg); }
    };
  });

  view.querySelectorAll('[data-det]').forEach((btn) => {
    btn.onclick = async () => {
      try {
        const r = await api('/api/promo/packages/' + btn.dataset.det);
        document.getElementById('det' + btn.dataset.det).textContent = JSON.stringify({
          payload_edges: r.payload_edges,
          snapshot_edges: r.snapshot_edges,
          receipt: r.receipt,
          lock: r.lock,
        }, null, 2);
      } catch (e) { setMsg(e.message, true); }
    };
  });
}
