"""S01 自己的 schema。别的服务不 import 这里，只透过 HTTP 契约交互。"""

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS tenant (
  tenant_id          TEXT PRIMARY KEY,
  slug               TEXT NOT NULL UNIQUE,
  lifecycle          TEXT NOT NULL CHECK (lifecycle IN
                       ('provisioning','active','suspended','terminated')),
  revocation_epoch   INTEGER NOT NULL DEFAULT 1,
  created_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS org_unit (
  tenant_id   TEXT NOT NULL,
  org_unit_id TEXT NOT NULL,
  parent_id   TEXT,
  name        TEXT NOT NULL,
  PRIMARY KEY (tenant_id, org_unit_id),
  FOREIGN KEY (tenant_id) REFERENCES tenant (tenant_id)
);

CREATE TABLE IF NOT EXISTS principal (
  tenant_id                 TEXT NOT NULL,
  principal_id              TEXT NOT NULL,
  principal_type            TEXT NOT NULL CHECK (principal_type IN
                              ('human','service','api_client','mcp_client')),
  status                    TEXT NOT NULL CHECK (status IN
                              ('invited','active','suspended','offboarded')),
  org_unit_id               TEXT,
  personal_revocation_epoch INTEGER NOT NULL DEFAULT 1,
  idp_connection_id         TEXT,
  idp_subject               TEXT,
  created_at                TEXT NOT NULL,
  PRIMARY KEY (tenant_id, principal_id),
  UNIQUE (tenant_id, idp_connection_id, idp_subject),
  FOREIGN KEY (tenant_id) REFERENCES tenant (tenant_id)
);

CREATE TABLE IF NOT EXISTS role (
  tenant_id  TEXT NOT NULL,
  role_key   TEXT NOT NULL,
  max_scope  TEXT NOT NULL CHECK (max_scope IN
               ('tenant','org_unit','project','series','episode')),
  permissions TEXT NOT NULL,            -- JSON array
  PRIMARY KEY (tenant_id, role_key),
  FOREIGN KEY (tenant_id) REFERENCES tenant (tenant_id)
);

CREATE TABLE IF NOT EXISTS role_grant (
  tenant_id    TEXT NOT NULL,
  grant_id     TEXT NOT NULL,
  principal_id TEXT NOT NULL,
  role_key     TEXT NOT NULL,
  scope_kind   TEXT NOT NULL CHECK (scope_kind IN
                 ('tenant','org_unit','project','series','episode')),
  scope_id     TEXT NOT NULL,
  effect       TEXT NOT NULL CHECK (effect IN ('allow','deny')),
  state        TEXT NOT NULL CHECK (state IN ('active','expired','revoked')),
  revision     INTEGER NOT NULL DEFAULT 1,
  created_at   TEXT NOT NULL,
  PRIMARY KEY (tenant_id, grant_id),
  FOREIGN KEY (tenant_id) REFERENCES tenant (tenant_id)
);

CREATE INDEX IF NOT EXISTS grant_by_principal
  ON role_grant (tenant_id, principal_id, state);

CREATE TABLE IF NOT EXISTS authorization_decision (
  tenant_id    TEXT NOT NULL,
  decision_id  TEXT PRIMARY KEY,
  principal_id TEXT NOT NULL,
  permission   TEXT NOT NULL,
  scope_kind   TEXT NOT NULL,
  scope_id     TEXT NOT NULL,
  purpose      TEXT,
  allowed      INTEGER NOT NULL CHECK (allowed IN (0,1)),
  reason       TEXT NOT NULL,
  decided_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS signing_key (
  key_id     TEXT PRIMARY KEY,
  public_key TEXT NOT NULL,
  status     TEXT NOT NULL CHECK (status IN ('active','retired'))
);
"""

# 权限键注册表来自契约文件，不在这里硬编码（plans/S01 §11 C11）。
# 加载不到就是启动失败——不能悄悄退回一份本地副本，那样两份会漂移。
def _load_permissions() -> dict[str, dict]:
    import json
    import pathlib

    path = pathlib.Path(__file__).resolve().parents[4] / "contracts" / "identity" / "permission-registry.v2.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    return {p["key"]: p for p in doc["permissions"]}


PERMISSIONS = _load_permissions()
PERMISSION_KEYS = frozenset(PERMISSIONS)

# high 风险 = 会触发付费或外部分发；破窗期间强制 deny（S01 §3.6）。
HIGH_RISK_PERMISSIONS = frozenset(
    k for k, v in PERMISSIONS.items() if v["risk"] == "high"
)

# 受众注册表：S01 不认识的值是 422 UNKNOWN_AUDIENCE。
AUDIENCES = frozenset({
    "s02.governance-policy", "s03.project-catalog", "s08.revision-snapshot",
    "s09.lineage-impact", "s19.agent-runtime", "s21.orchestration",
    "s22.scheduler-quota", "s23.budget-ledger", "s26.generation-jobs",
    "s27.provider-connectors", "s28.artifact-ingest", "s35.technical-qc",
    "s36.semantic-qc", "s37.audio-alignment", "s40.human-review",
    "s45.audit", "s46.developer-integration", "s47.realtime-projection",
})

SCOPE_PREFIX = {
    "tenant": "tnt_",
    "org_unit": "org_",
    "project": "prj_",
    "series": "ser_",
    "episode": "ep_",
}

SCOPE_RANK = {"tenant": 0, "org_unit": 1, "project": 2, "series": 3, "episode": 4}
