"""定义中试转化治理领域在模块边界使用的只读数据对象。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Project:
    """一个涂层工艺中试转化项目，技术方与生产方必须分属不同组织。"""

    project_id: str
    name: str
    ip_owner_org_id: str
    producer_org_id: str
    transfer_center_org_id: str
    status: str
    created_at: str


@dataclass(frozen=True)
class Agreement:
    """技术转让协议：约定知识产权权属、许可范围与风险责任。"""

    agreement_id: str
    project_id: str
    version: int
    ip_ownership: str
    license_scope: dict[str, Any]
    liability_terms: dict[str, Any]
    status: str
    created_at: str


@dataclass(frozen=True)
class ProcessVersion:
    """可执行工艺版本：配方步骤、放大参数偏差窗口与质量指标。"""

    process_id: str
    project_id: str
    version: str
    scale_stage: str
    based_on_process_id: str | None
    specification: dict[str, Any]
    parameter_windows: dict[str, Any]
    quality_metrics: dict[str, Any]
    status: str
    frozen_at: str | None
    created_at: str


@dataclass(frozen=True)
class EquipmentCapability:
    """生产方设备的能力窗口与所在放大阶段。"""

    equipment_id: str
    project_id: str
    site_id: str
    name: str
    scale_stage: str
    capability: dict[str, Any]
    created_at: str


@dataclass(frozen=True)
class MaterialGenealogy:
    """原料谱系节点：供应商、原料批次及其在工艺中的用途。"""

    material_id: str
    project_id: str
    material_key: str
    name: str
    supplier_org_id: str
    supplier_batch_no: str
    status: str
    discontinued: bool
    created_at: str


@dataclass(frozen=True)
class SupplierReplacement:
    """供应商替换裁决：仅对替换时间之后开批的批次生效。"""

    replacement_id: str
    project_id: str
    material_key: str
    old_material_id: str
    new_material_id: str
    reason: str
    effective_from: str
    decided_by: str
    created_at: str


@dataclass(frozen=True)
class StageGate:
    """阶段闸门：冻结工艺与前置证据，技术方与生产方职责分离双签。"""

    gate_id: str
    project_id: str
    scale_stage: str
    process_id: str
    evidence_refs: list[str]
    status: str
    tech_confirmed_by: str | None
    tech_confirmed_at: str | None
    producer_confirmed_by: str | None
    producer_confirmed_at: str | None
    activated_at: str | None
    created_at: str


@dataclass(frozen=True)
class ProductionBatch:
    """试生产批次：依据生效闸门与工艺、在供应商替换时点下选择原料。"""

    batch_id: str
    project_id: str
    scale_stage: str
    gate_id: str
    process_id: str
    equipment_id: str
    material_bindings: dict[str, str]
    status: str
    opened_at: str
    closed_at: str | None
    recalled_at: str | None


@dataclass(frozen=True)
class InspectionResult:
    """实验室或产线检测回调结果（按回调编号幂等）。"""

    result_id: str
    batch_id: str
    callback_id: str
    metric_values: dict[str, float]
    overall_verdict: str
    partial_metrics: list[str]
    recorded_by: str
    created_at: str


@dataclass(frozen=True)
class Deviation:
    """偏差处置单：部分达标或指标越限时由责任方承接处置。"""

    deviation_id: str
    batch_id: str
    project_id: str
    kind: str
    metric: str | None
    observed_value: float | None
    window: dict[str, Any] | None
    description: str
    owner_party: str | None
    disposition: str | None
    disposition_detail: dict[str, Any] | None
    status: str
    created_at: str
    decided_at: str | None


@dataclass(frozen=True)
class Delivery:
    """交付事实：一经签收不可改写。"""

    delivery_id: str
    batch_id: str
    quantity: float
    unit: str
    delivered_by: str
    delivered_at: str


@dataclass(frozen=True)
class Recall:
    """批次召回：停止流转并生成返还义务，不改变已交付事实。"""

    recall_id: str
    batch_id: str
    project_id: str
    reason: str
    requested_by: str
    created_at: str


@dataclass(frozen=True)
class LedgerEntry:
    """付款 / 返还 / 结算台账条目，只追加、不可改写。"""

    entry_id: str
    project_id: str
    batch_id: str | None
    stage: str | None
    entry_type: str
    amount: float
    currency: str
    direction: str
    status: str
    ref_type: str | None
    ref_id: str | None
    detail: dict[str, Any]
    created_at: str
    settled_by_entry_id: str | None


@dataclass(frozen=True)
class Termination:
    """协议终止：以新结算记录结清未完成义务，已交付与已支付事实不变。"""

    termination_id: str
    project_id: str
    agreement_id: str
    reason: str
    requested_by: str
    status: str
    created_at: str
