"""S08 版本快照与接受状态：manifest 版本、accepted pointer、execution_fence。

只写本服务自己的库；对下游只发引用（`service_id/id/revision/content_hash`），
从不复制内容。契约见 plans/S08-revision-snapshot.md。
"""

from .service import RevisionSnapshotService, ServiceError

__all__ = ["RevisionSnapshotService", "ServiceError"]
