export const labels = {
  queued: '等待处理', running: '正在观察', stopping: '正在收尾', completed: '已完成', cancelled: '已取消', interrupted: '已中断',
  waiting_rate_limit: '等待 GitHub 配额', failed: '采集失败', partial: '检查未完整', baseline: '基线建立', changed: '有内容变化',
  metrics_only: '仅指标变化', skipped_no_change: '无变化 · 已跳过', complete: '已完整采集', disabled: '仓库未启用',
  unauthorized: '缺少授权', forbidden: '权限不足', not_found: '不可访问', unsupported: '接口不支持', not_checked: '尚未检查',
  model_unconfigured: '等待模型配置', analysis_failed: '分析失败 · 事实保留', analysis_partial: '部分分析完成', analyzed: '分析完成', skipped: '未调用模型',
}
export const dimensions = { commits: '代码提交', releases: '版本发布', tags: '标签', issues: 'Issues', issue_comments: 'Issue 评论', pulls: 'Pull requests', review_comments: '评审评论', discussions: '讨论', security: '安全公告' }
export const phaseLabel = phase => ({ 'v2:planning': '主编选题与分工', 'v2:experts': '专家分析与补证', 'v2:accepting': '主编验收', 'v2:finalizing': '主编整理交付', completed: '执行已完成', recovered: '恢复只读采集' }[phase] || phase?.replace('collecting:', '正在采集 · ').replace('analyzing:', '正在分析 · ') || '等待执行')
export const formatTime = value => value ? new Date(typeof value === 'number' ? value * 1000 : value).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }) : '—'
export function activityState(repo) {
  const job = repo.latest_job
  if (job && ['queued', 'running', 'waiting_rate_limit', 'failed', 'interrupted'].includes(job.status)) return job.cancel ? 'stopping' : job.status
  return repo.latest_report?.status || (job?.result ? (() => { try { return JSON.parse(job.result).status } catch { return 'queued' } })() : 'queued')
}
export function createPoller(load, emit, options = {}) {
  const timer = options.timer || globalThis
  let stopped = false, pending = false, rerun = false, handle = null, controller = null
  async function refresh() {
    if (stopped) return
    if (pending) { rerun = true; return }
    pending = true
    timer.clearTimeout(handle)
    controller = new AbortController()
    try { const data = await load(controller.signal); if (!stopped) emit(null, data) }
    catch (error) { if (!stopped && error.name !== 'AbortError') emit(error) }
    finally { pending = false; if (!stopped) { handle = timer.setTimeout(refresh, rerun ? 0 : options.hidden?.() ? 90000 : 15000); rerun = false } }
  }
  return { refresh, stop() { stopped = true; timer.clearTimeout(handle); controller?.abort() } }
}
