"""中试转化治理 HTTP 路由测试。"""

import unittest

from science_strategy_foundation.service import DomainService
from science_strategy_foundation.storage import Database

from pilot_governance.api import route
from pilot_governance.service import PilotGovernanceService


def _bootstrap(service: DomainService, pilot: PilotGovernanceService) -> None:
    service.register_organization(request_id="ot", actor_id="bootstrap",
                                  organization_id="org-tech", name="高校")
    service.register_actor(request_id="ad", actor_id="bootstrap", new_actor_id="admin-1",
                           display_name="管理员", role="admin", organization_id="org-tech")
    service.register_organization(request_id="op", actor_id="admin-1",
                                  organization_id="org-prod", name="企业")
    service.register_organization(request_id="oc", actor_id="admin-1",
                                  organization_id="org-center", name="中心")
    service.register_organization(request_id="os", actor_id="admin-1",
                                  organization_id="org-sup", name="供应商")
    service.register_actor(request_id="at", actor_id="admin-1", new_actor_id="tech-1",
                           display_name="技术", role="operator", organization_id="org-tech")
    service.register_actor(request_id="ap", actor_id="admin-1", new_actor_id="prod-1",
                           display_name="生产", role="operator", organization_id="org-prod")
    service.register_actor(request_id="ac", actor_id="admin-1", new_actor_id="center-1",
                           display_name="中心员", role="reviewer", organization_id="org-center")
    service.register_site(request_id="st", actor_id="prod-1", site_id="plant-1",
                          organization_id="org-prod", name="车间", timezone_name="Asia/Shanghai")
    pilot.create_project(request_id="pj", actor_id="admin-1", project_id="proj-1", name="涂层",
                         ip_owner_org_id="org-tech", producer_org_id="org-prod",
                         transfer_center_org_id="org-center")


class PilotApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.foundation = DomainService(self.database)
        self.service = PilotGovernanceService(self.database)
        _bootstrap(self.foundation, self.service)

    def tearDown(self):
        self.database.close()

    def test_health(self):
        status, payload = route(self.service, "GET", "/health", None)
        self.assertEqual(200, status)
        self.assertEqual("ok", payload["status"])

    def test_full_chain_via_routes(self):
        headers_t = {"X-Actor-Id": "tech-1"}
        headers_p = {"X-Actor-Id": "prod-1"}
        headers_c = {"X-Actor-Id": "center-1"}
        headers_a = {"X-Actor-Id": "admin-1"}

        status, payload = route(self.service, "POST", "/pilot/agreements", {
            "request_id": "ag", "project_id": "proj-1", "agreement_id": "agr-1",
            "ip_ownership": "高校所有", "license_scope": {"field": "涂布"},
            "liability_terms": {"failed_batch": "裁决"},
            "payment_terms": {"currency": "CNY", "stage_amounts": {"lab": 500}}}, headers_t)
        self.assertEqual(201, status)

        status, payload = route(self.service, "POST", "/pilot/equipment", {
            "request_id": "eq", "project_id": "proj-1", "equipment_id": "eq-1",
            "site_id": "plant-1", "name": "实验机", "scale_stage": "lab",
            "capability": {"speed": {"min": 1, "max": 5}}}, headers_p)
        self.assertEqual(201, status)

        status, payload = route(self.service, "POST", "/pilot/materials", {
            "request_id": "ma", "project_id": "proj-1", "material_id": "mat-1",
            "material_key": "resin", "name": "树脂", "supplier_org_id": "org-sup",
            "supplier_batch_no": "B1"}, headers_p)
        self.assertEqual(201, status)

        status, payload = route(self.service, "POST", "/pilot/processes", {
            "request_id": "pr", "project_id": "proj-1", "process_id": "proc-1",
            "version": "1", "scale_stage": "lab", "specification": {"f": 1},
            "parameter_windows": {"temp": {"min": 1, "max": 2}},
            "quality_metrics": {"q": {"min": 0, "max": 10}},
            "requires_materials": ["resin"]}, headers_t)
        self.assertEqual(201, status)
        status, _ = route(self.service, "POST", "/pilot/processes/freeze",
                          {"request_id": "fr", "process_id": "proc-1"}, headers_t)
        self.assertEqual(201, status)
        status, _ = route(self.service, "POST", "/pilot/gates", {
            "request_id": "go", "project_id": "proj-1", "gate_id": "gate-1",
            "scale_stage": "lab", "process_id": "proc-1", "evidence_refs": ["ev://1"]},
            headers_t)
        self.assertEqual(201, status)
        status, _ = route(self.service, "POST", "/pilot/gates/confirm",
                          {"request_id": "ct", "gate_id": "gate-1"}, headers_t)
        self.assertEqual(201, status)
        status, _ = route(self.service, "POST", "/pilot/gates/confirm",
                          {"request_id": "cp", "gate_id": "gate-1"}, headers_p)
        self.assertEqual(201, status)

        status, _ = route(self.service, "POST", "/pilot/batches", {
            "request_id": "bo", "project_id": "proj-1", "batch_id": "batch-1",
            "scale_stage": "lab"}, headers_p)
        self.assertEqual(201, status)
        status, payload = route(self.service, "POST", "/pilot/inspections", {
            "request_id": "in", "batch_id": "batch-1", "callback_id": "cb-1",
            "metric_values": {"q": 5.0}}, headers_p)
        self.assertEqual(201, status)
        self.assertEqual("pass", payload["verdict"])

        status, payload = route(self.service, "GET",
                                "/pilot/products/provenance?batch_id=batch-1", None)
        self.assertEqual(200, status)
        self.assertEqual("agr-1", payload["authorization"]["agreement_id"])

        status, payload = route(self.service, "GET",
                                "/pilot/obligations?project_id=proj-1", None)
        self.assertEqual(200, status)
        self.assertEqual(1, len(payload["pending_obligations"]))

        status, payload = route(self.service, "GET",
                                "/pilot/change-impact?project_id=proj-1&process_id=proc-1", None)
        self.assertEqual(200, status)
        self.assertEqual(1, len(payload["affected_batches"]))

    def test_missing_actor_is_rejected_for_write(self):
        status, payload = route(self.service, "POST", "/pilot/projects", {
            "request_id": "x", "project_id": "p", "name": "n",
            "ip_owner_org_id": "org-tech", "producer_org_id": "org-prod",
            "transfer_center_org_id": "org-center"}, {})
        self.assertEqual(404, status)
        self.assertEqual("not_found", payload["error"])

    def test_unknown_pilot_route(self):
        status, payload = route(self.service, "GET", "/pilot/nope", None)
        self.assertEqual(404, status)


if __name__ == "__main__":
    unittest.main()
