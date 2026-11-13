"""Render README workflow diagrams as standalone SVGs, without external services."""
from html import escape
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / 'docs' / 'images'
LABELS = {
    'zh': {
        'title': 'ObserveTrack 工作原理',
        'start': ['手动检查 / 定时计划'],
        'collect': ['GitHub 只读采集'],
        'save': ['保存基线、游标、证据与内容版本'],
        'branches': [
            ['内容变化', '事实简报 / 可选模型分析'],
            ['仅指标变化', '更新指标与图表', '跳过模型'],
            ['完整检查无变化', '记录无变化', '跳过模型'],
            ['分页失败或缺失', '记录不完整与具体原因'],
        ],
        'records': ['保存运行结果与覆盖范围', '按可用材料生成日报 / 周报，保留来源与版本'],
        'view': ['Web 阅读 / 证据查看 / 图表 / 导出'],
        'note': '采集不完整不等于没有变化；报告保留缺失范围与限制。',
    },
    'en': {
        'title': 'How ObserveTrack works',
        'start': ['Manual check / Scheduled job'],
        'collect': ['Read only GitHub collection'],
        'save': ['Save baselines, cursors, evidence, and content versions'],
        'branches': [
            ['Content changed', 'Factual brief /', 'Optional model analysis'],
            ['Metrics only', 'Update metrics and charts', 'Skip model'],
            ['Complete check, no changes', 'Record no changes', 'Skip model'],
            ['Missing pages or failures', 'Record incomplete coverage', 'and reasons'],
        ],
        'records': ['Save run results and coverage', 'Build daily / weekly reports from available material; retain sources and versions'],
        'view': ['Web reader / Evidence / Charts / Exports'],
        'note': 'Incomplete collection is not a no change result. Report missing coverage and limitations.',
    },
}


def render(language, labels):
    parts = [f'''<svg xmlns="http://www.w3.org/2000/svg" width="1120" height="780" viewBox="0 0 1120 780" role="img" aria-labelledby="title desc" xml:lang="{language}">
<title id="title">{escape(labels['title'])}</title>
<desc id="desc">{escape(labels['note'])}</desc>
<defs><marker id="arrow" markerWidth="10" markerHeight="10" refX="8" refY="5" orient="auto"><path d="M0 0 L10 5 L0 10 Z" fill="#64748b"/></marker></defs>
<rect width="1120" height="780" rx="20" fill="#f8fafc"/>
<g font-family="system-ui, -apple-system, 'Segoe UI', 'Microsoft YaHei', sans-serif" text-anchor="middle" fill="#0f172a">
<text x="560" y="42" font-size="25" font-weight="700">{escape(labels['title'])}</text>''']

    def box(x, y, width, height, lines, fill='#ffffff'):
        parts.append(f'<rect x="{x}" y="{y}" width="{width}" height="{height}" rx="12" fill="{fill}" stroke="#94a3b8"/>')
        start = y + height / 2 - (len(lines) - 1) * 13 + 6
        for index, line in enumerate(lines):
            parts.append(f'<text x="{x + width / 2}" y="{start + index * 26}" font-size="17">{escape(line)}</text>')

    def arrow(path):
        parts.append(f'<path d="{path}" fill="none" stroke="#64748b" stroke-width="2" marker-end="url(#arrow)"/>')

    box(310, 65, 500, 54, labels['start'])
    arrow('M560 119 V143')
    box(310, 145, 500, 54, labels['collect'])
    arrow('M560 199 V223')
    box(220, 225, 680, 62, labels['save'], '#e0f2fe')
    for index, lines in enumerate(labels['branches']):
        x = 30 + index * 275
        center = x + 117.5
        arrow(f'M560 287 V318 H{center} V348')
        box(x, 350, 235, 126, lines, '#ecfdf5' if index == 0 else '#ffffff')
        arrow(f'M{center} 476 V516 H560 V546')
    box(140, 548, 840, 84, labels['records'], '#e0f2fe')
    arrow('M560 632 V656')
    box(220, 658, 680, 56, labels['view'])
    parts.append(f'<text x="560" y="752" font-size="16" fill="#475569">{escape(labels["note"])}</text></g></svg>')
    (OUT / f'workflow-{language}.svg').write_text('\n'.join(parts) + '\n', encoding='utf-8')


if __name__ == '__main__':
    OUT.mkdir(parents=True, exist_ok=True)
    for language, labels in LABELS.items():
        render(language, labels)
