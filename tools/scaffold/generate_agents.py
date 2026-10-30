"""Create agents/<role>/agent.yaml skeletons.

Roles come from docs/08-agents-prompts.md §1. Run from the repository root:

    python tools/scaffold/generate_agents.py
"""

import io
import os

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROLES = [
    ('producer', '制片', '项目进度、资源与预算约束下的任务拆解'),
    ('writer', '编剧', '剧本、对白与情节结构'),
    ('breakdown', '拆解', '剧本到场次/镜头的结构化拆解'),
    ('storyboard-director', '分镜导演', '分镜、景别、机位与运镜'),
    ('asset-curator', '资产策划', '角色/服装/场景/道具的候选与规范'),
    ('memory-extractor', '记忆抽取', '从已接受脚本提取结构化正史候选'),
    ('continuity-reviewer', '连续性审查', '跨集一致性与连续性缺陷'),
    ('prompt-director', 'Prompt导演', '导演DSL到编译Prompt的语义映射'),
    ('qc-reviewer', 'QC评审', '技术/语义质检结论与缺陷分级'),
    ('repair-planner', '修复规划', '缺陷到最小修复动作的计划'),
    ('editor', '剪辑', '剪辑点、节奏与成片结构'),
    ('sound-director', '声音导演', '配音、音效与音乐的听感设计'),
    ('conversation-editor', '对白编辑', '对白长度、语气与口型可行性调整'),
]

TEMPLATE = '''# {role} · {cn}

role: {role}
display_name: "{cn}"
responsibility: "{duty}"
runtime_service: S19-agent-runtime
status: design_only

# Agent 只返回候选结构，业务状态归 S04/S05/S06 等领域服务
authority: proposal_only

# 独立 Deployment / Task Queue / 工具白名单 / 费用限额，不共用无限额进程池
deployment:
  task_queue: agent-{role}
  image_binding: unassigned
  limits:
    tokens: unassigned
    cost: unassigned
    steps: unassigned

tools: []
evals: []
'''


def main():
    for role, cn, duty in ROLES:
        base = os.path.join(ROOT, 'agents', role)
        os.makedirs(base, exist_ok=True)
        with io.open(os.path.join(base, 'agent.yaml'), 'w',
                     encoding='utf-8', newline='\n') as f:
            f.write(TEMPLATE.format(role=role, cn=cn, duty=duty))
    print('wrote %d agents' % len(ROLES))


if __name__ == '__main__':
    main()
