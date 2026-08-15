/* 信衡 CreditSentry 工作台 · 前端
 *
 * 设计取向：每一块 UI 都要回答一个业务问题，而不是「把 JSON 显示出来」。
 *   · 证据表要能点开看到哈希与来源 —— 「结论有据可查」不能只是一句话
 *   · 对抗视图必须并排 —— 定性与质疑的分歧本身就是要展示的东西
 *   · 闸门要显示四个维度和命中的规则号 —— 「为什么是 L2」比「是 L2」重要
 *   · 审批要真的挡住流程 —— 一个自动放过去的审批，等于没有演示过审批
 *
 * 零构建、零依赖，原生 ES 模块之外的东西一概不用，与项目其余部分一致。
 */

'use strict';

const S = {
  boot: null,        // /api/bootstrap
  caseKey: null,
  runId: null,
  es: null,          // EventSource
  snapshot: null,
  spans: new Map(),  // span_id → {el, depth}
  evidence: new Map(),
  pendingApproval: null,
  t0: 0,
  timer: null,
  queue: [],         // 待播放的事件
  pumping: false,
  pace: 'talk',
  tools: 0,          // 已完成的工具调用次数（活动条计数）
  backendMs: null,   // 后端真实耗时，与播放时长分开显示
};

/* Agent 与工具的中文名。界面上不出现 kebab-case 的 id 与 server.tool 字面量——
 * 看的人关心的是「谁在做什么」，不是内部标识符。 */
const AGENT_CN = {
  'risk-commander': '风险指挥官',
  'signal-hub': '信号聚合官',
  'due-diligence': '尽调取证官',
  'risk-analyst': '风险定性官',
  'devils-advocate': '对抗质疑官',
  'disposition-executor': '处置执行官',
  'compliance-auditor': '合规审计官',
};

const TOOL_CN = {
  'get_facility': '调取授信额度明细',
  'get_exposure': '测绘敞口与关联主体',
  'get_collateral': '核对抵质押物状态',
  'get_guarantee_ledger': '调取对外担保台账',
  'get_credit_report': '调取征信报告',
  'diff_report': '比对征信期间变动',
  'get_query_history': '查征信被查询记录',
  'search_litigation': '检索涉诉案件',
  'get_judgment_doc': '调取裁判文书原文',
  'get_business_registration': '查工商登记信息',
  'get_change_history': '查工商变更历史',
  'query_transactions': '拉取账户流水明细',
  'get_counterparty_summary': '按对手方汇总资金往来',
  'get_flow_pattern': '识别资金异常模式',
  'adjust_limit': '调整授信额度',
  'add_guarantee': '追加担保要求',
  'rollback_adjustment': '回滚额度调整',
};

const SKILL_CN = {
  SignalFusion: '归并多源预警信号', ExposureMapping: '测绘敞口影响面',
  CreditReportProbe: '解析征信报告', LitigationProbe: '核查涉诉与工商',
  TxnFlowAnalyze: '分析资金流水', GuaranteeProbe: '核查对外担保',
  EvidenceLedger: '登记证据', QueryRewrite: '六维查询改写',
  PolicyRag: '召回监管与行内条款', CaseMemory: '召回相似历史案例',
  RiskRootCause: '风险根因归因与定性', RiskGate: '按四个维度给处置定档',
  SafeDisposition: '执行处置动作', ComplianceCheck: '逐条校验合规项',
  PostmortemDistill: '沉淀风险模式', ReportCompose: '生成成文报告',
};

/* 演示节拍。后端跑完一个案件通常不到 1 秒——全速播放等于没有过程。
 * 节流只作用于**播放**，不影响后端处置本身，顶栏耗时始终显示真实值。 */
const PACE = {
  talk:  { phase: 700, progress: 520, agent: 260, llm: 300, mcp: 110, skill: 90, other: 12 },
  brisk: { phase: 320, progress: 240, agent: 120, llm: 140, mcp: 50,  skill: 40, other: 6 },
  raw:   { phase: 0, progress: 0, agent: 0, llm: 0, mcp: 0, skill: 0, other: 0 },
};

const PHASES = [
  ['INTAKE', '受理与信号归并', '多源预警归并去重、压降噪声'],
  ['EVIDENCE', '尽调取证', '征信/司法/工商/流水取原文并登记证据'],
  ['ADJUDICATION', '定性与质疑', '定性官与质疑官并行，目标函数刻意对立'],
  ['DISPOSITION', '分级处置', '四维闸门定级 L0–L3，必要时等人工审批'],
  ['AUDIT', '合规审计', '核验处置是否生效、逐条校验监管合规项'],
  ['CLOSED', '闭环', '案件归档，经验沉淀入案例库'],
];

/* ────────────────── 工具 ────────────────── */
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const money = (n) => n == null ? '—' :
  (Math.abs(n) >= 1e8 ? (n / 1e8).toFixed(2) + ' 亿元'
    : Math.abs(n) >= 1e4 ? (n / 1e4).toLocaleString('zh-CN', { maximumFractionDigits: 0 }) + ' 万元'
      : n.toLocaleString('zh-CN') + ' 元');
const pct = (x) => x == null ? '—' : (x * 100).toFixed(1) + '%';

/** 把正文里的 [EV-xxxx-xxxx] 变成可点击的证据角标。
 *  这是「结论挂载可溯源证据」在界面上的落点：角标不是装饰，点开必须有东西。 */
function withEvRefs(text) {
  return esc(text).replace(/\[?(EV-[0-9A-Za-z]+-\d+)\]?/g,
    (_, id) => `<span class="evref" onclick="showEvidence('${id}')">${id}</span>`);
}

function api(path, opts) {
  return fetch(path, Object.assign({ headers: { 'Content-Type': 'application/json' } }, opts))
    .then((r) => r.json());
}

/* ────────────────── 启动 ────────────────── */
async function boot() {
  S.boot = await api('/api/bootstrap');
  renderCases();
  renderPresets();
  renderTeam();
  renderPipeline(null);

  $('opt-mode').onchange = (e) => {
    const live = e.target.value === 'live';
    $('preset-field').hidden = !live;
    $('cred-field').hidden = !live;
    if (live) loadCredentials();
  };
  $('opt-preset').onchange = loadCredentials;
  $('btn-cred-save').onclick = saveAndTestCredentials;
  $('btn-run').onclick = startRun;
  $('btn-probe').onclick = probe;
  document.querySelectorAll('.tab').forEach((t) => {
    t.onclick = () => {
      document.querySelectorAll('.tab').forEach((x) => x.classList.remove('active'));
      document.querySelectorAll('.tabpane').forEach((x) => x.classList.remove('active'));
      t.classList.add('active');
      $('pane-' + t.dataset.tab).classList.add('active');
    };
  });
}

function renderCases() {
  $('case-list').innerHTML = S.boot.cases.map((c) => `
    <button class="case-card" data-key="${c.key}" onclick="selectCase('${c.key}')">
      <div class="cc-top">
        <span class="cc-key">${c.key}</span>
        <span class="cc-pri ${c.priority === '高' ? '' : 'mid'}">${c.priority}优先</span>
      </div>
      <div class="cc-title">${esc(c.title)}</div>
      <div class="cc-brief">${esc(c.brief)}</div>
      <div class="cc-expect">预期结论　${esc(c.expect)}</div>
    </button>`).join('');
}

function renderPresets() {
  $('opt-preset').innerHTML = '<option value="">（权限矩阵默认绑定）</option>' +
    S.boot.presets.map((p) => `<option value="${p.name}">${p.name} · ${esc(p.desc)}</option>`).join('');
}

function selectCase(key) {
  S.caseKey = key;
  document.querySelectorAll('.case-card').forEach((el) =>
    el.classList.toggle('on', el.dataset.key === key));
  const c = S.boot.cases.find((x) => x.key === key);
  renderFactors(c);
  $('btn-run').disabled = false;
  $('run-note').textContent = `将处置 ${key} · ${c.title}`;
}

/** 因子调节面板。可改字段是白名单里被逐条说清楚的那几个，
 *  不是任意改写案件数据——递给评委的是一张「可改字段卡片」。 */
function renderFactors(c) {
  const p = $('factor-panel');
  if (!c.factors || !c.factors.length) { p.hidden = true; return; }
  p.hidden = false;
  $('factor-list').innerHTML = c.factors.map((f) => `
    <div class="factor" data-key="${f.key}">
      <div class="factor-h"><b>${esc(f.label)}</b><span class="chg" hidden>已改动</span></div>
      <div class="factor-why">${esc(f.why)}</div>
      <select onchange="onFactorChange(this)">
        ${f.options.map((o, i) => `<option value="${i}">${esc(o.name)}</option>`).join('')}
      </select>
      <div class="factor-expect">${esc(f.options[0].expect)}</div>
      <div class="factor-bind">口径　${esc(f.binding)}</div>
    </div>`).join('');
}

function onFactorChange(sel) {
  const box = sel.closest('.factor');
  const c = S.boot.cases.find((x) => x.key === S.caseKey);
  const f = c.factors.find((x) => x.key === box.dataset.key);
  const idx = +sel.value;
  box.querySelector('.chg').hidden = idx === 0;
  box.querySelector('.factor-expect').textContent = f.options[idx].expect;
}

/* ────────────────── 模型凭证 ────────────────── */
/* 让人在界面上填 API Key，而不是「先去改 shell 再重启服务」。
 * 但约束不变：值只进本进程内存，配置文件里写的仍然是 env: 引用。 */
