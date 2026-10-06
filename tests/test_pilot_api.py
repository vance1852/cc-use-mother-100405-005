import unittest
from datetime import datetime, timezone

from science_strategy_foundation.api import route
from science_strategy_foundation.clock import FixedClock
from science_strategy_foundation.pilot.service import PilotService
from science_strategy_foundation.service import DomainService
from science_strategy_foundation.storage import Database


class PilotApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        clock = FixedClock(datetime(2026, 10, 6, tzinfo=timezone.utc))
        self.service = DomainService(self.database, clock)
        self.pilot = PilotService(self.database, clock)

    def tearDown(self):
        self.database.close()

    def _bootstrap(self):
        self.service.register_organization(request_id="ot", actor_id="bootstrap",
                                            organization_id="univ", name="大学")
        self.service.register_organization(request_id="op", actor_id="bootstrap",
                                            organization_id="fab", name="工厂")
        self.service.register_actor(request_id="adm", actor_id="bootstrap", new_actor_id="adm",
                                    display_name="管理员", role="admin", organization_id="univ")
        self.service.register_actor(request_id="rt", actor_id="adm", new_actor_id="tech",
                                    display_name="技术方", role="operator", organization_id="univ")
        self.service.register_actor(request_id="rp", actor_id="adm", new_actor_id="prod",
                                    display_name="生产方", role="operator", organization_id="fab")
        self.service.register_site(request_id="st", actor_id="adm", site_id="site",
                                   organization_id="fab", name="中试线", timezone_name="Asia/Shanghai")

    def _post(self, path, body, actor="adm"):
        return route(self.service, "POST", path, body, {"X-Actor-Id": actor}, pilot=self.pilot)

    def _get(self, path, actor="adm"):
        return route(self.service, "GET", path, {}, {"X-Actor-Id": actor}, pilot=self.pilot)

    def test_full_pilot_flow_over_http(self):
        self._bootstrap()
        status, body = self._post("/pilot/agreements", {
            "request_id": "ag", "agreement_id": "A", "site_id": "site",
            "tech_org_id": "univ", "production_org_id": "fab", "title": "涂层",
            "license_scope": {"field": "coating"}})
        self.assertEqual(201, status)

        # 实验室版本冻结
        self._post("/pilot/process-versions", {
            "request_id": "vl", "agreement_id": "A", "stage": "lab",
            "formulation": {"f": 1}, "scale_params": {}, "scale_tolerance": {},
            "allowed_material_specs": {"M1": {}}, "equipment_capabilities": {}})
        lab_id = self._get_version("lab")
        status, body = self._post("/pilot/process-versions/freeze", {
            "request_id": "fl", "agreement_id": "A", "version_id": lab_id})
        self.assertEqual(201, status)

        # 中试版本与双方签收
        self._post("/pilot/process-versions", {
            "request_id": "vp", "agreement_id": "A", "stage": "pilot",
            "formulation": {"f": 1}, "scale_params": {}, "scale_tolerance": {},
            "allowed_material_specs": {"M1": {}}, "equipment_capabilities": {}})
        pilot_id = self._get_version("pilot")
        for req, category, actor in (
                ("e1", "lab_process_report", "tech"),
                ("e2", "raw_material_spec", "prod"),
                ("e3", "ip_authorization", "tech")):
            self._post("/pilot/evidence", {"request_id": req,
                                           "agreement_id": "A", "stage": "pilot",
                                           "category": category, "external_ref": req,
                                           "payload": {}}, actor=actor)
        self._post("/pilot/process-versions/freeze", {
            "request_id": "fp", "agreement_id": "A", "version_id": pilot_id})
        status, gate = self._post("/pilot/scale-gates", {
            "request_id": "g", "agreement_id": "A", "stage": "pilot",
            "version_id": pilot_id}, actor="prod")
        self.assertEqual(201, status)
        self._post("/pilot/scale-gates/confirm", {
            "request_id": "ct", "agreement_id": "A",
            "gate_id": gate["gate_id"], "party": "tech"}, actor="tech")
        self._post("/pilot/scale-gates/confirm", {
            "request_id": "cp", "agreement_id": "A",
            "gate_id": gate["gate_id"], "party": "production"}, actor="prod")

        # 原料、批次、检测回调
        self._post("/pilot/materials", {
            "request_id": "m", "agreement_id": "A", "material_id": "mat",
            "code": "M1", "name": "树脂", "maker": "京华", "batch_no": "LB1"}, actor="prod")
        status, batch = self._post("/pilot/batches", {
            "request_id": "b", "agreement_id": "A", "batch_no": "B1",
            "stage": "pilot", "version_id": pilot_id, "material_ids": ["mat"]}, actor="prod")
        self.assertEqual(201, status)
        status, callback = self._post("/pilot/quality-callbacks", {
            "request_id": "q", "agreement_id": "A",
            "batch_id": batch["batch_id"], "callback_key": "K1",
            "metrics": {"a": 1.0}, "targets": {"a": {"min": 0.0, "max": 2.0}}}, actor="prod")
        self.assertEqual(201, status)
        self.assertEqual("qualified", callback["result"])
        # 重放返回 200
        status, replay = self._post("/pilot/quality-callbacks", {
            "request_id": "q", "agreement_id": "A",
            "batch_id": batch["batch_id"], "callback_key": "K1",
            "metrics": {"a": 1.0}, "targets": {"a": {"min": 0.0, "max": 2.0}}}, actor="prod")
        self.assertEqual(200, status)
        self.assertTrue(replay["replayed"])

        # 查询血缘
        status, lineage = self._get(
            f"/pilot/agreements/A/product-lineage?batch_id={batch['batch_id']}")
        self.assertEqual(200, status)
        self.assertEqual("passed", lineage["authorization"]["status"])
        status, position = self._get("/pilot/agreements/A/financial-position")
        self.assertEqual(200, status)
        self.assertEqual([], position["open_obligations"])

    def test_pilot_route_missing_parameter_returns_400(self):
        status, body = self._get("/pilot/agreements/A/product-lineage")
        self.assertEqual(400, status)
        self.assertEqual("validation_error", body["error"])

    def test_unknown_pilot_route_returns_404(self):
        status, body = self._post("/pilot/nope", {})
        self.assertEqual(404, status)

    def _get_version(self, stage):
        row = self.database.connection.execute(
            "SELECT version_id FROM pilot_process_versions WHERE stage=? ORDER BY seq DESC LIMIT 1",
            (stage,)).fetchone()
        return row["version_id"]


if __name__ == "__main__":
    unittest.main()
