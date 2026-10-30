"""Regenerate services/<slug>/service.yaml and service.md from the service catalog.

Run from the repository root:

    python tools/scaffold/generate_services.py

The catalog at contracts/service-catalog.v2.json is the single source of truth.
"""

import io
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEAM_CN = {
    'platform': '平台', 'creation': '创作', 'workflow': '工作流', 'assets': '资产',
    'memory': '记忆', 'agents': 'Agent', 'execution': '执行', 'finance': '财务',
    'models': '模型', 'generation': '生成', 'media': '媒体', 'production': '生产',
    'quality': '质量', 'post': '后期', 'governance': '治理', 'analytics': '分析',
    'experience': '体验', 'data': '数据',
}

DOMAIN_CN = {
    'control': '控制面', 'creation': '创作', 'workflow': '工作流', 'assets': '资产',
    'memory': '记忆', 'agents': 'Agent', 'execution': '执行', 'finance': '财务',
    'models': '模型', 'production': '生成', 'quality': '质量', 'post': '后期',
    'enterprise': '企业', 'experience': '体验', 'analytics': '分析', 'governance': '治理',
    'media': '媒体', 'data': '数据', 'generation': '生成',
}

RESERVED = {
    'true', 'false', 'yes', 'no', 'on', 'off', 'null', '~',
    'True', 'False', 'Yes', 'No', 'On', 'Off', 'Null', 'NULL',
}
INDICATORS = set('-?:,[]{}#&*!|>\'"%@`')


def q(value):
    if value is None:
        return 'null'
    s = str(value)
    if s == '':
        return '""'
    plain = s == s.strip()
    if s.lower() in RESERVED:
        plain = False
    if s[0] in INDICATORS:
        plain = False
    if any(ch in s for ch in '\n\r\t'):
        plain = False
    if ': ' in s or ' #' in s or s.endswith(':'):
        plain = False
    if plain:
        try:
            float(s)
            plain = False
        except ValueError:
            pass
    if plain:
        return s
    escaped = s.replace('\\', '\\\\').replace('"', '\\"')
    escaped = escaped.replace('\n', '\\n').replace('\r', '\\r').replace('\t', '\\t')
    return '"%s"' % escaped


def render_yaml(s, topic_by_type):
    events = []
    for et in s['published_events']:
        t = topic_by_type.get(et, {})
        events.append((et, t.get('topic', ''), str(t.get('normal_retention_days', ''))))

    lines = [
        '# %s · %s' % (s['service_id'], s['name']),
        '# 由 contracts/service-catalog.v2.json 生成，勿手工编辑；改动请改 catalog。',
        '',
        'service_id: %s' % q(s['service_id']),
        'slug: %s' % q(s['slug']),
        'name: %s' % q(s['name']),
        'maturity: %s' % q(s['maturity']),
        '',
        'domain: %s' % q(s['domain']),
        'owner_team: %s' % q(s['runtime_owner_team']),
        'schema_owner: %s' % q(s['schema_owner']),
        'deployment_unit: %s' % q(s['deployment_unit']),
        '',
        '# 数据所有权：本服务是下列事实的唯一写者',
        'data_owner: %s' % q(s['data_owner']),
        '',
        'api:',
        '  command: %s' % q(s['command_api']),
        '  query: %s' % q(s['query_api']),
        '',
    ]
    if events:
        lines.append('publishes:')
        for et, topic, days in events:
            lines.append('  - event_type: %s' % q(et))
            if topic:
                lines.append('    topic: %s' % q(topic))
            if days:
                lines.append('    retention_days: %s' % days)
        lines.append('')
    if s['depends_on']:
        lines.append('depends_on:')
        for d in s['depends_on']:
            lines.append('  - %s' % q(d))
        lines.append('')
    lines += [
        'runtime:',
        '  scaling: %s' % q(s['scaling']),
        '  recovery: %s' % q(s['recovery']),
        '',
        'acceptance: %s' % q(s['acceptance']),
        '',
        '# SLO 为 docs/03 的规划目标，尚未实测',
        'slo:',
        '  verified: false',
        '',
    ]
    return '\n'.join(lines)


def render_md(s):
    spec = 'S%s-%s.md' % (s['service_id'][1:], s['slug'])
    return '\n'.join([
        '# %s · %s' % (s['service_id'], s['name']),
        '',
        '状态：`%s` —— 尚未实现。' % s['maturity'],
        '',
        '| 项 | 值 |',
        '|---|---|',
        '| 域 | %s |' % DOMAIN_CN.get(s['domain'], s['domain']),
        '| 团队 | %s |' % TEAM_CN.get(s['runtime_owner_team'], s['runtime_owner_team']),
        '| schema | `%s` |' % s['schema_owner'],
        '| 命令入口 | `%s` |' % s['command_api'],
        '| 查询入口 | `%s` |' % s['query_api'],
        '',
        '设计说明：[%s](../../docs/design-package/services/%s)' % (spec, spec),
        '',
        '本服务的实现约束见 `service.yaml`（机器可读）与上述设计说明（语义与验收）。',
        '',
    ])


def main():
    with io.open(os.path.join(ROOT, 'contracts/service-catalog.v2.json'), encoding='utf-8') as f:
        catalog = json.load(f)['services']
    with io.open(os.path.join(ROOT, 'contracts/event-topics.v2.json'), encoding='utf-8') as f:
        topics = json.load(f)
    if isinstance(topics, dict):
        topics = topics.get('topics', topics.get('events', []))
    topic_by_type = {t['event_type']: t for t in topics}

    for s in catalog:
        base = os.path.join(ROOT, 'services', s['slug'])
        os.makedirs(base, exist_ok=True)
        with io.open(os.path.join(base, 'service.yaml'), 'w',
                     encoding='utf-8', newline='\n') as f:
            f.write(render_yaml(s, topic_by_type))
        with io.open(os.path.join(base, 'service.md'), 'w',
                     encoding='utf-8', newline='\n') as f:
            f.write(render_md(s))
    print('regenerated %d services' % len(catalog))


if __name__ == '__main__':
    main()