async function loadCredentials() {
  const preset = $('opt-preset').value || '';
  const r = await api('/api/credentials?preset=' + encodeURIComponent(preset));
  if (r.error) {
    $('cred-list').innerHTML = `<div class="credrow bad">${esc(r.error)}</div>`;
    return;
  }
  if (r.single) return renderSinglePlatform(r);
  if (r.custom) return renderCustomEndpoints(r.roles);
  // 面板必须自报「我现在反映的是哪一套绑定」。不写清楚的话，
  // 看到的是权限矩阵里那套**生产默认**（质疑官绑豆包），
  // 而使用者以为自己在看刚选的预设——两边对不上，就会以为是文档写错了
  const presetName = preset || '';
  const header = presetName
    ? `<div class="credrow">当前预设 <b>${esc(presetName)}</b>，下面是它需要的凭证。</div>`
    : `<div class="credrow warn">当前是<b>「权限矩阵默认绑定」</b>——这是<b>生产上</b>的配置
       （定性用百炼国内站、质疑用火山方舟豆包），未必是你手上的 Key。<br>
       本机跑通请在上方<b>「模型预设」</b>里按你有的 Key 选一个。</div>`;
  $('cred-list').innerHTML = header + r.items.map((c) => {
    const ready = c.source !== 'none';
    const from = c.source === 'runtime' ? '已在本页填入'
      : c.source === 'env' ? '来自环境变量' : '未设置';
    return `<div class="cred" data-env="${esc(c.env_name || '')}">
      <div class="cred-h">
        <b>${esc(c.role_label || c.used_by)}</b>
        <span class="st ${ready || !c.needed ? 'ok' : 'no'}">${c.needed ? from : '无需鉴权'}</span>
      </div>
      <div class="cred-sub">
        <span class="mono">${esc(c.model)}</span>　族 ${esc(c.family)}<br>
        <span class="mono" style="font-size:10.5px">${esc(c.base_url)}</span>
      </div>
      ${c.tip ? `<div class="cred-tip">${esc(c.tip)}${c.console
        ? `<br><a href="${esc(c.console)}" target="_blank" rel="noopener">${esc(c.console)}</a>` : ''}</div>` : ''}
      ${c.needed ? `<input type="password" autocomplete="off" spellcheck="false"
        placeholder="${ready ? '已就绪，留空则沿用' : '粘贴 ' + esc(c.env_name)}">` : ''}
    </div>`;
  }).join('');
}

/** 「只用一个平台」面板。
 *
 *  绝大多数人手上只有一个平台的账号。而两个角色要用不同技术路线的模型——
 *  这不等于要两个平台：同一个平台上往往挂着好几家的模型。
 *  难点在于**使用者不知道自己账号开通了哪些**，拿到 404 也判断不出是
 *  「名字写错」还是「没开通」。所以这里不让他猜，直接探测。 */
function renderSinglePlatform(r) {
  const cfg = (r.configured || {})['risk-analyst'] || {};
  $('cred-list').innerHTML = `
    <div class="credrow">选平台、填一个 Key，然后点<b>检测可用模型</b>。
      系统会告诉你这个账号能用哪些模型、够不够凑出两条技术路线。</div>
    <div class="cred" id="single-box">
      <div class="cred-h"><b>服务平台</b></div>
      <select class="c-url-pick" onchange="onEndpointPick(this)">
        <option value="">— 选择平台 —</option>
        ${(r.endpoints || []).map((e) => `<option value="${esc(e.base_url)}"
          ${e.base_url === (r.base_url || cfg.base_url) ? 'selected' : ''}
          data-hint="${esc(e.hint || '')}">${esc(e.label)}</option>`).join('')}
        <option value="__other__">其他（手动填服务地址）</option>
      </select>
      <div class="cred-tip" id="single-hint">${esc(
        (r.endpoints || []).find((e) => e.base_url === (r.base_url || cfg.base_url))?.hint || '')}</div>
      <input class="c-url" type="text" spellcheck="false" autocomplete="off"
             placeholder="服务地址，写到 /v1 这一层"
             value="${esc(r.base_url || cfg.base_url || '')}">
      <input class="c-key" type="password" spellcheck="false" autocomplete="off"
             placeholder="${r.key_ready ? 'Key 已就绪，留空则沿用' : '粘贴 API Key'}">
      <div class="cred-actions" style="margin-top:8px">
        <button class="btn btn-sm btn-primary" id="btn-discover"
                onclick="discoverModels()">检测可用模型</button>
      </div>
      <div id="discover-result"></div>
    </div>`;
}

function onEndpointPick(sel) {
  const box = sel.closest('.cred');
  const opt = sel.selectedOptions[0];
  $('single-hint').textContent = (opt && opt.dataset.hint) || '';
  if (sel.value && sel.value !== '__other__') box.querySelector('.c-url').value = sel.value;
  else if (sel.value === '__other__') box.querySelector('.c-url').focus();
}

