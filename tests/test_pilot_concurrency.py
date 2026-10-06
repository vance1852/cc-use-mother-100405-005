"""验证并发签收时每个阶段最多一个工艺版本生效。"""

import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path

from science_strategy_foundation.errors import ConflictError
from science_strategy_foundation.service import DomainService
from science_strategy_foundation.storage import Database

from pilot_governance.clock_helpers import SteppingClock
from pilot_governance.service import PilotGovernanceService


class ConcurrentSignoffTest(unittest.TestCase):
    def _seed(self, path: Path):
        database = Database(path)
        clock = SteppingClock(datetime(2026, 10, 6, tzinfo=timezone.utc))
        foundation = DomainService(database, clock)
        service = PilotGovernanceService(database, clock)
        foundation.register_organization(request_id="ot", actor_id="bootstrap",
                                         organization_id="org-tech", name="高校")
        foundation.register_actor(request_id="ad", actor_id="bootstrap", new_actor_id="admin-1",
                                  display_name="管理员", role="admin", organization_id="org-tech")
        foundation.register_organization(request_id="op", actor_id="admin-1",
                                         organization_id="org-prod", name="企业")
        foundation.register_organization(request_id="oc", actor_id="admin-1",
                                         organization_id="org-center", name="中心")
        foundation.register_organization(request_id="os", actor_id="admin-1",
                                         organization_id="org-sup", name="供应商")
        foundation.register_actor(request_id="at", actor_id="admin-1", new_actor_id="tech-1",
                                  display_name="技术", role="operator", organization_id="org-tech")
        foundation.register_actor(request_id="ap", actor_id="admin-1", new_actor_id="prod-1",
                                  display_name="生产", role="operator", organization_id="org-prod")
        foundation.register_site(request_id="st", actor_id="prod-1", site_id="plant-1",
                                 organization_id="org-prod", name="车间",
                                 timezone_name="Asia/Shanghai")
        service.create_project(request_id="pj", actor_id="admin-1", project_id="proj-1", name="涂层",
                               ip_owner_org_id="org-tech", producer_org_id="org-prod",
                               transfer_center_org_id="org-center")
        service.register_agreement(
            request_id="ag", actor_id="tech-1", project_id="proj-1", agreement_id="agr-1",
            ip_ownership="高校所有", license_scope={"field": "涂布"},
            liability_terms={"failed_batch": "裁决"},
            payment_terms={"currency": "CNY", "stage_amounts": {"lab": 1}})
        service.register_equipment(request_id="eq", actor_id="prod-1", project_id="proj-1",
                                   equipment_id="eq-1", site_id="plant-1", name="机",
                                   scale_stage="lab",
                                   capability={"speed": {"min": 1, "max": 5}})
        service.register_material(request_id="ma", actor_id="prod-1", project_id="proj-1",
                                  material_id="mat-1", material_key="resin", name="树脂",
                                  supplier_org_id="org-sup", supplier_batch_no="B1")
        for suffix in ("a", "b"):
            service.create_process(
                request_id=f"pr-{suffix}", actor_id="tech-1", project_id="proj-1",
                process_id=f"proc-{suffix}", version=f"1-{suffix}", scale_stage="lab",
                specification={"f": suffix},
                parameter_windows={"temp": {"min": 1, "max": 2}},
                quality_metrics={"q": {"min": 0, "max": 10}},
                requires_materials=["resin"])
            service.freeze_process(request_id=f"fr-{suffix}", actor_id="tech-1",
                                   process_id=f"proc-{suffix}")
            clock.tick()
            service.open_gate(request_id=f"go-{suffix}", actor_id="tech-1", project_id="proj-1",
                              gate_id=f"gate-{suffix}", scale_stage="lab",
                              process_id=f"proc-{suffix}", evidence_refs=[f"ev://{suffix}"])
            # 技术方先签收，留下两个只差生产方签收即生效的闸门。
            service.confirm_gate(request_id=f"ct-{suffix}", actor_id="tech-1",
                                 gate_id=f"gate-{suffix}")
        return database

    def test_concurrent_producer_signoff_activates_exactly_one_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "concurrent.sqlite3"
            seeder = self._seed(path)
            seeder.close()

            results: dict[str, object] = {}
            barrier = threading.Barrier(2)

            def worker(name: str, request_id: str, gate_id: str) -> None:
                database = Database(path)
                service = PilotGovernanceService(database)
                barrier.wait()
                try:
                    receipt = service.confirm_gate(request_id=request_id, actor_id="prod-1",
                                                   gate_id=gate_id)
                    results[name] = ("activated", receipt.replayed)
                except ConflictError as exc:
                    results[name] = ("conflict", str(exc))
                finally:
                    database.close()

            t1 = threading.Thread(target=worker, args=("a", "cp-a", "gate-a"))
            t2 = threading.Thread(target=worker, args=("b", "cp-b", "gate-b"))
            t1.start()
            t2.start()
            t1.join(timeout=10)
            t2.join(timeout=10)

            outcomes = sorted(results.values())
            self.assertEqual(2, len(results))
            activated = [name for name, value in results.items() if value[0] == "activated"]
            conflicts = [name for name, value in results.items() if value[0] == "conflict"]
            self.assertEqual(1, len(activated), results)
            self.assertEqual(1, len(conflicts), results)

            checker = Database(path)
            active = checker.connection.execute(
                "SELECT gate_id FROM pilot_stage_gates WHERE status='active'"
            ).fetchall()
            self.assertEqual(1, len(active))
            self.assertEqual(f"gate-{activated[0]}", active[0]["gate_id"])
            checker.close()


if __name__ == "__main__":
    unittest.main()
