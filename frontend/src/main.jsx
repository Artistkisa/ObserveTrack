import React, { useEffect, useMemo, useRef, useState } from 'react'
import { createRoot } from 'react-dom/client'
import Markdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import Ghost from './Ghost'
import EditorAnalysis, { EditorRun, EditorDialog } from './EditorAnalysis'
import { labels, dimensions, formatTime, activityState, createPoller, phaseLabel } from './state'
import './style.css'
import './observatory.css'

let csrf = ''
async function api(path, options = {}) {
  const response = await fetch('/api' + path, { credentials: 'same-origin', ...options, headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf, ...options.headers } })
  const data = await response.json().catch(() => ({}))
  if (!response.ok) { const error = new Error(typeof data.detail === 'string' ? data.detail : '请求失败'); error.status = response.status; throw error }
  return data
}
const post = (path, body = {}) => api(path, { method: 'POST', body: JSON.stringify(body) })
const patch = (path, body) => api(path, { method: 'PATCH', body: JSON.stringify(body) })
const Icon = ({ type, size = 20 }) => <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{type === 'grid' ? <><rect x="3" y="3" width="7" height="7" rx="2"/><rect x="14" y="3" width="7" height="7" rx="2"/><rect x="3" y="14" width="7" height="7" rx="2"/><rect x="14" y="14" width="7" height="7" rx="2"/></> : type === 'report' ? <><path d="M6 3h8l4 4v14H6zM14 3v5h4M9 12h6M9 16h6"/></> : type === 'run' ? <><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></> : type === 'settings' ? <><path d="M4 6h16M4 12h16M4 18h16"/><circle cx="9" cy="6" r="2" fill="#141e28"/><circle cx="16" cy="12" r="2" fill="#141e28"/><circle cx="8" cy="18" r="2" fill="#141e28"/></> : <path d="M12 5v14M5 12h14"/>}</svg>
const Status = ({ value }) => <span className={`status status-${value}`}><i/>{labels[value] || value || '尚未观察'}</span>
const Empty = ({ title, children }) => <section className="empty"><Ghost size={110}/><h2>{title}</h2><p>{children}</p></section>

