"""中试转化治理领域服务的单元测试。"""

import unittest
from datetime import datetime, timezone

from science_strategy_foundation.errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from science_strategy_foundation.service import DomainService
from science_strategy_foundation.storage import Database

from pilot_governance.clock_helpers import SteppingClock
from pilot_governance.service import PilotGovernanceService


class PilotCase(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.clock = SteppingClock(datetime(2026, 10, 6, tzinfo=timezone.utc))
        self.foundation = DomainService(self.database, self.clock)
        self.service = PilotGovernanceService(self.database, self.clock)
        f = self.foundation
        f.register_organization(request_id="ot", actor_id="bootstrap",
                                organization_id="org-tech", name="高校")
        f.register_actor(request_id="ad", actor_id="bootstrap", new_actor_id="admin-1",
                         display_name="管理员", role="admin", organization_id="org-tech")
        f.register_organization(request_id="op", actor_id="admin-1",
                                organization_id="org-prod", name="企业")
        f.register_organization(request_id="oc", actor_id="admin-1",
                                organization_id="org-center", name="转化中心")
        f.register_organization(request_id="os", actor_id="admin-1",
                                organization_id="org-sup", name="供应商")
        f.register_actor(request_id="at", actor_id="admin-1", new_actor_id="tech-1",
                         display_name="技术负责人", role="operator", organization_id="org-tech")
        f.register_actor(request_id="ap", actor_id="admin-1", new_actor_id="prod-1",
                         display_name="质量负责人", role="operator", organization_id="org-prod")
        f.register_actor(request_id="ac", actor_id="admin-1", new_actor_id="center-1",
                         display_name="协调员", role="reviewer", organization_id="org-center")
        f.register_site(request_id="st", actor_id="prod-1", site_id="plant-1",
                        organization_id="org-prod", name="车间", timezone_name="Asia/Shanghai")
        self.service.create_project(request_id="pj", actor_id="admin-1", project_id="proj-1",
                                    name="涂层中试", ip_owner_org_id="org-tech",
                                    producer_org_id="org-prod", transfer_center_org_id="org-center")
        self.service.register_agreement(
            request_id="ag", actor_id="tech-1", project_id="proj-1", agreement_id="agr-1",
            ip_ownership="IP 归高校",
            license_scope={"field": "涂布"}, liability_terms={"failed_batch": "裁决分担"},
            payment_terms={"currency": "CNY",
                           "stage_amounts": {"lab": 1000, "pilot_small": 3000}})
        self.service.register_equipment(request_id="eq", actor_id="prod-1", project_id="proj-1",
                                        equipment_id="eq-1", site_id="plant-1", name="实验机",
                                        scale_stage="lab",
                                        capability={"speed": {"min": 1, "max": 5}})
        self.service.register_material(request_id="ma", actor_id="prod-1", project_id="proj-1",
                                       material_id="mat-1", material_key="resin", name="树脂",
                                       supplier_org_id="org-sup", supplier_batch_no="B1")
        self.service.create_process(
            request_id="pr", actor_id="tech-1", project_id="proj-1", process_id="proc-1",
            version="1", scale_stage="lab",
            specification={"f": 1},
            parameter_windows={"temp": {"min": 1, "max": 2}},
            quality_metrics={"q": {"min": 0, "max": 10}, "t": {"min": 0, "max": 10}},
            requires_materials=["resin"])
        self.service.freeze_process(request_id="fr", actor_id="tech-1", process_id="proc-1")
        self.clock.tick()
        self.service.open_gate(request_id="go", actor_id="tech-1", project_id="proj-1",
                               gate_id="gate-1", scale_stage="lab", process_id="proc-1",
                               evidence_refs=["ev://1"])

    def tearDown(self):
        self.database.close()

    def activate_lab_gate(self):
        self.service.confirm_gate(request_id="c1", actor_id="tech-1", gate_id="gate-1")
        return self.service.confirm_gate(request_id="c2", actor_id="prod-1", gate_id="gate-1")

    def test_tech_and_producer_must_be_different_organizations(self):
        with self.assertRaises(ValidationError):
            self.service.create_project(
                request_id="pj-bad", actor_id="admin-1", project_id="proj-bad", name="x",
                ip_owner_org_id="org-tech", producer_org_id="org-tech",
                transfer_center_org_id="org-center")

    def test_producer_cannot_draft_process(self):
        with self.assertRaises(PermissionDenied):
            self.service.create_process(
                request_id="prx", actor_id="prod-1", project_id="proj-1", process_id="proc-x",
                version="9", scale_stage="pilot_small", specification={"f": 1},
                parameter_windows={"temp": {"min": 1, "max": 2}},
                quality_metrics={"q": {"min": 0, "max": 10}, "t": {"min": 0, "max": 10}},
                requires_materials=["resin"])

    def test_gate_requires_both_separated_parties(self):
        self.service.confirm_gate(request_id="c1", actor_id="tech-1", gate_id="gate-1")
        # 技术方不能重复签收，也不能代替生产方。
        with self.assertRaises(ConflictError):
            self.service.confirm_gate(request_id="c1b", actor_id="tech-1", gate_id="gate-1")
        with self.assertRaises(PermissionDenied):
            self.service.confirm_gate(request_id="ca", actor_id="admin-1", gate_id="gate-1")
        gate = self.service.confirm_gate(request_id="c2", actor_id="prod-1", gate_id="gate-1")
        self.assertFalse(gate.replayed)
        # 生效后再签收被拒绝。
        with self.assertRaises(ConflictError):
            self.service.confirm_gate(request_id="c3", actor_id="prod-1", gate_id="gate-1")

    def test_only_one_active_gate_per_stage_under_concurrent_signoff(self):
        self.activate_lab_gate()
        # 同阶段再起草第二版工艺与闸门。
        self.service.create_process(
            request_id="pr2", actor_id="tech-1", project_id="proj-1", process_id="proc-2",
            version="1.1", scale_stage="lab",
            specification={"f": 2},
            parameter_windows={"temp": {"min": 1, "max": 2}},
            quality_metrics={"q": {"min": 0, "max": 10}, "t": {"min": 0, "max": 10}},
            requires_materials=["resin"])
        self.service.freeze_process(request_id="fr2", actor_id="tech-1", process_id="proc-2")
        self.service.open_gate(request_id="go2", actor_id="tech-1", project_id="proj-1",
                               gate_id="gate-2", scale_stage="lab", process_id="proc-2",
                               evidence_refs=["ev://2"])
        self.service.confirm_gate(request_id="g2t", actor_id="tech-1", gate_id="gate-2")
        with self.assertRaises(ConflictError):
            self.service.confirm_gate(request_id="g2p", actor_id="prod-1", gate_id="gate-2")

    def test_cannot_open_batch_without_active_gate_or_frozen_process(self):
        # 闸门仅单方签收，未生效。
        self.service.confirm_gate(request_id="c1", actor_id="tech-1", gate_id="gate-1")
        with self.assertRaises(NotFoundError):
            self.service.open_batch(request_id="b0", actor_id="prod-1", project_id="proj-1",
                                    batch_id="batch-x", scale_stage="lab")

    def test_inspection_callback_is_idempotent_and_advances_once(self):
        self.activate_lab_gate()
        self.service.open_batch(request_id="bo", actor_id="prod-1", project_id="proj-1",
                                batch_id="batch-1", scale_stage="lab")
        values = {"q": 5.0, "t": 5.0}
        first = self.service.record_inspection(
            request_id="i1", actor_id="prod-1", batch_id="batch-1",
            callback_id="cb-1", metric_values=values)
        replay = self.service.record_inspection(
            request_id="i1r", actor_id="prod-1", batch_id="batch-1",
            callback_id="cb-1", metric_values=values)
        self.assertFalse(first.replayed)
        self.assertTrue(replay.replayed)
        self.assertEqual(first.resource_id, replay.resource_id)
        with self.assertRaises(ConflictError):
            self.service.record_inspection(
                request_id="i1d", actor_id="prod-1", batch_id="batch-1",
                callback_id="cb-1", metric_values={"q": 6.0, "t": 5.0})

    def test_partial_compliance_opens_deviation_and_blocks_delivery(self):
        self.activate_lab_gate()
        self.service.open_batch(request_id="bo", actor_id="prod-1", project_id="proj-1",
                                batch_id="batch-1", scale_stage="lab")
        self.service.record_inspection(
            request_id="i1", actor_id="prod-1", batch_id="batch-1",
            callback_id="cb-1", metric_values={"q": 99.0, "t": 5.0})
        info = self.service.batch_deviations("batch-1")
        self.assertEqual("partial", info["batch_status"])
        deviation_id = info["deviations"][0]["deviation_id"]
        # 生产方不能自裁偏差，只有转化中心可以。
        with self.assertRaises(PermissionDenied):
            self.service.decide_deviation(
                request_id="d0", actor_id="prod-1", deviation_id=deviation_id,
                owner_party="producer", disposition="producer_bear")
        self.service.decide_deviation(
            request_id="d1", actor_id="center-1", deviation_id=deviation_id,
            owner_party="producer", disposition="producer_bear", amount=1000)
        # 结论不可改写。
        with self.assertRaises(ConflictError):
            self.service.decide_deviation(
                request_id="d2", actor_id="center-1", deviation_id=deviation_id,
                owner_party="tech", disposition="tech_remediate")
        # 部分达标批次不能直接交付。
        with self.assertRaises(ConflictError):
            self.service.deliver_batch(request_id="dl", actor_id="prod-1",
                                       batch_id="batch-1", quantity=1, unit="平")

    def test_concession_allows_delivery_while_scrap_closes_batch(self):
        self.activate_lab_gate()
        # 批次 A：让步接收 → 转为 accepted 可交付。
        self.service.open_batch(request_id="ba", actor_id="prod-1", project_id="proj-1",
                                batch_id="batch-a", scale_stage="lab")
        self.service.record_inspection(
            request_id="ia", actor_id="prod-1", batch_id="batch-a",
            callback_id="cb-a", metric_values={"q": 99.0, "t": 5.0})
        dev_a = self.service.batch_deviations("batch-a")["deviations"][0]["deviation_id"]
        self.service.decide_deviation(
            request_id="da", actor_id="center-1", deviation_id=dev_a,
            owner_party="producer", disposition="concession")
        self.service.deliver_batch(request_id="dla", actor_id="prod-1",
                                   batch_id="batch-a", quantity=1, unit="平")
        self.assertEqual("delivered",
                         self.service.product_provenance("batch-a")["status"])
        # 批次 B：报废 → rejected 关闭，不能交付。
        self.service.open_batch(request_id="bb", actor_id="prod-1", project_id="proj-1",
                                batch_id="batch-b", scale_stage="lab")
        self.service.record_inspection(
            request_id="ib", actor_id="prod-1", batch_id="batch-b",
            callback_id="cb-b", metric_values={"q": 99.0, "t": 5.0})
        dev_b = self.service.batch_deviations("batch-b")["deviations"][0]["deviation_id"]
        self.service.decide_deviation(
            request_id="db", actor_id="center-1", deviation_id=dev_b,
            owner_party="producer", disposition="scrap")
        self.assertEqual("rejected",
                         self.service.batch_deviations("batch-b")["batch_status"])
        with self.assertRaises(ConflictError):
            self.service.deliver_batch(request_id="dlb", actor_id="prod-1",
                                       batch_id="batch-b", quantity=1, unit="平")

    def test_failed_batch_cannot_deliver_but_pass_can(self):
        self.activate_lab_gate()
        self.service.open_batch(request_id="bo", actor_id="prod-1", project_id="proj-1",
                                batch_id="batch-ok", scale_stage="lab")
        self.service.record_inspection(
            request_id="iok", actor_id="prod-1", batch_id="batch-ok",
            callback_id="cb-ok", metric_values={"q": 5.0, "t": 5.0})
        self.service.deliver_batch(request_id="dl", actor_id="prod-1",
                                   batch_id="batch-ok", quantity=2, unit="平")
        provenance = self.service.product_provenance("batch-ok")
        self.assertEqual("delivered", provenance["status"])
        self.assertEqual("proc-1", provenance["process"]["process_id"])
        self.assertEqual("agr-1", provenance["authorization"]["agreement_id"])

    def test_discontinued_material_blocks_new_batch_until_replaced(self):
        self.activate_lab_gate()
        self.service.mark_material_discontinued(request_id="ds", actor_id="prod-1",
                                                material_id="mat-1")
        with self.assertRaises(ConflictError):
            self.service.open_batch(request_id="bbad", actor_id="prod-1", project_id="proj-1",
                                    batch_id="batch-bad", scale_stage="lab")
        # 生产方无权裁决替换。
        self.foundation.register_organization(request_id="os2", actor_id="admin-1",
                                              organization_id="org-sup2", name="新供应商")
        self.service.register_material(request_id="m2", actor_id="prod-1", project_id="proj-1",
                                       material_id="mat-2", material_key="resin", name="新树脂",
                                       supplier_org_id="org-sup2", supplier_batch_no="B2")
        with self.assertRaises(PermissionDenied):
            self.service.replace_supplier(
                request_id="rp0", actor_id="prod-1", project_id="proj-1",
                replacement_id="rep-0", material_key="resin",
                new_material_id="mat-2", reason="停产")
        self.clock.tick()
        self.service.replace_supplier(
            request_id="rp1", actor_id="center-1", project_id="proj-1",
            replacement_id="rep-1", material_key="resin",
            new_material_id="mat-2", reason="原批次停产")
        self.service.open_batch(request_id="bnew", actor_id="prod-1", project_id="proj-1",
                                batch_id="batch-new", scale_stage="lab")
        bindings = self.service.product_provenance("batch-new")["materials"]
        self.assertEqual("mat-2", bindings[0]["material_id"])

    def test_payment_is_idempotent_and_cannot_be_paid_twice(self):
        self.activate_lab_gate()
        pending = self.service.outstanding_obligations("proj-1")["pending_obligations"]
        entry_id = pending[0]["entry_id"]
        self.service.settle_payment(request_id="pay1", actor_id="prod-1", entry_id=entry_id)
        with self.assertRaises(ConflictError):
            self.service.settle_payment(request_id="pay2", actor_id="prod-1", entry_id=entry_id)
        # 技术方不能支付应付款（方向为 inbound，由生产方支付）。
        # 已支付事实在义务清单中消失，但保留在累计已付金额。
        obligations = self.service.outstanding_obligations("proj-1")
        self.assertEqual(1000, obligations["paid_total"])
        self.assertNotIn(entry_id, [o["entry_id"] for o in obligations["pending_obligations"]])

    def test_termination_closes_open_obligations_but_keeps_paid_facts(self):
        self.activate_lab_gate()
        pending = self.service.outstanding_obligations("proj-1")["pending_obligations"]
        self.service.settle_payment(request_id="pay1", actor_id="prod-1",
                                    entry_id=pending[0]["entry_id"])
        self.service.open_batch(request_id="bo", actor_id="prod-1", project_id="proj-1",
                                batch_id="batch-1", scale_stage="lab")
        self.service.record_inspection(
            request_id="i1", actor_id="prod-1", batch_id="batch-1",
            callback_id="cb-1", metric_values={"q": 99.0, "t": 5.0})
        deviation_id = self.service.batch_deviations("batch-1")["deviations"][0]["deviation_id"]
        self.service.decide_deviation(
            request_id="d1", actor_id="center-1", deviation_id=deviation_id,
            owner_party="tech", disposition="tech_remediate", amount=400)
        result = self.service.terminate_agreement(
            request_id="t1", actor_id="center-1", project_id="proj-1", reason="协商终止")
        self.assertFalse(result.replayed)
        obligations = self.service.outstanding_obligations("proj-1")
        self.assertEqual("terminated", obligations["project_status"])
        settlement = obligations["pending_obligations"]
        self.assertEqual(1, len(settlement))
        self.assertEqual("settlement", settlement[0]["entry_type"])
        self.assertEqual(400, settlement[0]["amount"])  # 只剩技术方返还义务
        self.assertEqual(1000, obligations["paid_total"])
        # 终止后禁止新的生产性动作。
        with self.assertRaises(ConflictError):
            self.service.open_batch(request_id="bafter", actor_id="prod-1", project_id="proj-1",
                                    batch_id="batch-after", scale_stage="lab")

    def test_change_impact_tracks_in_progress_batches(self):
        self.activate_lab_gate()
        self.service.open_batch(request_id="b1", actor_id="prod-1", project_id="proj-1",
                                batch_id="batch-a", scale_stage="lab")
        self.service.open_batch(request_id="b2", actor_id="prod-1", project_id="proj-1",
                                batch_id="batch-b", scale_stage="lab")
        impact = self.service.change_impact("proj-1", process_id="proc-1",
                                            in_progress_only=True)
        self.assertEqual({"batch-a", "batch-b"},
                         {b["batch_id"] for b in impact["affected_batches"]})

    def test_request_replay_returns_same_receipt(self):
        first = self.service.register_material(
            request_id="dup", actor_id="prod-1", project_id="proj-1", material_id="mat-dup",
            material_key="resin2", name="树脂2", supplier_org_id="org-sup", supplier_batch_no="B9")
        second = self.service.register_material(
            request_id="dup", actor_id="prod-1", project_id="proj-1", material_id="mat-dup",
            material_key="resin2", name="树脂2", supplier_org_id="org-sup", supplier_batch_no="B9")
        self.assertFalse(first.replayed)
        self.assertTrue(second.replayed)
        with self.assertRaises(ConflictError):
            self.service.register_material(
                request_id="dup", actor_id="prod-1", project_id="proj-1", material_id="mat-dup",
                material_key="resin2", name="改名", supplier_org_id="org-sup", supplier_batch_no="B9")


if __name__ == "__main__":
    unittest.main()
