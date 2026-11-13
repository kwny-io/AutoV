"""S24 model-registry。对外只经 HTTP 契约，其他服务不 import 本包。"""

from .schema import SCHEMA, schema_sql
from .service import ModelRegistryService, ServiceError, Transaction

__all__ = ["SCHEMA", "schema_sql", "ModelRegistryService", "ServiceError",
           "Transaction"]
