"""S28 素材归档与制品目录。

对外只经 HTTP 契约交互；别的服务不 import 这里的业务代码。
"""

from .schema import SCHEMA

__all__ = ["SCHEMA"]
