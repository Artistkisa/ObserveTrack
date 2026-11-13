import React, { useEffect, useRef, useState } from 'react'
import Ghost from './Ghost'
import { formatTime } from './state'

const roles = { editor: '主编', code: '代码与发布', security: '安全', community: '社区与贡献', reviewer: '独立复核' }
const states = { queued: '等待模型槽', running: '正在请求模型', waiting_retry: '等待重试', completed: '已完成', partial: '部分结果已保留', failed: '阶段失败', interrupted: '已中断' }
const dispositions = { adopt: '采用', merge: '合并', revise: '要求修订', disputed: '保留分歧', decline: '不采用' }

export function EditorBudget({ summary }) {
  const budget = summary.budget
  return <div className="editor-budget"><span>调用 <strong>{summary.usage.logical_calls}</strong> / {budget.base + (budget.extra_authorized ? budget.extra : 0)}</span><span>基础 {budget.base} · 可追加 {budget.extra}</span><span>剩余 {summary.remaining}</span><span>补读请求 {budget.followup_used} / {budget.followup_limit}</span><span>Token {summary.usage.tokens_complete ? summary.usage.tokens : `${summary.usage.tokens}（部分未返回）`}</span>{budget.extra_authorized && <p>追加原因：{budget.extra_reason?.reason}</p>}</div>
}

export function EditorRun({ summary, inspect }) {
  return <section className="editor-run"><div className="section-title"><strong>主编与专家 · V2</strong><button onClick={() => inspect(summary.id)}>查看分析流程 →</button></div><EditorBudget summary={summary}/><div className="editor-stages">{summary.stages.map(stage => <div className={`editor-stage state-${stage.status}`} key={stage.id}><Ghost size={36} active={stage.status === 'running'}/><div><strong>{roles[stage.role] || stage.role}</strong><span>{states[stage.status] || stage.status}</span></div></div>)}</div></section>
}

