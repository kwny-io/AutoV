/* studio-web 前端 —— 原生 ES 模块，无构建步骤。
 *
 * 每个页面只调真实接口。拿不到数据时如实显示状态码与错误码，不造行。
 * 所有请求带 X-Tenant-Id: tenant-demo（与 launcher.py 播种的租户一致），
 * 租户从不在这里被"补"——服务端该拒就拒。
 */

const TENANT = 'tenant-demo';
const PRINCIPAL = 'principal-demo';

/* ------------------------------------------------------------ 请求层 */

async function api(slug, path, opts = {}) {
  const headers = {
    'X-Tenant-Id': TENANT,
    'X-Principal-Id': PRINCIPAL,
    ...(opts.headers || {}),
  };
  if (opts.body !== undefined) headers['Content-Type'] = 'application/json';
  const response = await fetch(`/api/${slug}${path}`, {
    method: opts.method || 'GET',
    headers,
    body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
  });
  const text = await response.text();
  let payload;
  try { payload = text ? JSON.parse(text) : null; }
  catch { payload = { raw: text }; }
  return { ok: response.ok, status: response.status, payload };
}

/* ------------------------------------------------------------ 格式化 */

const esc = (v) => String(v ?? '').replace(/[&<>"']/g,
  (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const yuan = (micros) => micros == null ? '—'
  : '¥' + (Number(micros) / 1e6).toLocaleString('zh-CN',
      { minimumFractionDigits: 2, maximumFractionDigits: 2 });

const num = (v) => v == null ? '—' : Number(v).toLocaleString('zh-CN');

const pct = (a, b) => (!b ? 0 : Math.round(a / b * 100));

function ago(iso) {
  if (!iso) return '—';
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return String(iso).slice(0, 19).replace('T', ' ');
  const secs = Math.max(0, Math.round((Date.now() - then) / 1000));
  if (secs < 60) return `${secs} 秒前`;
  if (secs < 3600) return `${Math.round(secs / 60)} 分钟前`;
  if (secs < 86400) return `${Math.round(secs / 3600)} 小时前`;
  return `${Math.round(secs / 86400)} 天前`;
}

const clock = (iso) => iso ? String(iso).slice(0, 19).replace('T', ' ') : '—';

/* 运行状态 -> 界面语言。键来自 S21/S47 的 state 枚举，不在这里发明新状态。 */
const STATE = {
  QUEUED:          ['排队', 'todo'],
  RUNNING:         ['生成中', 'doing'],
  WAITING_REVIEW:  ['待审', 'review'],
  WAITING_BUDGET:  ['等预算', 'review'],
  PAUSED:          ['已暂停', 'todo'],
  SUCCEEDED:       ['已完成', 'done'],
  FAILED:          ['失败', 'fail'],
  CANCELLED:       ['已取消', 'fail'],
};
const stateLabel = (s) => STATE[s]?.[0] ?? s ?? '—';
const stateKind = (s) => STATE[s]?.[1] ?? 'todo';

/* ------------------------------------------------------------ 通用片段 */

function errorBox(title, res) {
  const code = res.payload?.code || res.payload?.detail?.code || '';
  const message = res.payload?.message || res.payload?.detail?.message ||
    res.payload?.detail || '';
  return `<div class="error"><strong>${esc(title)}</strong>
    HTTP ${res.status}${code ? ` · <code style="display:inline">${esc(code)}</code>` : ''}
    ${message ? `<div style="margin-top:6px">${esc(message)}</div>` : ''}
    <code>GET /api/… → ${esc(JSON.stringify(res.payload)).slice(0, 400)}</code>
  </div>`;
}

function emptyBox(title, body, hint) {
  return `<div class="empty"><strong>${esc(title)}</strong>${body}
    ${hint ? `<div style="margin-top:8px"><code>${esc(hint)}</code></div>` : ''}</div>`;
}

const loading = (what) => `<div class="loading">正在读取${esc(what)}</div>`;

/* ------------------------------------------------------------ 页面 1 总览 */

async function pageOverview() {
  const [summary, runs, capacity] = await Promise.all([
    api('s47', '/dashboard/summary'),
    api('s47', '/runs'),
    api('s22', '/capacity'),
  ]);

  if (!summary.ok) return errorBox('S47 总览读取失败', summary);

  const s = summary.payload;
  const items = runs.ok ? (runs.payload.items || []) : [];
  const atRisk = items.filter((r) =>
    ['FAILED', 'WAITING_BUDGET', 'PAUSED'].includes(r.run_state));

  const cards = `<div class="metrics">
    <div class="metric"><div class="lab">总运行</div>
      <div class="val">${num(s.total_runs)}</div>
      <div class="foot">S47 投影 · ${esc(clock(s.as_of))}</div></div>
    <div class="metric"><div class="lab">进行中</div>
      <div class="val">${num(s.active_runs)}</div>
      <div class="foot">RUNNING / WAITING_*</div></div>
    <div class="metric"><div class="lab">已完成</div>
      <div class="val">${num(s.completed_runs)}</div>
      <div class="foot">终态 SUCCEEDED</div></div>
    <div class="metric"><div class="lab">事件缺口</div>
      <div class="val" style="color:${s.gapped_runs ? 'var(--bad)' : 'inherit'}">${num(s.gapped_runs)}</div>
      <div class="foot">首事件 sequence &gt; 1</div></div>
  </div>`;

  const rows = items.map((r) => `<tr>
    <td><b>${esc(r.run_id)}</b>
      <div style="font-size:11px;color:var(--text-3)">
        ${esc(r.episode_id || '—')} · ${esc(r.branch_id || '—')}</div></td>
    <td><span class="pill ${stateKind(r.run_state) === 'done' ? 'good'
      : stateKind(r.run_state) === 'fail' ? 'bad'
      : stateKind(r.run_state) === 'review' ? 'warn' : 'accent'}">
      ${esc(stateLabel(r.run_state))}</span></td>
    <td class="num">${num(r.completed_shots)}${r.total_shots != null
      ? '/' + num(r.total_shots) : ''}</td>
    <td>${(() => {
      if (r.total_shots == null) {
        /* 总镜数在 S47 没有来源（C47-16），不编一个分母出来。 */
        return `<span style="font-size:11px;color:var(--text-3)">
          已生成 ${num(r.completed_shots)} 节点 · 总数不投影</span>`;
      }
      const w = pct(r.completed_shots || 0, r.total_shots);
      return `<div style="display:flex;align-items:center;gap:8px">
        <div style="flex:1;height:4px;background:var(--surface-2);border-radius:2px;overflow:hidden">
          <i style="display:block;height:100%;width:${w}%;background:var(--accent)"></i></div>
        <span style="font-size:11px;color:var(--text-3);font-family:var(--font-mono)">${w}%</span></div>`;
    })()}</td>
    <td class="num">${yuan(r.spent_micros)}</td>
    <td style="font-size:12px;color:var(--text-3)">${esc(ago(r.updated_at))}</td>
  </tr>`).join('');

  const table = items.length ? `<table class="grid">
    <thead><tr><th>运行 / 分集</th><th>状态</th><th class="num">镜头</th>
      <th>进度</th><th class="num">已花费</th><th>更新</th></tr></thead>
    <tbody>${rows}</tbody></table>`
    : emptyBox('暂无运行记录',
        '<div>经 S47 事件投影查询不到任何 run。</div>',
        'GET /api/s47/runs');

  const capBlock = capacity.ok
    ? (capacity.payload.items?.length
        ? `<table class="grid"><thead><tr><th>资源池</th><th>模态</th>
             <th class="num">占用</th><th class="num">最早等待</th></tr></thead><tbody>
           ${capacity.payload.items.map((c) => `<tr>
             <td><b>${esc(c.pool_id)}</b><div style="font-size:11px;color:var(--text-3)">
               ${esc(c.provider_id || '—')} · ${esc(c.region || '—')}</div></td>
             <td>${esc(c.modality || '—')}</td>
             <td class="num">${num(c.inflight_used)}/${num(c.inflight_limit)}</td>
             <td class="num">${num(c.oldest_wait_ms)} ms</td></tr>`).join('')}
           </tbody></table>`
        : emptyBox('暂无容量记录', '<div>没有登记的资源池。</div>', 'GET /api/s22/capacity'))
    : errorBox('S22 容量读取失败', capacity);

  const risk = atRisk.length
    ? `<table class="grid"><thead><tr><th>运行</th><th>状态</th><th class="num">缺口</th></tr></thead><tbody>
       ${atRisk.map((r) => `<tr><td>${esc(r.run_id)}</td>
         <td><span class="pill bad">${esc(stateLabel(r.run_state))}</span></td>
         <td class="num">${r.total_shots == null ? '—'
           : num(r.total_shots - r.completed_shots)}</td></tr>`).join('')}
       </tbody></table>`
    : '<div style="font-size:13px;color:var(--text-2)">当前没有需要人工介入的运行。</div>';

  return `${cards}
    <div class="card" style="margin-top:24px"><h3>运行清单</h3>
      <p class="hint">来自 S47 realtime-projection 的 run 投影，按变更序号排序。</p>
      ${table}</div>
    <div class="split" style="margin-top:24px">
      <div class="card"><h3>需要处理</h3>
        <p class="hint">失败、等预算、暂停的运行。</p>${risk}</div>
      <div class="card"><h3>资源池容量</h3>
        <p class="hint">S22 调度配额的实时水位。</p>${capBlock}</div>
    </div>`;
}

/* ------------------------------------------------------------ 页面 2 创作 */

async function pageCreative() {
  const [projects, scripts] = await Promise.all([
    api('s03', '/projects'),
    api('s04', '/scripts'),
  ]);

  const projBlock = !projects.ok
    ? errorBox('S03 项目目录读取失败', projects)
    : (projects.payload.projects?.length
      ? `<table class="grid"><thead><tr><th>项目</th><th>slug</th>
           <th>生命周期</th><th class="num">head</th></tr></thead><tbody>
         ${projects.payload.projects.map((p) => `<tr>
           <td><b>${esc(p.display_name)}</b>
             <div style="font-size:11px;color:var(--text-3)">${esc(p.project_id)}</div></td>
           <td style="font-family:var(--font-mono);font-size:12px">${esc(p.slug)}</td>
           <td><span class="pill">${esc(p.lifecycle)}</span></td>
           <td class="num">${num(p.head_revision)}</td></tr>`).join('')}
         </tbody></table>`
      : emptyBox('暂无项目',
          '<div>目录服务在线，但这个租户下没有已登记的项目。</div>'
          + '<div style="margin-top:8px;font-size:12px">创建项目要走 '
          + '<code style="display:inline">POST /api/s03/projects</code>，'
          + '该写入口需要 S01 授权上下文，而写权限键尚未注册（缺口 G5），'
          + '服务会返回 503 <code style="display:inline">PERMISSION_KEY_UNREGISTERED</code>。'
          + '这里不直接写库来凑数据。</div>',
          'GET /api/s03/projects'));

  const scriptBlock = !scripts.ok
    ? errorBox('S04 剧本读取失败', scripts)
    : (scripts.payload.scripts?.length
      ? `<table class="grid"><thead><tr><th>剧本</th><th>分集</th>
           <th>状态</th><th class="num">修订</th></tr></thead><tbody>
         ${scripts.payload.scripts.map((x) => `<tr>
           <td><b>${esc(x.script_id)}</b>
             <div style="font-size:11px;color:var(--text-3)">${esc(x.title || '')}</div></td>
           <td>${esc(x.episode_id || '—')}</td>
           <td><span class="pill">${esc(x.state || '—')}</span></td>
           <td class="num">${num(x.revision ?? x.head_revision)}</td></tr>`).join('')}
         </tbody></table>`
      : emptyBox('暂无剧本',
          '<div>剧本服务在线，没有已定稿的剧本修订。</div>',
          'GET /api/s04/scripts'));

  return `<div class="banner">
      <div><b>这一页为什么是空的：</b>创作链路（剧本 → 分镜 → 拆解）的写入口都挂在
      S01 授权后面，当前写权限键未注册（缺口 G5）。页面只读，不代填。</div>
    </div>
    <div class="card"><h3>项目目录</h3>
      <p class="hint">S03 project-catalog</p>${projBlock}</div>
    <div class="card"><h3>剧本库</h3>
      <p class="hint">S04 narrative</p>${scriptBlock}</div>`;
}

/* ------------------------------------------------------------ 页面 3 资产 */

async function pageAssets() {
  const res = await api('s10', '/assets');
  if (!res.ok) return errorBox('S10 资产检索失败', res);
  const items = res.payload.items || [];

  if (!items.length) {
    return `<div class="banner warn"><div><b>资产身份表为空。</b>
      这里列出的是 S10 记录的资产<i>身份</i>（谁拥有、什么类型、在哪个项目），
      不是资产文件本身。</div></div>
      ${emptyBox('暂无资产', '<div>检索服务在线，没有登记过的资产身份。</div>',
        'GET /api/s10/assets')}`;
  }

  const byType = items.reduce((acc, a) => {
    (acc[a.asset_type] ??= []).push(a); return acc;
  }, {});

  return `<div class="metrics">
    ${Object.entries(byType).slice(0, 4).map(([type, list]) => `
      <div class="metric"><div class="lab">${esc(type)}</div>
        <div class="val">${num(list.length)}</div>
        <div class="foot">资产身份</div></div>`).join('')}
    ${Object.keys(byType).length < 4 ? `<div class="metric">
      <div class="lab">合计</div><div class="val">${num(items.length)}</div>
      <div class="foot">${Object.keys(byType).length} 种类型</div></div>` : ''}
    </div>
    <div class="card" style="margin-top:24px"><h3>资产清单</h3>
      <p class="hint">S10 asset-catalog · 别名只用于检索，绑定解析走 /assets/{id}/resolve</p>
      <table class="grid"><thead><tr><th>资产</th><th>类型</th>
        <th>所有者</th><th>状态</th><th>创建</th></tr></thead><tbody>
      ${items.map((a) => `<tr>
        <td><b>${esc(a.asset_id)}</b>
          <div style="font-size:11px;color:var(--text-3)">${esc(a.role_key || '')}</div></td>
        <td>${esc(a.asset_type)}</td>
        <td style="font-size:12px">${esc(a.owner_service)}
          <span style="color:var(--text-3)">/ ${esc(a.owner_local_id)}</span></td>
        <td><span class="pill">${esc(a.status)}</span></td>
        <td style="font-size:12px;color:var(--text-3)">${esc(ago(a.created_at))}</td>
      </tr>`).join('')}
      </tbody></table></div>`;
}

/* ------------------------------------------------------------ 页面 4 记忆 */

async function pageMemory() {
  return `<div class="banner warn"><div>
      <b>跨集记忆只有写入口和查询入口，没有"列表"入口。</b>
      S16 story-memory 的读接口是 <code>POST /series/{series_id}/memory/query</code>，
      必须带 series_id 与查询主体。先有正史提交，才有可查的边界。</div></div>
    ${emptyBox('需要先有 series 与正史提交',
      `<div>知识边界（谁在第几集知道什么）是提交出来的，不是配置出来的。
       界面上没有 series 可选，是因为 S03 项目目录当前为空。</div>`,
      'POST /api/s16/series/{series_id}/memory/query')}
    <div class="card" style="margin-top:24px"><h3>这一页为什么不做成假列表</h3>
      <p class="hint">三件事如实说</p>
      <table class="grid"><tbody>
        <tr><td style="width:180px"><b>没有 GET 列表</b></td>
          <td>服务只提供按 series 查询。凭空列一个"记忆条目"表格就得自己造数据，
            那就成了第二权威。</td></tr>
        <tr><td><b>分支要显式分叉</b></td>
          <td>局部重生成走 <code>POST /series/{id}/branches</code>，
            正史指针只认合并提交。界面上不提供"改一句试试"的按钮。</td></tr>
        <tr><td><b>未回收伏笔</b></td>
          <td>依赖关系由 <code>invalidation-bound</code> 计算，
            不是前端遍历剧本猜出来的。</td></tr>
      </tbody></table></div>`;
}

/* ------------------------------------------------------------ 页面 5 治理 */

async function pageGovernance() {
  const [runs, policies] = await Promise.all([
    api('s21', '/runs'),
    api('s47', '/runs'),
  ]);

  const runItems = runs.ok ? (runs.payload.items || []) : [];
  const projRuns = policies.ok ? (policies.payload.items || []) : [];

  const blockersHint = runItems.length
    ? `<table class="grid"><thead><tr><th>运行</th><th>状态</th>
        <th class="num">完成</th><th class="num">阻塞</th><th class="num">失败</th>
        <th class="num">fence</th></tr></thead><tbody>
      ${runItems.map((r) => `<tr>
        <td><b>${esc(r.run_id)}</b>
          <div style="font-size:11px;color:var(--text-3)">
            ${esc(r.episode_id || '')} · ${esc(r.branch_id || '')}</div></td>
        <td><span class="pill ${stateKind(r.state) === 'done' ? 'good'
          : stateKind(r.state) === 'fail' ? 'bad' : 'accent'}">
          ${esc(stateLabel(r.state))}</span></td>
        <td class="num">${num(r.completed_nodes)}</td>
        <td class="num" style="color:${r.blocked_nodes ? 'var(--warn)' : 'inherit'}">${num(r.blocked_nodes)}</td>
        <td class="num" style="color:${r.failed_nodes ? 'var(--bad)' : 'inherit'}">${num(r.failed_nodes)}</td>
        <td class="num">${num(r.execution_fence)}</td>
      </tr>`).join('')}
      </tbody></table>`
    : emptyBox('编排服务暂无 run',
        '<div>S21 orchestration 是运行权威。它的 run 由 S03 分集触发',
        'GET /api/s21/runs');

  return `<div class="banner"><div>
      <b>两个"runs"不是同一份数据。</b>左边是 S21 编排的运行权威（含 execution_fence、
      节点阻塞数）；右边是 S47 的实时投影（含镜头进度与花费）。投影永远不反推权威。</div></div>
    <div class="card"><h3>运行与阻塞</h3>
      <p class="hint">S21 orchestration · execution_fence 是 run 级取消与故障切换的栅栏</p>
      ${!runs.ok ? errorBox('S21 读取失败', runs) : blockersHint}</div>
    <div class="card"><h3>投影视角</h3>
      <p class="hint">S47 realtime-projection</p>
      ${!policies.ok ? errorBox('S47 读取失败', policies)
        : projRuns.length ? `<table class="grid"><thead><tr><th>运行</th>
            <th>投影状态</th><th class="num">change_seq</th>
            <th class="num">水位</th></tr></thead><tbody>
          ${projRuns.map((r) => `<tr>
            <td>${esc(r.run_id)}</td>
            <td><span class="pill">${esc(r.projection_state)}</span></td>
            <td class="num">${num(r.change_seq)}</td>
            <td class="num">${num(r.applied_watermark)}</td></tr>`).join('')}
          </tbody></table>`
        : emptyBox('投影为空', '<div>没有已应用的运行事件。</div>')}
    </div>
    <div class="card"><h3>治理策略</h3>
      <p class="hint">S02 governance-policy · 只提供按项目取生效策略</p>
      ${emptyBox('需要一个项目 ID 才能查生效策略',
        '<div>接口是 <code>GET /projects/{project_id}/effective-policy</code>，'
        + '没有"列出全部策略"的入口。项目目录为空时，这里没有可选对象。</div>',
        'GET /api/s02/projects/{project_id}/effective-policy')}
    </div>`;
}

/* ------------------------------------------------------------ 页面 6 队列 */

const COLUMNS = [
  ['QUEUED', '排队'], ['RUNNING', '生成中'], ['WAITING_REVIEW', '待审'],
  ['WAITING_BUDGET', '等预算'], ['SUCCEEDED', '已完成'], ['FAILED', '失败'],
];

async function pageQueue() {
  const [runs, capacity] = await Promise.all([
    api('s47', '/runs'),
    api('s22', '/capacity'),
  ]);
  if (!runs.ok) return errorBox('S47 读取失败', runs);

  const items = runs.payload.items || [];
  const known = new Set(COLUMNS.map((c) => c[0]));
  const extra = [...new Set(items.map((r) => r.run_state).filter((s) => !known.has(s)))];
  const cols = [...COLUMNS, ...extra.map((s) => [s, stateLabel(s)])];

  if (!items.length) {
    return emptyBox('队列为空', '<div>没有经过投影的运行。</div>', 'GET /api/s47/runs');
  }

  const board = `<div class="kb-wrap">
    ${cols.map(([key, label]) => {
      const list = items.filter((r) => r.run_state === key);
      return `<div class="kb-col" data-st="${stateKind(key)}">
        <div class="kb-hd"><span class="dot"></span>${esc(label)}
          <span class="cnt">${list.length}</span></div>
        <div class="kb-body">
          ${list.map((r) => `<div class="kb-card">
              <div class="t">${esc(r.run_id)}</div>
              <div class="m"><span>${esc(r.episode_id || '—')}</span>
                <span>${esc(r.branch_id || '—')}</span></div>
              <div class="m" style="margin-top:6px">
                <span>${num(r.completed_shots)} 节点完成</span>
                <span>${yuan(r.spent_micros)}</span></div>
            </div>`).join('') || `<div style="font-size:12px;color:var(--text-3);
            padding:8px 4px">无</div>`}
        </div>
      </div>`;
    }).join('')}
  </div>`;

  const capBlock = capacity.ok
    ? (capacity.payload.items?.length
      ? `<table class="grid"><thead><tr><th>池</th><th>状态</th>
          <th class="num">in-flight</th><th class="num">账号额度</th>
          <th class="num">等待</th></tr></thead><tbody>
        ${capacity.payload.items.map((c) => `<tr>
          <td><b>${esc(c.pool_id)}</b>
            <div style="font-size:11px;color:var(--text-3)">
              ${esc(c.provider_id || '')} · ${esc(c.region || '')} · ${esc(c.modality || '')}</div></td>
          <td><span class="pill ${c.state === 'ACTIVE' ? 'good' : 'warn'}">${esc(c.state)}</span></td>
          <td class="num">${num(c.inflight_used)}/${num(c.inflight_limit)}</td>
          <td class="num">${num(c.account_used_inflight)}/${num(c.account_cap_inflight)}</td>
          <td class="num">${num(c.oldest_wait_ms)} ms</td></tr>`).join('')}
        </tbody></table>
        <div style="font-size:11px;color:var(--text-3);margin-top:12px">
          watermark ${esc(clock(capacity.payload.watermark))} ·
          generated ${esc(clock(capacity.payload.generated_at))}</div>`
      : emptyBox('没有资源池', '<div>未登记 provider 资源池。</div>'))
    : errorBox('S22 读取失败', capacity);

  return `<div class="banner"><div>
      <b>按状态分列，横向滚动。</b>列宽 <code>minmax(258px,1fr)</code>：
      列少时等分，列多时横向滚。卡片上的进度来自投影的
      <code>completed_nodes</code>（对外叫 completed_shots）。总镜数在投影里没有来源，
      所以卡片不画进度条——画一个分母是编的。</div></div>
    ${board}
    <div class="card" style="margin-top:24px"><h3>调度与配额</h3>
      <p class="hint">S22 scheduler-quota · in-flight 与账号额度是两个不同的上限</p>
      ${capBlock}</div>`;
}

/* ------------------------------------------------------------ 页面 7 审片 */

async function pageReview() {
  const res = await api('s40', '/review-tasks');
  if (!res.ok) return errorBox('S40 审片任务读取失败', res);
  const tasks = res.payload.tasks || [];

  if (!tasks.length) {
    return `<div class="banner"><div>
        <b>审片任务由编排在需要人工判断时创建。</b>当前没有待审任务，
        所以这里没有卡片可点——不是页面坏了。</div></div>
      ${emptyBox('暂无审片任务', '<div>审片服务在线，队列为空。</div>',
        'GET /api/s40/review-tasks')}
      <div class="card" style="margin-top:24px"><h3>审片任务长什么样</h3>
        <p class="hint">任务一旦创建，每张卡会带这些字段</p>
        <table class="grid"><tbody>
          <tr><td style="width:150px"><b>kind / state</b></td>
            <td>审片类型与生命周期状态；决策要写回 S40，再由事件通知编排。</td></tr>
          <tr><td><b>evidence</b></td>
            <td>可核对的材料引用。没有证据就不该让人做决定。</td></tr>
          <tr><td><b>options</b></td>
            <td>可选动作。每个动作对应一个 lesson 编号，不是自由文本。</td></tr>
          <tr><td><b>quorum</b></td>
            <td>所需票数。单签与双签在契约里是两个值。</td></tr>
          <tr><td><b>deadline</b></td>
            <td>过期后由 <code>:expire-due</code> 处理，不会永远挂着。</td></tr>
        </tbody></table></div>`;
  }

  return `<div class="banner"><div><b>${tasks.length} 个待审任务。</b>
      每张卡显示证据、选项与法定票数。决策提交后由 S40 发事件，
      编排才继续——界面不直接改运行状态。</div></div>
    <div class="split" style="grid-template-columns:1fr">
      ${tasks.map((t) => `<div class="card">
        <div style="display:flex;gap:12px;align-items:flex-start;margin-bottom:12px">
          <div style="flex:1">
            <h3 style="margin-bottom:2px">${esc(t.kind)} · ${esc(t.review_task_id)}</h3>
            <div style="font-size:12px;color:var(--text-3)">
              ${esc(t.subject?.kind || '')} ${esc(t.subject?.id || t.subject?.ref || '')}
              · rev ${num(t.revision)}</div>
          </div>
          <span class="pill warn">${esc(t.state)}</span>
        </div>
        <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px">
          ${(t.options || []).map((o) => `<span class="pill accent">
            ${esc(o.label || o.id || o.action || JSON.stringify(o))}</span>`).join('')
            || '<span class="pill">无选项</span>'}
          ${t.quorum ? `<span class="pill">票数 ${num(t.quorum.required ?? t.quorum)}</span>` : ''}
        </div>
        ${t.reason ? `<div style="font-size:13px;color:var(--text-2);
          background:var(--surface-2);border-radius:8px;padding:12px 14px">
          ${esc(typeof t.reason === 'string' ? t.reason : JSON.stringify(t.reason))}</div>` : ''}
        <div style="display:flex;gap:16px;flex-wrap:wrap;margin-top:12px;
          font-size:11px;color:var(--text-3);font-family:var(--font-mono)">
          <span>deadline ${esc(clock(t.deadline))}</span>
          <span>pack ${esc(String(t.pack_hash || '').slice(0, 12))}</span>
          <span>created ${esc(ago(t.created_at))}</span>
        </div>
        <div style="margin-top:16px">
          <input type="text" data-reason="${esc(t.review_task_id)}"
            placeholder="决定理由（服务端要求至少 4 个字，标点不算）"
            style="width:100%;padding:9px 12px;border:1px solid var(--border-strong);
              border-radius:8px;font:inherit;font-size:13px;margin-bottom:8px;
              background:var(--surface)">
          <div style="display:flex;gap:8px;flex-wrap:wrap">
          ${(t.options || []).slice(0, 4).map((o) => `<button class="pill accent"
            data-decide="${esc(t.review_task_id)}"
            data-option="${esc(o.option_id || o.id || o.action || '')}"
            data-revision="${esc(t.revision)}"
            type="button" style="cursor:pointer;padding:7px 14px">
            ${esc(o.label || o.option_id || o.id || o.action)}</button>`).join('')}
          </div>
        </div>
      </div>`).join('')}
    </div>`;
}

/* ------------------------------------------------------------ 页面 8 交付 */

async function pageDelivery() {
  const [packages, runs] = await Promise.all([
    api('s43', '/delivery-packages'),
    api('s47', '/runs'),
  ]);
  const items = packages.ok ? (packages.payload.items || []) : [];

  const pkgBlock = !packages.ok ? errorBox('S43 交付包读取失败', packages)
    : items.length ? `<table class="grid"><thead><tr><th>交付包</th>
        <th>类型</th><th>状态</th><th class="num">镜头</th><th>创建</th></tr></thead><tbody>
      ${items.map((p) => `<tr>
        <td><b>${esc(p.package_id)}</b>
          <div style="font-size:11px;color:var(--text-3)">${esc(p.episode_id || '')}</div></td>
        <td>${esc(p.package_type || '—')}</td>
        <td><span class="pill">${esc(p.state || '—')}</span></td>
        <td class="num">${num(p.shot_count ?? p.shots)}</td>
        <td style="font-size:12px;color:var(--text-3)">${esc(ago(p.created_at))}</td>
      </tr>`).join('')}</tbody></table>`
    : emptyBox('暂无交付包',
        '<div>交付包在成片通过 QC 后创建，当前没有。</div>',
        'GET /api/s43/delivery-packages');

  /* 时间线泳道：每个 run 一条泳道，按状态给出推进位置。
     这是真实状态，不是估算的甘特条。

     投影只有当前状态和已完成节点数，**没有历史轨迹**（C47-16 的同一处
     限制：S47 不存状态迁移史）。所以只画能证明的部分：行存在 ⇒ 排队已过；
     completed_shots > 0 ⇒ 生成已开始。剩下的不猜。

     失败 / 等预算 / 暂停 / 取消都不是这四段里的某一段，硬塞进第 0 段会把
     「停在排队」画成故障，故障段还会落在「排队」格子上。它们单独标出来。 */
  const runsItems = runs.ok ? (runs.payload.items || []) : [];
  const stages = ['排队', '生成', '待审', '完成'];
  const OFF_PATH = { FAILED: '失败', WAITING_BUDGET: '等预算',
    PAUSED: '已暂停', CANCELLED: '已取消' };
  const progressOf = (r) => {
    const started = (r.completed_shots || 0) > 0;
    if (r.run_state === 'SUCCEEDED') return { filled: 4, off: null };
    if (r.run_state === 'WAITING_REVIEW') return { filled: 3, off: null };
    if (r.run_state === 'QUEUED') return { filled: 1, off: null };
    if (r.run_state === 'RUNNING') return { filled: started ? 2 : 1, off: null };
    return { filled: started ? 2 : 1,
      off: OFF_PATH[r.run_state] || r.run_state };
  };
  const lanes = runsItems.slice(0, 10).map((r) => {
    const { filled, off } = progressOf(r);
    const offColor = r.run_state === 'WAITING_BUDGET'
      || r.run_state === 'PAUSED' ? 'var(--warn)' : 'var(--bad)';
    return `<tr>
      <td style="width:160px"><b>${esc(r.run_id)}</b>
        <div style="font-size:11px;color:var(--text-3)">${esc(r.episode_id || '')}</div></td>
      <td>
        <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:4px">
          ${stages.map((_, i) => `<div style="height:8px;border-radius:4px;
            background:${i < filled ? (off && i === filled - 1
              ? offColor : 'var(--accent)') : 'var(--surface-2)'}"></div>`).join('')}
        </div>
        <div style="display:flex;justify-content:space-between;margin-top:6px;
          font-size:10px;color:var(--text-3);letter-spacing:.04em">
          ${stages.map((s) => `<span>${esc(s)}</span>`).join('')}
        </div>
        ${off ? `<div style="margin-top:4px;font-size:11px;color:${offColor}">
          在「${esc(off)}」处停下，四段未走完</div>` : ''}
      </td>
      <td style="width:110px;text-align:right">
        <span class="pill ${stateKind(r.run_state) === 'done' ? 'good'
          : stateKind(r.run_state) === 'fail' ? 'bad' : 'accent'}">
          ${esc(stateLabel(r.run_state))}</span></td>
    </tr>`;
  }).join('');

  const timeline = runsItems.length
    ? `<table class="grid"><tbody>${lanes}</tbody></table>`
    : emptyBox('没有可画的时间线', '<div>没有运行记录。</div>');

  return `<div class="card"><h3>时间线</h3>
      <p class="hint">从 S47 投影读到的运行阶段。四段对应 排队 → 生成 → 待审 → 完成。</p>
      ${timeline}</div>
    <div class="card"><h3>交付包</h3>
      <p class="hint">S43 delivery · 包的封存与发布是两个独立动作</p>
      ${pkgBlock}</div>`;
}

/* ------------------------------------------------------------ 页面 9 成本 */

async function pageCost() {
  const [budgets, metrics] = await Promise.all([
    api('s23', '/budgets'),
    api('s44', '/metrics'),
  ]);

  const bItems = budgets.ok ? (budgets.payload.items || []) : [];
  const budgetBlock = !budgets.ok ? errorBox('S23 预算读取失败', budgets)
    : bItems.length ? `<table class="grid"><thead><tr><th>预算</th>
        <th>范围</th><th class="num">额度</th><th class="num">已预留</th>
        <th class="num">已发生</th></tr></thead><tbody>
      ${bItems.map((b) => `<tr>
        <td><b>${esc(b.budget_id)}</b></td>
        <td style="font-size:12px">${esc(b.scope_type || '')}
          <span style="color:var(--text-3)">/ ${esc(b.scope_ref || '')}</span></td>
        <td class="num">${yuan(b.limit_micro)}</td>
        <td class="num">${yuan(b.reserved_micro)}</td>
        <td class="num">${yuan(b.accrued_micro ?? b.spent_micro)}</td>
      </tr>`).join('')}</tbody></table>`
    : emptyBox('暂无预算',
        `<div>预算树还没建立。记账服务在线，但这个租户下没有预算节点。</div>
         <div style="margin-top:8px;font-size:12px">建立预算要走
         <code style="display:inline">POST /api/s23/budgets</code>，同样受缺口 G5 限制。</div>`,
        'GET /api/s23/budgets');

  const m = metrics.ok ? metrics.payload : null;
  const mList = Array.isArray(m?.metrics) ? m.metrics
    : Object.entries(m?.metrics || {}).map(([k, v]) => ({ metric_key: k, value: v }));
  const metricBlock = !metrics.ok ? errorBox('S44 指标读取失败', metrics)
    : (mList.length
      ? `<table class="grid"><thead><tr><th>指标</th><th class="num">值</th></tr></thead><tbody>
        ${mList.map((x) => `<tr>
          <td>${esc(x.metric_key || x.key || '—')}</td>
          <td class="num">${esc(x.value ?? x.metric_value ?? JSON.stringify(x))}</td>
        </tr>`).join('')}</tbody></table>
        <div style="font-size:11px;color:var(--text-3);margin-top:12px">
          data_as_of ${esc(clock(m.data_as_of))} · watermark ${num(m.projection_watermark)}</div>`
      : emptyBox('暂无指标快照',
          `<div>评测服务在线，没有已入库的指标快照。</div>
           <div style="margin-top:8px;font-size:12px">没有快照的指标不会出现在列表里，
           也不会显示成 <code style="display:inline">0</code>——那会把"没测"说成"是零"。</div>`,
          'GET /api/s44/metrics'));

  return `<div class="banner warn"><div>
      <b>金额单位：</b>接口里是 <code>micros</code>（百万分之一），页面按 ÷1,000,000 显示。
      余额不在投影里——S47 的快照只有 <code>remaining_ref</code> 指针，
      实时余额必须问 S23。</div></div>
    <div class="card"><h3>预算树</h3>
      <p class="hint">S23 budget-ledger · 唯一的账本权威</p>${budgetBlock}</div>
    <div class="card"><h3>经营指标</h3>
      <p class="hint">S44 evaluation-analytics</p>${metricBlock}</div>`;
}

/* ------------------------------------------------------------ 页面 10 管理 */

async function pageAdmin() {
  const [audit, services, operations] = await Promise.all([
    api('s45', '/audit-records'),
    api('s47', '/health/readiness'),
    api('s46', '/operations'),
  ]);

  let mounted = null;
  try {
    const r = await fetch('/services.json');
    mounted = r.ok ? (await r.json()) : null;
  } catch { /* 忽略 */ }

  const start = performance.now();
  const health = await api('s22', '/health/readiness');
  const latency = Math.round(performance.now() - start);

  const auditBlock = audit.ok
    ? ((audit.payload.items || audit.payload.records || []).length
      ? '<div style="font-size:13px;color:var(--text-2)">已读到审计记录。</div>'
      : emptyBox('审计流为空', '<div>当前没有已写入的审计记录。</div>'))
    : errorBox('S45 审计读取被拒绝（这是设计行为）', audit) + `
      <div style="font-size:12px;color:var(--text-2);margin-top:12px;line-height:1.7">
        审计读必须在有授权上下文时才能进行：服务在
        <code style="display:inline">S01_AUTHORIZATION_SOURCE</code> 未配置时
        直接 <b>fail closed</b>，而不是放行。本地预览没有接 S01，
        所以这里看到 503 是正确结果。</div>`;

  const opItems = operations.ok
    ? (operations.payload.items || operations.payload.operations || [])
    : [];

  const opBlock = !operations.ok
    ? errorBox('S46 需要 API 凭证（这是设计行为）', operations) + `
      <div style="font-size:12px;color:var(--text-2);margin-top:12px;line-height:1.7">
        开发者接口按 API key 鉴权，不认 <code style="display:inline">X-Tenant-Id</code>。
        本地预览没有签发过 API key，所以这里拿不到凭证——401 是正确结果，
        不是服务坏了。要试这条路径得先用
        <code style="display:inline">POST /api/s46/api-keys</code> 签发一把 key。</div>`
    : opItems.length ? `<table class="grid"><thead><tr><th>操作</th>
        <th>状态</th><th>创建</th></tr></thead><tbody>
      ${opItems.map((o) => `<tr>
        <td><b>${esc(o.operation_id)}</b>
          <div style="font-size:11px;color:var(--text-3)">${esc(o.kind || o.action || '')}</div></td>
        <td><span class="pill">${esc(o.state || o.status || '—')}</span></td>
        <td style="font-size:12px;color:var(--text-3)">${esc(ago(o.created_at))}</td>
      </tr>`).join('')}</tbody></table>`
    : emptyBox('暂无操作记录', '<div>没有通过开发者接口发起的操作。</div>',
        'GET /api/s46/operations');

  return `<div class="metrics">
      <div class="metric"><div class="lab">预览后端</div>
        <div class="val" style="font-size:20px">${mounted ? 'up' : '—'}</div>
        <div class="foot">单进程挂载 48 个服务</div></div>
      <div class="metric"><div class="lab">S22 健康探测</div>
        <div class="val" style="font-size:20px">${health.ok ? 'ok' : 'HTTP ' + health.status}</div>
        <div class="foot">往返 ${latency} ms</div></div>
      <div class="metric"><div class="lab">S47 健康</div>
        <div class="val" style="font-size:20px">${services.ok ? 'ok' : 'HTTP ' + services.status}</div>
        <div class="foot">realtime-projection</div></div>
      <div class="metric"><div class="lab">开发接口操作</div>
        <div class="val" style="font-size:20px">${operations.ok ? num(opItems.length) : '—'}</div>
        <div class="foot">S46 operations</div></div>
    </div>

    <div class="card" style="margin-top:24px"><h3>审计</h3>
      <p class="hint">S45 audit · 读入口要求 S01 授权上下文</p>${auditBlock}</div>

    <div class="card"><h3>开发者接口</h3>
      <p class="hint">S46 developer-integration · 幂等操作与 webhook 投递</p>
      ${opBlock}
    </div>

    <div class="card"><h3>服务目录</h3>
      <p class="hint">48 个独立业务服务，按域分组（与 layout.html 的分组一致）</p>
      <div style="font-size:12px;color:var(--text-2);line-height:1.9">
      企业治理 S01–S03 · 创作 S04–S09 · 资产与训练 S10–S15 · 跨集记忆 S16–S18 ·
      Agent 与 Prompt S19–S20 · 生产控制 S21–S28 · 生成 S29–S34 ·
      质量 S35–S39 · 审核与后期 S40–S43 · 经营 S44–S45 · 开发者与实时 S46–S47 ·
      改编训练 S48</div>
    </div>`;
}

/* ------------------------------------------------------------ 右窄栏 */

async function refreshRail() {
  const summary = document.getElementById('rail-summary');
  const alerts = document.getElementById('rail-alerts');
  const note = document.getElementById('rail-note');
  if (!summary) return;

  const [s, runs, review] = await Promise.all([
    api('s47', '/dashboard/summary'),
    api('s47', '/runs'),
    api('s40', '/review-tasks'),
  ]);

  if (!s.ok) {
    summary.innerHTML = `<div style="font-size:12px;color:var(--bad)">
      S47 不可达（HTTP ${s.status}）</div>`;
  } else {
    summary.innerHTML = `
      <div class="kv"><span>总运行</span><b>${num(s.payload.total_runs)}</b></div>
      <div class="kv"><span>进行中</span><b>${num(s.payload.active_runs)}</b></div>
      <div class="kv"><span>已完成</span><b>${num(s.payload.completed_runs)}</b></div>
      <div class="kv"><span>事件缺口</span><b style="color:${s.payload.gapped_runs
        ? 'var(--bad)' : 'inherit'}">${num(s.payload.gapped_runs)}</b></div>
      <div class="note">as_of ${esc(clock(s.payload.as_of))}</div>`;
  }

  const items = runs.ok ? (runs.payload.items || []) : [];
  const bad = items.filter((r) => ['FAILED', 'WAITING_BUDGET'].includes(r.run_state));
  const tasks = review.ok ? (review.payload.tasks || []) : [];
  const rows = [];
  for (const r of bad.slice(0, 4)) {
    rows.push(`<div class="kv"><span style="color:var(--text-2)">
      ${esc(r.run_id)}</span>
      <span class="pill ${r.run_state === 'FAILED' ? 'bad' : 'warn'}"
        style="font-size:10px">${esc(stateLabel(r.run_state))}</span></div>`);
  }
  if (tasks.length) {
    rows.push(`<div class="kv"><span>待审任务</span>
      <b>${tasks.length}</b></div>`);
  }
  alerts.innerHTML = rows.join('') ||
    '<div style="font-size:13px;color:var(--text-2)">没有需要处理的运行。</div>';

  note.innerHTML = `演示租户 <code style="font-family:var(--font-mono)">${TENANT}</code>。
    运行数据经 <code style="font-family:var(--font-mono)">POST /s47/events:consume</code>
    播种，走的是真实事件应用路径。未发生任何模型调用或付费生成。`;
}

/* ------------------------------------------------------------ 路由 */

const PAGES = [
  { id: 'overview',   group: '企业运营', label: '企业总览',       sub: '运行全景、异常与容量水位', render: pageOverview },
  { id: 'creative',   group: '制作',     label: '创作与分镜',     sub: '项目目录与剧本库',        render: pageCreative },
  { id: 'assets',     group: '制作',     label: '资产中心',       sub: '资产身份与绑定',          render: pageAssets },
  { id: 'memory',     group: '制作',     label: '跨集记忆',       sub: '知识边界与伏笔依赖',      render: pageMemory },
  { id: 'governance', group: '运行治理', label: '自动化治理',     sub: '运行权威与事件投影',      render: pageGovernance },
  { id: 'queue',      group: '运行治理', label: '生成队列',       sub: '按状态分列的调度看板',    render: pageQueue },
  { id: 'review',     group: '运行治理', label: '审片返工',       sub: '待审任务与决策',          render: pageReview },
  { id: 'delivery',   group: '运行治理', label: '时间线与交付',   sub: '阶段推进与交付包',        render: pageDelivery },
  { id: 'cost',       group: '企业运营', label: '成本与评测',     sub: '预算树与经营指标',        render: pageCost },
  { id: 'admin',      group: '企业运营', label: '管理与审计',     sub: '审计、开发者接口与服务目录', render: pageAdmin },
];

const byId = Object.fromEntries(PAGES.map((p) => [p.id, p]));

function buildNav() {
  const nav = document.getElementById('nav');
  let html = '', last = null;
  for (const p of PAGES) {
    if (p.group !== last) { html += `<div class="nav-group">${esc(p.group)}</div>`; last = p.group; }
    html += `<a class="nav-item" href="#/${p.id}" data-page="${p.id}">${esc(p.label)}</a>`;
  }
  nav.innerHTML = html;
}

async function route() {
  const id = (location.hash.replace(/^#\/?/, '') || 'overview').split('?')[0];
  const page = byId[id] || byId.overview;

  document.querySelectorAll('.nav-item').forEach((a) =>
    a.classList.toggle('is-active', a.dataset.page === page.id));
  document.getElementById('page-title').textContent = page.label;
  document.getElementById('page-sub').textContent = page.sub;

  const content = document.getElementById('content');
  content.innerHTML = loading(page.label);
  try {
    content.innerHTML = await page.render();
  } catch (err) {
    content.innerHTML = `<div class="error"><strong>页面渲染失败</strong>
      <code>${esc(err && err.stack ? err.stack : String(err))}</code></div>`;
  }
  content.scrollTop = 0;
  bindActions();
}

/* 审片决定按钮：点下去走真实 POST，并把结果如实回显。

   请求形状对齐 S40 的 `submit_decision`，不是我自己想的：
   - `Idempotency-Key` 头是必需的（服务端 `Header(alias=...)`，缺了直接 422），
     且必须等于 body 里的 `command_id`，不一致 400。
   - **`decision` 只有契约三值**（accept / reject / request_repair），
     按钮上的扩展选项走 `option_id`（C40-1）。`replace` 的映射有测试依据：
     `decision=reject` + `option_id=replace`（test_contract_decisions_reject_extended_values）。
   - 决策人放 `actor.principal_id`，不是顶层。
   - `expected_task_revision` 做乐观并发：页面加载后任务若已推进，
     服务端 409 TASK_SUPERSEDED，而不是把旧判断写进去。

   **扩展选项只映射有据可依的那些。** `replace` → `reject` 有测试锁定；
   `change_spec` / `exception_accept` 该配哪个契约值，服务端没有任何测试或
   文档说死。硬配一个就是替服务端许下它没承诺的语义（S02 会据此判事），
   所以这两颗按钮如实显示"未接"，而不是发一个猜出来的 decision。 */
const EXTENDED_DECISION = { replace: 'reject' };  // 有测试依据，见上
const CONTRACT_DECISIONS = ['accept', 'reject', 'request_repair'];

function decisionFor(option) {
  if (CONTRACT_DECISIONS.includes(option)) return option;
  return EXTENDED_DECISION[option] || null;
}

function bindActions() {
  document.querySelectorAll('[data-decide]').forEach((btn) => {
    btn.addEventListener('click', async () => {
      const id = btn.dataset.decide;
      const option = btn.dataset.option;
      const decision = decisionFor(option);
      const original = btn.textContent;

      if (!decision) {
        btn.outerHTML = `<span class="pill warn" title="服务端没有定义这个选项对应哪个契约 decision">
          ${esc(option)} · 界面未接</span>`;
        return;
      }

      const reason = (document.querySelector(
        `[data-reason="${CSS.escape(id)}"]`)?.value || '').trim();
      if (reason.length < 4) {
        btn.outerHTML = `<span class="pill warn" title="服务端要求理由至少 4 个字">
          先填理由再提交（≥4 字）</span>`;
        return;
      }

      btn.disabled = true;
      btn.textContent = '提交中…';
      const commandId = `dec-${id}-${option}-${Date.now()}`;
      const res = await api('s40',
        `/review-tasks/${encodeURIComponent(id)}/decisions`, {
          method: 'POST',
          headers: { 'Idempotency-Key': commandId },
          body: {
            command_id: commandId,
            decision,
            option_id: option,
            reason,
            actor: { principal_id: PRINCIPAL },
            expected_task_revision: Number(btn.dataset.revision),
          },
        });
      btn.disabled = false;
      btn.textContent = original;
      if (res.ok) {
        btn.outerHTML = `<span class="pill good">已提交 ${esc(option)}</span>`;
        setTimeout(route, 800);
      } else {
        const code = res.payload?.code || res.payload?.detail?.code || '';
        const msg = res.payload?.message || '';
        btn.outerHTML = `<span class="pill bad" title="${esc(JSON.stringify(res.payload))}">
          HTTP ${res.status}${code ? ' · ' + esc(code) : ''}</span>`;
        if (msg) btn.title = msg;
      }
    });
  });
}

async function init() {
  buildNav();
  document.getElementById('reload').addEventListener('click', () => { route(); refreshRail(); });
  window.addEventListener('hashchange', route);
  await route();
  await refreshRail();
  setInterval(() => { refreshRail(); }, 30000);
}

init();
