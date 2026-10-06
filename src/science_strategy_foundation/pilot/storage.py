"""中试转化治理的 SQLite 表结构。

所有写操作都在既有 Database 的短事务中执行，与基础服务共用同一审计链。
协议、工艺版本、证据、签收门、原料谱系、批次、质量回调、偏差、
义务、付款和协议终止均为只追加（append-only）事实；任何结清都通过
新增记录完成，不更新也不删除已经交付或支付的事实。
"""

from __future__ import annotations

PILOT_SCHEMA = """
CREATE TABLE IF NOT EXISTS pilot_agreements (
    agreement_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL,
    tech_org_id TEXT NOT NULL,
    production_org_id TEXT NOT NULL,
    title TEXT NOT NULL,
    license_scope_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active','terminated')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pilot_ip_terms (
    term_id TEXT PRIMARY KEY,
    agreement_id TEXT NOT NULL REFERENCES pilot_agreements(agreement_id),
    ip_id TEXT NOT NULL,
    ownership TEXT NOT NULL,
    scope_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(agreement_id, ip_id)
);
CREATE TABLE IF NOT EXISTS pilot_process_versions (
    version_id TEXT PRIMARY KEY,
    agreement_id TEXT NOT NULL REFERENCES pilot_agreements(agreement_id),
    stage TEXT NOT NULL CHECK(stage IN ('lab','pilot','continuous')),
    seq INTEGER NOT NULL CHECK(seq >= 1),
    status TEXT NOT NULL CHECK(status IN ('draft','frozen')),
    formulation_hash TEXT NOT NULL,
    scale_params_json TEXT NOT NULL,
    scale_tolerance_json TEXT NOT NULL,
    allowed_material_specs_json TEXT NOT NULL,
    equipment_capabilities_json TEXT NOT NULL,
    supersedes_version_id TEXT,
    frozen_at TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(agreement_id, stage, seq)
);
CREATE TABLE IF NOT EXISTS pilot_evidence (
    evidence_id TEXT PRIMARY KEY,
    agreement_id TEXT NOT NULL REFERENCES pilot_agreements(agreement_id),
    stage TEXT NOT NULL CHECK(stage IN ('lab','pilot','continuous')),
    category TEXT NOT NULL,
    external_ref TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(agreement_id, stage, category)
);
CREATE TABLE IF NOT EXISTS pilot_scale_gates (
    gate_id TEXT PRIMARY KEY,
    agreement_id TEXT NOT NULL REFERENCES pilot_agreements(agreement_id),
    stage TEXT NOT NULL CHECK(stage IN ('pilot','continuous')),
    version_id TEXT NOT NULL REFERENCES pilot_process_versions(version_id),
    status TEXT NOT NULL CHECK(status IN ('open','passed')),
    tech_confirmation_id TEXT,
    production_confirmation_id TEXT,
    passed_at TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pilot_gates_agreement_stage
    ON pilot_scale_gates(agreement_id, stage);
CREATE TABLE IF NOT EXISTS pilot_gate_confirmations (
    confirmation_id TEXT PRIMARY KEY,
    gate_id TEXT NOT NULL REFERENCES pilot_scale_gates(gate_id),
    party TEXT NOT NULL CHECK(party IN ('tech','production')),
    actor_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(gate_id, party)
);
CREATE TABLE IF NOT EXISTS pilot_materials (
    material_id TEXT PRIMARY KEY,
    agreement_id TEXT NOT NULL REFERENCES pilot_agreements(agreement_id),
    code TEXT NOT NULL,
    name TEXT NOT NULL,
    maker TEXT NOT NULL,
    batch_no TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('available','discontinued','substituted')),
    substituted_by_material_id TEXT,
    discontinued_at TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(agreement_id, code, batch_no)
);
CREATE TABLE IF NOT EXISTS pilot_batches (
    batch_id TEXT PRIMARY KEY,
    agreement_id TEXT NOT NULL REFERENCES pilot_agreements(agreement_id),
    batch_no TEXT NOT NULL,
    stage TEXT NOT NULL CHECK(stage IN ('lab','pilot','continuous')),
    version_id TEXT NOT NULL REFERENCES pilot_process_versions(version_id),
    gate_id TEXT REFERENCES pilot_scale_gates(gate_id),
    material_ids_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('released','in_production','qualified','partial_qualified','failed')),
    callback_id TEXT,
    released_at TEXT NOT NULL,
    decided_at TEXT,
    UNIQUE(agreement_id, batch_no)
);
CREATE TABLE IF NOT EXISTS pilot_quality_callbacks (
    callback_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES pilot_batches(batch_id),
    callback_key TEXT NOT NULL,
    metrics_json TEXT NOT NULL,
    targets_json TEXT NOT NULL,
    result TEXT NOT NULL CHECK(result IN ('qualified','partial_qualified','failed')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(batch_id, callback_key)
);
CREATE TABLE IF NOT EXISTS pilot_deviations (
    deviation_id TEXT PRIMARY KEY,
    agreement_id TEXT NOT NULL REFERENCES pilot_agreements(agreement_id),
    batch_id TEXT REFERENCES pilot_batches(batch_id),
    code TEXT NOT NULL,
    classification TEXT NOT NULL CHECK(classification IN ('process_design','execution','material_supply')),
    detail_json TEXT NOT NULL,
    responsible_party TEXT NOT NULL CHECK(responsible_party IN ('tech','production')),
    disposition TEXT NOT NULL CHECK(disposition IN ('open','accepted','rejected','reworked')),
    closed_at TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(agreement_id, code)
);
CREATE TABLE IF NOT EXISTS pilot_obligations (
    obligation_id TEXT PRIMARY KEY,
    agreement_id TEXT NOT NULL REFERENCES pilot_agreements(agreement_id),
    kind TEXT NOT NULL CHECK(kind IN ('payment_due','rework','compensation','return','refund')),
    party TEXT NOT NULL CHECK(party IN ('tech','production')),
    amount REAL NOT NULL CHECK(amount >= 0),
    currency TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('open','settled')),
    settled_by_type TEXT,
    settled_by_id TEXT,
    settled_at TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pilot_payments (
    payment_id TEXT PRIMARY KEY,
    agreement_id TEXT NOT NULL REFERENCES pilot_agreements(agreement_id),
    milestone TEXT NOT NULL,
    stage TEXT,
    amount REAL NOT NULL CHECK(amount >= 0),
    currency TEXT NOT NULL,
    direction TEXT NOT NULL CHECK(direction IN ('production_to_tech','tech_to_production')),
    ref TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(agreement_id, ref)
);
CREATE TABLE IF NOT EXISTS pilot_terminations (
    termination_id TEXT PRIMARY KEY,
    agreement_id TEXT NOT NULL UNIQUE REFERENCES pilot_agreements(agreement_id),
    reason TEXT NOT NULL,
    open_obligation_ids_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pilot_obligations_agreement ON pilot_obligations(agreement_id, status);
CREATE INDEX IF NOT EXISTS idx_pilot_batches_agreement ON pilot_batches(agreement_id);
CREATE INDEX IF NOT EXISTS idx_pilot_batches_version ON pilot_batches(version_id);
CREATE INDEX IF NOT EXISTS idx_pilot_deviations_agreement ON pilot_deviations(agreement_id);
"""


def ensure_pilot_schema(database) -> None:
    """幂等建表。"""

    database.connection.executescript(PILOT_SCHEMA)
