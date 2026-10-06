"""中试转化治理领域的表结构，在基础服务同一 SQLite 数据库上扩展。"""

from __future__ import annotations

# 复用基础库的 organizations / actors / sites / request_receipts / audit_events，
# 治理领域只追加自己的业务表；全部写操作仍走基础库的事务与哈希审计。
PILOT_SCHEMA = """
CREATE TABLE IF NOT EXISTS pilot_projects (
    project_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    ip_owner_org_id TEXT NOT NULL REFERENCES organizations(organization_id),
    producer_org_id TEXT NOT NULL REFERENCES organizations(organization_id),
    transfer_center_org_id TEXT NOT NULL REFERENCES organizations(organization_id),
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    CHECK(ip_owner_org_id != producer_org_id)
);
CREATE TABLE IF NOT EXISTS pilot_agreements (
    agreement_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES pilot_projects(project_id),
    version INTEGER NOT NULL CHECK(version >= 1),
    ip_ownership TEXT NOT NULL,
    license_scope_json TEXT NOT NULL,
    liability_terms_json TEXT NOT NULL,
    payment_terms_json TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(project_id, version)
);
CREATE TABLE IF NOT EXISTS pilot_process_versions (
    process_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES pilot_projects(project_id),
    version TEXT NOT NULL,
    scale_stage TEXT NOT NULL,
    based_on_process_id TEXT REFERENCES pilot_process_versions(process_id),
    specification_json TEXT NOT NULL,
    specification_hash TEXT NOT NULL,
    parameter_windows_json TEXT NOT NULL,
    quality_metrics_json TEXT NOT NULL,
    requires_materials_json TEXT NOT NULL,
    status TEXT NOT NULL,
    frozen_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(project_id, version)
);
CREATE TABLE IF NOT EXISTS pilot_equipment (
    equipment_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES pilot_projects(project_id),
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    name TEXT NOT NULL,
    scale_stage TEXT NOT NULL,
    capability_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pilot_materials (
    material_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES pilot_projects(project_id),
    material_key TEXT NOT NULL,
    name TEXT NOT NULL,
    supplier_org_id TEXT NOT NULL REFERENCES organizations(organization_id),
    supplier_batch_no TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(project_id, material_key, supplier_org_id, supplier_batch_no)
);
CREATE TABLE IF NOT EXISTS pilot_supplier_replacements (
    replacement_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES pilot_projects(project_id),
    material_key TEXT NOT NULL,
    old_material_id TEXT NOT NULL REFERENCES pilot_materials(material_id),
    new_material_id TEXT NOT NULL REFERENCES pilot_materials(material_id),
    reason TEXT NOT NULL,
    effective_from TEXT NOT NULL,
    decided_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    CHECK(old_material_id != new_material_id)
);
CREATE TABLE IF NOT EXISTS pilot_stage_gates (
    gate_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES pilot_projects(project_id),
    scale_stage TEXT NOT NULL,
    process_id TEXT NOT NULL REFERENCES pilot_process_versions(process_id),
    evidence_refs_json TEXT NOT NULL,
    status TEXT NOT NULL,
    tech_confirmed_by TEXT,
    tech_confirmed_at TEXT,
    producer_confirmed_by TEXT,
    producer_confirmed_at TEXT,
    activated_at TEXT,
    created_at TEXT NOT NULL
);
-- 并发签收时每个项目每个放大阶段最多一个生效版本。
CREATE UNIQUE INDEX IF NOT EXISTS ux_active_gate_per_stage
    ON pilot_stage_gates(project_id, scale_stage) WHERE status = 'active';
CREATE TABLE IF NOT EXISTS pilot_batches (
    batch_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES pilot_projects(project_id),
    scale_stage TEXT NOT NULL,
    gate_id TEXT NOT NULL REFERENCES pilot_stage_gates(gate_id),
    process_id TEXT NOT NULL REFERENCES pilot_process_versions(process_id),
    equipment_id TEXT NOT NULL REFERENCES pilot_equipment(equipment_id),
    material_bindings_json TEXT NOT NULL,
    status TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    recalled_at TEXT
);
CREATE TABLE IF NOT EXISTS pilot_inspections (
    result_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES pilot_batches(batch_id),
    callback_id TEXT NOT NULL UNIQUE,
    metric_values_json TEXT NOT NULL,
    overall_verdict TEXT NOT NULL,
    partial_metrics_json TEXT NOT NULL,
    recorded_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pilot_deviations (
    deviation_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES pilot_batches(batch_id),
    project_id TEXT NOT NULL REFERENCES pilot_projects(project_id),
    kind TEXT NOT NULL,
    metric TEXT,
    observed_value REAL,
    window_json TEXT,
    description TEXT NOT NULL,
    owner_party TEXT,
    disposition TEXT,
    disposition_detail_json TEXT,
    refund_entry_id TEXT,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    decided_at TEXT
);
CREATE TABLE IF NOT EXISTS pilot_deliveries (
    delivery_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES pilot_batches(batch_id),
    quantity REAL NOT NULL CHECK(quantity > 0),
    unit TEXT NOT NULL,
    delivered_by TEXT NOT NULL,
    delivered_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pilot_recalls (
    recall_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES pilot_batches(batch_id),
    project_id TEXT NOT NULL REFERENCES pilot_projects(project_id),
    reason TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    refund_entry_id TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pilot_ledger (
    entry_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES pilot_projects(project_id),
    batch_id TEXT,
    stage TEXT,
    entry_type TEXT NOT NULL,
    amount REAL NOT NULL CHECK(amount >= 0),
    currency TEXT NOT NULL,
    direction TEXT NOT NULL,
    status TEXT NOT NULL,
    ref_type TEXT,
    ref_id TEXT,
    detail_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    settled_by_entry_id TEXT
);
CREATE TABLE IF NOT EXISTS pilot_terminations (
    termination_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL UNIQUE REFERENCES pilot_projects(project_id),
    agreement_id TEXT NOT NULL REFERENCES pilot_agreements(agreement_id),
    reason TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    settlement_entry_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def extend_schema(connection) -> None:
    """在已有基础库连接上创建治理领域表。"""

    connection.executescript(PILOT_SCHEMA)
