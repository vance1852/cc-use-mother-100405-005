"""运行中试转化治理的离线端到端验收。

场景对应业务背景：高校新型涂层工艺进入连续产线前，实验室原料批次停产，
需要冻结工艺与前置证据、双方职责分离签收、替代供应商、检测回调幂等、
部分达标定责、阶段付款与召回、协议终止保留事实，并通过查询接口回答
"这批产品依据哪套工艺与授权生产、偏差谁处置、还欠什么、变更波及哪些批次"。
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from ..clock import FixedClock
from ..errors import ConflictError, PermissionDenied, ValidationError
from ..service import DomainService
from ..storage import Database
from .service import PilotService


QUALITY_TARGETS = {
    "adhesion_mpa": {"min": 5.0, "max": 8.0},
    "thickness_um": {"min": 20.0, "max": 40.0},
    "salt_spray_h": {"min": 720.0, "max": 2000.0},
}


def run() -> dict[str, object]:
    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "pilot_acceptance.sqlite3")
        clock = FixedClock(datetime(2026, 10, 6, 8, 0, tzinfo=timezone.utc))
        base = DomainService(database, clock)
        pilot = PilotService(database, clock)

        # ---- 组织与操作者：技术方（高校）与生产方（制造企业）分离 ----
        base.register_organization(request_id="org-tech", actor_id="bootstrap",
                                   organization_id="org-univ", name="东海大学")
        base.register_organization(request_id="org-prod", actor_id="bootstrap",
                                   organization_id="org-fab", name="华新制造")
        base.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="admin-1",
                            display_name="平台管理员", role="admin", organization_id="org-univ")
        base.register_actor(request_id="tech-1", actor_id="admin-1", new_actor_id="tech-1",
                            display_name="高校技术负责人", role="operator", organization_id="org-univ")
        base.register_actor(request_id="prod-1", actor_id="admin-1", new_actor_id="prod-1",
                            display_name="企业质量负责人", role="operator", organization_id="org-fab")
        base.register_site(request_id="site", actor_id="admin-1", site_id="site-1",
                           organization_id="org-fab", name="涂层中试基地", timezone_name="Asia/Shanghai")

        # ---- 协议、知识产权权属与许可范围 ----
        pilot.create_agreement(
            request_id="ag", actor_id="admin-1", agreement_id="ag-1", site_id="site-1",
            tech_org_id="org-univ", production_org_id="org-fab",
            title="新型陶瓷涂层工艺中试转化协议",
            license_scope={"field": "industrial_coating", "territory": "CN",
                           "exclusive": False, "capacity_limit_t_per_year": 500})
        pilot.register_ip_term(request_id="ip-core", actor_id="tech-1", agreement_id="ag-1",
                               ip_id="ip-formula-7", ownership="org-univ",
                               scope={"type": "core_formulation", "sublicense": False})

        # ---- 实验室工艺冻结（无需签收门） ----
        lab_formulation = {"binder": "A7", "ceramic_powder": "CP-2", "ratio": [62, 31, 7]}
        pilot.draft_process_version(
            request_id="v-lab", actor_id="tech-1", agreement_id="ag-1", stage="lab",
            formulation=lab_formulation,
            scale_params={"cure_temp_c": 180, "cure_minutes": 30},
            scale_tolerance={"cure_temp_c": 3, "cure_minutes": 2},
            allowed_material_specs={"RES-A7": {"grade": "tech"}, "POW-CP2": {"d50_um": 2.0}},
            equipment_capabilities={"spray_line": {"max_width_mm": 600}})
        lab_version_id = _resource_id(database, "ag-1", "lab")
        pilot.add_evidence(request_id="ev-lab-1", actor_id="tech-1", agreement_id="ag-1", stage="lab",
                           category="lab_notebook", external_ref="NB-2026-001",
                           payload={"note": "实验室记录本"})
        pilot.freeze_process_version(request_id="fz-lab", actor_id="tech-1",
                                    agreement_id="ag-1", version_id=lab_version_id)

        # ---- 中试放大：前置证据 + 双方签收 ----
        pilot.draft_process_version(
            request_id="v-pilot", actor_id="tech-1", agreement_id="ag-1", stage="pilot",
            formulation=lab_formulation,
            scale_params={"cure_temp_c": 182, "cure_minutes": 32, "line_speed_m_min": 1.2},
            scale_tolerance={"cure_temp_c": 5, "cure_minutes": 3, "line_speed_m_min": 0.1},
            allowed_material_specs={"RES-A7": {"grade": "tech"}, "POW-CP2": {"d50_um": 2.2}},
            equipment_capabilities={"pilot_line": {"max_width_mm": 800}})
        pilot_version_id = _resource_id(database, "ag-1", "pilot")
        pilot.add_evidence(request_id="ev-p-1", actor_id="tech-1", agreement_id="ag-1", stage="pilot",
                           category="lab_process_report", external_ref="LPR-9",
                           payload={"report": "实验室工艺报告"})
        pilot.add_evidence(request_id="ev-p-2", actor_id="prod-1", agreement_id="ag-1", stage="pilot",
                           category="raw_material_spec", external_ref="RMS-9",
                           payload={"spec": "原料规格书"})
        pilot.add_evidence(request_id="ev-p-3", actor_id="tech-1", agreement_id="ag-1", stage="pilot",
                           category="ip_authorization", external_ref="IPA-9",
                           payload={"auth": "知识产权授权"})

        # 证据未齐时冻结被拒（先缺一类再补齐的反向用例放在测试里，这里直接齐套冻结）
        pilot.freeze_process_version(request_id="fz-pilot", actor_id="tech-1",
                                    agreement_id="ag-1", version_id=pilot_version_id)
        gate = pilot.open_scale_gate(request_id="gate-p", actor_id="prod-1", agreement_id="ag-1",
                                     stage="pilot", version_id=pilot_version_id)
        # 技术方不能替生产方确认（职责分离）
        _expect_permission(lambda: pilot.confirm_scale_gate(
            request_id="cross", actor_id="tech-1", agreement_id="ag-1",
            gate_id=gate["gate_id"], party="production"))
        pilot.confirm_scale_gate(request_id="cf-p-t", actor_id="tech-1", agreement_id="ag-1",
                                 gate_id=gate["gate_id"], party="tech")
        # 只有一方确认时不能放行批次
        _expect_conflict(lambda: pilot.release_batch(
            request_id="early", actor_id="prod-1", agreement_id="ag-1", batch_no="PB-EARLY",
            stage="pilot", version_id=pilot_version_id, material_ids=[]))
        pilot.confirm_scale_gate(request_id="cf-p-p", actor_id="prod-1", agreement_id="ag-1",
                                 gate_id=gate["gate_id"], party="production")

        # ---- 原料谱系：实验室批次停产，替代供应商 ----
        pilot.register_material(request_id="m-old-resin", actor_id="prod-1", agreement_id="ag-1",
                                material_id="mat-resin-old", code="RES-A7", name="A7树脂",
                                maker="京华化工", batch_no="LB-2025-11")
        pilot.register_material(request_id="m-old-powder", actor_id="prod-1", agreement_id="ag-1",
                                material_id="mat-powder-old", code="POW-CP2", name="CP-2陶瓷粉",
                                maker="晶格新材", batch_no="LB-2025-08")
        pilot.register_material(request_id="m-new-powder", actor_id="prod-1", agreement_id="ag-1",
                                material_id="mat-powder-new", code="POW-CP2", name="CP-2陶瓷粉(替代)",
                                maker="南方晶科", batch_no="NB-2026-09")

        # 用即将停产的旧粉先放行一个在制批次（验证后续变更波及）
        pilot.release_batch(request_id="pb-1", actor_id="prod-1", agreement_id="ag-1",
                            batch_no="PB-001", stage="pilot", version_id=pilot_version_id,
                            material_ids=["mat-resin-old", "mat-powder-old"])
        pilot.start_batch(request_id="pb-1-start", actor_id="prod-1", agreement_id="ag-1",
                          batch_id=_batch_id(database, "ag-1", "PB-001"))

        substitution = pilot.substitute_material(
            request_id="sub-powder", actor_id="prod-1", agreement_id="ag-1",
            material_id="mat-powder-old", replacement_material_id="mat-powder-new",
            justification="原供应商晶格新材该批次已停产，改用同规格南方晶科")
        assert substitution["affected_batch_ids"] == [_batch_id(database, "ag-1", "PB-001")]
        # 被替代原料不能再用于新批次
        _expect_conflict(lambda: pilot.release_batch(
            request_id="pb-bad", actor_id="prod-1", agreement_id="ag-1", batch_no="PB-BAD",
            stage="pilot", version_id=pilot_version_id, material_ids=["mat-powder-old"]))

        # 替代粉投产后部分达标
        pb1_id = _batch_id(database, "ag-1", "PB-001")
        pilot.release_batch(request_id="pb-2", actor_id="prod-1", agreement_id="ag-1",
                            batch_no="PB-002", stage="pilot", version_id=pilot_version_id,
                            material_ids=["mat-resin-old", "mat-powder-new"])
        pb2_id = _batch_id(database, "ag-1", "PB-002")
        callback = pilot.quality_callback(
            request_id="qc-2", actor_id="prod-1", agreement_id="ag-1", batch_id=pb2_id,
            callback_key="QC-PB002-FINAL",
            metrics={"adhesion_mpa": 6.1, "thickness_um": 30.0, "salt_spray_h": 600.0},
            targets=QUALITY_TARGETS)
        assert callback["result"] == "partial_qualified", callback
        # 相同检测回调重放：不重复推进状态
        replay = pilot.quality_callback(
            request_id="qc-2", actor_id="prod-1", agreement_id="ag-1", batch_id=pb2_id,
            callback_key="QC-PB002-FINAL",
            metrics={"adhesion_mpa": 6.1, "thickness_um": 30.0, "salt_spray_h": 600.0},
            targets=QUALITY_TARGETS)
        assert replay["replayed"] is True and replay["state_advanced"] is False
        # 相同 key 不同数据被拒
        _expect_conflict(lambda: pilot.quality_callback(
            request_id="qc-other", actor_id="prod-1", agreement_id="ag-1", batch_id=pb2_id,
            callback_key="QC-PB002-FINAL",
            metrics={"adhesion_mpa": 6.1, "thickness_um": 30.0, "salt_spray_h": 900.0},
            targets=QUALITY_TARGETS))

        # ---- 偏差定责：耐盐雾不足判定为工艺设计问题，责任在技术方 ----
        deviation = pilot.open_deviation(
            request_id="dev-1", actor_id="prod-1", agreement_id="ag-1", code="DEV-SALT-1",
            classification="process_design", batch_id=pb2_id,
            detail={"metric": "salt_spray_h", "observed": 600.0, "required": 720.0})
        assert deviation["responsible_party"] == "tech"
        # 技术方不能自行处置自己责任的偏差（由相对方确认结清）
        _expect_permission(lambda: pilot.resolve_deviation(
            request_id="self-close", actor_id="tech-1", agreement_id="ag-1",
            deviation_id=deviation["deviation_id"], disposition="accepted"))
        resolved = pilot.resolve_deviation(
            request_id="dev-1-close", actor_id="prod-1", agreement_id="ag-1",
            deviation_id=deviation["deviation_id"], disposition="accepted",
            create_obligation={"kind": "compensation", "amount": 80000.0, "currency": "CNY"})
        compensation_obligation_id = resolved["obligation_id"]
        # 已结清偏差不能改写
        _expect_conflict(lambda: pilot.resolve_deviation(
            request_id="dev-1-again", actor_id="prod-1", agreement_id="ag-1",
            deviation_id=deviation["deviation_id"], disposition="reworked"))

        # ---- 阶段付款：先挂账再支付，支付事实不可改写 ----
        pilot.schedule_payment(request_id="pay-sched-pilot", actor_id="admin-1", agreement_id="ag-1",
                               milestone="MS-PILOT-PASS", amount=300000.0, stage="pilot")
        payment = pilot.record_payment(request_id="pay-1", actor_id="prod-1", agreement_id="ag-1",
                                       ref="TX-0001", amount=300000.0,
                                       direction="production_to_tech", milestone="MS-PILOT-PASS",
                                       stage="pilot")
        assert payment["settled_obligation_ids"], "阶段款应结清到期付款义务"

        # ---- 批次召回：已交付事实保留，新增返还义务 ----
        recall = pilot.recall_batch(request_id="recall-1", actor_id="tech-1", agreement_id="ag-1",
                                    batch_id=pb1_id, reason="旧原料批次停产前产品需复核封存")
        assert recall["preserved_status"] == "in_production"
        # 结清返还义务（实物回收凭证）
        pilot.settle_obligation(request_id="return-1", actor_id="prod-1", agreement_id="ag-1",
                                obligation_id=recall["obligation_id"],
                                settlement_ref="RETURN-NOTE-77")

        # ---- 连续产线：用新工艺版本开门，验证并发签收只一个版本生效 ----
        pilot.draft_process_version(
            request_id="v-cont-1", actor_id="tech-1", agreement_id="ag-1", stage="continuous",
            formulation={**lab_formulation, "additive": "X1"},
            scale_params={"cure_temp_c": 185, "line_speed_m_min": 2.0},
            scale_tolerance={"cure_temp_c": 5, "line_speed_m_min": 0.15},
            allowed_material_specs={"RES-A7": {"grade": "tech"}, "POW-CP2": {"d50_um": 2.2}},
            equipment_capabilities={"continuous_line": {"max_width_mm": 1250}})
        cont_v1 = _resource_id(database, "ag-1", "continuous", seq=1)
        for category, ref in (("pilot_batch_report", "PBR-1"), ("quality_qualification", "QQ-1"),
                              ("capability_study", "CS-1")):
            pilot.add_evidence(request_id=f"ev-c-{category}", actor_id="tech-1", agreement_id="ag-1",
                               stage="continuous", category=category, external_ref=ref, payload={})
        pilot.freeze_process_version(request_id="fz-c1", actor_id="tech-1", agreement_id="ag-1",
                                     version_id=cont_v1)
        gate_c1 = pilot.open_scale_gate(request_id="gate-c1", actor_id="prod-1", agreement_id="ag-1",
                                        stage="continuous", version_id=cont_v1)
        # 未决门未通过前，并发为另一版本开门被拒
        pilot.draft_process_version(
            request_id="v-cont-2", actor_id="tech-1", agreement_id="ag-1", stage="continuous",
            formulation={**lab_formulation, "additive": "X2"},
            scale_params={"cure_temp_c": 184, "line_speed_m_min": 2.0},
            scale_tolerance={"cure_temp_c": 5, "line_speed_m_min": 0.15},
            allowed_material_specs={"RES-A7": {"grade": "tech"}, "POW-CP2": {"d50_um": 2.2}},
            equipment_capabilities={"continuous_line": {"max_width_mm": 1250}},
            version_id="cont-ver-2")
        # 证据按阶段登记、对该阶段所有版本有效；v2 复用连续阶段证据集即可冻结。
        pilot.freeze_process_version(request_id="fz-c2", actor_id="tech-1", agreement_id="ag-1",
                                     version_id="cont-ver-2")
        _expect_conflict(lambda: pilot.open_scale_gate(
            request_id="gate-c2", actor_id="prod-1", agreement_id="ag-1",
            stage="continuous", version_id="cont-ver-2"))
        # 双方通过 v1 后，v2 才能开门（随后终止协议，不再推进）
        pilot.confirm_scale_gate(request_id="cf-c-t", actor_id="tech-1", agreement_id="ag-1",
                                 gate_id=gate_c1["gate_id"], party="tech")
        pilot.confirm_scale_gate(request_id="cf-c-p", actor_id="prod-1", agreement_id="ag-1",
                                 gate_id=gate_c1["gate_id"], party="production")

        # ---- 变更波及分析：旧粉替代影响哪些在制批次 ----
        impact = pilot.change_impact(agreement_id="ag-1", material_id="mat-powder-old")
        impacted_nos = {b["batch_no"] for b in impact["impacted_batches"]}
        assert "PB-001" in impacted_nos and "PB-002" not in impacted_nos

        # ---- 协议终止：未付义务随终止核减，补偿义务保留，已付事实不变 ----
        pilot.schedule_payment(request_id="pay-sched-cont", actor_id="admin-1", agreement_id="ag-1",
                               milestone="MS-CONT-START", amount=500000.0, stage="continuous")
        before_payments = len(pilot.financial_position("ag-1")["payments"])
        termination = pilot.terminate_agreement(request_id="term-1", actor_id="admin-1",
                                                agreement_id="ag-1", reason="替代料验证周期超期，双方终止")
        position = pilot.financial_position("ag-1")
        assert len(position["payments"]) == before_payments
        open_kinds = {o["kind"] for o in position["open_obligations"]}
        assert "payment_due" not in open_kinds, "未支付阶段款应在终止时核减"
        assert "compensation" in open_kinds, "技术方补偿义务应继续有效"
        # 终止后技术方仍可支付补偿履行遗留义务
        comp_payment = pilot.record_payment(request_id="pay-comp", actor_id="tech-1",
                                            agreement_id="ag-1", ref="TX-0002", amount=80000.0,
                                            direction="tech_to_production")
        assert compensation_obligation_id in comp_payment["settled_obligation_ids"]
        # 终止后不能再放行新批次
        _expect_conflict(lambda: pilot.release_batch(
            request_id="pb-after", actor_id="prod-1", agreement_id="ag-1", batch_no="PB-AFTER",
            stage="pilot", version_id=pilot_version_id,
            material_ids=["mat-resin-old", "mat-powder-new"]))

        # ---- 最终查询：血缘、处置、财务、审计 ----
        lineage = pilot.product_lineage("ag-1", pb2_id)
        assert lineage["process_version"]["version_id"] == pilot_version_id
        assert lineage["authorization"]["status"] == "passed"
        assert {m["material_id"] for m in lineage["materials"]} == {"mat-resin-old", "mat-powder-new"}
        dispositions = pilot.batch_dispositions("ag-1", pb2_id)
        assert dispositions["deviations"][0]["responsible_party"] == "tech"
        valid, event_count = base.verify_audit()
        result = {
            "status": "ok",
            "audit_valid": valid,
            "audit_events": event_count,
            "partial_batch": pb2_id,
            "callback_result": callback["result"],
            "deviation_responsible": deviation["responsible_party"],
            "recall_preserved_status": recall["preserved_status"],
            "termination_status": termination["status"],
            "cancelled_payment_obligations": len(termination["cancelled_payment_obligation_ids"]),
            "remaining_open_obligations": len(pilot.financial_position("ag-1")["open_obligations"]),
            "impacted_batch_count": len(impact["impacted_batches"]),
            "lineage_authorized": lineage["authorization"]["status"] == "passed",
        }
        database.close()
        return result


def _resource_id(database: Database, agreement_id: str, stage: str, seq: int | None = None) -> str:
    sql = ("SELECT version_id FROM pilot_process_versions WHERE agreement_id=? AND stage=? "
           "ORDER BY seq DESC LIMIT 1")
    row = database.connection.execute(sql, (agreement_id, stage)).fetchone()
    return row["version_id"]


def _batch_id(database: Database, agreement_id: str, batch_no: str) -> str:
    row = database.connection.execute(
        "SELECT batch_id FROM pilot_batches WHERE agreement_id=? AND batch_no=?",
        (agreement_id, batch_no)).fetchone()
    return row["batch_id"]


def _expect_conflict(action) -> None:
    try:
        action()
    except ConflictError:
        return
    raise AssertionError("预期 ConflictError")


def _expect_permission(action) -> None:
    try:
        action()
    except PermissionDenied:
        return
    raise AssertionError("预期 PermissionDenied")


def main() -> int:
    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" and result["audit_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