function App() {
  const [auth, setAuth] = useState(null)
  const [snapshot, setSnapshot] = useState(null)
  const [page, setPage] = useState(() => { const saved = sessionStorage.getItem('observe-page'); return !['overview', 'reports', 'runs', 'settings', 'reader', 'repository'].includes(saved) ? 'overview' : saved === 'reader' ? 'reports' : saved === 'repository' && !sessionStorage.getItem('observe-repository') ? 'overview' : saved || 'overview' })
  const [selected, setSelected] = useState(() => Number(sessionStorage.getItem('observe-repository')) || null)
  const [detail, setDetail] = useState(null)
  const [report, setReport] = useState(null)
  const [reports, setReports] = useState([])
  const [jobs, setJobs] = useState([])
  const [error, setError] = useState('')
  const [disconnected, setDisconnected] = useState(false)
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState(false)
  const [modal, setModal] = useState(false)
  const [filter, setFilter] = useState('')
  const [flowId, setFlowId] = useState(null)
  const [flowDetail, setFlowDetail] = useState(null)
  const poller = useRef(null)
  const state = useRef({ page, selected, filter, flowId })
  state.current = { page, selected, filter, flowId }
  const heading = useRef(null)
  useEffect(() => { api('/session').then(data => { csrf = data.csrf; setAuth(true) }).catch(() => setAuth(false)) }, [])
  useEffect(() => {
    if (!auth) return
    const chain = createPoller(async signal => {
      const current = { ...state.current }
      const overview = await api('/overview', { signal })
      let secondary = null
      if (current.page === 'runs') secondary = await api('/jobs', { signal })
      if (current.page === 'reports') secondary = await api('/reports', { signal })
      if (current.page === 'repository' && current.selected) secondary = await api(`/repositories/${current.selected}?dimension=${current.filter}`, { signal })
      const flow = current.flowId ? await api('/analysis/' + current.flowId, { signal }) : null
      return { overview, secondary, current, flow }
    }, (failure, data) => {
      if (failure) { setDisconnected(true); if (failure.status === 401) setAuth(false); else setError(failure.message); return }
      setDisconnected(false)
      setSnapshot(data.overview)
      if (data.current.flowId === state.current.flowId) setFlowDetail(data.flow)
      if (data.current.page !== state.current.page || data.current.selected !== state.current.selected || data.current.filter !== state.current.filter) return
      if (data.current.page === 'runs') setJobs(data.secondary)
      if (data.current.page === 'reports') setReports(data.secondary)
      if (data.current.page === 'repository') setDetail(previous => previous?.expanded ? { ...data.secondary, events: [...new Map([...data.secondary.events, ...previous.events].map(event => [event.id, event])).values()], expanded: true, next_before: previous.next_before, has_more: previous.has_more } : data.secondary)
    }, { hidden: () => document.hidden })
    poller.current = chain
    const visibility = () => { if (!document.hidden) chain.refresh() }
    document.addEventListener('visibilitychange', visibility)
    chain.refresh()
    return () => { chain.stop(); document.removeEventListener('visibilitychange', visibility); poller.current = null }
  }, [auth])
  useEffect(() => { sessionStorage.setItem('observe-page', page); poller.current?.refresh(); heading.current?.focus() }, [page, selected, filter])
  useEffect(() => { poller.current?.refresh() }, [flowId])
  const navigate = next => { setPage(next); setError(''); if (next !== 'repository') setDetail(null) }
  const action = async callback => { setBusy(true); setError(''); try { await callback(); poller.current?.refresh() } catch (failure) { setError(failure.message) } finally { setBusy(false) } }
  const openRepo = id => { sessionStorage.setItem('observe-repository', String(id)); setSelected(id); setDetail(null); setFilter(''); navigate('repository') }
  const openReport = id => action(async () => { const value = await api('/reports/' + id); setReport(value); navigate('reader') })
  const add = async event => { event.preventDefault(); const name = new FormData(event.currentTarget).get('name'); await action(async () => { await post('/repositories', { full_name: name }); setModal(false); setNotice('仓库已加入观察，正在建立基线。') }) }
  const inspectFlow = id => { setFlowDetail(null); setFlowId(id) }
  const continueFlow = id => action(async () => { await post('/analysis/' + id + '/continue'); setNotice('继续分析已排队，沿用原流程和剩余额度。') })
  const loadEvidence = (id, reference) => api('/analysis/' + id + '/evidence/' + reference)
  const loadStep = (id, stageId) => api('/analysis/' + id + '/steps/' + stageId)
  const running = snapshot?.active.filter(job => job.status === 'running' && job.visual_active !== false).length || 0
  if (auth === null) return <div className="loading">正在连接 ObserveTrack…</div>
  if (!auth) return <div className="login"><section><div className="eyebrow">G.H.O.S.T / OBSERVETRACK</div><Ghost size={120}/><h1>让每一次改进<br/>都有迹可循。</h1><p>你的 GitHub 观察空间。追踪变化，读懂影响。</p><form onSubmit={event => { event.preventDefault(); const password = new FormData(event.currentTarget).get('password'); action(async () => { const data = await post('/login', { password }); csrf = data.csrf; setAuth(true); setPage('overview') }) }}><label>管理员密码<input name="password" type="password" autoComplete="current-password" required/></label><button className="primary" disabled={busy}>进入观察空间 →</button></form>{error && <p role="alert" className="error">{error}</p>}<small>私有工作台 · 不会向 GitHub 写入内容</small></section></div>
  const repos = snapshot?.repositories || []
  const current = repos.find(repo => repo.id === selected)
  return <div className={`app ${snapshot?.settings.quiet || disconnected ? 'quiet' : ''}`}>
    <aside className="navigation"><div className="brand"><Ghost size={48}/><div><strong>G.H.O.S.T</strong><small>OBSERVETRACK</small></div></div><div className="nav-label">观察空间</div><nav>{[['overview', 'grid', '总览'], ['reports', 'report', '报告档案'], ['runs', 'run', '运行记录'], ['settings', 'settings', '设置']].map(([key, icon, label]) => <button key={key} className={page === key || key === 'overview' && page === 'repository' ? 'selected' : ''} onClick={() => navigate(key)}><Icon type={icon}/><span>{label}</span>{key === 'overview' && <small>{repos.length}</small>}</button>)}</nav><div className="nav-bottom"><div className="worker-indicator"><i className={snapshot?.worker_online ? 'online' : ''}/>{snapshot?.worker_online ? '观察引擎在线' : '观察引擎待连接'}</div><small>北京时间 · 每日持续观察</small><button onClick={() => action(async () => { await post('/logout'); csrf = ''; setAuth(false); setSnapshot(null); setDetail(null); setReport(null); setJobs([]); setReports([]); setFlowId(null); setFlowDetail(null) })}>退出登录</button></div></aside>
    <div className="workspace"><header className="topbar"><span className="breadcrumb">Workspace <span>/</span> {page === 'repository' ? current?.full_name : page === 'reader' ? '阅读报告' : { overview: '观察总览', reports: '报告档案', runs: '运行记录', settings: '设置' }[page]}</span><div className="top-actions"><span className="live-dot"/>只读观察<button className="avatar" title="管理员">G</button></div></header>
    <main><div ref={heading} tabIndex={-1} className="page-focus"/>{error && <div className="banner error" role="alert">{error}<button onClick={() => setError('')}>关闭</button></div>}{notice && <div className="banner" role="status">{notice}<button onClick={() => setNotice('')}>知道了</button></div>}
    {!snapshot ? <Empty title="连接观察空间">正在读取真实运行状态…</Empty> : <>
    {page === 'overview' && <><section className="hero"><div><div className="eyebrow">YOUR OPEN SOURCE OBSERVATORY</div><h1>追踪变化，<span>洞见下一步。</span></h1><p>代码、讨论与社区，一处观察。重要改进不遗漏，安静的一天也如实记录。</p><div className="hero-meta"><span>◷ 下期日报 {formatTime(snapshot.next_daily)}</span><span>Asia / Shanghai</span></div></div><div className="hero-ghost"><div className="orbit"/><Ghost size={150} active={running > 0}/><span>{running ? `${running} 项观察正在进行` : '等待下一次变化'}</span></div></section>
    <div className="stats">{[['追踪仓库', repos.length, '最多 20 个公开项目'], ['有内容变化', repos.filter(repo => activityState(repo) === 'changed').length, '以最新观察结果为准'], ['无变化已跳过', repos.filter(repo => activityState(repo) === 'skipped_no_change').length, '完整检查后才跳过'], ['正在处理', snapshot.active.length, '包含排队与配额等待']].map(([label, value, note]) => <section key={label}><small>{label}</small><strong>{value.toString().padStart(2, '0')}</strong><span>{note}</span></section>)}</div>
    {(!snapshot.readiness.github || !snapshot.readiness.model) && <div className="config-notice"><Icon type="settings"/><span>{!snapshot.readiness.github ? 'GitHub Token 尚未配置：公开 REST 可观察，Discussions 将显示缺少授权。' : '独立模型尚未配置：可生成事实简报，暂不调用 AI。'}</span></div>}
    <div className="section-title"><div><h2>我的观察列表 <small>{repos.length}</small></h2><p>关注真实变化，保留完整证据。</p></div><button className="primary" onClick={() => setModal(true)}><Icon type="plus" size={17}/>添加仓库</button></div>
    {repos.length ? <div className="repo-grid">{repos.map((repo, index) => <article className="repo-card" key={repo.id}><div className="repo-top"><span className={`repo-symbol color-${index % 3}`}>{repo.full_name.split('/')[0].slice(0, 2).toUpperCase()}</span><Status value={activityState(repo)}/></div><button className="repo-name" onClick={() => openRepo(repo.id)}>{repo.full_name.split('/')[0]}<strong>{repo.full_name.split('/')[1]}</strong></button><p className="description">{repo.description || '基线建立后，这里会显示仓库简介与最新观察结果。'}</p><div className="repo-numbers"><span>☆ {repo.metrics.stars?.toLocaleString() ?? '—'}</span><span>⑂ {repo.metrics.forks?.toLocaleString() ?? '—'}</span><span className="language">{repo.language || '待采集'}</span></div><div className="repo-footer"><span>{repo.enabled ? `最近 ${formatTime(repo.latest_job?.ended_at)}` : '已暂停观察'}</span><button onClick={() => openRepo(repo.id)}>查看观察 →</button></div></article>)}</div> : <Empty title="从一个你关心的仓库开始">添加 GitHub 仓库后，小幽灵会建立基线，并在变化发生时整理报告。</Empty>}</>}
    {page === 'repository' && (detail ? <><div className="section-title"><div><div className="eyebrow">REPOSITORY INTELLIGENCE</div><h1>{detail.repository.full_name}</h1><p>{detail.repository.metadata.description}</p></div><div className="button-row"><button disabled={busy} onClick={() => action(async () => { await post(`/repositories/${selected}/check`); setNotice('已排队，任务会在可用执行槽中开始。') })}>立即检查</button><button onClick={() => action(async () => { await patch(`/repositories/${selected}`, { enabled: !detail.repository.enabled }) })}>{detail.repository.enabled ? '暂停观察' : '恢复观察'}</button></div></div><div className="coverage-grid">{detail.coverage.map(item => <div key={item.dimension}><span>{dimensions[item.dimension]}</span><Status value={item.status}/><small>{item.error || formatTime(item.checked_at)}</small></div>)}</div>
    <section className="panel"><h2>报告时间线</h2><div className="report-strip">{detail.reports.map(item => <button key={item.id} onClick={() => openReport(item.id)}><Icon type="report"/><strong>{item.period}</strong><small>{item.kind === 'weekly' ? '周报' : '日报'} · v{item.version}</small><Status value={item.status}/></button>)}{!detail.reports.length && <p>采集完成后生成第一份报告。</p>}</div><form className="inline-form" onSubmit={event => { event.preventDefault(); const data = new FormData(event.currentTarget); action(async () => { await post(`/repositories/${selected}/reports`, { kind: data.get('kind'), period: data.get('period') }); setNotice('报告生成已排队。') }) }}><label>生成指定周期<select name="kind"><option value="daily">日报</option><option value="weekly">周报（起始周一）</option></select></label><label>起始日期<input name="period" type="date" required/></label><button disabled={busy}>生成报告</button></form></section>
    <section className="panel"><div className="section-title"><h2>变化记录</h2><label className="filter-label">维度<select value={filter} onChange={event => { setDetail(null); setFilter(event.target.value) }}><option value="">全部维度</option>{Object.entries(dimensions).map(([key, label]) => <option value={key} key={key}>{label}</option>)}</select></label></div><div className="event-list">{detail.events.map(item => <article key={item.id}><span className="event-mark"/><div><div className="event-meta">{dimensions[item.dimension]} <span>{formatTime(item.occurred_at)}</span></div><h3>{item.data.title || item.data.body?.slice(0, 160) || item.entity_id}</h3><p>{item.transition?.from && item.transition?.to ? `${item.transition.from} → ${item.transition.to}` : item.data.event || item.data.state || '内容更新'}</p><small>证据 {item.evidence_id.slice(0, 16)} · {item.data.author || 'GitHub'}</small>{/^https:\/\/(github\.com|api\.github\.com)\//.test(item.data.url) && <a href={item.data.url} target="_blank" rel="noreferrer">查看来源 ↗</a>}</div></article>)}</div>{!detail.events.length && <p className="muted">当前筛选下没有记录；请结合采集覆盖判断。</p>}{detail.has_more && <button onClick={() => action(async () => { const more = await api(`/repositories/${selected}?dimension=${filter}&before=${detail.next_before}`); setDetail(previous => ({ ...more, events: [...previous.events, ...more.events], expanded: true })) })}>加载更早记录</button>}</section>
    <details className="panel"><summary>仓库关注设置</summary><form className="settings-form" onSubmit={event => { event.preventDefault(); const data = new FormData(event.currentTarget); action(async () => { await patch(`/repositories/${selected}`, { labels: data.get('labels').split(',').map(item => item.trim()).filter(Boolean), paths: data.get('paths').split(',').map(item => item.trim()).filter(Boolean) }); setNotice('关注设置已保存。') }) }}><label>优先标签（逗号分隔）<input name="labels" defaultValue={detail.repository.config.labels?.join(', ')}/></label><label>优先路径（逗号分隔）<input name="paths" defaultValue={detail.repository.config.paths?.join(', ')}/></label><button>保存关注项</button></form><button className="danger" onClick={() => { if (confirm('停止追踪此仓库？已有报告按保留策略保存。')) action(async () => { await api(`/repositories/${selected}`, { method: 'DELETE' }); navigate('overview') }) }}>移除仓库</button></details></> : <Empty title="正在读取仓库记录">加载持久化证据与报告。</Empty>)}
    {page === 'reports' && <><div className="section-title"><div><div className="eyebrow">KNOWLEDGE, WITH A TIMELINE</div><h1>报告档案</h1><p>日报看变化，周报看演进。每份判断都带着来源。</p></div></div><div className="archive-list">{reports.map(item => <button className="archive-item" key={item.id} onClick={() => openReport(item.id)}><div className="date-tile"><small>{item.kind === 'weekly' ? 'WEEKLY' : 'DAILY'}</small><strong>{item.period.slice(5)}</strong></div><div><h3>{item.full_name}</h3><p>{item.kind === 'weekly' ? '周报' : '日报'} · 版本 {item.version} · {formatTime(item.created_at)}</p></div><Status value={item.status}/><span>↗</span></button>)}</div>{!reports.length && <Empty title="报告还在路上">添加仓库并完成首轮采集后，报告会出现在这里。</Empty>}</>}
    {page === 'reader' && (report ? <><div className="section-title"><div><div className="eyebrow">OBSERVATION / {report.kind === 'weekly' ? 'WEEKLY' : 'DAILY'}</div><h1>{report.period} 观察报告</h1><p>版本 {report.version} · {formatTime(report.created_at)} · {labels[report.data.analysis.status]}</p></div><button onClick={() => action(async () => { await post(`/reports/${report.id}/regenerate`); setNotice('新版本已排队，旧版保留。') })}>重新分析</button></div><div className="reader-layout"><aside className="reader-nav"><h3>阅读目录</h3>{['观察范围', ...['本期结论', '改进与升级影响', 'PR 与 Issue', '安全观察', '社区讨论', '使用建议', '贡献机会', '未验证事项'], '事实简报', '变化与来源', '变更图'].map(label => <a key={label} href={'#' + encodeURIComponent(label)}>{label}</a>)}<div className="exports">{['md', 'html', 'zip'].map(kind => <a key={kind} href={`/api/reports/${report.id}/download/${kind}`}>下载 {kind.toUpperCase()} ↓</a>)}</div></aside><article className="report-body"><Markdown remarkPlugins={[remarkGfm]} components={{ h2: ({ children }) => <h2 id={String(children)}>{children}</h2>, a: ({ href, children }) => <a href={href} target="_blank" rel="noreferrer">{children}</a>, img: () => <span>外部图片未自动加载</span> }}>{report.data.reader_markdown || report.markdown}</Markdown>{report.data.analysis.collaboration && <details className="editor-section"><summary>查看主编与专家分析</summary><EditorAnalysis summary={report.data.analysis.collaboration} evidence={report.data.evidence} onContinue={continueFlow} onInspect={inspectFlow} loadStep={loadStep} loadEvidence={loadEvidence}/></details>}<h2 id="变更图">变更图</h2>{Object.entries(report.data.charts).map(([name, svg]) => <figure key={name}><img alt={{ trend: '指标与活动趋势', transitions: '事项状态变更', relationships: '交付关联图' }[name]} src={'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(svg)}/><figcaption><a href={`/api/reports/${report.id}/download/${name}`}>下载 SVG ↓</a></figcaption></figure>)}</article></div></> : <Empty title="请选择一份报告">前往报告档案浏览。</Empty>)}
    {page === 'runs' && <><div className="section-title"><div><div className="eyebrow">EVERY STEP IS ACCOUNTED FOR</div><h1>运行记录</h1><p>等待、采集、分析与跳过，都有真实记录。</p></div><button onClick={() => poller.current?.refresh()}>刷新</button></div><div className="run-list">{jobs.map(job => <article className="panel run-card" key={job.id}><header><div><h3>{job.full_name}</h3><small>{job.kind === 'collect' ? 'GitHub 采集' : job.kind === 'weekly' ? '周报' : '日报'} · {job.period} · {formatTime(job.created_at)}</small></div><Status value={job.cancel && job.status === 'running' ? 'stopping' : job.status}/></header><div className="run-summary"><span>{phaseLabel(job.phase)}</span><span>GitHub 请求 {job.requests}</span><span>模型调用 {job.model_calls.length}</span><span>重试 {job.model_calls.reduce((total, call) => total + call.retries, 0)}</span></div>{job.error && <p className="error">{job.error}</p>}{job.status === 'waiting_rate_limit' && <p>预计可重试时间 {formatTime(job.not_before)} · 等待不会调用模型。</p>}{job.analysis_flow && <EditorRun summary={job.analysis_flow} inspect={inspectFlow}/>}<details><summary>执行详情</summary><pre>{JSON.stringify({ result: job.result, model_calls: job.model_calls }, null, 2)}</pre></details><div className="button-row">{['queued', 'running', 'waiting_rate_limit'].includes(job.status) ? <button disabled={busy || !!job.cancel} onClick={() => action(() => post(`/jobs/${job.id}/cancel`))}>停止此任务</button> : <button disabled={busy} onClick={() => action(() => post(`/jobs/${job.id}/retry`))}>重试此阶段</button>}</div></article>)}</div>{!jobs.length && <Empty title="没有运行记录">观察任务会先进入持久化队列。</Empty>}</>}
    {page === 'settings' && <Settings snapshot={snapshot} save={values => action(async () => { await patch('/settings', values); setNotice('设置已保存，后续调度使用新值。') })}/>}
    </>}</main><footer className="site-footer">G.H.O.S.T — GitHub ObserveTrack <span>只读 · 有据 · 持续观察</span></footer></div>
    {flowId && <EditorDialog summary={flowDetail} close={() => { setFlowId(null); setFlowDetail(null) }} onContinue={continueFlow} loadStep={loadStep} loadEvidence={loadEvidence}/>}
    {modal && <AddDialog close={() => setModal(false)} submit={add} busy={busy}/>}
  </div>
}

function AddDialog({ close, submit, busy }) {
  const dialog = useRef(null)
  useEffect(() => { const origin = document.activeElement; dialog.current.showModal(); return () => origin?.focus() }, [])
  return <dialog ref={dialog} onCancel={close}><form onSubmit={submit}><div className="section-title"><h2>添加观察仓库</h2><button type="button" aria-label="关闭" onClick={close}>×</button></div><p>支持公开 GitHub 仓库。首次采集建立基线并回补最近 7 天可见活动。</p><label>仓库地址<input autoFocus name="name" placeholder="owner/repository" required/></label><button className="primary" disabled={busy}>{busy ? '验证仓库…' : '开始观察 →'}</button></form></dialog>
}

function Settings({ snapshot, save }) {
  const preferences = snapshot.settings
  return <><div className="section-title"><div><div className="eyebrow">MAKE OBSERVATION YOURS</div><h1>观察设置</h1><p>时区固定为北京时间，预算是上限，不会自动用满。</p></div></div><section className="panel"><h2>日程与分析预算</h2><form className="settings-form" onSubmit={event => { event.preventDefault(); const data = new FormData(event.currentTarget); save(Object.fromEntries([...data].map(([key, value]) => [key, ['daily_items', 'weekly_items', 'analysis_calls', 'retention_days', 'analysis_v2_base_calls', 'analysis_v2_extra_calls', 'analysis_followup_requests'].includes(key) ? Number(value) : key === 'quiet' ? value === 'true' : value]))) }}>{[['collect_time', '每日采集时间', 'time'], ['daily_time', '日报发布时间', 'time'], ['weekly_time', '周一周报时间', 'time'], ['daily_items', '日报重点条目（最多 20）', 'number'], ['weekly_items', '周报重点条目（最多 40）', 'number'], ['analysis_calls', '旧版逻辑调用（最多 4）', 'number'], ['analysis_v2_base_calls', 'V2 基础调用（2–6）', 'number'], ['analysis_v2_extra_calls', 'V2 追加额度（0–2）', 'number'], ['analysis_followup_requests', 'V2 补读请求（0–12）', 'number'], ['retention_days', '数据保留天数（最多 365）', 'number']].map(([key, label, type]) => <label key={key}>{label}<input type={type} name={key} defaultValue={preferences[key]} required min={['analysis_v2_extra_calls', 'analysis_followup_requests'].includes(key) ? 0 : 1}/></label>)}<label>角色动画<select name="quiet" defaultValue={String(preferences.quiet)}><option value="false">活跃（遵守系统减少动态效果）</option><option value="true">安静</option></select></label><button className="primary">保存设置</button></form></section><section className="panel"><h2>独立服务配置</h2><div className="configuration"><span>GitHub 只读凭据</span><Status value={snapshot.readiness.github ? 'complete' : 'unauthorized'}/><span>独立模型接口</span><Status value={snapshot.readiness.model ? 'complete' : 'model_unconfigured'}/></div><p className="muted">凭据仅由服务器环境配置管理，网页不会显示或保存密钥。未配置模型仍可阅读事实简报。Discussions 需要 GitHub Token；不可见的数据会如实标记。</p></section></>
}

createRoot(document.getElementById('root')).render(<App/>)
