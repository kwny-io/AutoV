"""Create connectors/<provider>/<channel>/connector.yaml skeletons.

Channels come from docs/09-model-execution.md §2. Every connector starts disabled.

    python tools/scaffold/generate_connectors.py
"""

import io
import os

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CHANNELS = [
    ('seedance', 'volcengine', '火山方舟 Seedance 官方渠道'),
    ('seedance', 'byteplus', 'BytePlus ModelArk 渠道，与 Volcengine 独立部署'),
    ('kling', 'api', 'Kling 官方 API'),
    ('wan', 'official', 'Wan 官方 API'),
    ('wan', 'self-hosted', 'Wan 自托管部署'),
    ('veo', 'official', 'Veo 各官方渠道'),
    ('sora', 'historical', 'Sora 历史占位；Videos API 已于 2026-09-24 停用'),
    ('vidu', 'api', 'Vidu 官方 API'),
    ('minimax', 'api', 'MiniMax 官方 API'),
]

TEMPLATE = '''# {provider} / {channel}

provider: {provider}
channel: {channel}
description: "{desc}"
status: disabled

# 真实供应商全默认 disabled；官方文档与账户探测后 validating → active
lifecycle: disabled
evidence_checked_at: null

protocol:
  - capabilities
  - validate
  - estimate
  - submit
  - get_status
  - cancel
  - verify_callback
  - fetch_manifest

# 连接器只做参数映射与协议处理，无权换模型、调预算或接受成片
may_change_model: false
may_adjust_budget: false
may_accept_output: false
'''


def main():
    for provider, channel, desc in CHANNELS:
        base = os.path.join(ROOT, 'connectors', provider, channel)
        os.makedirs(base, exist_ok=True)
        with io.open(os.path.join(base, 'connector.yaml'), 'w',
                     encoding='utf-8', newline='\n') as f:
            f.write(TEMPLATE.format(provider=provider, channel=channel, desc=desc))
    print('wrote %d connectors' % len(CHANNELS))


if __name__ == '__main__':
    main()
