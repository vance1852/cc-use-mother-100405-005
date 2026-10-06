"""中试转化治理领域的离线端到端验收。

剧情对应真实场景：实验室原料批次停产 → 供应商替换裁决 → 逐级放大双签冻结 →
部分达标偏差归责 → 检测回调幂等 → 交付后召回 → 协议终止轧差，
最后通过溯源、义务与变更波及查询回答治理四问。
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from science_strategy_foundation.service import DomainService
from science_strategy_foundation.storage import Database

from .clock_helpers import SteppingClock
from .service import PilotGovernanceService


def run() -> dict[str, object]:
    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "pilot_acceptance.sqlite3")
        clock = SteppingClock(datetime(2026, 10, 6, 8, 0, tzinfo=timezone.utc))
        foundation = DomainService(database, clock)
        service = PilotGovernanceService(database, clock)

        # ---- 主体建档：高校（技术方）、制造企业（生产方）、成果转化中心、两家供应商 ----
        foundation.register_organization(request_id="org-tech", actor_id="bootstrap",
                                         organization_id="org-tech", name="示范理工大学")
        foundation.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="admin-1",
                                  display_name="平台管理员", role="admin", organization_id="org-tech")
        foundation.register_organization(request_id="org-prod", actor_id="admin-1",
                                         organization_id="org-prod", name="连续制造有限公司")
        foundation.register_organization(request_id="org-center", actor_id="admin-1",
                                         organization_id="org-center", name="成果转化中心")
        foundation.register_organization(request_id="org-sup-a", actor_id="admin-1",
                                         organization_id="org-sup-a", name="旧供应商化工A厂")
        foundation.register_organization(request_id="org-sup-b", actor_id="admin-1",
                                         organization_id="org-sup-b", name="替代供应商新材B厂")
        foundation.register_actor(request_id="actor-tech", actor_id="admin-1", new_actor_id="tech-1",
                                  display_name="高校技术负责人", role="operator",
                                  organization_id="org-tech")
        foundation.register_actor(request_id="actor-prod", actor_id="admin-1", new_actor_id="prod-1",
                                  display_name="企业质量负责人", role="operator",
                                  organization_id="org-prod")
        foundation.register_actor(request_id="actor-center", actor_id="admin-1", new_actor_id="center-1",
                                  display_name="转化中心协调员", role="reviewer",
                                  organization_id="org-center")
        foundation.register_site(request_id="site", actor_id="prod-1", site_id="plant-1",
                                 organization_id="org-prod", name="企业中试车间",
                                 timezone_name="Asia/Shanghai")

        # ---- 项目与技术转让协议（IP 权属、许可范围、失败批次责任、阶段付款） ----
        service.create_project(request_id="p-1", actor_id="admin-1", project_id="coat-001",
                               name="新型涂层工艺中试转化", ip_owner_org_id="org-tech",
                               producer_org_id="org-prod", transfer_center_org_id="org-center")
        service.register_agreement(
            request_id="a-1", actor_id="tech-1", project_id="coat-001", agreement_id="agr-1",
            ip_ownership="涂层核心配方及工艺改进的知识产权归示范理工大学所有",
            license_scope={"field": "工业连续涂布", "territory": "中国大陆",
                           "exclusive": False, "sublicense": False},
            liability_terms={"failed_batch": "按偏差裁决结论由责任方承担",
                             "raw_material_change": "须经成果转化中心裁决后方可替代",
                             "scale_parameter_deviation": "以冻结工艺的偏差窗口为准"},
            payment_terms={"currency": "CNY",
                           "stage_amounts": {"lab": 100000, "pilot_small": 300000,
                                             "pilot_line": 600000, "continuous": 1000000}})

        # ---- 实验室阶段：冻结工艺 + 双签闸门 ----
        service.register_equipment(request_id="eq-lab", actor_id="prod-1", project_id="coat-001",
                                   equipment_id="eq-lab-1", site_id="plant-1", name="实验室涂布机",
                                   scale_stage="lab",
                                   capability={"line_speed": {"min": 0.5, "max": 5}})
        service.register_material(request_id="mat-a", actor_id="prod-1", project_id="coat-001",
                                  material_id="mat-resin-a", material_key="resin",
                                  name="功能树脂（旧批次）", supplier_org_id="org-sup-a",
                                  supplier_batch_no="A-2025-11")
        service.create_process(
            request_id="proc-lab", actor_id="tech-1", project_id="coat-001",
            process_id="proc-coat-v1", version="1.0-lab", scale_stage="lab",
            specification={"formula": {"resin": 100, "curing_agent": 30},
                           "steps": ["配料", "涂布", "固化"]},
            parameter_windows={"curing_temp_c": {"min": 140, "max": 160},
                               "line_speed": {"min": 1, "max": 3}},
            quality_metrics={"adhesion_mpa": {"min": 5.0, "max": 9.0},
                             "thickness_um": {"min": 20.0, "max": 30.0}},
            requires_materials=["resin"])
        service.freeze_process(request_id="freeze-lab", actor_id="tech-1", process_id="proc-coat-v1")
        clock.tick()
        service.open_gate(request_id="gate-lab-open", actor_id="tech-1", project_id="coat-001",
                          gate_id="gate-lab", scale_stage="lab", process_id="proc-coat-v1",
                          evidence_refs=["ev://lab-report-01", "ev://hazard-check-01"])
        service.confirm_gate(request_id="gate-lab-tech", actor_id="tech-1", gate_id="gate-lab")
        lab_gate = service.confirm_gate(request_id="gate-lab-prod", actor_id="prod-1",
                                        gate_id="gate-lab")
        clock.tick()

        # 实验室批次：依据生效闸门开批、检测全达标、付款、交付（事实随后不可改写）。
        service.open_batch(request_id="batch-lab-1", actor_id="prod-1", project_id="coat-001",
                           batch_id="batch-lab-01", scale_stage="lab")
        service.record_inspection(request_id="insp-lab-1", actor_id="prod-1",
                                  batch_id="batch-lab-01", callback_id="cb-lab-01",
                                  metric_values={"adhesion_mpa": 6.5, "thickness_um": 25.0})
        obligations_now = service.outstanding_obligations("coat-001")
        lab_payment_id = obligations_now["pending_obligations"][0]["entry_id"]
        service.settle_payment(request_id="pay-lab", actor_id="prod-1", entry_id=lab_payment_id)
        service.deliver_batch(request_id="deliver-lab-1", actor_id="prod-1", batch_id="batch-lab-01",
                              quantity=100, unit="平方米")
        clock.tick()

        # 第二实验室批次保持在制，用于后续变更波及分析。
        service.open_batch(request_id="batch-lab-2", actor_id="prod-1", project_id="coat-001",
                           batch_id="batch-lab-02", scale_stage="lab")
        clock.tick()

        # ---- 原料停产：必须以替换记录裁决，不能直接改写旧批次绑定 ----
        service.mark_material_discontinued(request_id="disc-a", actor_id="prod-1",
                                           material_id="mat-resin-a")
        service.register_material(request_id="mat-b", actor_id="prod-1", project_id="coat-001",
                                  material_id="mat-resin-b", material_key="resin",
                                  name="功能树脂（替代批次）", supplier_org_id="org-sup-b",
                                  supplier_batch_no="B-2026-10")
        replacement = service.replace_supplier(
            request_id="rep-1", actor_id="center-1", project_id="coat-001",
            replacement_id="rep-resin-1", material_key="resin",
            new_material_id="mat-resin-b", reason="原供应商批次 A-2025-11 已停产")
        clock.tick()

        # ---- 小试放大：新版本工艺必须显式继承且阶段递进，重新冻结、重新双签 ----
        service.register_equipment(request_id="eq-small", actor_id="prod-1", project_id="coat-001",
                                   equipment_id="eq-small-1", site_id="plant-1", name="小试涂布线",
                                   scale_stage="pilot_small",
                                   capability={"line_speed": {"min": 2, "max": 20}})
        service.create_process(
            request_id="proc-small", actor_id="tech-1", project_id="coat-001",
            process_id="proc-coat-v2", version="2.0-small", scale_stage="pilot_small",
            based_on_process_id="proc-coat-v1",
            specification={"formula": {"resin": 100, "curing_agent": 30, "leveling_agent": 0.5},
                           "steps": ["配料", "过滤", "涂布", "固化"]},
            parameter_windows={"curing_temp_c": {"min": 145, "max": 165},
                               "line_speed": {"min": 5, "max": 12}},
            quality_metrics={"adhesion_mpa": {"min": 5.0, "max": 9.0},
                             "thickness_um": {"min": 20.0, "max": 30.0}},
            requires_materials=["resin"])
        service.freeze_process(request_id="freeze-small", actor_id="tech-1",
                               process_id="proc-coat-v2")
        clock.tick()
        service.open_gate(request_id="gate-small-open", actor_id="prod-1", project_id="coat-001",
                          gate_id="gate-small", scale_stage="pilot_small",
                          process_id="proc-coat-v2",
                          evidence_refs=["ev://scale-up-calc-02", "ev://equipment-qual-02"])
        service.confirm_gate(request_id="gate-small-prod", actor_id="prod-1", gate_id="gate-small")
        service.confirm_gate(request_id="gate-small-tech", actor_id="tech-1", gate_id="gate-small")
        clock.tick()

        # 小试批次：开批时自动按替换记录绑定新原料。
        service.open_batch(request_id="batch-small-1", actor_id="prod-1", project_id="coat-001",
                           batch_id="batch-small-01", scale_stage="pilot_small")

        # 部分达标：一项指标越窗 → 自动生成待裁决偏差，批次挂起。
        service.record_inspection(request_id="insp-small-1", actor_id="prod-1",
                                  batch_id="batch-small-01", callback_id="cb-small-01",
                                  metric_values={"adhesion_mpa": 6.0, "thickness_um": 33.0})
        # 相同检测回调重放：返回原回执，不重复推进、不重复生成偏差。
        replay = service.record_inspection(request_id="insp-small-1-replay", actor_id="prod-1",
                                           batch_id="batch-small-01", callback_id="cb-small-01",
                                           metric_values={"adhesion_mpa": 6.0, "thickness_um": 33.0})
        deviations_before = service.batch_deviations("batch-small-01")
        # 转化中心裁决：放大参数偏差归技术方补救，登记 5 万返还义务。
        deviation_id = deviations_before["deviations"][0]["deviation_id"]
        service.decide_deviation(request_id="dev-1", actor_id="center-1",
                                 deviation_id=deviation_id, owner_party="tech",
                                 disposition="tech_remediate", amount=50000,
                                 note="固化窗口外偏差，技术方调整工艺")
        clock.tick()

        # ---- 已交付实验室批次召回：插入召回与返还记录，交付事实保留 ----
        recall = service.recall_batch(request_id="recall-1", actor_id="center-1",
                                      batch_id="batch-lab-01",
                                      reason="留样复检发现长期附着力衰减", refund_amount=20000)
        clock.tick()

        # ---- 治理四问 ----
        provenance = service.product_provenance("batch-small-01")
        obligations = service.outstanding_obligations("coat-001")
        impact = service.change_impact("coat-001", replacement_id="rep-resin-1")
        in_progress = service.in_progress_batches("coat-001")

        # ---- 协议终止：轧差未完成义务，已支付与已交付事实不动 ----
        termination = service.terminate_agreement(request_id="term-1", actor_id="center-1",
                                                  project_id="coat-001",
                                                  reason="替代原料无法稳定满足指标，双方协商终止")
        obligations_after = service.outstanding_obligations("coat-001")

        # ---- 校验结论 ----
        checks: dict[str, bool] = {}
        # 1. 小试批次依据 v2 工艺与生效授权（双签闸门 + 协议）生产。
        checks["provenance_process_v2"] = provenance["process"]["process_id"] == "proc-coat-v2"
        checks["provenance_gate_active"] = provenance["authorization"]["gate_status"] == "active"
        checks["provenance_dual_signed"] = bool(
            provenance["authorization"]["tech_confirmed_by"]
            and provenance["authorization"]["producer_confirmed_by"])
        checks["provenance_new_material"] = provenance["materials"][0]["material_id"] == "mat-resin-b"
        # 2. 偏差由技术方承接处置，并关联返还义务。
        devs = service.batch_deviations("batch-small-01")["deviations"]
        checks["deviation_owner_tech"] = devs[0]["owner_party"] == "tech"
        checks["deviation_refund_linked"] = devs[0]["refund"] is not None
        # 3. 幂等回调只推进一次：只有一条偏差，重放返回 replayed。
        checks["inspection_replayed"] = replay.replayed is True
        checks["single_deviation"] = len(devs) == 1
        # 4. 变更波及：替换波及换料前在制的 lab-02 与换料后 small-01。
        affected = {b["batch_id"] for b in impact["affected_batches"]}
        checks["impact_covers_lab02"] = "batch-lab-02" in affected
        checks["impact_covers_small01"] = "batch-small-01" in affected
        # 5. 召回保留交付事实。
        lab_provenance = service.product_provenance("batch-lab-01")
        checks["recall_status"] = lab_provenance["status"] == "recalled"
        checks["delivery_preserved"] = lab_provenance["delivery"] is not None
        checks["recall_recorded"] = lab_provenance["recall"] is not None
        # 6. 终止轧差：入 300000（小试阶段款）- 出 70000（偏差 50000 + 召回 20000）= 230000。
        settlement = [o for o in obligations_after["pending_obligations"]
                      if o["entry_type"] == "settlement"]
        checks["settlement_amount"] = bool(settlement) and abs(settlement[0]["amount"] - 230000) < 0.01
        checks["settlement_net_inbound"] = bool(settlement) and \
            settlement[0]["direction"] == "inbound"
        checks["paid_preserved"] = abs(obligations_after["paid_total"] - 100000) < 0.01
        remaining_pending = [o for o in obligations_after["pending_obligations"]
                             if o["entry_type"] != "settlement"]
        checks["old_obligations_closed"] = remaining_pending == []
        # 7. 在制清单包含终止前仍挂起的批次。
        checks["in_progress_tracked"] = {b["batch_id"] for b in in_progress} >= {
            "batch-lab-02", "batch-small-01"}

        from science_strategy_foundation.audit import verify_chain
        audit_valid, event_count = verify_chain(database.connection)

        result = {
            "status": "ok" if all(checks.values()) and audit_valid else "failed",
            "checks": checks,
            "audit_valid": audit_valid,
            "audit_events": event_count,
            "settlement": settlement[0] if settlement else None,
            "affected_batch_ids": sorted(affected),
            "in_progress_before_termination": sorted(b["batch_id"] for b in in_progress),
        }
        database.close()
        return result


def main() -> int:
    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" and result["audit_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