export default function EditorAnalysis({ summary, evidence = [], onContinue, onInspect, loadStep, loadEvidence }) {
  const [selected, setSelected] = useState(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const [source, setSource] = useState(null)
  const sourceNode = useRef(null)
  const mounted = useRef(true)
  useEffect(() => () => { mounted.current = false }, [])
  useEffect(() => { if (source) { sourceNode.current?.scrollIntoView({ block: 'nearest' }); sourceNode.current?.focus() } }, [source])
  if (!summary) return null
  const citations = new Set((summary.findings || []).flatMap(finding => (finding.claims || []).flatMap(claim => claim.evidence_ids)))
  const open = async stage => {
    if (!loadStep || loading) return
    setLoading(true)
    try { const result = await loadStep(summary.id, stage.id); if (mounted.current) { setSelected(result); setError('') } }
    catch (failure) { if (mounted.current) setError(failure.message) }
    finally { if (mounted.current) setLoading(false) }
  }
  const openSource = async (event, identity) => {
    event.preventDefault()
    if (loading) return
    setLoading(true)
    try {
      const value = evidence.find(item => item.id === identity) || await loadEvidence?.(summary.id, identity)
      if (mounted.current) { setSource(value); if (!value) setError('当前视图无法加载此证据，请在流程详情中查看。') }
    } catch (failure) { if (mounted.current) setError(failure.message) }
    finally { if (mounted.current) setLoading(false) }
  }
  return <section className="editor-analysis" aria-label="主编与专家分析"><div className="section-title"><div><div className="eyebrow">EDITORIAL WORKSPACE</div><h2>主编与专家分析</h2><p>{states[summary.status] || summary.status} · {formatTime(summary.updated_at)}</p></div><Ghost size={54} active={summary.stages.some(stage => stage.role === 'editor' && stage.status === 'running')}/></div>
    <EditorBudget summary={summary}/>
    <div className="editor-stages">{summary.stages.map(stage => <button disabled={!loadStep || loading} className={`editor-stage state-${stage.status}`} key={stage.id} onClick={() => open(stage)}><span className="editor-seq">{stage.seq}</span><div><strong>{roles[stage.role] || stage.role}</strong><span>{states[stage.status] || stage.status}</span></div></button>)}</div>
    <details open className="editor-section"><summary>本期编辑计划</summary>{summary.plans?.length ? summary.plans.map(plan => <div key={plan.stage_id} className="editor-plan">{plan.questions.map((question, index) => <article key={`${question.id}-${index}`}><strong>{question.id} · {question.text}</strong><p>{question.acceptance || '未声明额外交付要求'}</p><small>{roles[question.owner] || question.owner || '主编协调'} · {question.priority === 'high' ? '优先关注' : '常规观察'}</small></article>)}</div>) : <p>主编尚未提交编辑计划。</p>}</details>
    <details className="editor-section"><summary>专家发现与依据（{summary.findings?.length || 0} 份交付）</summary>{summary.findings?.map(finding => <article className="editor-finding" key={finding.stage_id}><h3>{roles[finding.role] || finding.role}</h3><p>{finding.summary}</p>{finding.claims?.map(claim => <div id={`claim-${claim.id}`} className="editor-claim" key={claim.id}><strong>{claim.text}</strong><small>{claim.certainty} · {claim.id}</small>{claim.locators?.map((locator, index) => <blockquote key={index}><p>{locator.quote}</p><a href={`#evidence-${locator.evidence_id}`} onClick={event => openSource(event, locator.evidence_id)}>{locator.filename || locator.field} · 查看证据</a></blockquote>)}</div>)}{finding.limitations?.map((item, index) => <p className="muted" key={index}>限制：{item}</p>)}</article>)}</details>
    <details className="editor-section"><summary>主编处理与分歧</summary>{summary.decisions?.length ? summary.decisions.map((decision, index) => <article key={index}><a href={`#claim-${decision.claim_id}`} onClick={() => document.querySelectorAll('.editor-analysis details').forEach(node => { node.open = true })}>{decision.claim_id}</a><strong> · {dispositions[decision.disposition]}</strong><p>{decision.reason}</p></article>) : <p>尚无明确处理记录。</p>}{summary.unresolved?.length > 0 && <div className="editor-unresolved"><h3>未解决事项</h3><ul>{summary.unresolved.map((item, index) => <li key={index}>{item}</li>)}</ul></div>}</details>
    <details className="editor-section"><summary>证据覆盖与实际用量</summary><p>进入请求不等于已理解；“已审阅”仅表示模型显式声明，不代表独立验证。</p><div className="editor-coverage">{summary.evidence_coverage?.map(item => <div key={item.evidence_id}><code title={item.evidence_id}>{item.evidence_id.slice(0, 12)}</code><span>{item.assigned ? '已分配' : '未分配'}</span><span>{item.submitted ? '已进入请求' : '未进入请求'}</span><span>{item.reviewed ? '声明已审阅' : '未声明审阅'}</span><span>{item.cited ? '已引用' : '未引用'}</span></div>)}</div><p>实际请求 {summary.usage.requests} · 重试 {summary.usage.retries} · 等待模型槽 {summary.usage.queue_seconds}s · 请求与重试 {summary.usage.request_seconds}s</p></details>
    {evidence.filter(item => citations.has(item.id)).map(item => <details className="editor-section" id={`evidence-${item.id}`} key={item.id}><summary>证据：{item.data.title || item.entity_id}</summary><code>{item.id}</code><p>{item.data.body}</p>{/^https:\/\/(github\.com|api\.github\.com)\//.test(item.data.url || '') && <a href={item.data.url} target="_blank" rel="noreferrer">打开原始来源 ↗</a>}</details>)}
    {error && <p role="alert" className="error">{error}</p>}
    {source && <section className="editor-section" ref={sourceNode} tabIndex={-1}><h3>{source.data.title || source.entity_id}</h3><code>{source.id}</code><p>{source.data.body}</p>{/^https:\/\/(github\.com|api\.github\.com)\//.test(source.data.url || '') && <a href={source.data.url} target="_blank" rel="noreferrer">打开原始来源 ↗</a>}</section>}
    {selected && <details className="editor-section" open><summary>第 {selected.seq} 阶段详情</summary><p>{selected.error}</p><pre>{JSON.stringify(selected.output, null, 2)}</pre><details><summary>实际输入与提示词版本</summary><pre>{JSON.stringify(selected.input, null, 2)}</pre></details></details>}
    <div className="button-row">{['partial', 'interrupted'].includes(summary.status) && onContinue && <button disabled={summary.remaining < 1} onClick={() => onContinue(summary.id)}>继续未完成分析 · 沿用剩余额度</button>}{onInspect && <button onClick={() => onInspect(summary.id)}>查看流程最新状态</button>}</div>
    {summary.remaining < 1 && summary.status !== 'completed' && <p className="muted">原流程额度已用尽。需要继续时，请明确重新生成报告，建立新版本和新预算。</p>}
  </section>
}

export function EditorDialog({ summary, close, onContinue, loadStep, loadEvidence }) {
  const dialog = useRef(null)
  useEffect(() => { const origin = document.activeElement; dialog.current.showModal(); return () => origin?.focus() }, [])
  return <dialog className="editor-dialog" ref={dialog} onCancel={close}><div className="editor-dialog-top"><strong>分析工作台</strong><button aria-label="关闭分析工作台" onClick={close}>关闭 ×</button></div>{summary ? <EditorAnalysis key={summary.id} summary={summary} onContinue={onContinue} loadStep={loadStep} loadEvidence={loadEvidence}/> : <p role="status">正在读取已保存的分析流程…</p>}</dialog>
}
