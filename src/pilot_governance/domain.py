"""定义中试转化治理领域允许的状态、阶段与判定常量。"""

from __future__ import annotations

# 放大阶段必须沿实验室 → 小试 → 中试 → 连续产线逐级推进。
SCALE_STAGES = ("lab", "pilot_small", "pilot_line", "continuous")
STAGE_ORDER = {stage: index for index, stage in enumerate(SCALE_STAGES)}

# 工艺版本生命周期。
PROCESS_DRAFT = "draft"
PROCESS_FROZEN = "frozen"
PROCESS_ACTIVE = "active"
PROCESS_SUPERSEDED = "superseded"

# 闸门状态：起草 → 单方待签收 → 双签生效；拒绝或终止后关闭。
GATE_DRAFT = "draft"
GATE_WAITING = "waiting_counterparty"
GATE_ACTIVE = "active"
GATE_REJECTED = "rejected"
GATE_CLOSED = "closed"

# 批次状态。
BATCH_OPEN = "open"            # 在制
BATCH_INSPECTED = "inspected"  # 已完成检测回调
BATCH_PARTIAL = "partial"      # 部分达标，偏差处置中
BATCH_ACCEPTED = "accepted"    # 全部达标
BATCH_REJECTED = "rejected"    # 不达标
BATCH_DELIVERED = "delivered"  # 已交付（事实不可改写）
BATCH_RECALLED = "recalled"    # 已召回（交付事实保留）

# 检测结论。
VERDICT_PASS = "pass"
VERDICT_PARTIAL = "partial"
VERDICT_FAIL = "fail"

# 偏差处置。
DEVIATION_OPEN = "open"
DEVIATION_DECIDED = "decided"

DISPOSITION_TECH = "tech_remediate"      # 技术方补救
DISPOSITION_PRODUCER = "producer_bear"   # 生产方承担
DISPOSITION_SHARED = "shared"            # 双方分担
DISPOSITION_SCRAP = "scrap"              # 报废
DISPOSITION_CONCESSION = "concession"    # 让步接收

# 台账条目。
LEDGER_PAYMENT = "payment"        # 阶段付款（生产方 → 技术方）
LEDGER_REFUND = "refund"          # 返还（技术方 → 生产方）
LEDGER_SETTLEMENT = "settlement"  # 终止结算

LEDGER_PENDING = "pending"
LEDGER_PAID = "paid"

ENTRY_DIRECTION_INBOUND = "inbound"   # 技术方应收
ENTRY_DIRECTION_OUTBOUND = "outbound" # 技术方应付（返还）

# 项目状态。
PROJECT_ACTIVE = "active"
PROJECT_TERMINATED = "terminated"

# 协议状态。
AGREEMENT_ACTIVE = "active"
AGREEMENT_TERMINATED = "terminated"

# 原料状态。
MATERIAL_ACTIVE = "active"
MATERIAL_DISCONTINUED = "discontinued"
MATERIAL_REPLACED = "replaced"

# 责任方常量。
PARTY_TECH = "tech"
PARTY_PRODUCER = "producer"


def stage_index(stage: str) -> int:
    try:
        return STAGE_ORDER[stage]
    except KeyError as exc:
        raise ValueError(f"未知放大阶段: {stage}") from exc