async function discoverModels() {
  const box = $('single-box');
  const base = box.querySelector('.c-url').value.trim();
  const key = box.querySelector('.c-key').value.trim();
  if (!base) { $('discover-result').innerHTML =
    '<div class="credrow bad">请先选择或填写服务地址</div>'; return; }

  const btn = $('btn-discover');
  btn.disabled = true; btn.textContent = '检测中…（约 10 秒）';
  const d = await api('/api/credentials/discover', {
    method: 'POST', body: JSON.stringify({ base_url: base, api_key: key }) });
  btn.disabled = false; btn.textContent = '重新检测';

  if (d.error) { $('discover-result').innerHTML =
    `<div class="credrow bad">${esc(d.error)}</div>`; return; }

  if (d.auth_failed) {
    $('discover-result').innerHTML = `<div class="credrow bad">
      <b>这个 Key 在该平台上不被接受</b>，${d.tried} 个模型全部鉴权失败。<br>
      最常见的原因是<b>选错了地域</b>——百炼的国际站与中国大陆站互不相通，
      Key 不能跨站使用。核对一下你登录的控制台域名再换一个平台试试。</div>`;
    return;
  }
  if (!d.available.length) {
    $('discover-result').innerHTML = `<div class="credrow bad">
      连上了，但 ${d.tried} 个候选模型都不可用。去控制台的「模型广场」开通至少两个
      <b>不同来源</b>的模型再试。</div>`;
    return;
  }

  const byFam = {};
  d.available.forEach((m) => { (byFam[m.family] = byFam[m.family] || []).push(m.model); });
  const list = Object.entries(byFam).map(([f, ms]) =>
    `<div class="fam-row"><span class="fam">${esc(f)}</span>
      <span>${ms.map((m) => `<span class="mono">${esc(m)}</span>`).join('、')}</span></div>`).join('');

  const s = d.suggestion;
  $('discover-result').innerHTML = `
    <div class="credrow ${d.can_pair ? 'ok' : 'warn'}">
      检测到 <b>${d.available.length}</b> 个可用模型，分属 <b>${Object.keys(byFam).length}</b> 条技术路线。
    </div>
    <div class="famlist">${list}</div>
    ${d.can_pair ? `
      <div class="credrow ok">建议这样分工，<b>一个 Key 就够</b>：<br>
        风险定性岗　<span class="mono">${esc(s.analyst.model)}</span>（${esc(s.analyst.family)}）<br>
        对抗质疑岗　<span class="mono">${esc(s.advocate.model)}</span>（${esc(s.advocate.family)}）</div>
      <button class="btn btn-sm btn-ok" style="margin-top:8px"
        onclick='applySingle(${JSON.stringify({ base_url: base, pair: s })
          .replace(/'/g, "&#39;")})'>就按这个配置</button>`
      : `<div class="credrow warn">
        <b>只检测到一条技术路线（${esc(Object.keys(byFam)[0])}）。</b>
        风险定性岗与对抗质疑岗必须用不同路线的模型——两边同源的话，
        「唱反调」就成了自说自话，系统会拒绝启动。<br><br>
        两个办法：<b>①</b> 去这个平台的「模型广场」再开通一个别家的模型
        （百炼上就挂着 DeepSeek 系列），开通后回来重新检测；
        <b>②</b> 换用需要两个 Key 的方案，或装个本地 Ollama 补第二条路线。</div>`}`;
}

async function applySingle(payload) {
  const key = $('single-box').querySelector('.c-key').value.trim();
  const r = await api('/api/credentials/apply-single', {
    method: 'POST', body: JSON.stringify({ ...payload, api_key: key }) });
  $('single-box').querySelector('.c-key').value = '';
  if (r.error) {
    $('cred-result').innerHTML = `<div class="credrow bad">${esc(r.error)}</div>`;
    return;
  }
  $('cred-result').innerHTML = renderTestResult(r, true);
}


/** 连接测试结果。
 *
 *  单看每一行的报错，最自然的推论是「Key 有问题」——毕竟两行用的是同一个 Key。
 *  但同一个 Key 通过了第一行的鉴权，就说明密钥有效，第二行失败只可能是
 *  **那个模型**或**那个平台**的问题。这个推论要跨行才成立，所以由服务端
 *  给出结论（diagnosis），而不是让使用者自己拼。 */
function renderTestResult(t, byRole) {
  if (t.error) return `<div class="credrow bad">${esc(t.error)}</div>`;
  const rows = (t.results || []).map((r) => `
    <div class="credrow ${r.ok ? 'ok' : 'bad'}">
      <b>${r.ok ? '✓' : '✗'} ${byRole ? esc(AGENT_CN[r.agent] || r.agent) + '　' : ''}${esc(r.model)}</b>
      ${r.latency_ms ? `<span class="muted">　${r.latency_ms} ms</span>` : ''}
      <br>${esc(r.detail)}</div>`).join('');
  const diag = t.diagnosis
    ? `<div class="credrow warn">${bold(t.diagnosis)}</div>` : '';
  const hetero = (t.results && t.results.length && !t.heterogeneous)
    ? `<div class="credrow bad">两个岗位落在同一条技术路线上，会被拒绝启动——
       换一个模型方案，或让其中一个改用别家的模型。</div>` : '';
  const done = t.ok ? '<div class="credrow ok"><b>配置完成，可以开始处置了。</b></div>' : '';
  return rows + diag + hetero + done;
}

/** 只认 **粗体** 这一种标记。诊断文案里需要强调「不是密钥的问题」，
 *  其余一律转义，避免服务端文案变成注入点。 */
function bold(s) {
  return esc(s).replace(/\*\*(.+?)\*\*/g, '<b>$1</b>');
}

/** 自定义端点表单。
 *  只给 API Key 的输入框是不够的——base_url 决定请求发到哪里，
 *  端点对不上，key 填得再对也调不通。 */
function renderCustomEndpoints(roles) {
  $('cred-list').innerHTML = `
    <div class="credrow">填入任意 <b>OpenAI 兼容</b>端点即可。
      base_url 要写到 <span class="mono">/v1</span> 这一层，系统会自己接
      <span class="mono">/chat/completions</span>。</div>
    ` + roles.map((c) => `
    <div class="cred custom" data-role="${c.role}">
      <div class="cred-h">
        <b>${esc(c.label)}</b>
        <span class="st ${c.configured && c.source !== 'none' ? 'ok' : 'no'}">
          ${c.configured && c.source !== 'none' ? '已配置' : '待配置'}</span>
      </div>
      <div class="cred-sub">${esc(c.hint)}</div>
      <input class="c-url" type="text" placeholder="base_url，如 https://api.deepseek.com/v1"
             value="${esc(c.base_url || '')}" autocomplete="off" spellcheck="false">
      <input class="c-model" type="text" placeholder="模型名，如 deepseek-chat"
             value="${esc(c.model || '')}" autocomplete="off" spellcheck="false">
      <div class="c-row">
        <input class="c-family" type="text" placeholder="模型族（留空自动推断）"
               value="${esc(c.family || '')}" autocomplete="off" spellcheck="false">
        <select class="c-res">
          <option value="cn"${c.data_residency === 'cn' ? ' selected' : ''}>境内</option>
          <option value="local"${c.data_residency === 'local' ? ' selected' : ''}>本地</option>
          <option value="overseas"${c.data_residency === 'overseas' ? ' selected' : ''}>境外</option>
        </select>
      </div>
      <input class="c-key" type="password" autocomplete="off" spellcheck="false"
             placeholder="${c.source !== 'none' ? 'Key 已就绪，留空则沿用' : 'API Key'}">
    </div>`).join('') + `
    <div class="credrow">两侧<b>必须用不同技术路线的模型</b>——分成两个岗位就是为了避免自说自话，
      同族权重会让对抗在权重层面坍缩，配成同族会被拒绝启动。<br>
      数据存放地选「境外」时，唯一能接触个人敏感信息的岗位会被拦下，这是设计如此。</div>`;
}

async function saveAndTestCredentials() {
  const btn = $('btn-cred-save');
  btn.disabled = true; btn.textContent = '测试中…';
  const values = {};
  const endpoints = {};
  document.querySelectorAll('#cred-list .cred').forEach((box) => {
    if (box.classList.contains('custom')) {
      const v = (s) => (box.querySelector(s) || {}).value || '';
      endpoints[box.dataset.role] = {
        base_url: v('.c-url').trim(), model: v('.c-model').trim(),
        family: v('.c-family').trim(), data_residency: v('.c-res'),
        api_key: v('.c-key').trim(),
      };
      return;
    }
    const input = box.querySelector('input');
    if (input && input.value.trim()) values[box.dataset.env] = input.value.trim();
  });
  const preset = $('opt-preset').value || null;
  const saved = await api('/api/credentials',
    { method: 'POST', body: JSON.stringify({ preset, values, endpoints }) });
  if (saved.error) {
    $('cred-result').innerHTML = `<div class="credrow bad">${esc(saved.error)}</div>`;
    btn.disabled = false; btn.textContent = '保存并测试连接';
    return;
  }
  // 密钥填过就清空：它没有理由继续留在 DOM 里。
  // base_url 与模型名不是秘密，留着方便改
  document.querySelectorAll('#cred-list input[type=password]').forEach((i) => { i.value = ''; });

  const t = await api('/api/credentials/test', { method: 'POST', body: JSON.stringify({ preset }) });
  $('cred-result').innerHTML = renderTestResult(t, false);
  await loadCredentials();
  btn.disabled = false; btn.textContent = '保存并测试连接';
}

function collectFactors() {
  const out = {};
  document.querySelectorAll('#factor-list .factor').forEach((b) => {
    out[b.dataset.key] = +b.querySelector('select').value;
  });
  return out;
}

/* ────────────────── 运行 ────────────────── */
async function startRun() {
  if (!S.caseKey) return;
  resetStage();
  $('btn-run').disabled = true;
  $('run-note').textContent = '处置中…';

  S.pace = $('opt-pace').value;
  const body = {
    case: S.caseKey,
    llm_mode: $('opt-mode').value,
    preset: $('opt-preset').value || null,
    approval_mode: $('opt-approval').value,
    factors: collectFactors(),
  };
  const r = await api('/api/runs', { method: 'POST', body: JSON.stringify(body) });
  if (r.error) { fail(r.error); return; }

  S.runId = r.run_id;
  S.t0 = Date.now();
  // 刻意不在运行中把「播放已进行多久」显示成耗时——那会把演示节奏
  // 伪装成后端性能。真实耗时来自 Trace 根 Span，跑完才有，跑完才显示。
  $('s-elapsed').textContent = '处置中…';
  $('s-mode').textContent = body.llm_mode === 'live'
    ? '大模型' : '规则推理';
  $('btn-probe').disabled = false;

  S.es = new EventSource(`/api/runs/${r.run_id}/stream?from=0`);
  S.es.onmessage = (e) => enqueue(JSON.parse(e.data));
  S.es.onerror = () => { /* 服务端结束流会触发一次，属正常 */ };
}

function resetStage() {
  if (S.es) { S.es.close(); S.es = null; }
  if (S.timer) { clearInterval(S.timer); S.timer = null; }
  S.spans.clear(); S.evidence.clear(); S.snapshot = null; S.pendingApproval = null;
  S.queue = []; S.pumping = false; S.tools = 0; S.backendMs = null;
  $('cards').innerHTML = '';
  $('trace').innerHTML = '';
  $('logs').innerHTML = '';
  $('audit').innerHTML = '';
  $('approval-banner').hidden = true;
  $('activity').hidden = true;
  $('activity').classList.remove('idle');
  $('act-tools').textContent = '0';
  $('act-ev').textContent = '0';
  ['s-token', 's-cost', 's-elapsed'].forEach((i) => $(i).textContent = '—');
  renderPipeline(null);
}

/* 事件先入队，再按节奏播放。
 * 后端全速跑（真实性能不受影响），前端负责让人看得清——两件事分开。 */
function enqueue(ev) {
  S.queue.push(ev);
  if (!S.pumping) pump();
}

function pump() {
  const ev = S.queue.shift();
  if (!ev) { S.pumping = false; setActivityIdle(); return; }
  S.pumping = true;
  try { dispatch(ev); } catch (e) { console.error(e); }
  // 审批是硬停顿：等人点击期间不消费后续事件，也不需要节流
  const d = S.pendingApproval ? 0 : delayFor(ev);
  if (d > 0) setTimeout(pump, d); else pump();
}

function delayFor(ev) {
  const p = PACE[S.pace] || PACE.talk;
  if (ev.kind === 'phase') return p.phase;
  if (ev.kind === 'progress') return p.progress;
  if (ev.kind === 'span_start') {
    const k = ev.span.kind;
    if (k === 'agent') return p.agent;
    if (k === 'llm') return p.llm;
    if (k === 'mcp') return p.mcp;
    if (k === 'skill' || k === 'rag') return p.skill;
  }
  return p.other;
}

function dispatch(ev) {
  switch (ev.kind) {
    case 'run_started': onRunStarted(ev); break;
    case 'span_start': addSpan(ev.span); onActivity(ev.span); break;
    case 'span_end': endSpan(ev); break;
    case 'log': addLog(ev.record); break;
    case 'progress': onProgress(ev); break;
    case 'phase': onPhase(ev); break;
    case 'approval_required': onApprovalRequired(ev.request, ev.snapshot); break;
    case 'approval_decided': $('approval-banner').hidden = true; break;
    case 'done': onDone(ev.snapshot); break;
    case 'error': fail(ev.message, ev.traceback); break;
  }
}

/* ────────────────── 活动条 ────────────────── */
function onActivity(sp) {
  const box = $('activity');
  box.hidden = false;
  box.classList.remove('idle');
  if (sp.kind === 'agent') {
    $('act-agent').textContent = AGENT_CN[sp.name] || sp.name;
    $('act-what').textContent = '开始工作…';
    return;
  }
  let what = null;
  if (sp.kind === 'mcp') {
    const tool = sp.name.split('.').pop();
    what = '正在' + (TOOL_CN[tool] || tool);
  } else if (sp.kind === 'skill') {
    what = '正在' + (SKILL_CN[sp.name] || sp.name);
  } else if (sp.kind === 'llm') {
    what = sp.name === 'risk_root_cause' ? '正在做根因归因与五级分类判断'
      : sp.name === 'devils_advocate' ? '正在逐条证伪定性方的每个主因' : '正在推理';
  } else if (sp.kind === 'routing') {
    what = '路由决策：' + sp.name;
  } else if (sp.kind === 'adjudication') {
    what = '裁决：' + sp.name;
  } else if (sp.kind === 'approval') {
    what = '等待人工审批';
  } else if (sp.kind === 'execution') {
    what = '执行处置动作';
  }
  if (what) $('act-what').textContent = what;
  if (sp.kind === 'mcp') {
    S.tools += 1;
    $('act-tools').textContent = S.tools;
  }
}

function setActivityIdle() {
  const box = $('activity');
  if (box.hidden) return;
  box.classList.add('idle');
  if (S.pendingApproval) {
    $('act-agent').textContent = '已暂停';
    $('act-what').textContent = '等待你的审批决策';
  } else if (S.snapshot && ['CLOSED', 'EVIDENCE_GAP'].includes(S.snapshot.phase)) {
    $('act-agent').textContent = '已完成';
    $('act-what').textContent = S.snapshot.phase === 'EVIDENCE_GAP'
      ? '取证不足，已输出取证清单转人工' : '案件闭环，产出已落盘 poc/out/';
  }
}

/* 某个 Worker 干完就刷新一次卡片流——这样界面是随处置过程长出来的，
   而不是等阶段结束一次性刷出来 */
function onProgress(ev) {
  S.snapshot = ev.snapshot;
  $('act-what').textContent = (AGENT_CN[ev.agent] || ev.agent) + ' 已完成';
  $('act-ev').textContent = (ev.snapshot.evidence.items || []).length;
  render();
}

function onRunStarted(ev) {
  const changed = (ev.factors || []).filter((f) => f.changed);
  if (!changed.length) return;
  // 因子被改动过就显式说明，避免「跑出来的结论」和「原始案件」被混为一谈
  $('cards').insertAdjacentHTML('beforeend', card('因子已调整', '本次运行不是原始案件数据', `
    <div class="notice warn">
      以下决定性因子被改动，结论应当随之改变。若结论不变，说明它并非由该因子驱动。
    </div>
    <table class="grid" style="margin-top:10px">
      <tr><th>因子</th><th>取值</th><th>预期影响</th></tr>
      ${changed.map((f) => `<tr><td><b>${esc(f.label)}</b></td>
        <td>${esc(f.option)}</td><td class="muted">${esc(f.expect)}</td></tr>`).join('')}
    </table>`, 'alert'));
}

function onPhase(ev) {
  S.snapshot = ev.snapshot;
  renderPipeline(ev.phase);
  render();
}

function onDone(snap) {
  S.snapshot = snap;
  if (S.timer) { clearInterval(S.timer); S.timer = null; }
  const ms = (snap.metrics || {}).end_to_end_ms;
  $('s-elapsed').textContent = ms == null ? '—'
    : (ms >= 1000 ? (ms / 1000).toFixed(2) + ' s' : Math.round(ms) + ' ms');
  $('act-ev').textContent = (snap.evidence.items || []).length;
  renderPipeline(snap.phase, true);
  render();
  setActivityIdle();
  $('btn-run').disabled = false;
  $('run-note').textContent = snap.phase === 'EVIDENCE_GAP'
    ? '案件转人工：取证清单已输出' : '处置闭环完成';
  if (S.es) { S.es.close(); S.es = null; }
}

function fail(msg, tb) {
  $('cards').insertAdjacentHTML('beforeend', card('运行失败', '', `
    <div class="notice danger">${esc(msg)}</div>
    ${tb ? `<pre>${esc(tb)}</pre>` : ''}`, 'alert'));
  $('btn-run').disabled = false;
  $('run-note').textContent = '运行失败';
  if (S.timer) { clearInterval(S.timer); S.timer = null; }
  // 必须显式关闭：EventSource 断开后会自动无限重连，
  // 而这条流已经不会再有新事件了，留着只会一直重试
  if (S.es) { S.es.close(); S.es = null; }
}

// 前端出错要看得见。一个白屏的演示界面比一个报错的演示界面糟糕得多——
// 前者让人以为是系统卡住了，后者至少指明了去哪儿看。
window.addEventListener('error', (e) => {
  const box = $('cards');
  if (box) box.insertAdjacentHTML('afterbegin', card('界面渲染异常', '',
    `<div class="notice danger">${esc(e.message)}<br>
     <span class="mono">${esc((e.filename || '').split('/').pop())}:${e.lineno}</span></div>
     <div class="notice">案件处置本身在后端独立运行，不受此影响；
       产出仍会完整落到 <span class="mono">poc/out/</span>。</div>`, 'alert'));
});

/* ────────────────── 阶段流水线 ────────────────── */
function renderPipeline(current, finished) {
  const gap = current === 'EVIDENCE_GAP';
  const order = PHASES.map((p) => p[0]);
  const curIdx = order.indexOf(current);
  $('pipeline').innerHTML = PHASES.map(([key, name, sub], i) => {
    let cls = 'idle';
    if (current) {
      if (gap) cls = i <= 1 ? 'done' : 'idle';
      else if (i < curIdx) cls = 'done';
      else if (i === curIdx) cls = finished ? 'done' : 'now';
    }
    return `<div class="pstep ${cls}">
      <div class="pstep-n">${String(i + 1).padStart(2, '0')} · ${key}</div>
      <div class="pstep-t">${name}</div>
      <div class="pstep-s">${cls === 'now' ? '进行中' : cls === 'done' ? '已完成' : sub}</div>
    </div>`;
  }).join('') + (gap ? `<div class="pstep gap">
      <div class="pstep-n">— · EVIDENCE_GAP</div>
      <div class="pstep-t">转人工</div>
      <div class="pstep-s">取证重试用尽</div></div>` : '');
}

/* ────────────────── 卡片流 ────────────────── */
function card(title, sub, body, cls = '', right = '') {
  return `<div class="card ${cls}">
    <div class="card-h"><span class="dot"></span><h3>${title}</h3>
      ${sub ? `<span class="sub">${sub}</span>` : ''}
      ${right ? `<div class="right">${right}</div>` : ''}</div>
    <div class="card-b">${body}</div></div>`;
}

function render() {
  const s = S.snapshot; if (!s) return;
  (s.evidence.items || []).forEach((e) => S.evidence.set(e.evidence_id, e));

  const parts = [];
  parts.push(cardSubject(s));
  if (s.signal && s.signal.types.length) parts.push(cardSignal(s));
  if (s.evidence.items.length) parts.push(cardEvidence(s));
  parts.push(cardKnowledge(s));
  if (s.assertion) parts.push(cardDuel(s));
  if (s.gate && Object.keys(s.gate).length) parts.push(cardGate(s));
  if (s.execution) parts.push(cardExecution(s));
  if (s.audit) parts.push(cardAudit(s));
  if (s.retrospective) parts.push(cardRetro(s));

  // 只重绘变化的部分会让代码复杂很多；卡片总数十来个，整体重绘更省心也更不易出错。
  // 但要保留「因子已调整」这张前置卡与审批框的输入状态。
  const keep = $('cards').querySelector('.card.alert');
  const keepHtml = keep && keep.querySelector('.card-h h3').textContent === '因子已调整'
    ? keep.outerHTML : '';
  $('cards').innerHTML = keepHtml + parts.join('');

  const m = s.metrics || {};
  $('s-token').textContent = ((m.tokens_in || 0) + (m.tokens_out || 0)).toLocaleString();
  const u = s.model_usage || {};
  $('s-cost').textContent = u.total_cost_cny ? '￥' + u.total_cost_cny.toFixed(4) : '￥0';
  renderAudit();
}

function cardSubject(s) {
  const ex = s.exposure || {};
  return card('客户与授信敞口', s.case_id, `
    <div class="kpis">
      <div class="kpi"><div class="kpi-k">客户</div>
        <div class="kpi-v" style="font-size:14px">${esc(s.subject.name)}</div>
        <div class="kpi-n">${esc(s.subject.industry || '')} · ${esc(s.subject.region || '')}</div></div>
      <div class="kpi"><div class="kpi-k">本行直接敞口</div>
        <div class="kpi-v">${money(ex.total_exposure)}</div>
        <div class="kpi-n">当前分类 ${esc(s.subject.current_grade || '—')}</div></div>
      <div class="kpi"><div class="kpi-k">关联主体</div>
        <div class="kpi-v">${(ex.related_subjects || []).length}</div>
        <div class="kpi-n">担保圈 / 集团户 / 上下游穿透 2 层</div></div>
      ${ex.contagion_amount ? `<div class="kpi"><div class="kpi-k">或有代偿敞口</div>
        <div class="kpi-v">${money(ex.contagion_amount)}</div>
        <div class="kpi-n">为直接敞口的 ${ex.contagion_multiple} 倍</div></div>` : ''}
    </div>
    ${s.as_of ? `<div class="notice">本案为<b>历史回溯</b>，决策时点冻结在 <b>${s.as_of}</b>。
      只喂入该日之前的公开信息；后来实际发生了什么，在数据准备时就被摘走，系统全程看不到。</div>` : ''}`);
}

function cardSignal(s) {
  const g = s.signal;
  return card('预警信号归并', '多源信号去重与降噪', `
    <div class="kpis">
      <div class="kpi"><div class="kpi-k">入池信号</div><div class="kpi-v">${(g.kept || []).length + (g.dropped || []).length}</div>
        <div class="kpi-n">归并后保留 ${(g.kept || []).length} 条</div></div>
      <div class="kpi"><div class="kpi-k">降噪率</div><div class="kpi-v">${pct(g.denoise_rate)}</div>
        <div class="kpi-n">被丢弃的每一条都记录了理由</div></div>
      <div class="kpi"><div class="kpi-k">风险信号类型</div>
        <div class="kpi-v" style="font-size:13px">${g.types.map((t) => `<span class="tag">${t}</span>`).join(' ')}</div></div>
    </div>
    ${(g.dropped || []).length ? `<table class="grid" style="margin-top:12px">
      <tr><th>被丢弃的信号</th><th>来源</th><th>丢弃理由</th></tr>
      ${g.dropped.map((d) => `<tr><td>${esc(d.detail)}</td><td class="muted">${esc(d.source)}</td>
        <td class="muted">${esc(d.drop_reason)}</td></tr>`).join('')}
    </table>
    <div class="notice">丢弃理由逐条留痕，是为了让「高危信号被静默漏掉」这件事可被查出来。</div>` : ''}`);
}

/** 证据卡片：一条证据 = 一句话结论 + 关键字段。
 *  之前直接铺 JSON 与 s3:// URI——那是机器要的形状，
 *  风险经理没法在三秒内判断「这条证据说明了什么」。 */
function evItem(it) {
  const h = it.human || {};
  const facts = (h.facts || []).slice(0, 6).map((f) =>
    `<span class="fact ${f.hot ? 'hot' : ''}">${esc(f.k)} <b>${esc(f.v)}</b></span>`).join('');
  return `<div class="evcard ${it.level === '缺失' ? 'gap' : ''}"
       onclick="showEvidence('${it.evidence_id}')">
    <div class="evcard-h">
      <b>${esc(h.fact_label || it.fact_type)}</b>
      <span class="lvl ${it.level}">${it.level}</span>
      <span class="evcard-src">来自${esc(h.source_label || it.source_system)}</span>
      <span class="evref" style="margin-left:auto">${it.evidence_id}</span>
    </div>
    <div class="evcard-head">${esc(h.headline || '')}</div>
    <div class="evcard-facts">${facts}</div>
  </div>`;
}

function cardEvidence(s) {
  const e = s.evidence;
  const cl = e.checklist;
  // 政策条款单独归到「知识与经验」卡片，这里只放案件事实类证据
  const facts = e.items.filter((i) => i.source_system !== 'policy-kb');
  return card('证据材料', `共 ${e.items.length} 份，其中案件事实 ${facts.length} 份　只可追加、不可修改`, `
    <div class="kpis">
      <div class="kpi"><div class="kpi-k">证据充分度</div><div class="kpi-v">${e.sufficiency}</div>
        <div class="kpi-n">低于 0.7 不得进入裁决阶段</div></div>
      ${cl ? `<div class="kpi"><div class="kpi-k">取证清单覆盖</div><div class="kpi-v">${pct(cl.coverage)}</div>
        <div class="kpi-n">按信号类型派生「本应取到什么」</div></div>` : ''}
      <div class="kpi"><div class="kpi-k">显式证据缺口</div><div class="kpi-v">${(e.gaps || []).length}</div>
        <div class="kpi-n">记录「我们知道自己不知道什么」</div></div>
    </div>
    <div style="margin-top:12px">${facts.map(evItem).join('')}</div>
    <div class="notice">点任意一条<b>直接翻开原件</b>，并核对它的防篡改校验码。高亮的是<b>真正驱动了判断的那几个字段</b>。</div>`);
}

/** 知识与经验：召回的条款 + 沉淀下来的风险模式。
 *  这是用户说的「资料库」，里面必须是结论，不是一串编号。 */
function cardKnowledge(s) {
  const k = s.knowledge || {};
  const clauses = k.clauses || [];
  const p = k.pattern;
  if (!clauses.length && !p) return '';
  // 同一条款可能被求证方与证伪方各召回一次，按标题去重后合并命中维度
  const uniq = new Map();
  clauses.forEach((c) => {
    const title = (c.facts.find((f) => f.k === '条款标题') || {}).v || c.headline;
    const prev = uniq.get(title);
    if (prev) {
      const d = new Set([...prev.dims, ...(c.facts.find((f) => f.k === '命中的改写维度') || {}).v.split('、')]);
      prev.dims = [...d];
    } else {
      uniq.set(title, {
        title,
        src: (c.facts.find((f) => f.k === '出处') || {}).v,
        eff: (c.facts.find((f) => f.k === '生效日期') || {}).v,
        dims: ((c.facts.find((f) => f.k === '命中的改写维度') || {}).v || '').split('、'),
      });
    }
  });
  return card('知识与经验', '召回的条款依据 + 本案沉淀', `
    ${uniq.size ? `<h4 style="font-size:12.5px;margin-bottom:8px">召回的条款依据（${uniq.size} 条）</h4>
    <div class="klist">${[...uniq.values()].map((c) => `<div class="kitem">
      <b>${esc(c.title)}</b>
      <span>${esc(c.src)} · ${esc(c.eff)} 起生效 · 由 ${c.dims.filter(Boolean).map((d) =>
        `<span class="tag">${esc(d)}</span>`).join(' ') || '—'} 维召回</span>
    </div>`).join('')}</div>
    ${s.as_of ? `<div class="notice">条款已按案件时点 <b>${esc(s.as_of)}</b> 过滤：
      生效日晚于该时点的规定一律不召回。这是知识维度的前视污染防线——
      条款看起来「一直都在」，但它在当时可能还不存在。</div>` : ''}` : ''}

    ${p ? `<h4 style="font-size:12.5px;margin:16px 0 8px">本案沉淀的风险模式</h4>
    <div class="kitem"><b>${esc(p.title)}</b>
      <span class="mono">${esc(p.id)}</span></div>
    <dl class="prow">${p.rows.filter(([, v]) => v).map(([kk, v]) =>
      `<dt>${esc(kk)}</dt><dd>${esc(v)}</dd>`).join('')}</dl>
    <div class="notice">已归档入知识库，
      下次同类案件可被召回。<b>可以回溯「哪一次案件产生了哪条经验」</b>——
      注意「反例」这一行：沉淀的不只是「什么情况算风险」，还有「什么情况不算」。</div>` : ''}`);
}

function cardDuel(s) {
  const a = s.assertion || {}, r = s.rebuttal || {}, adj = s.adjudication;
  const mu = s.model_usage.by_caller || {};
  const ma = mu['risk-analyst'] || {}, md = mu['devils-advocate'] || {};
  const refuted = new Set((r.rebuttals || []).map((x) => x.target));
  const byTarget = {};
  (r.checklist_resolutions || []).forEach((x) => { byTarget[x.target] = x; });
  (r.rebuttals || []).forEach((x) => { byTarget[x.target] = byTarget[x.target] || { status: 'REFUTED', resolution: x.argument }; });
  (r.attempted_but_failed || []).forEach((x) => {
    byTarget[x.target] = byTarget[x.target] || { status: 'ATTEMPTED_FAILED', resolution: `${x.tried}；${x.failed_because}` };
  });

  const causes = (a.root_causes || []).map((c) => {
    const res = byTarget[c.type] || {};
    const done = res.status === 'REFUTED';
    return `<div class="cause ${done ? 'refuted' : 'survived'}">
      <div class="cause-h"><b>${esc(c.type)}</b>
        ${res.status ? `<span class="rstat ${res.status}">${res.status === 'REFUTED' ? '已被推翻'
          : res.status === 'ATTEMPTED_FAILED' ? '反驳未成立' : '证据不足'}</span>` : ''}
        <span class="conf">置信度 ${c.confidence}</span></div>
      <div class="cause-r">${withEvRefs(c.rationale || '')}</div>
      <div class="cause-r muted">依据　${(c.evidence_ids || []).map((i) =>
        `<span class="evref" onclick="showEvidence('${i}')">${i}</span>`).join(' ')}</div>
      ${res.resolution ? `<div class="cause-r" style="margin-top:5px">
        <b>质疑官：</b>${withEvRefs(res.resolution)}</div>` : ''}
    </div>`;
  }).join('') || '<div class="muted">未产出主因</div>';

  const cl = s.rebuttal_checklist;
  const verdictCls = !adj ? '' : adj.verdict === 'RISK_CONFIRMED' ? 'confirmed'
    : adj.verdict === 'RISK_REFUTED' ? 'refuted' : 'insufficient';

  return card('风险定性 ⇄ 对抗质疑', '两个岗位看到的材料完全一样，但一个负责论证风险成立，另一个负责推翻它', `
    <div class="duel">
      <div class="side"><div class="side-h">
        <b>风险定性官</b><span class="obj">负责论证「风险成立」。不给它任何查询工具，防止边查边下结论</span>
        <span class="mdl">${esc(ma.provider || '')}/${esc(ma.declared_model || '—')}　族 ${esc(ma.family || '—')}${ma.stubbed ? '　（本次由规则推理，未调用大模型）' : ''}</span>
      </div><div class="side-b">
        <div><span class="tag">结论 ${esc(a.conclusion || '—')}</span>
          <span class="tag">建议分类 ${esc(a.suggested_grade || '—')}</span></div>
        <div class="cause-r">${withEvRefs(a.summary || '')}</div>
      </div></div>
      <div class="side"><div class="side-h">
        <b>对抗质疑官</b><span class="obj">负责推翻上面的结论。逐条找反证，宁可漏判也不能错杀</span>
        <span class="mdl">${esc(md.provider || '')}/${esc(md.declared_model || '—')}　族 ${esc(md.family || '—')}${md.stubbed ? '　（本次由规则推理，未调用大模型）' : ''}</span>
      </div><div class="side-b">
        <div><span class="tag">裁定 ${esc(r.verdict || '—')}</span>
          <span class="tag">成立反驳 ${(r.rebuttals || []).length} 条</span>
          <span class="tag">尝试未成立 ${(r.attempted_but_failed || []).length} 条</span></div>
        <div class="cause-r">${esc(typeof r.summary === 'string' ? r.summary : '')}</div>
      </div></div>
    </div>
    ${ma.family && md.family && ma.family !== md.family ? `<div class="notice">
      <b>双方使用不同技术路线的模型</b>：（${esc(ma.family)} ⇄ ${esc(md.family)}）。
      若两边用同一个模型，「自说自话」只是换了个地方发生——配成同一技术路线会被<b>拒绝启动</b>。</div>` : ''}

    <h4 style="margin:16px 0 8px;font-size:12.5px">逐条主因与质疑结论</h4>
    ${causes}

    ${cl ? `<div class="notice ${cl.complete ? '' : 'warn'}">
      <b>质疑清单覆盖 ${pct(cl.coverage)}</b>（${cl.items.length} 项，由系统按定性方给出的疑点自动生成，<b>不是模型自己报的</b>，答完再逐项核对）。
      ${cl.complete ? '' : `未覆盖：${cl.unaddressed.join('、')}。`}
      没人质疑过 ≠ 质疑通过——前者只是没人看过，所以覆盖不满就退回补材料。</div>` : ''}

    ${adj ? `<div class="verdict-bar ${verdictCls}">
      <div><div class="vb-k">裁决</div><div class="vb-v">${adj.verdict}</div></div>
      <div class="vb-basis">${esc(adj.basis)}
        ${adj.final_grade ? `<br><span class="muted">五级分类调整为「${adj.final_grade}」</span>` : ''}</div>
    </div>` : ''}`);
}

function cardGate(s) {
  const g = s.gate, ap = s.approval;
  const lv = (s.evidence.items || []).some((e) => e.level === '强') ? '强'
    : (s.evidence.items || []).some((e) => e.level === '弱') ? '弱' : '缺失';
  const acts = (g.actions || []).map((a) => `
    <div class="action-row">
      <div><div class="an">${esc(a.label || a.action_label)}</div>
        <div class="ar">${esc(a.reason || '')}</div></div>
      <div class="right">
        <span class="tier ${a.action_tier}">${a.action_tier}</span><br>
        <span class="muted mono" style="font-size:10.5px">规则 ${a.rule_id}</span><br>
        <span class="muted" style="font-size:10.5px">${a.reversible ? '可逆 · ' + esc(a.rollback_point || '') : '不可逆'}</span>
      </div></div>`).join('');

  let approvalBox = '';
  if (S.pendingApproval) {
    approvalBox = `<div class="approve-box" id="approve-box">
      <div class="approve-h">此动作影响客户授信，必须由 ${esc((g.approver_roles || []).join(' / '))} 人工审批</div>
      <div class="approve-b">
        <input type="text" id="apv-reason" placeholder="审批意见（可选）">
        <div class="approve-actions">
          <button class="btn btn-ok" onclick="decide(true)">批准执行</button>
          <button class="btn btn-danger" onclick="decide(false)">驳回</button>
        </div>
        <p class="muted" style="font-size:11.5px">
          驳回后案件<b>退回只读诊断</b>，客户额度不会被修改——可在执行回执与取数留痕里核对。</p>
      </div></div>`;
  } else if (ap) {
    approvalBox = `<div class="approve-box"><div class="approve-h"
      style="background:${ap.decision === 'APPROVED' ? 'var(--l0-bg)' : 'var(--l3-bg)'};color:${ap.decision === 'APPROVED' ? 'var(--l0)' : 'var(--l3)'}">
      审批${ap.decision === 'APPROVED' ? '通过' : '被驳回'} · ${esc(ap.approver)}</div>
      <div class="approved-note">
        ${ap.token ? `审批令牌 <span class="mono">${esc(ap.token)}</span>，执行方需校验后方可写入` : ''}
        ${ap.reason ? `<br>意见：${esc(ap.reason)}` : ''}
        ${!ap.token ? '<br><b>已降级为 L0 只读诊断，不执行任何处置动作。</b>' : ''}
      </div></div>`;
  }

  return card('处置方案与分级', '按四个维度定档：风险等级 × 证据强度 × 敞口金额 × 是否可撤销', `
    <div class="gate4">
      <div class="g4"><div class="g4-k">风险等级</div><div class="g4-v">${esc((s.adjudication || {}).final_grade || '—')}</div></div>
      <div class="g4"><div class="g4-k">证据等级</div><div class="g4-v">${lv}</div></div>
      <div class="g4"><div class="g4-k">敞口金额</div><div class="g4-v" style="font-size:12.5px">${money((s.exposure || {}).total_exposure)}</div></div>
      <div class="g4"><div class="g4-k">可逆性</div><div class="g4-v">${g.reversible ? '可逆' : '不可逆'}</div></div>
    </div>
    ${acts}
    <div class="notice ${g.action_tier === 'L3' ? 'danger' : g.action_tier === 'L2' ? 'warn' : ''}">
      <b>整体层级 ${g.action_tier}</b> —— ${esc(S.boot.tiers[g.action_tier] || '')}。
      一批动作里只要有一个需要审批，整批都走审批。
      ${g.action_tier === 'L3' ? '<br><b>此档系统全程不派发执行岗，对业务系统的写操作为 0。</b>' : ''}
    </div>
    ${approvalBox}`, S.pendingApproval ? 'alert' : '',
    `<span class="tier ${g.action_tier}">${g.action_tier}</span>`);
}

function cardExecution(s) {
  const ex = s.execution;
  return card('执行回执', '全系统唯一有操作权的岗位', `
    <table class="grid">
      <tr><th>动作</th><th>状态</th><th>审计流水</th></tr>
      ${(ex.results || []).map((r) => `<tr>
        <td>${esc(r.label || r.action)}</td>
        <td><span class="tag">${esc(r.status)}</span></td>
        <td class="mono muted">${esc(r.audit_serial || '—')}</td></tr>`).join('')}
    </table>
    <div class="notice">执行岗<b>没有任何自主决策权</b>：只做清单内的动作，需审批的必须先验审批凭据，
      每个动作都可重复执行不出错、也都能撤回。它还不能审计自己的结果——执行与审计不是同一个岗位。</div>`);
}

function cardAudit(s) {
  const a = s.audit || {}, c = a.compliance || {};
  const items = (c.items || []).map((i) => `<tr>
    <td>${i.result === 'PASS' ? '<span class="lvl 强">通过</span>'
      : '<span class="lvl 缺失">未通过</span>'}</td>
    <td><span class="mono muted">${esc(i.rule_id || '')}</span><br>${esc(i.source || '')}</td>
    <td class="muted" style="font-size:11px">${withEvRefs(i.detail || '')}</td></tr>`).join('');
  const distilled = a.distilled || {};
  return card('合规审计与经验沉淀', '逐条核验并归档', `
    <div class="kpis">
      <div class="kpi"><div class="kpi-k">合规项</div><div class="kpi-v">${c.passed || 0} / ${(c.passed || 0) + (c.failed || 0)}</div>
        <div class="kpi-n">逐条举证，发现缺失只报告不修复</div></div>
      <div class="kpi"><div class="kpi-k">端到端耗时</div><div class="kpi-v">${(s.metrics || {}).end_to_end_ms || 0} ms</div></div>
      <div class="kpi"><div class="kpi-k">工具调用成功率</div><div class="kpi-v">${pct((s.metrics || {}).tool_success_rate)}</div></div>
    </div>
    ${items ? `<table class="grid" style="margin-top:12px">
      <tr><th style="width:64px">结果</th><th>合规项</th><th>举证</th></tr>${items}</table>` : ''}
    ${(distilled.patterns || distilled.lessons) ? `<div class="notice" style="margin-top:12px">
      <b>经验沉淀</b>　${esc(JSON.stringify(distilled.patterns || distilled.lessons)).slice(1, 300)}
      <br><span class="muted">确认的风险模式回流案例库，可追溯「哪次案件产生了哪条规则」。</span></div>` : ''}
    <div class="reptabs" style="margin-top:14px">
      <button class="btn btn-sm" onclick="loadReport('opinion',this)">处置意见书</button>
      <button class="btn btn-sm" onclick="loadReport('audit',this)">审计报告</button>
    </div>
    <div class="report" id="report-box">点上方按钮查看成文报告。正文中每处结论都带 [EV-xxxx] 证据角标，可点开。</div>`);
}

function cardRetro(s) {
  const r = s.retrospective;
  return card('历史结局', '仅用于事后评分，未进入取证路径', `
    <div class="notice warn"><b>这段信息系统全程取不到</b>——它在案件数据准备时就被摘走，
      任何环节都够不到，是结构上做不到而非约定不去看。案件办完后才在这里揭晓。</div>
    <div class="kpis" style="margin-top:10px">
      <div class="kpi"><div class="kpi-k">决策时点</div><div class="kpi-v" style="font-size:14px">${esc(s.as_of)}</div></div>
      <div class="kpi"><div class="kpi-k">结局发生</div><div class="kpi-v" style="font-size:14px">${esc(r.outcome_date)}</div></div>
      <div class="kpi"><div class="kpi-k">预警提前量</div><div class="kpi-v">${r.lead_time_months} 个月</div></div>
    </div>
    <div class="cause-r" style="margin-top:10px">${esc(r.detail)}</div>`);
}

/* ────────────────── 审批交互 ────────────────── */
function onApprovalRequired(req, snapshot) {
  // 快照随事件一起来：闸门刚算完、还没有任何 Agent span 或阶段迁移把它带出来，
  // 不接住这一份的话，审批框依赖的 gate 数据是空的
  if (snapshot) S.snapshot = snapshot;
  S.pendingApproval = req;
  const b = $('approval-banner');
  b.hidden = false;
  $('ab-detail').textContent =
    `${req.action_tier} · ${(req.actions || []).map((a) => a.label).join('、')} · 待 ${(req.approver_roles || []).join('/')} 决策`;
  render();
  setActivityIdle();
  setTimeout(scrollToApproval, 160);
}

function scrollToApproval() {
  // 依次退让：审批框 → 闸门卡片 → 卡片流末尾。
  // 「点了没反应」比「滚到了大概位置」糟糕得多
  const el = $('approve-box')
    || [...document.querySelectorAll('.card')].find(
      (c) => c.querySelector('.card-h h3').textContent.includes('闸门'))
    || $('cards').lastElementChild;
  if (!el) return;
  el.scrollIntoView({ behavior: 'smooth', block: 'center' });
  el.animate([{ boxShadow: '0 0 0 0 rgba(176,106,0,.65)' },
              { boxShadow: '0 0 0 12px rgba(176,106,0,0)' }],
             { duration: 900, iterations: 2 });
  const input = $('apv-reason');
  if (input) setTimeout(() => input.focus(), 400);
}

async function decide(approved) {
  const reason = ($('apv-reason') || {}).value || '';
  S.pendingApproval = null;
  $('approval-banner').hidden = true;
  await api(`/api/runs/${S.runId}/approve`, {
    method: 'POST',
    body: JSON.stringify({ approved, reason, approver: '风险经理-赵' }),
  });
}

/* ────────────────── 右栏 ────────────────── */

const SERVER_CN = {
  'credit-core-mcp': '信贷核心', 'bureau-mcp': '人行征信',
  'judicial-mcp': '司法工商', 'txn-mcp': '账务流水',
};
const KIND_CN = {
  case: '案件', agent: '岗位', skill: '能力', mcp: '取数', rag: '检索',
  llm: '推理', routing: '路由', adjudication: '裁决', approval: '审批', execution: '执行',
};
const PHASE_CN = {
  INTAKE: '受理', EVIDENCE: '取证', ADJUDICATION: '裁决',
  DISPOSITION: '处置', AUDIT: '审计', CLOSED: '闭环', EVIDENCE_GAP: '转人工',
};

/** Span 名字翻译成人话。原来直接铺 `credit-core-mcp.get_facility` 这类字面量，
 *  对看的人是噪声——他关心的是「谁在做什么」，不是内部标识符。 */
function spanTitle(sp) {
  const n = sp.name;
  if (sp.kind === 'agent') return AGENT_CN[n] || n;
  if (sp.kind === 'mcp') {
    const [srv, tool] = n.split('.');
    return `${SERVER_CN[srv] || srv} · ${TOOL_CN[tool] || tool}`;
  }
  if (sp.kind === 'skill') return SKILL_CN[n] || n;
  if (sp.kind === 'llm') {
    return n === 'risk_root_cause' ? '根因归因与五级分类'
      : n === 'devils_advocate' ? '逐条证伪定性结论' : n;
  }
  if (sp.kind === 'routing') {
    const [from, to] = n.split('→');
    return `${PHASE_CN[from] || from} → ${PHASE_CN[to] || to}`;
  }
  if (sp.kind === 'case') return '案件 ' + n;
  return n;
}

function addSpan(sp) {
  const parent = S.spans.get(sp.parent_id);
  const depth = parent ? parent.depth + 1 : 0;
  const el = document.createElement('div');
  el.className = 'tr running';
  el.innerHTML = `<span style="width:${depth * 9}px;flex:none"></span>
    <span class="tr-k ${sp.kind}">${KIND_CN[sp.kind] || sp.kind}</span>
    <span class="tr-n">${esc(spanTitle(sp))}</span><span class="tr-d">…</span>`;
  $('trace').appendChild(el);
  // 路由与裁决的关键属性直接展开——「为什么走了这条分支」是可举证的核心。
  // 但只展开人能读的那几项，规则版本号之类的收进 trace.json 里就够了
  const a = sp.attributes || {};
  if (sp.kind === 'routing' && a.rule_id) {
    addTraceNote(`命中规则 ${a.rule_id}` + (a.dispatch && a.dispatch.length
      ? `　派发给 ${a.dispatch.map((x) => AGENT_CN[x] || x).join('、')}` : ''));
  } else if (sp.kind === 'adjudication' && a.verdict) {
    addTraceNote(`裁定 ${a.verdict}${a.basis ? '　' + a.basis : ''}`);
  } else if (sp.kind === 'llm' && a['model.declared']) {
    addTraceNote(`${a['model.provider']}/${a['model.declared']}　族 ${a['model.family']}`
      + (a['model.stubbed'] ? '　（本次由规则推理，未调用大模型）' : ''));
  } else if (sp.kind === 'approval') {
    addTraceNote(`层级 ${a.action_tier}　待 ${(a.approver_roles || []).join('/')} 决策`);
  }
  S.spans.set(sp.span_id, { el, depth });
  $('trace').scrollTop = $('trace').scrollHeight;
}

function addTraceNote(text) {
  const d = document.createElement('div');
  d.className = 'tr-attr';
  d.textContent = text;
  $('trace').appendChild(d);
}

function endSpan(ev) {
  const rec = S.spans.get(ev.span_id); if (!rec) return;
  rec.el.classList.remove('running');
  if (ev.status !== 'OK') rec.el.classList.add('err');
  rec.el.querySelector('.tr-d').textContent = `${ev.duration_ms}ms`;
}

/* 日志事件名 → 人话。原来直接铺 `risk_root_cause.done` 加一串 JSON 字段，
 * 信息量是有的，但要读的人先在脑子里做一次翻译。 */
const EVENT_CN = {
  'routing.decided': '路由决策',
  'signal_fusion.done': '信号归并完成',
  'exposure_mapping.done': '敞口测绘完成',
  'credit_report_probe.done': '征信取证完成',
  'litigation_probe.done': '涉诉与工商取证完成',
  'txn_flow_analyze.done': '流水分析完成',
  'guarantee_probe.done': '担保台账取证完成',
  'evidence_checklist.checked': '取证清单逐项核对',
  'rebuttal_checklist.checked': '质疑清单逐项核对',
  'risk_root_cause.done': '风险定性完成',
  'risk_root_cause.degraded': '定性失败，按失败策略降级',
  'devils_advocate.done': '对抗质疑完成',
  'devils_advocate.degraded': '质疑环节失效，按「失败即阻断」处理',
  'adjudication.done': '裁决完成',
  'risk_gate.decided': '闸门定级',
  'approval.granted': '人工审批通过',
  'approval.rejected': '人工审批驳回',
  'safe_disposition.done': '处置动作执行完成',
  'compliance_check.done': '合规校验完成',
  'postmortem_distill.done': '经验沉淀写入知识库',
  'report_compose.done': '报告成文',
  'query_rewrite.done': '查询改写完成',
  'policy_rag.done': '条款召回完成',
  'case_memory.done': '相似案例召回完成',
  'rebuttal_checklist.unmatched_resolution': '质疑回执对不上清单项',
};

/* 日志字段里值得显示给人看的那些。其余（trace_id、evidence_ids 之类）
 * 落在 logs.jsonl 里供事后排查，不占界面。 */
const LOG_FIELD_CN = {
  rule_id: '规则', next_phase: '下一阶段', reason: '理由', verdict: '裁定',
  basis: '依据', conclusion: '结论', grade: '建议分类', cause_count: '主因数',
  coverage: '覆盖率', rebuttals: '成立反驳', attempted: '尝试未成立',
  approver: '审批人', action: '动作', status: '状态', denoise_rate: '降噪率',
  anomalies: '异常模式', related_party: '含关联方', action_tier: '层级',
  checklist_coverage: '清单覆盖', passed: '通过项', failed: '未通过项',
};

function addLog(r) {
  const shown = Object.entries(r)
    .filter(([k, v]) => LOG_FIELD_CN[k] && v !== null && v !== '' &&
      !(Array.isArray(v) && !v.length))
    .map(([k, v]) => `${LOG_FIELD_CN[k]} ${Array.isArray(v) ? v.join('、')
      : typeof v === 'number' ? (v < 1 && v > 0 ? v.toFixed(2) : v) : v}`);
  const el = document.createElement('div');
  el.className = 'logrow';
  el.innerHTML = `<div class="log-h">
      <span class="log-lv ${r.level}">${r.level}</span>
      <span class="log-actor">${esc(AGENT_CN[r.actor] || r.actor)}</span>
      <span class="log-ev">${esc(EVENT_CN[r.event] || r.event)}</span></div>
    ${shown.length ? `<div class="log-f">${esc(shown.join('　').slice(0, 260))}</div>` : ''}`;
  $('logs').appendChild(el);
  $('logs').scrollTop = $('logs').scrollHeight;
}

const ERR_CN = {
  PERMISSION_DENIED: '越权，已拒绝', AUTHORIZATION_MISSING: '缺查询授权',
  RATE_LIMITED: '源系统限流', NOT_FOUND: '未查到',
  APPROVAL_REQUIRED: '缺审批令牌', APPROVAL_INVALID: '审批令牌无效',
  ROLLBACK_POINT_NOT_FOUND: '回滚点不存在',
};

function renderAudit() {
  const rows = ((S.snapshot || {}).mcp_audit) || [];
  $('audit').innerHTML = rows.map((a) => {
    const denied = a.error_code === 'PERMISSION_DENIED';
    return `<div class="auditrow ${denied ? 'denied' : ''}">
      <span class="au-s ${a.status}">${a.status === 'OK' ? '放行' : '拒绝'}</span>
      <span class="au-t">${esc(AGENT_CN[a.caller] || a.caller)}
        <span class="muted">→</span> ${esc(SERVER_CN[a.server] || a.server)} ·
        ${esc(TOOL_CN[a.tool] || a.tool)}</span>
      <span class="au-c">${a.error_code ? esc(ERR_CN[a.error_code] || a.error_code)
        : a.latency_ms + 'ms'}</span>
    </div>`;
  }).join('') || '<div class="muted" style="padding:8px 0;font-size:12px">暂无调用记录</div>';
}

function renderTeam() {
  $('team').innerHTML = S.boot.agents.map((a) => {
    const badges = [];
    if (a.is_leader) badges.push('<span class="badge ld">Team Leader</span>');
    if (a.writes.length) badges.push('<span class="badge w">唯一写触点</span>');
    if (a.pii_access) badges.push('<span class="badge p">唯一接触敏感信息</span>');
    if (!a.llm) badges.push('<span class="badge n">无模型入口</span>');
    if (a.llm && !Object.keys(a.mcp).length) badges.push('<span class="badge n">零工具权限</span>');
    const b = S.boot.bindings[a.id];
    return `<div class="agent">
      <div class="agent-h"><b>${a.id}</b>${badges.join(' ')}</div>
      <div class="agent-role">${esc(a.role)}</div>
      <div class="agent-meta">权限等价类　${esc(a.equivalence_class)}</div>
      <div class="agent-meta">可访问系统　${Object.keys(a.mcp).length
        ? Object.entries(a.mcp).map(([s, t]) => `${s}(${Object.keys(t).length})`).join('、') : '无'}</div>
      <div class="agent-meta">模型　${b ? `${esc(b.provider)}/${esc(b.model)}　族 ${esc(b.family)}` : '不绑定'}</div>
      <div class="agent-meta muted">禁止　${esc(a.notes)}</div>
    </div>`;
  }).join('') + `<div class="notice" style="margin:10px 0">
    <b>6 个岗位 = 5 类不同的数据权限 + 1 个专门唱反调的。</b>
    岗位数量是按「谁该看到什么」推导出来的，不是拍脑袋定的；合并任意两个都会造成越权。</div>`;
}

async function probe() {
  const r = await api(`/api/runs/${S.runId}/probe`, {
    method: 'POST',
    body: JSON.stringify({ caller: 'risk-analyst', server: 'credit-core-mcp', tool: 'adjust_limit' }),
  });
  if (S.snapshot && r.audit_entries) {
    S.snapshot.mcp_audit = (S.snapshot.mcp_audit || []).concat(r.audit_entries);
    renderAudit();
  }
  openModal(`<h3>越权调用${r.allowed ? '被放行（异常！）' : '已被拒绝'}</h3>
    <p class="muted" style="font-size:12.5px">
      ${esc(r.request.caller)} → ${esc(r.request.server)}.${esc(r.request.tool)}</p>
    <div class="notice ${r.allowed ? 'danger' : ''}" style="margin-top:12px">
      ${r.allowed ? '权限矩阵未拦住这次调用，这是缺陷。'
      : `<b>${esc(r.code)}</b>　${esc(r.message)}`}</div>
    <div class="notice">重点不是它被拒了，而是<b>这次被拒的尝试已经写进取数留痕</b>（见右栏，红底那行）。
      只在成功路径留痕的系统，没法向监管解释「有没有人试过绕过去」。</div>`);
}

/* ────────────────── 弹窗 ────────────────── */
function showEvidence(id) {
  const e = S.evidence.get(id);
  if (!e) return openModal(`<h3>${esc(id)}</h3><p class="muted">该证据不在本次账本中。</p>`);
  const h = e.human || {};
  // 详情的顺序是刻意的：先说这条证据「说明了什么」，再给字段，
  // 最后才是哈希与快照 URI——后者是举证锚点，不是阅读材料
  const rows = (h.facts || []).map((f) =>
    `<dt>${esc(f.k)}</dt><dd${f.hot ? ' style="font-weight:620"' : ''}>${esc(f.v)}</dd>`).join('');
  openModal(`<h3>${esc(h.fact_label || e.fact_type)}　<span class="lvl ${e.level}">${e.level}</span></h3>
    <p class="muted" style="font-size:12.5px">
      ${esc(e.evidence_id)} · 来自${esc(h.source_label || e.source_system)} · 采集于 ${esc(e.collected_at)}</p>
    <div class="notice" style="margin-top:12px">${esc(h.headline || '')}</div>
    <dl class="kv" style="grid-template-columns:118px 1fr">${rows}</dl>
    <div class="notice" style="margin-top:14px">
      <b>证据等级「${e.level}」</b>　${esc(e.level_reason || '—')}<br>
      等级决定这份材料能不能单独支撑处置决定，不是一个评分。</div>
    <div class="doc-slot" id="doc-slot">
      <button class="btn btn-sm" onclick="openSnapshot('${e.evidence_id}')">📄 翻开原件</button>
      <span class="muted" style="font-size:11.5px">查看这份材料的原始文件，并核对防篡改校验码</span>
    </div>
    <div class="notice">证据材料<b>只可追加、不可修改</b>：同一编号不得二次写入，任何改写尝试会被直接拒绝。</div>`);
}

/** 翻开原件。
 *  账本里存的是一串 s3:// 地址，给系统看没问题，给人看就是「你自己去查」。
 *  复核的人要的是当场翻到那份材料，所以这里把它渲染成一份看得懂的文件。 */
async function openSnapshot(id) {
  const slot = $('doc-slot');
  if (!slot) return;
  slot.innerHTML = '<span class="muted" style="font-size:12px">正在调取原件…</span>';
  const d = await api(`/api/runs/${S.runId}/snapshot/${id}`);
  if (d.error) { slot.innerHTML = `<div class="notice danger">${esc(d.error)}</div>`; return; }

  const b = d.body || {};
  const rows = (b.rows || []).map(([k, v]) =>
    `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join('');
  const tables = (b.tables || []).map((t) => `
    <div class="doc-table-cap">${esc(t.caption)}
      ${t.total > t.rows.length ? `<span class="muted">（共 ${t.total} 条，显示前 ${t.rows.length} 条）</span>` : ''}</div>
    <table class="grid">
      <tr>${t.cols.map((c) => `<th>${esc(c)}</th>`).join('')}</tr>
      ${t.rows.map((r) => `<tr>${r.map((x) => `<td>${esc(x)}</td>`).join('')}</tr>`).join('')}
    </table>`).join('');

  slot.innerHTML = `
    <div class="doc">
      <div class="doc-h">
        <div>
          <div class="doc-title">${esc(d.title)}</div>
          <div class="doc-issuer">${esc(d.issuer)}</div>
        </div>
        <span class="doc-seal ${d.hash_ok ? 'ok' : 'bad'}">
          ${d.hash_ok ? '✓ 校验通过' : '✗ 校验不符'}</span>
      </div>
      <div class="doc-b">
        ${b.kind === 'text'
          ? `<div class="doc-text">${esc(b.text || '（原件为空）')}</div>`
          : `${rows ? `<dl class="doc-kv">${rows}</dl>` : ''}${tables}`}
      </div>
      <div class="doc-f">
        ${d.redacted && d.redacted.length
          ? `<div class="doc-redact">本件含个人敏感信息，
             <b>${d.redacted.length}</b> 处已按规定脱敏后留存</div>` : ''}
        <div>归档位置　<span class="mono">${esc(d.uri || '—')}</span></div>
        <div>防篡改校验码　<span class="mono">${esc(d.hash || '—')}</span></div>
        <div class="muted">刚才这次调取已当场重算校验码并与账本比对，
          ${d.hash_ok ? '两者一致，说明原件自登记以来未被改动。'
                      : '<b>两者不一致，原件或账本已被改动，此材料不可采信。</b>'}</div>
      </div>
    </div>`;
}

async function loadReport(kind, btn) {
  btn.parentElement.querySelectorAll('.btn').forEach((b) => b.classList.remove('btn-primary'));
  btn.classList.add('btn-primary');
  const r = await api(`/api/runs/${S.runId}/report/${kind}`);
  $('report-box').innerHTML = r.markdown
    ? withEvRefs(r.markdown).replace(/^#{1,6}\s*(.+)$/gm, '<h3>$1</h3>')
      .replace(/\*\*(.+?)\*\*/g, '<b>$1</b>')
    : '<span class="muted">本次运行未产出该报告。</span>';
}

function openModal(html) { $('modal-body').innerHTML = html; $('modal').hidden = false; }
function closeModal() { $('modal').hidden = true; }
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeModal(); });

boot();
