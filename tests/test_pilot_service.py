import unittest
from datetime import datetime, timezone

from science_strategy_foundation.clock import FixedClock
from science_strategy_foundation.errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from science_strategy_foundation.pilot import domain as D
from science_strategy_foundation.pilot.service import PilotService
from science_strategy_foundation.service import DomainService
from science_strategy_foundation.storage import Database

TARGETS = {"adhesion": {"min": 5.0, "max": 8.0}, "thickness": {"min": 20.0, "max": 40.0}}


class PilotServiceTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        clock = FixedClock(datetime(2026, 10, 6, tzinfo=timezone.utc))
        self.base = DomainService(self.database, clock)
        self.pilot = PilotService(self.database, clock)
        self.base.register_organization(request_id="ot", actor_id="bootstrap",
                                        organization_id="univ", name="大学")
        self.base.register_organization(request_id="op", actor_id="bootstrap",
                                        organization_id="fab", name="工厂")
        self.base.register_actor(request_id="adm", actor_id="bootstrap", new_actor_id="adm",
                                 display_name="管理员", role="admin", organization_id="univ")
        self.base.register_actor(request_id="rt", actor_id="adm", new_actor_id="tech",
                                 display_name="技术方", role="operator", organization_id="univ")
        self.base.register_actor(request_id="rp", actor_id="adm", new_actor_id="prod",
                                 display_name="生产方", role="operator", organization_id="fab")
        self.base.register_site(request_id="st", actor_id="adm", site_id="site",
                                organization_id="fab", name="中试线", timezone_name="Asia/Shanghai")
        self.pilot.create_agreement(request_id="ag", actor_id="adm", agreement_id="A", site_id="site",
                                    tech_org_id="univ", production_org_id="fab", title="涂层转化",
                                    license_scope={"field": "coating"})

    def tearDown(self):
        self.database.close()

    # ---- 帮助函数 ---------------------------------------------------- #
    def _version(self, stage, request_id, **overrides):
        defaults = dict(
            formulation={"f": 1}, scale_params={"temp": 180}, scale_tolerance={"temp": 5},
            allowed_material_specs={"M1": {"grade": "a"}},
            equipment_capabilities={"line": {}})
        defaults.update(overrides)
        self.pilot.draft_process_version(request_id=request_id, actor_id="tech", agreement_id="A",
                                         stage=stage, **defaults)
        row = self.database.connection.execute(
            "SELECT version_id FROM pilot_process_versions WHERE agreement_id='A' AND stage=? "
            "ORDER BY seq DESC LIMIT 1", (stage,)).fetchone()
        return row["version_id"]

    def _evidence(self, stage, categories, prefix="e"):
        for index, category in enumerate(categories):
            self.pilot.add_evidence(request_id=f"{prefix}-{stage}-{category}-{index}", actor_id="tech",
                                    agreement_id="A", stage=stage, category=category,
                                    external_ref=f"ref-{category}", payload={"i": index})

    def _freeze_lab(self):
        vid = self._version("lab", "vlab")
        self.pilot.freeze_process_version(request_id="flab", actor_id="tech",
                                          agreement_id="A", version_id=vid)
        return vid

    def _passed_gate(self, stage="pilot", gate_req="g", cf_t="ct", cf_p="cp",
                     evidence_categories=None, version_request="vp"):
        self._freeze_lab()
        vid = self._version(stage, version_request)
        self._evidence(stage, evidence_categories or
                       ["lab_process_report", "raw_material_spec", "ip_authorization"])
        self.pilot.freeze_process_version(request_id="f" + version_request, actor_id="tech",
                                          agreement_id="A", version_id=vid)
        gate = self.pilot.open_scale_gate(request_id=gate_req, actor_id="prod", agreement_id="A",
                                          stage=stage, version_id=vid)
        self.pilot.confirm_scale_gate(request_id=cf_t, actor_id="tech", agreement_id="A",
                                      gate_id=gate["gate_id"], party="tech")
        self.pilot.confirm_scale_gate(request_id=cf_p, actor_id="prod", agreement_id="A",
                                      gate_id=gate["gate_id"], party="production")
        return vid, gate["gate_id"]

    def _material(self, request_id, material_id, code="M1", maker="供应商", batch_no="B1",
                  actor="prod"):
        self.pilot.register_material(request_id=request_id, actor_id=actor, agreement_id="A",
                                     material_id=material_id, code=code, name=code, maker=maker,
                                     batch_no=batch_no)

    # ---- 工艺冻结 ---------------------------------------------------- #
    def test_freeze_requires_prerequisite_evidence(self):
        vid = self._version("lab", "v1")  # lab 本身无前置证据
        self.pilot.freeze_process_version(request_id="f1", actor_id="tech",
                                          agreement_id="A", version_id=vid)
        with self.assertRaises(ConflictError):
            self.pilot.freeze_process_version(request_id="f1b", actor_id="tech",
                                              agreement_id="A", version_id=vid)
        pv = self._version("pilot", "v2")
        with self.assertRaises(ConflictError):  # 证据缺失
            self.pilot.freeze_process_version(request_id="f2", actor_id="tech",
                                              agreement_id="A", version_id=pv)
        self._evidence("pilot", ["lab_process_report", "raw_material_spec", "ip_authorization"])
        self.pilot.freeze_process_version(request_id="f2b", actor_id="tech",
                                          agreement_id="A", version_id=pv)

    def test_pilot_stage_requires_frozen_lab(self):
        vid = self._version("pilot", "vp")
        self._evidence("pilot", ["lab_process_report", "raw_material_spec", "ip_authorization"])
        with self.assertRaises(ConflictError):
            self.pilot.freeze_process_version(request_id="fx", actor_id="tech",
                                              agreement_id="A", version_id=vid)

    # ---- 签收门职责分离 ---------------------------------------------- #
    def test_gate_needs_both_parties_and_separation_of_duty(self):
        self._freeze_lab()
        vid = self._version("pilot", "vp")
        self._evidence("pilot", ["lab_process_report", "raw_material_spec", "ip_authorization"])
        self.pilot.freeze_process_version(request_id="fp", actor_id="tech", agreement_id="A",
                                          version_id=vid)
        gate = self.pilot.open_scale_gate(request_id="g", actor_id="prod", agreement_id="A",
                                          stage="pilot", version_id=vid)
        # 技术方不能代替生产方确认
        with self.assertRaises(PermissionDenied):
            self.pilot.confirm_scale_gate(request_id="x1", actor_id="tech", agreement_id="A",
                                          gate_id=gate["gate_id"], party="production")
        self.pilot.confirm_scale_gate(request_id="ct", actor_id="tech", agreement_id="A",
                                      gate_id=gate["gate_id"], party="tech")
        with self.assertRaises(ConflictError):  # 重复确认
            self.pilot.confirm_scale_gate(request_id="ct2", actor_id="tech", agreement_id="A",
                                          gate_id=gate["gate_id"], party="tech")
        # 仅一方确认时不能放行批次
        self._material("m1", "mat1")
        with self.assertRaises(ConflictError):
            self.pilot.release_batch(request_id="b0", actor_id="prod", agreement_id="A",
                                     batch_no="B0", stage="pilot", version_id=vid,
                                     material_ids=["mat1"])
        self.pilot.confirm_scale_gate(request_id="cp", actor_id="prod", agreement_id="A",
                                      gate_id=gate["gate_id"], party="production")

    def test_concurrent_gate_only_one_open_version_per_stage(self):
        vid, _ = self._passed_gate()
        # 门通过后可以为新版本开门
        vid2 = self._version("pilot", "vp2", formulation={"f": 2})
        self.pilot.freeze_process_version(request_id="fp2", actor_id="tech", agreement_id="A",
                                          version_id=vid2)
        gate2 = self.pilot.open_scale_gate(request_id="g2", actor_id="prod", agreement_id="A",
                                           stage="pilot", version_id=vid2)
        # 未决门期间不能并发为 v3 开门
        vid3 = self._version("pilot", "vp3", formulation={"f": 3})
        self.pilot.freeze_process_version(request_id="fp3", actor_id="tech", agreement_id="A",
                                          version_id=vid3)
        with self.assertRaises(ConflictError):
            self.pilot.open_scale_gate(request_id="g3", actor_id="prod", agreement_id="A",
                                       stage="pilot", version_id=vid3)
        self.pilot.confirm_scale_gate(request_id="c2t", actor_id="tech", agreement_id="A",
                                      gate_id=gate2["gate_id"], party="tech")
        self.pilot.confirm_scale_gate(request_id="c2p", actor_id="prod", agreement_id="A",
                                      gate_id=gate2["gate_id"], party="production")
        gate3 = self.pilot.open_scale_gate(request_id="g3b", actor_id="prod", agreement_id="A",
                                           stage="pilot", version_id=vid3)
        self.assertEqual("open", gate3["status"])

    # ---- 原料与批次 -------------------------------------------------- #
    def test_discontinued_material_cannot_enter_new_batch(self):
        vid, _ = self._passed_gate()
        self._material("m-old", "old", maker="京华", batch_no="LB1")
        self._material("m-new", "new", maker="南方", batch_no="NB1")
        self.pilot.release_batch(request_id="b1", actor_id="prod", agreement_id="A", batch_no="B1",
                                 stage="pilot", version_id=vid, material_ids=["old"])
        result = self.pilot.substitute_material(request_id="sub", actor_id="prod", agreement_id="A",
                                                material_id="old", replacement_material_id="new",
                                                justification="停产替代")
        self.assertEqual([self._batch_id("B1")], result["affected_batch_ids"])
        with self.assertRaises(ConflictError):
            self.pilot.release_batch(request_id="b2", actor_id="prod", agreement_id="A",
                                     batch_no="B2", stage="pilot", version_id=vid,
                                     material_ids=["old"])

    def test_substitution_requires_same_material_code(self):
        self._passed_gate()
        self._material("m-old", "old", code="M1")
        self._material("m-new", "new", code="M2")
        with self.assertRaises(ValidationError):
            self.pilot.substitute_material(request_id="sub", actor_id="prod", agreement_id="A",
                                           material_id="old", replacement_material_id="new",
                                           justification="不同物料")

    def test_batch_material_must_match_allowed_specs(self):
        vid, _ = self._passed_gate()
        self._material("mx", "matx", code="ZZ")
        with self.assertRaises(ConflictError):
            self.pilot.release_batch(request_id="bx", actor_id="prod", agreement_id="A",
                                     batch_no="BX", stage="pilot", version_id=vid,
                                     material_ids=["matx"])

    # ---- 检测回调幂等 ------------------------------------------------ #
    def _released_batch(self, batch_no="B1", req="b1"):
        vid, _ = self._passed_gate()
        self._material("m1", "mat1")
        self.pilot.release_batch(request_id=req, actor_id="prod", agreement_id="A",
                                 batch_no=batch_no, stage="pilot", version_id=vid,
                                 material_ids=["mat1"])
        return self._batch_id(batch_no)

    def _batch_id(self, batch_no):
        return self.database.connection.execute(
            "SELECT batch_id FROM pilot_batches WHERE agreement_id='A' AND batch_no=?",
            (batch_no,)).fetchone()["batch_id"]

    def test_duplicate_callback_does_not_advance_state(self):
        bid = self._released_batch()
        first = self.pilot.quality_callback(request_id="q1", actor_id="prod", agreement_id="A",
                                            batch_id=bid, callback_key="K1",
                                            metrics={"adhesion": 6.0, "thickness": 30.0},
                                            targets=TARGETS)
        self.assertTrue(first["state_advanced"])
        self.assertEqual("qualified", first["result"])
        # 同 key 重放
        second = self.pilot.quality_callback(request_id="q1", actor_id="prod", agreement_id="A",
                                             batch_id=bid, callback_key="K1",
                                             metrics={"adhesion": 6.0, "thickness": 30.0},
                                             targets=TARGETS)
        self.assertTrue(second["replayed"])
        self.assertFalse(second["state_advanced"])
        # 新 key 的第二次回调到达终态批次，不再推进
        third = self.pilot.quality_callback(request_id="q2", actor_id="prod", agreement_id="A",
                                            batch_id=bid, callback_key="K2",
                                            metrics={"adhesion": 2.0, "thickness": 30.0},
                                            targets=TARGETS)
        self.assertFalse(third["state_advanced"])
        status = self.database.connection.execute(
            "SELECT status FROM pilot_batches WHERE batch_id=?", (bid,)).fetchone()["status"]
        self.assertEqual("qualified", status)

    def test_partial_and_failed_classification(self):
        bid = self._released_batch("B2", "b2")
        partial = self.pilot.quality_callback(request_id="q", actor_id="prod", agreement_id="A",
                                              batch_id=bid, callback_key="K",
                                              metrics={"adhesion": 6.0, "thickness": 99.0},
                                              targets=TARGETS)
        self.assertEqual("partial_qualified", partial["result"])
        bid2 = self._released_batch("B3", "b3")
        failed = self.pilot.quality_callback(request_id="qf", actor_id="prod", agreement_id="A",
                                             batch_id=bid2, callback_key="KF",
                                             metrics={"adhesion": 1.0, "thickness": 99.0},
                                             targets=TARGETS)
        self.assertEqual("failed", failed["result"])

    def test_callback_targets_must_cover_every_metric(self):
        bid = self._released_batch()
        with self.assertRaises(ValidationError):
            self.pilot.quality_callback(request_id="q", actor_id="prod", agreement_id="A",
                                        batch_id=bid, callback_key="K",
                                        metrics={"adhesion": 6.0, "thickness": 30.0},
                                        targets={"adhesion": {"min": 5.0, "max": 8.0}})

    # ---- 偏差责任 ---------------------------------------------------- #
    def test_deviation_responsibility_and_countersign_resolution(self):
        bid = self._released_batch()
        dev = self.pilot.open_deviation(request_id="d", actor_id="prod", agreement_id="A",
                                        code="D1", classification="process_design",
                                        batch_id=bid, detail={"why": "配方"})
        self.assertEqual(D.PARTY_TECH, dev["responsible_party"])
        # 责任方不能自行结清
        with self.assertRaises(PermissionDenied):
            self.pilot.resolve_deviation(request_id="ds", actor_id="tech", agreement_id="A",
                                         deviation_id=dev["deviation_id"], disposition="accepted")
        resolved = self.pilot.resolve_deviation(request_id="ds2", actor_id="prod", agreement_id="A",
                                                deviation_id=dev["deviation_id"],
                                                disposition="accepted",
                                                create_obligation={"kind": "compensation",
                                                                   "amount": 1000.0})
        self.assertEqual("open", self._obligation_status(resolved["obligation_id"]))
        with self.assertRaises(ConflictError):  # 已结清不可改写
            self.pilot.resolve_deviation(request_id="ds3", actor_id="prod", agreement_id="A",
                                         deviation_id=dev["deviation_id"], disposition="rejected")

    def test_execution_deviation_is_production_responsibility(self):
        bid = self._released_batch()
        dev = self.pilot.open_deviation(request_id="d", actor_id="tech", agreement_id="A",
                                        code="D2", classification="execution", batch_id=bid,
                                        detail={"why": "操作"})
        self.assertEqual(D.PARTY_PRODUCTION, dev["responsible_party"])
        with self.assertRaises(PermissionDenied):
            self.pilot.resolve_deviation(request_id="ds", actor_id="prod", agreement_id="A",
                                         deviation_id=dev["deviation_id"], disposition="accepted")

    def _obligation_status(self, obligation_id):
        return self.database.connection.execute(
            "SELECT status FROM pilot_obligations WHERE obligation_id=?",
            (obligation_id,)).fetchone()["status"]

    # ---- 付款与义务 -------------------------------------------------- #
    def test_payment_settles_fifo_and_underpayment_keeps_obligation_open(self):
        self.pilot.schedule_payment(request_id="s1", actor_id="adm", agreement_id="A",
                                    milestone="MS1", amount=100.0)
        self.pilot.schedule_payment(request_id="s2", actor_id="adm", agreement_id="A",
                                    milestone="MS2", amount=50.0)
        pay = self.pilot.record_payment(request_id="pay1", actor_id="prod", agreement_id="A",
                                        ref="T1", amount=100.0, direction="production_to_tech")
        self.assertEqual(1, len(pay["settled_obligation_ids"]))
        position = self.pilot.financial_position("A")
        open_amounts = [o["amount"] for o in position["open_obligations"]]
        self.assertEqual([50.0], open_amounts)
        # 付款事实不可删除：再付一次形成新流水
        pay2 = self.pilot.record_payment(request_id="pay2", actor_id="prod", agreement_id="A",
                                         ref="T2", amount=50.0, direction="production_to_tech")
        self.assertEqual(1, len(pay2["settled_obligation_ids"]))
        self.assertEqual(2, len(self.pilot.financial_position("A")["payments"]))

    def test_wrong_payer_party_rejected(self):
        self.pilot.schedule_payment(request_id="s1", actor_id="adm", agreement_id="A",
                                    milestone="MS1", amount=100.0)
        with self.assertRaises(PermissionDenied):
            self.pilot.record_payment(request_id="payx", actor_id="tech", agreement_id="A",
                                      ref="TX", amount=100.0, direction="production_to_tech")

    # ---- 召回 -------------------------------------------------------- #
    def test_recall_preserves_batch_fact_and_creates_return_obligation(self):
        bid = self._released_batch()
        recall = self.pilot.recall_batch(request_id="r", actor_id="tech", agreement_id="A",
                                         batch_id=bid, reason="质量召回")
        self.assertEqual("released", recall["preserved_status"])
        with self.assertRaises(ConflictError):
            self.pilot.recall_batch(request_id="r2", actor_id="prod", agreement_id="A",
                                    batch_id=bid, reason="再次召回")
        disposition = self.pilot.batch_dispositions("A", bid)
        self.assertTrue(disposition["recall"]["recalled"])

    # ---- 终止 -------------------------------------------------------- #
    def test_termination_cancels_unpaid_and_preserves_paid_facts(self):
        vid, _ = self._passed_gate()
        self._material("m1", "mat1")
        self.pilot.release_batch(request_id="b", actor_id="prod", agreement_id="A", batch_no="B1",
                                 stage="pilot", version_id=vid, material_ids=["mat1"])
        bid = self._batch_id("B1")
        dev = self.pilot.open_deviation(request_id="d", actor_id="prod", agreement_id="A",
                                        code="D1", classification="process_design", batch_id=bid,
                                        detail={})
        self.pilot.resolve_deviation(request_id="ds", actor_id="prod", agreement_id="A",
                                     deviation_id=dev["deviation_id"], disposition="accepted",
                                     create_obligation={"kind": "compensation", "amount": 200.0})
        self.pilot.schedule_payment(request_id="s1", actor_id="adm", agreement_id="A",
                                    milestone="MS1", amount=300.0)
        self.pilot.record_payment(request_id="pay", actor_id="prod", agreement_id="A", ref="T1",
                                  amount=300.0, direction="production_to_tech")
        self.pilot.schedule_payment(request_id="s2", actor_id="adm", agreement_id="A",
                                    milestone="MS2", amount=500.0)
        term = self.pilot.terminate_agreement(request_id="tm", actor_id="adm", agreement_id="A",
                                              reason="终止")
        self.assertEqual(1, len(term["cancelled_payment_obligation_ids"]))
        position = self.pilot.financial_position("A")
        kinds = {o["kind"] for o in position["open_obligations"]}
        self.assertEqual({"compensation"}, kinds)
        self.assertEqual(1, len(position["payments"]))  # 已支付事实保留
        # 终止后不能再放行批次，但可履行补偿
        with self.assertRaises(ConflictError):
            self.pilot.release_batch(request_id="ba", actor_id="prod", agreement_id="A",
                                     batch_no="BA", stage="pilot", version_id=vid,
                                     material_ids=["mat1"])
        comp = self.pilot.record_payment(request_id="pc", actor_id="tech", agreement_id="A",
                                         ref="T2", amount=200.0, direction="tech_to_production")
        self.assertEqual(1, len(comp["settled_obligation_ids"]))

    # ---- 变更波及 ---------------------------------------------------- #
    def test_change_impact_covers_version_lineage(self):
        vid1, _ = self._passed_gate()
        self._material("m1", "mat1")
        self.pilot.release_batch(request_id="b1", actor_id="prod", agreement_id="A", batch_no="B1",
                                 stage="pilot", version_id=vid1, material_ids=["mat1"])
        # 新版本替代 v1
        vid2 = self._version("pilot", "vp2", formulation={"f": 2})
        self.pilot.freeze_process_version(request_id="fp2", actor_id="tech", agreement_id="A",
                                          version_id=vid2)
        impact = self.pilot.change_impact(agreement_id="A", version_id=vid2)
        self.assertEqual(1, len(impact["impacted_batches"]))
        self.assertIn("runs_on_superseded_version_lineage",
                      impact["impacted_batches"][0]["reasons"])

    # ---- 查询 -------------------------------------------------------- #
    def test_product_lineage_answers_which_process_and_authorization(self):
        vid, gate_id = self._passed_gate()
        self._material("m1", "mat1", maker="京华", batch_no="LB9")
        self.pilot.release_batch(request_id="b", actor_id="prod", agreement_id="A", batch_no="B1",
                                 stage="pilot", version_id=vid, material_ids=["mat1"])
        bid = self._batch_id("B1")
        lineage = self.pilot.product_lineage("A", bid)
        self.assertEqual(vid, lineage["process_version"]["version_id"])
        self.assertEqual(gate_id, lineage["authorization"]["gate_id"])
        self.assertEqual("passed", lineage["authorization"]["status"])
        self.assertEqual("京华", lineage["materials"][0]["maker"])
        self.assertEqual("coating", lineage["license_scope"]["field"])

    def test_unknown_agreement_and_batch_raise_not_found(self):
        with self.assertRaises(NotFoundError):
            self.pilot.product_lineage("NOPE", "x")


if __name__ == "__main__":
    unittest.main()
