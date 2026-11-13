import html
from collections import Counter
from datetime import datetime
from zoneinfo import ZoneInfo


def escape(value):
    return html.escape(str(value), quote=True)


def svg(title, content, height=300):
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 900 {height}" role="img" aria-label="{escape(title)}">
    <rect width="900" height="{height}" rx="20" fill="#151e28"/><style>text{{font-family:system-ui,sans-serif;fill:#dae4ec;font-size:13px}}</style>
    <text x="28" y="36" style="font-size:19px;font-weight:600">{escape(title)}</text>{content}</svg>'''


def trend(metrics, changes):
    content = '<text x="28" y="61">真实采样点 · 间隔超过 36 小时断线 · 首次基线不外推历史</text>'
    if not metrics:
        return svg('观察趋势', content + '<text x="28" y="120">尚无指标采样</text>')
    for index, (field, label, color) in enumerate([('stars', 'Stars', '#9ce0c8'), ('forks', 'Forks', '#b7b0ee'), ('open_items', '开放 Issue + PR', '#86c8f6')]):
        values = [sample['data'].get(field) for sample in metrics]
        known = [value for value in values if value is not None]
        maximum, minimum = max(known, default=1), min(known, default=0)
        top = 95 + index * 115
        content += f'<text x="28" y="{top}">{label} · 当前 {escape(values[-1] if values[-1] is not None else "未采集")}</text>'
        previous = None
        for offset, sample in enumerate(metrics):
            value = sample['data'].get(field)
            if value is None:
                previous = None
                continue
            horizontal = 210 + offset * 630 / max(1, len(metrics) - 1)
            vertical = top + 55 - (value - minimum) / max(1, maximum - minimum) * 45
            if previous and sample['observed_at'] - previous[2] <= 36 * 3600:
                content += f'<path d="M{previous[0]:.1f},{previous[1]:.1f} L{horizontal:.1f},{vertical:.1f}" stroke="{color}" stroke-width="2" fill="none"/>'
            content += f'<circle cx="{horizontal:.1f}" cy="{vertical:.1f}" r="3" fill="{color}"><title>{escape(value)}</title></circle>'
            previous = (horizontal, vertical, sample['observed_at'])
    counts = Counter(item['dimension'] for item in changes)
    content += '<text x="28" y="450">本期已记录对象变化（不是全仓库历史活动）</text>'
    for index, (dimension, count) in enumerate(sorted(counts.items())):
        vertical = 480 + index * 29
        content += f'<text x="28" y="{vertical}">{escape(dimension)}</text><rect x="185" y="{vertical - 13}" height="17" width="{max(2, count / max(counts.values()) * 550):.1f}" rx="4" fill="#578e86"/><text x="755" y="{vertical}">{count}</text>'
    offset = max(520, 510 + len(counts) * 29)
    activity = Counter((datetime.fromtimestamp(item['occurred_at'], ZoneInfo('Asia/Shanghai')).strftime('%m-%d'), item['dimension']) for item in changes if item.get('occurred_at'))
    dates = sorted({day for day, dimension in activity})
    content += f'<text x="28" y="{offset}">按日观察到的活动 · 空白表示没有可展示记录，不补零</text>'
    for index, dimension in enumerate(['commits', 'pulls', 'issues', 'issue_comments', 'discussions']):
        vertical = offset + 40 + index * 43
        content += f'<text x="28" y="{vertical}">{escape(dimension)}</text>'
        for position, day in enumerate(dates):
            count = activity.get((day, dimension))
            horizontal = 240 + position * 600 / max(1, len(dates) - 1)
            if index == 0:
                content += f'<text x="{horizontal-14:.1f}" y="{offset+20}">{escape(day)}</text>'
            if count:
                content += f'<circle cx="{horizontal:.1f}" cy="{vertical-5}" r="15" fill="#25443d"/><text text-anchor="middle" x="{horizontal:.1f}" y="{vertical}">{count}</text>'
    return svg('指标与活动趋势', content, offset + 245)


def transitions(changes):
    counts = Counter()
    for item in changes:
        transition = item.get('transition') or {}
        if item['dimension'] in {'issues', 'pulls'} and transition.get('from') and transition.get('to') and transition['from'] != transition['to']:
            counts[(transition['from'], transition['to'])] += 1
        elif transition.get('event') in {'closed', 'reopened', 'merged'}:
            counts[('事件', transition['event'])] += 1
    content = '<text x="28" y="66">状态快照差异与独立事件分列，不相加推断唯一事项数</text>'
    if not counts:
        content += '<text x="28" y="120">本期没有观测到可确认的状态流转</text>'
    for index, ((before, after), count) in enumerate(counts.items()):
        vertical = 100 + index * 58
        content += f'<rect x="28" y="{vertical}" width="240" height="35" rx="8" fill="#283445"/><text x="45" y="{vertical+23}">{escape(before)}</text><text x="300" y="{vertical+23}">→ {count} 条 →</text><rect x="460" y="{vertical}" width="240" height="35" rx="8" fill="#25443d"/><text x="480" y="{vertical+23}">{escape(after)}</text>'
    return svg('事项状态变更', content, max(210, 160 + len(counts) * 58))


def relationships(evidence):
    edges = set()
    for item in evidence:
        data = item['data']
        target = data.get('title') or str(data.get('number') or item['entity_id'])
        if data.get('source', {}).get('number'):
            edges.add((f"关联事项 #{data['source']['number']}", f"事项 {data.get('parent')}", 'GitHub timeline'))
        if data.get('merge_commit_sha'):
            edges.add((f"PR #{data.get('number')}", data['merge_commit_sha'][:12], 'merge_commit_sha'))
        if data.get('sha') and item['dimension'] == 'tags':
            edges.add((target, data['sha'][:12], 'Tag 指向'))
        if data.get('tag_name') and item['dimension'] == 'releases':
            edges.add((target, data['tag_name'], 'Release tag_name'))
    content = '<text x="28" y="66">仅展示 API 明确关联；不会根据相似标题补线</text>'
    if not edges:
        content += '<text x="28" y="120">当前证据没有明确关联边</text>'
    for index, (origin, target, reason) in enumerate(sorted(edges)[:30]):
        vertical = 100 + index * 48
        content += f'<text x="28" y="{vertical}">{escape(origin[:35])}</text><path d="M300 {vertical-5} H470" stroke="#9ce0c8"/><text x="480" y="{vertical}">{escape(target[:35])}</text><text x="700" y="{vertical}">{escape(reason)}</text>'
    if len(edges) > 30:
        content += '<text x="28" y="1580">图中显示前 30 条，完整证据见报告来源</text>'
    return svg('交付关联图', content, max(210, 150 + min(30, len(edges)) * 48))
