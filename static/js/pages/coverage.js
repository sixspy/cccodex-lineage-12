import { api } from '../api.js';

export async function renderCoverage(view, setMsg) {
  const reports = await api('/api/coverage/reports');
  const promo = await api('/api/promo/state').catch(() => null);
  const last = promo && promo.last_receipt;
  const lastIngest = promo && promo.last_ingest;
  view.innerHTML = `
    <section class="card">
      <h2>覆盖对账</h2>
      <div class="kv" style="margin-bottom:.75rem">
        <b>接收方式</b><span><span class="pill mode">${promo ? promo.receive_mode : '—'}</span></span>
        <b>晋升来源</b><span>${last ? `${last.source_repo} → ${last.target_repo}（${last.package_code}，${last.edge_count} 条）` : '暂无晋升'}</span>
        <b>晋升覆盖进度</b><span>${promo ? `已接收 ${promo.package_received} / 共 ${promo.package_total} 个包，目标边表 ${promo.target_edge_total} 条` : '—'}</span>
        <b>全量抓取入库</b><span>${lastIngest ? `<span class="pill ${lastIngest.status === '已通' ? 'ok' : 'bad'}">${lastIngest.status}</span> <span class="hint">${lastIngest.reason}</span>` : '未执行'}</span>
      </div>
      <div class="row">
        <button class="primary" id="go" type="button">跑覆盖对账</button>
      </div>
    </section>
    <section class="wall" style="margin-top:1rem">
      ${(reports.items || []).map((r) => `<article class="card">
        <h3>报告 #${r.id}</h3>
        <p>${r.summary}</p>
        <p class="muted">节点 ${r.node_count} · 已发布边 ${r.edge_count} · 孤儿 ${r.orphan_count} · 草稿边 ${r.uncovered_edges}</p>
      </article>`).join('') || '<p class="muted">暂无对账报告</p>'}
    </section>`;
  document.getElementById('go').onclick = async () => {
    try {
      const r = await api('/api/coverage/reports', { method: 'POST', body: '{}' });
      setMsg(r.report.summary);
      await renderCoverage(view, setMsg);
    } catch (e) { setMsg(e.message, true); }
  };
}
