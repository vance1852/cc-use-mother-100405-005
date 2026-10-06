"""中试转化治理领域模型。

技术方（高校/成果转化方）与生产方（制造企业）是协议的两个当事方；
放大阶段只能沿 lab -> pilot -> continuous 单向前进。
"""

from __future__ import annotations


PARTY_TECH = "tech"
PARTY_PRODUCTION = "production"
PARTIES = frozenset({PARTY_TECH, PARTY_PRODUCTION})

# 放大阶段顺序：实验室 -> 中试 -> 连续产线
STAGES = ("lab", "pilot", "continuous")
STAGE_RANK = {stage: index for index, stage in enumerate(STAGES)}
SCALE_GATE_STAGES = frozenset({"pilot", "continuous"})

# 工艺版本生命周期
VERSION_DRAFT = "draft"
VERSION_FROZEN = "frozen"

# 每次放大冻结工艺时必须齐备的前置证据类别
REQUIRED_EVIDENCE: dict[str, frozenset[str]] = {
    "lab": frozenset(),
    "pilot": frozenset({"lab_process_report", "raw_material_spec", "ip_authorization"}),
    "continuous": frozenset({"pilot_batch_report", "quality_qualification", "capability_study"}),
}

GATE_OPEN = "open"
GATE_PASSED = "passed"

# 批次状态
BATCH_RELEASED = "released"
BATCH_IN_PRODUCTION = "in_production"
BATCH_QUALIFIED = "qualified"
BATCH_PARTIAL = "partial_qualified"
BATCH_FAILED = "failed"
BATCH_TERMINAL = frozenset({BATCH_QUALIFIED, BATCH_PARTIAL, BATCH_FAILED})

# 偏差类别决定处置责任方：工艺/配方设计问题归技术方，执行与来料问题归生产方
DEVIATION_RESPONSIBILITY = {
    "process_design": PARTY_TECH,
    "execution": PARTY_PRODUCTION,
    "material_supply": PARTY_PRODUCTION,
}

# 义务类别
OBLIGATION_REWORK = "rework"
OBLIGATION_COMPENSATION = "compensation"
OBLIGATION_RETURN = "return"
OBLIGATION_REFUND = "refund"
OBLIGATION_PAYMENT_DUE = "payment_due"


def responsible_party_for(classification: str) -> str:
    """根据偏差类别确定性地返回责任方。"""

    try:
        return DEVIATION_RESPONSIBILITY[classification]
    except KeyError as exc:
        raise ValueError("偏差类别不被支持") from exc
