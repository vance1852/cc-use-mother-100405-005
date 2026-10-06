"""中试转化治理领域服务：冻结、双签、偏差处置与只追加义务台账。"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any, Callable

from science_strategy_foundation.audit import append_event, canonical_json, digest
from science_strategy_foundation.clock import Clock, SystemClock
from science_strategy_foundation.errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from science_strategy_foundation.models import Actor
from science_strategy_foundation.storage import Database

from . import domain as D
from .storage import extend_schema

IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")
# 在制状态：变更波及分析与义务追踪针对这些批次。
IN_PROGRESS_STATUSES = (D.BATCH_OPEN, D.BATCH_INSPECTED, D.BATCH_PARTIAL)
LEDGER_SETTLED = "settled"


class PilotGovernanceService:
    """在基础服务的事务、幂等与审计边界上实现中试转化规则。"""

    def __init__(self, database: Database, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()
        extend_schema(database.connection)

    # ------------------------------------------------------------------
    # 通用辅助
    # ------------------------------------------------------------------
    def _now(self) -> str:
        return self.clock.now().isoformat().replace("+00:00", "Z")

    def _id(self, value: str, field: str) -> str:
        value = str(value).strip()
        if not IDENTIFIER.fullmatch(value):
            raise ValidationError(f"{field} 格式无效")
        return value

    def _text(self, value: str, field: str, limit: int = 500) -> str:
        value = str(value).strip()
        if not value or len(value) > limit:
            raise ValidationError(f"{field} 不能为空且不能超过 {limit} 个字符")
        return value

    def _dict(self, value: Any, field: str) -> dict[str, Any]:
        if not isinstance(value, dict) or not value:
            raise ValidationError(f"{field} 必须是非空对象")
        return value

    def _window(self, mapping: Any, field: str) -> dict[str, Any]:
        mapping = self._dict(mapping, field)
        normalized: dict[str, Any] = {}
        for name, bounds in mapping.items():
            if not isinstance(bounds, dict) or "min" not in bounds or "max" not in bounds:
                raise ValidationError(f"{field}.{name} 必须包含 min 与 max")
            low, high = bounds["min"], bounds["max"]
            if not isinstance(low, (int, float)) or not isinstance(high, (int, float)) or low > high:
                raise ValidationError(f"{field}.{name} 的偏差窗口无效")
            normalized[str(name)] = {"min": float(low), "max": float(high)}
        return normalized

    def _actor(self, connection, actor_id: str) -> Actor:
        row = connection.execute("SELECT * FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
        if row is None:
            raise NotFoundError("操作者不存在")
        actor = Actor(row["actor_id"], row["display_name"], row["role"], row["organization_id"], bool(row["active"]))
        if not actor.active:
            raise PermissionDenied("操作者已停用")
        if actor.role == "auditor":
            raise PermissionDenied("审计角色为只读，不能执行写操作")
        return actor

    def _org_exists(self, connection, org_id: str) -> None:
        if connection.execute("SELECT 1 FROM organizations WHERE organization_id=?", (org_id,)).fetchone() is None:
            raise NotFoundError(f"组织不存在: {org_id}")

    def _project(self, connection, project_id: str):
        row = connection.execute("SELECT * FROM pilot_projects WHERE project_id=?", (project_id,)).fetchone()
        if row is None:
            raise NotFoundError("项目不存在")
        return row

    def _require_active(self, project_row) -> None:
        if project_row["status"] != D.PROJECT_ACTIVE:
            raise ConflictError("项目已终止，不能再发起新的生产性动作")

    def _party(self, actor: Actor, project_row) -> str:
        if actor.role == "admin":
            return "admin"
        if actor.organization_id == project_row["ip_owner_org_id"]:
            return D.PARTY_TECH
        if actor.organization_id == project_row["producer_org_id"]:
            return D.PARTY_PRODUCER
        if actor.organization_id == project_row["transfer_center_org_id"]:
            return "center"
        raise PermissionDenied("操作者不属于本项目的技术方、生产方或成果转化中心")

    def _idempotent(self, connection, *, request_id: str, action: str,
                    payload: dict[str, Any], create: Callable[[], tuple[str, str, dict[str, Any]]]):
        request_id = self._id(request_id, "request_id")
        payload_hash = digest(payload)
        row = connection.execute("SELECT * FROM request_receipts WHERE request_id=?", (request_id,)).fetchone()
        if row:
            if row["action"] != action or row["payload_hash"] != payload_hash:
                raise ConflictError("request_id 已被不同内容使用")
            from science_strategy_foundation.models import WriteReceipt
            return WriteReceipt(request_id, row["resource_type"], row["resource_id"], True)
        resource_type, resource_id, response = create()
        connection.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,resource_id,response_json,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (request_id, action, payload_hash, resource_type, resource_id, canonical_json(response), self._now()),
        )
        from science_strategy_foundation.models import WriteReceipt
        return WriteReceipt(request_id, resource_type, resource_id, False)

    def _audit(self, connection, *, actor_id: str, action: str, resource_type: str,
               resource_id: str, detail: dict[str, Any]) -> None:
        append_event(connection, actor_id=actor_id, action=action, resource_type=resource_type,
                     resource_id=resource_id, detail=detail, occurred_at=self._now())

    def _latest_agreement(self, connection, project_id: str):
        row = connection.execute(
            "SELECT * FROM pilot_agreements WHERE project_id=? ORDER BY version DESC LIMIT 1", (project_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("项目尚未登记技术转让协议")
        return row

    def _process(self, connection, process_id: str):
        row = connection.execute("SELECT * FROM pilot_process_versions WHERE process_id=?", (process_id,)).fetchone()
        if row is None:
            raise NotFoundError("工艺版本不存在")
        return row

    # ------------------------------------------------------------------
    # 项目与协议
    # ------------------------------------------------------------------
    def create_project(self, *, request_id: str, actor_id: str, project_id: str, name: str,
                       ip_owner_org_id: str, producer_org_id: str,
                       transfer_center_org_id: str):
        payload = {"actor_id": actor_id, "project_id": project_id, "name": name,
                   "ip_owner_org_id": ip_owner_org_id, "producer_org_id": producer_org_id,
                   "transfer_center_org_id": transfer_center_org_id}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            if actor.role != "admin":
                raise PermissionDenied("仅管理员可以创建项目")
            project_id = self._id(project_id, "project_id")
            name = self._text(name, "name")
            for org_id in (ip_owner_org_id, producer_org_id, transfer_center_org_id):
                self._org_exists(conn, org_id)
            if ip_owner_org_id == producer_org_id:
                raise ValidationError("技术方与生产方必须分属不同组织，才能职责分离")

            def create():
                try:
                    conn.execute(
                        "INSERT INTO pilot_projects(project_id,name,ip_owner_org_id,producer_org_id,"
                        "transfer_center_org_id,status,created_at) VALUES(?,?,?,?,?,?,?)",
                        (project_id, name, ip_owner_org_id, producer_org_id, transfer_center_org_id,
                         D.PROJECT_ACTIVE, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("项目编号已经存在") from exc
                self._audit(conn, actor_id=actor_id, action="pilot.project.created",
                            resource_type="project", resource_id=project_id,
                            detail={"name": name, "ip_owner_org_id": ip_owner_org_id,
                                    "producer_org_id": producer_org_id,
                                    "transfer_center_org_id": transfer_center_org_id})
                return "project", project_id, {"project_id": project_id, "status": D.PROJECT_ACTIVE}

            return self._idempotent(conn, request_id=request_id, action="pilot.create_project",
                                    payload=payload, create=create)

    def register_agreement(self, *, request_id: str, actor_id: str, project_id: str, agreement_id: str,
                           ip_ownership: str, license_scope: dict[str, Any],
                           liability_terms: dict[str, Any], payment_terms: dict[str, Any]):
        payload = {"actor_id": actor_id, "project_id": project_id, "agreement_id": agreement_id,
                   "ip_ownership": ip_ownership, "license_scope": license_scope,
                   "liability_terms": liability_terms, "payment_terms": payment_terms}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            project = self._project(conn, project_id)
            self._require_active(project)
            party = self._party(actor, project)
            if party not in ("tech", "center", "admin"):
                raise PermissionDenied("协议由技术方会同成果转化中心登记")
            agreement_id = self._id(agreement_id, "agreement_id")
            ip_ownership = self._text(ip_ownership, "ip_ownership", 300)
            license_scope = self._dict(license_scope, "license_scope")
            liability_terms = self._dict(liability_terms, "liability_terms")
            payment_terms = self._dict(payment_terms, "payment_terms")
            version_row = conn.execute(
                "SELECT COALESCE(MAX(version),0) AS v FROM pilot_agreements WHERE project_id=?", (project_id,)
            ).fetchone()
            version = version_row["v"] + 1

            def create():
                try:
                    conn.execute(
                        "INSERT INTO pilot_agreements(agreement_id,project_id,version,ip_ownership,"
                        "license_scope_json,liability_terms_json,payment_terms_json,status,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?)",
                        (agreement_id, project_id, version, ip_ownership, canonical_json(license_scope),
                         canonical_json(liability_terms), canonical_json(payment_terms),
                         D.AGREEMENT_ACTIVE, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("协议编号已经存在") from exc
                self._audit(conn, actor_id=actor_id, action="pilot.agreement.registered",
                            resource_type="agreement", resource_id=agreement_id,
                            detail={"project_id": project_id, "version": version,
                                    "ip_ownership": ip_ownership,
                                    "license_hash": digest(license_scope),
                                    "liability_hash": digest(liability_terms)})
                return "agreement", agreement_id, {"agreement_id": agreement_id, "version": version}

            return self._idempotent(conn, request_id=request_id, action="pilot.register_agreement",
                                    payload=payload, create=create)

    # ------------------------------------------------------------------
    # 工艺版本
    # ------------------------------------------------------------------
    def create_process(self, *, request_id: str, actor_id: str, project_id: str, process_id: str,
                       version: str, scale_stage: str, specification: dict[str, Any],
                       parameter_windows: dict[str, Any], quality_metrics: dict[str, Any],
                       requires_materials: list[str], based_on_process_id: str | None = None):
        payload = {"actor_id": actor_id, "project_id": project_id, "process_id": process_id,
                   "version": version, "scale_stage": scale_stage, "specification": specification,
                   "parameter_windows": parameter_windows, "quality_metrics": quality_metrics,
                   "requires_materials": requires_materials, "based_on_process_id": based_on_process_id}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            project = self._project(conn, project_id)
            self._require_active(project)
            if self._party(actor, project) not in ("tech", "admin"):
                raise PermissionDenied("可执行工艺由技术方编制")
            process_id = self._id(process_id, "process_id")
            version = self._text(version, "version", 40)
            if scale_stage not in D.STAGE_ORDER:
                raise ValidationError("scale_stage 非法")
            specification = self._dict(specification, "specification")
            parameter_windows = self._window(parameter_windows, "parameter_windows")
            quality_metrics = self._window(quality_metrics, "quality_metrics")
            if not isinstance(requires_materials, list) or not requires_materials:
                raise ValidationError("requires_materials 必须是非空清单")
            requires_materials = [self._text(m, "material_key", 80) for m in requires_materials]
            if based_on_process_id:
                base = self._process(conn, based_on_process_id)
                if base["project_id"] != project_id:
                    raise ValidationError("母版工艺不属于同一项目")
                if D.STAGE_ORDER[base["scale_stage"]] >= D.STAGE_ORDER[scale_stage]:
                    raise ValidationError("新工艺的放大阶段必须高于母版工艺")
            spec_hash = digest(specification)

            def create():
                try:
                    conn.execute(
                        "INSERT INTO pilot_process_versions(process_id,project_id,version,scale_stage,"
                        "based_on_process_id,specification_json,specification_hash,parameter_windows_json,"
                        "quality_metrics_json,requires_materials_json,status,frozen_at,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (process_id, project_id, version, scale_stage, based_on_process_id,
                         canonical_json(specification), spec_hash, canonical_json(parameter_windows),
                         canonical_json(quality_metrics), canonical_json(requires_materials),
                         D.PROCESS_DRAFT, None, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("工艺编号或项目内版本号已经存在") from exc
                self._audit(conn, actor_id=actor_id, action="pilot.process.created",
                            resource_type="process", resource_id=process_id,
                            detail={"project_id": project_id, "version": version,
                                    "scale_stage": scale_stage, "specification_hash": spec_hash})
                return "process", process_id, {"process_id": process_id, "status": D.PROCESS_DRAFT}

            return self._idempotent(conn, request_id=request_id, action="pilot.create_process",
                                    payload=payload, create=create)

    def freeze_process(self, *, request_id: str, actor_id: str, process_id: str):
        """放大前冻结可执行工艺；冻结后内容不可变，只能新建后继版本。"""
        payload = {"actor_id": actor_id, "process_id": process_id}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            process = self._process(conn, process_id)
            project = self._project(conn, process["project_id"])
            if self._party(actor, project) not in ("tech", "admin"):
                raise PermissionDenied("工艺由技术方冻结")
            if process["status"] != D.PROCESS_DRAFT:
                raise ConflictError(f"工艺当前状态为 {process['status']}，不能冻结")
            frozen_at = self._now()

            def create():
                conn.execute("UPDATE pilot_process_versions SET status=?, frozen_at=? WHERE process_id=?",
                             (D.PROCESS_FROZEN, frozen_at, process_id))
                self._audit(conn, actor_id=actor_id, action="pilot.process.frozen",
                            resource_type="process", resource_id=process_id,
                            detail={"project_id": process["project_id"], "version": process["version"],
                                    "scale_stage": process["scale_stage"],
                                    "specification_hash": process["specification_hash"],
                                    "frozen_at": frozen_at})
                return "process", process_id, {"process_id": process_id, "status": D.PROCESS_FROZEN}

            return self._idempotent(conn, request_id=request_id, action="pilot.freeze_process",
                                    payload=payload, create=create)

    # ------------------------------------------------------------------
    # 设备能力与原料谱系
    # ------------------------------------------------------------------
    def register_equipment(self, *, request_id: str, actor_id: str, project_id: str, equipment_id: str,
                           site_id: str, name: str, scale_stage: str, capability: dict[str, Any]):
        payload = {"actor_id": actor_id, "project_id": project_id, "equipment_id": equipment_id,
                   "site_id": site_id, "name": name, "scale_stage": scale_stage, "capability": capability}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            project = self._project(conn, project_id)
            self._require_active(project)
            if self._party(actor, project) not in ("producer", "admin"):
                raise PermissionDenied("设备能力由生产方登记")
            site = conn.execute("SELECT * FROM sites WHERE site_id=?", (site_id,)).fetchone()
            if site is None:
                raise NotFoundError("场所不存在")
            if site["organization_id"] != project["producer_org_id"] and actor.role != "admin":
                raise ValidationError("设备必须位于生产方场所")
            if scale_stage not in D.STAGE_ORDER:
                raise ValidationError("scale_stage 非法")
            equipment_id = self._id(equipment_id, "equipment_id")
            capability = self._window(capability, "capability")
            name = self._text(name, "name")

            def create():
                try:
                    conn.execute(
                        "INSERT INTO pilot_equipment(equipment_id,project_id,site_id,name,scale_stage,"
                        "capability_json,created_at) VALUES(?,?,?,?,?,?,?)",
                        (equipment_id, project_id, site_id, name, scale_stage,
                         canonical_json(capability), self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("设备编号已经存在") from exc
                self._audit(conn, actor_id=actor_id, action="pilot.equipment.registered",
                            resource_type="equipment", resource_id=equipment_id,
                            detail={"project_id": project_id, "site_id": site_id,
                                    "scale_stage": scale_stage})
                return "equipment", equipment_id, {"equipment_id": equipment_id}

            return self._idempotent(conn, request_id=request_id, action="pilot.register_equipment",
                                    payload=payload, create=create)

    def register_material(self, *, request_id: str, actor_id: str, project_id: str, material_id: str,
                          material_key: str, name: str, supplier_org_id: str, supplier_batch_no: str):
        payload = {"actor_id": actor_id, "project_id": project_id, "material_id": material_id,
                   "material_key": material_key, "name": name, "supplier_org_id": supplier_org_id,
                   "supplier_batch_no": supplier_batch_no}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            project = self._project(conn, project_id)
            self._require_active(project)
            if self._party(actor, project) not in ("producer", "tech", "admin"):
                raise PermissionDenied("原料由生产方或技术方登记")
            self._org_exists(conn, supplier_org_id)
            material_id = self._id(material_id, "material_id")
            material_key = self._text(material_key, "material_key", 80)
            name = self._text(name, "name")
            supplier_batch_no = self._text(supplier_batch_no, "supplier_batch_no", 120)

            def create():
                try:
                    conn.execute(
                        "INSERT INTO pilot_materials(material_id,project_id,material_key,name,"
                        "supplier_org_id,supplier_batch_no,status,created_at) VALUES(?,?,?,?,?,?,?,?)",
                        (material_id, project_id, material_key, name, supplier_org_id,
                         supplier_batch_no, D.MATERIAL_ACTIVE, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("原料编号或同键同批记录已经存在") from exc
                self._audit(conn, actor_id=actor_id, action="pilot.material.registered",
                            resource_type="material", resource_id=material_id,
                            detail={"project_id": project_id, "material_key": material_key,
                                    "supplier_org_id": supplier_org_id,
                                    "supplier_batch_no": supplier_batch_no})
                return "material", material_id, {"material_id": material_id, "status": D.MATERIAL_ACTIVE}

            return self._idempotent(conn, request_id=request_id, action="pilot.register_material",
                                    payload=payload, create=create)

    def mark_material_discontinued(self, *, request_id: str, actor_id: str, material_id: str):
        """登记供应商停产这一外部事实；历史批次绑定的原料记录保持不变。"""
        payload = {"actor_id": actor_id, "material_id": material_id}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            row = conn.execute("SELECT * FROM pilot_materials WHERE material_id=?", (material_id,)).fetchone()
            if row is None:
                raise NotFoundError("原料不存在")
            project = self._project(conn, row["project_id"])
            if self._party(actor, project) not in ("producer", "center", "admin"):
                raise PermissionDenied("停产信息由生产方或成果转化中心登记")
            if row["status"] != D.MATERIAL_ACTIVE:
                raise ConflictError(f"原料当前状态为 {row['status']}")

            def create():
                conn.execute("UPDATE pilot_materials SET status=? WHERE material_id=?",
                             (D.MATERIAL_DISCONTINUED, material_id))
                self._audit(conn, actor_id=actor_id, action="pilot.material.discontinued",
                            resource_type="material", resource_id=material_id,
                            detail={"project_id": row["project_id"], "material_key": row["material_key"],
                                    "supplier_batch_no": row["supplier_batch_no"]})
                return "material", material_id, {"material_id": material_id,
                                                  "status": D.MATERIAL_DISCONTINUED}

            return self._idempotent(conn, request_id=request_id, action="pilot.material_discontinued",
                                    payload=payload, create=create)

    def replace_supplier(self, *, request_id: str, actor_id: str, project_id: str, replacement_id: str,
                         material_key: str, new_material_id: str, reason: str):
        """供应商替换以新记录生效：只影响生效时点之后开批的批次，历史批次绑定不变。"""
        payload = {"actor_id": actor_id, "project_id": project_id, "replacement_id": replacement_id,
                   "material_key": material_key, "new_material_id": new_material_id, "reason": reason}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            project = self._project(conn, project_id)
            self._require_active(project)
            if self._party(actor, project) not in ("center", "admin"):
                raise PermissionDenied("供应商替换由成果转化中心裁决")
            material_key = self._text(material_key, "material_key", 80)
            reason = self._text(reason, "reason")
            new_row = conn.execute(
                "SELECT * FROM pilot_materials WHERE material_id=?", (new_material_id,)
            ).fetchone()
            if new_row is None:
                raise NotFoundError("新原料不存在")
            if new_row["project_id"] != project_id or new_row["material_key"] != material_key:
                raise ValidationError("新原料不属于本项目或物料键不一致")
            if new_row["status"] != D.MATERIAL_ACTIVE:
                raise ConflictError("替代原料必须处于有效状态")
            last_replacement = conn.execute(
                "SELECT new_material_id FROM pilot_supplier_replacements WHERE project_id=? AND material_key=? "
                "ORDER BY effective_from DESC, created_at DESC LIMIT 1",
                (project_id, material_key),
            ).fetchone()
            if last_replacement:
                old_id = last_replacement["new_material_id"]
            else:
                old = conn.execute(
                    "SELECT material_id FROM pilot_materials WHERE project_id=? AND material_key=? "
                    "AND material_id<>? ORDER BY created_at DESC LIMIT 1",
                    (project_id, material_key, new_material_id),
                ).fetchone()
                old_id = old["material_id"] if old else None
            if not old_id:
                raise NotFoundError("该物料键尚无在册原料，无需替换")
            if old_id == new_material_id:
                raise ConflictError("替代原料与当前原料相同")
            old_row = conn.execute("SELECT * FROM pilot_materials WHERE material_id=?", (old_id,)).fetchone()
            if old_row["supplier_org_id"] == new_row["supplier_org_id"] and \
                    old_row["supplier_batch_no"] == new_row["supplier_batch_no"]:
                raise ValidationError("替代供应来源与原来源完全相同")
            effective_from = self._now()

            def create():
                try:
                    conn.execute(
                        "INSERT INTO pilot_supplier_replacements(replacement_id,project_id,material_key,"
                        "old_material_id,new_material_id,reason,effective_from,decided_by,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?)",
                        (replacement_id, project_id, material_key, old_id, new_material_id, reason,
                         effective_from, actor_id, effective_from),
                    )
                except Exception as exc:
                    raise ConflictError("替换记录编号已经存在") from exc
                if old_row["status"] == D.MATERIAL_ACTIVE:
                    conn.execute("UPDATE pilot_materials SET status=? WHERE material_id=?",
                                 (D.MATERIAL_REPLACED, old_id))
                self._audit(conn, actor_id=actor_id, action="pilot.supplier.replaced",
                            resource_type="supplier_replacement", resource_id=replacement_id,
                            detail={"project_id": project_id, "material_key": material_key,
                                    "old_material_id": old_id, "new_material_id": new_material_id,
                                    "effective_from": effective_from, "reason": reason})
                return "replacement", replacement_id, {"replacement_id": replacement_id,
                                                        "old_material_id": old_id,
                                                        "new_material_id": new_material_id,
                                                        "effective_from": effective_from}

            return self._idempotent(conn, request_id=request_id, action="pilot.replace_supplier",
                                    payload=payload, create=create)

    # ------------------------------------------------------------------
    # 阶段闸门：冻结证据 + 职责分离双签
    # ------------------------------------------------------------------
    def open_gate(self, *, request_id: str, actor_id: str, project_id: str, gate_id: str,
                  scale_stage: str, process_id: str, evidence_refs: list[str]):
        payload = {"actor_id": actor_id, "project_id": project_id, "gate_id": gate_id,
                   "scale_stage": scale_stage, "process_id": process_id, "evidence_refs": evidence_refs}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            project = self._project(conn, project_id)
            self._require_active(project)
            self._party(actor, project)  # 任一方均可起草
            if scale_stage not in D.STAGE_ORDER:
                raise ValidationError("scale_stage 非法")
            process = self._process(conn, process_id)
            if process["project_id"] != project_id or process["scale_stage"] != scale_stage:
                raise ValidationError("工艺与项目或放大阶段不匹配")
            if process["status"] != D.PROCESS_FROZEN:
                raise ConflictError("闸门只能引用已冻结工艺")
            if not isinstance(evidence_refs, list) or not evidence_refs or \
                    any(not isinstance(ref, str) or not ref.strip() for ref in evidence_refs):
                raise ValidationError("evidence_refs 必须是非空字符串清单（前置证据）")
            stage_no = D.STAGE_ORDER[scale_stage]
            if stage_no > 0:
                prev_stage = D.SCALE_STAGES[stage_no - 1]
                prev = conn.execute(
                    "SELECT 1 FROM pilot_stage_gates WHERE project_id=? AND scale_stage=? AND status=?",
                    (project_id, prev_stage, D.GATE_ACTIVE),
                ).fetchone()
                if not prev:
                    raise ConflictError(f"必须先完成前一阶段 {prev_stage} 的生效闸门")
            equipment = conn.execute(
                "SELECT 1 FROM pilot_equipment WHERE project_id=? AND scale_stage=?",
                (project_id, scale_stage),
            ).fetchone()
            if not equipment:
                raise ConflictError("该阶段尚无在册设备能力，不能冻结闸门")
            required = json.loads(process["requires_materials_json"])
            for key in required:
                material_id = self._resolve_material_id(conn, project_id, key, self._now())
                row = conn.execute("SELECT status FROM pilot_materials WHERE material_id=?",
                                   (material_id,)).fetchone()
                if row["status"] != D.MATERIAL_ACTIVE:
                    raise ConflictError(f"物料 {key} 的当前来源无效，需先完成供应商替换")

            def create():
                try:
                    conn.execute(
                        "INSERT INTO pilot_stage_gates(gate_id,project_id,scale_stage,process_id,"
                        "evidence_refs_json,status,tech_confirmed_by,tech_confirmed_at,"
                        "producer_confirmed_by,producer_confirmed_at,activated_at,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (gate_id, project_id, scale_stage, process_id, canonical_json(evidence_refs),
                         D.GATE_DRAFT, None, None, None, None, None, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("闸门编号已经存在") from exc
                self._audit(conn, actor_id=actor_id, action="pilot.gate.opened",
                            resource_type="stage_gate", resource_id=gate_id,
                            detail={"project_id": project_id, "scale_stage": scale_stage,
                                    "process_id": process_id, "evidence_refs": evidence_refs,
                                    "specification_hash": process["specification_hash"]})
                return "stage_gate", gate_id, {"gate_id": gate_id, "status": D.GATE_DRAFT}

            return self._idempotent(conn, request_id=request_id, action="pilot.open_gate",
                                    payload=payload, create=create)

    def confirm_gate(self, *, request_id: str, actor_id: str, gate_id: str):
        """技术方与生产方分别签收；双方齐备的瞬间闸门生效，并发下仅一个版本生效。"""
        payload = {"actor_id": actor_id, "gate_id": gate_id}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            gate = conn.execute("SELECT * FROM pilot_stage_gates WHERE gate_id=?", (gate_id,)).fetchone()
            if gate is None:
                raise NotFoundError("闸门不存在")
            project = self._project(conn, gate["project_id"])
            self._require_active(project)
            party = self._party(actor, project)
            if party not in (D.PARTY_TECH, D.PARTY_PRODUCER, "admin"):
                raise PermissionDenied("只有技术方与生产方可以签收闸门")
            if gate["status"] in (D.GATE_ACTIVE, D.GATE_REJECTED, D.GATE_CLOSED):
                raise ConflictError(f"闸门当前状态为 {gate['status']}，不能再签收")

            if party == D.PARTY_TECH:
                if gate["tech_confirmed_by"]:
                    raise ConflictError("技术方已经签收，不能重复签收")
                tech_who, tech_when = actor_id, self._now()
                producer_who = gate["producer_confirmed_by"]
                producer_when = gate["producer_confirmed_at"]
            elif party == D.PARTY_PRODUCER:
                if gate["producer_confirmed_by"]:
                    raise ConflictError("生产方已经签收，不能重复签收")
                producer_who, producer_when = actor_id, self._now()
                tech_who = gate["tech_confirmed_by"]
                tech_when = gate["tech_confirmed_at"]
            else:  # admin 不构成职责分离中的任何一方
                raise PermissionDenied("管理员不能代替技术方或生产方签收")

            will_activate = bool(tech_who and producer_who)
            new_status = D.GATE_ACTIVE if will_activate else D.GATE_WAITING

            def create():
                activated_at = gate["activated_at"]
                if will_activate:
                    activated_at = self._now()
                try:
                    conn.execute(
                        "UPDATE pilot_stage_gates SET status=?, tech_confirmed_by=?, tech_confirmed_at=?, "
                        "producer_confirmed_by=?, producer_confirmed_at=?, activated_at=? WHERE gate_id=?",
                        (new_status, tech_who, tech_when, producer_who, producer_when,
                         activated_at, gate_id),
                    )
                except Exception as exc:
                    raise ConflictError("该阶段已有生效闸门，并发签收仅一个版本生效") from exc
                self._audit(conn, actor_id=actor_id,
                            action="pilot.gate.activated" if will_activate else "pilot.gate.confirmed",
                            resource_type="stage_gate", resource_id=gate_id,
                            detail={"project_id": gate["project_id"], "scale_stage": gate["scale_stage"],
                                    "process_id": gate["process_id"], "party": party,
                                    "activated": will_activate})
                response = {"gate_id": gate_id, "status": new_status}
                if will_activate:
                    # 同一项目同一时刻只有一个生效工艺版本。
                    conn.execute(
                        "UPDATE pilot_process_versions SET status=? WHERE project_id=? AND status=? "
                        "AND process_id<>?",
                        (D.PROCESS_SUPERSEDED, gate["project_id"], D.PROCESS_ACTIVE, gate["process_id"]),
                    )
                    conn.execute("UPDATE pilot_process_versions SET status=? WHERE process_id=?",
                                 (D.PROCESS_ACTIVE, gate["process_id"]))
                    response["process_id"] = gate["process_id"]
                    response["payment_obligation"] = self._create_stage_payment(
                        conn, project, gate["process_id"], gate["scale_stage"], gate_id, actor_id)
                return "stage_gate", gate_id, response

            return self._idempotent(conn, request_id=request_id, action="pilot.confirm_gate",
                                    payload=payload, create=create)

    def _create_stage_payment(self, conn, project_row, process_id: str, stage: str,
                              gate_id: str, actor_id: str) -> dict[str, Any] | None:
        """闸门生效时按协议付款条款生成阶段付款义务（待支付台账）。"""
        agreement = self._latest_agreement(conn, project_row["project_id"])
        terms = json.loads(agreement["payment_terms_json"])
        amounts = terms.get("stage_amounts", {})
        if stage not in amounts:
            return None
        amount = float(amounts[stage])
        currency = str(terms.get("currency", "CNY"))
        entry_id = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO pilot_ledger(entry_id,project_id,batch_id,stage,entry_type,amount,currency,"
            "direction,status,ref_type,ref_id,detail_json,created_at,settled_by_entry_id) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (entry_id, project_row["project_id"], None, stage, D.LEDGER_PAYMENT, amount, currency,
             D.ENTRY_DIRECTION_INBOUND, D.LEDGER_PENDING, "stage_gate", gate_id,
             canonical_json({"agreement_id": agreement["agreement_id"], "agreement_version":
                             agreement["version"], "process_id": process_id}),
             self._now(), None),
        )
        self._audit(conn, actor_id=actor_id, action="pilot.payment.obligated",
                    resource_type="ledger_entry", resource_id=entry_id,
                    detail={"project_id": project_row["project_id"], "stage": stage,
                            "amount": amount, "currency": currency, "gate_id": gate_id})
        return {"entry_id": entry_id, "amount": amount, "currency": currency, "status": D.LEDGER_PENDING}

    # ------------------------------------------------------------------
    # 试生产批次
    # ------------------------------------------------------------------
    def _resolve_material_id(self, conn, project_id: str, material_key: str, at: str) -> str:
        replacement = conn.execute(
            "SELECT new_material_id FROM pilot_supplier_replacements WHERE project_id=? AND material_key=? "
            "AND effective_from<=? ORDER BY effective_from DESC, created_at DESC LIMIT 1",
            (project_id, material_key, at),
        ).fetchone()
        if replacement:
            return replacement["new_material_id"]
        row = conn.execute(
            "SELECT material_id FROM pilot_materials WHERE project_id=? AND material_key=? "
            "ORDER BY created_at DESC LIMIT 1", (project_id, material_key)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"物料 {material_key} 没有可用来源")
        return row["material_id"]

    def open_batch(self, *, request_id: str, actor_id: str, project_id: str, batch_id: str,
                   scale_stage: str, gate_id: str | None = None, equipment_id: str | None = None):
        payload = {"actor_id": actor_id, "project_id": project_id, "batch_id": batch_id,
                   "scale_stage": scale_stage, "gate_id": gate_id, "equipment_id": equipment_id}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            project = self._project(conn, project_id)
            self._require_active(project)
            if self._party(actor, project) not in ("producer", "admin"):
                raise PermissionDenied("试生产批次由生产方开批")
            if scale_stage not in D.STAGE_ORDER:
                raise ValidationError("scale_stage 非法")
            if gate_id is None:
                gate_row = conn.execute(
                    "SELECT * FROM pilot_stage_gates WHERE project_id=? AND scale_stage=? AND status=?",
                    (project_id, scale_stage, D.GATE_ACTIVE),
                ).fetchone()
                if gate_row is None:
                    raise NotFoundError("该阶段尚无生效闸门，不能开批")
            else:
                gate_row = conn.execute("SELECT * FROM pilot_stage_gates WHERE gate_id=?",
                                        (gate_id,)).fetchone()
                if gate_row is None:
                    raise NotFoundError("闸门不存在")
                if gate_row["project_id"] != project_id or gate_row["scale_stage"] != scale_stage or \
                        gate_row["status"] != D.GATE_ACTIVE:
                    raise ConflictError("只能依据本阶段已生效闸门开批")
            gate_id = gate_row["gate_id"]
            process = self._process(conn, gate_row["process_id"])
            if equipment_id is None:
                equipment_rows = conn.execute(
                    "SELECT * FROM pilot_equipment WHERE project_id=? AND scale_stage=?",
                    (project_id, scale_stage),
                ).fetchall()
                if len(equipment_rows) != 1:
                    raise ValidationError("存在多台或零台设备，必须显式指定 equipment_id")
                equipment_row = equipment_rows[0]
            else:
                equipment_row = conn.execute(
                    "SELECT * FROM pilot_equipment WHERE equipment_id=?", (equipment_id,)
                ).fetchone()
                if equipment_row is None:
                    raise NotFoundError("设备不存在")
                if equipment_row["project_id"] != project_id or \
                        equipment_row["scale_stage"] != scale_stage:
                    raise ValidationError("设备与项目或放大阶段不匹配")
            opened_at = self._now()
            bindings: dict[str, str] = {}
            for key in json.loads(process["requires_materials_json"]):
                material_id = self._resolve_material_id(conn, project_id, key, opened_at)
                status = conn.execute("SELECT status FROM pilot_materials WHERE material_id=?",
                                      (material_id,)).fetchone()["status"]
                if status != D.MATERIAL_ACTIVE:
                    raise ConflictError(f"物料 {key} 当前来源无效，不能开批")
                bindings[key] = material_id

            def create():
                try:
                    conn.execute(
                        "INSERT INTO pilot_batches(batch_id,project_id,scale_stage,gate_id,process_id,"
                        "equipment_id,material_bindings_json,status,opened_at,closed_at,recalled_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (batch_id, project_id, scale_stage, gate_id, process["process_id"],
                         equipment_row["equipment_id"], canonical_json(bindings), D.BATCH_OPEN,
                         opened_at, None, None),
                    )
                except Exception as exc:
                    raise ConflictError("批次编号已经存在") from exc
                self._audit(conn, actor_id=actor_id, action="pilot.batch.opened",
                            resource_type="batch", resource_id=batch_id,
                            detail={"project_id": project_id, "scale_stage": scale_stage,
                                    "gate_id": gate_id, "process_id": process["process_id"],
                                    "equipment_id": equipment_row["equipment_id"],
                                    "material_bindings": bindings})
                return "batch", batch_id, {"batch_id": batch_id, "status": D.BATCH_OPEN,
                                           "gate_id": gate_id, "process_id": process["process_id"],
                                           "material_bindings": bindings}

            return self._idempotent(conn, request_id=request_id, action="pilot.open_batch",
                                    payload=payload, create=create)

    def record_inspection(self, *, request_id: str, actor_id: str, batch_id: str,
                          callback_id: str, metric_values: dict[str, float]):
        """检测回调：相同 callback_id 重放不推进状态；按工艺质量窗口判定通过/部分/失败。"""
        payload = {"actor_id": actor_id, "batch_id": batch_id, "callback_id": callback_id,
                   "metric_values": metric_values}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            batch = conn.execute("SELECT * FROM pilot_batches WHERE batch_id=?", (batch_id,)).fetchone()
            if batch is None:
                raise NotFoundError("批次不存在")
            project = self._project(conn, batch["project_id"])
            self._party(actor, project)
            callback_id = self._id(callback_id, "callback_id")
            if not isinstance(metric_values, dict) or not metric_values:
                raise ValidationError("metric_values 必须是非空指标对象")
            values = {str(k): float(v) for k, v in metric_values.items()
                      if isinstance(v, (int, float))}
            if len(values) != len(metric_values):
                raise ValidationError("metric_values 只能包含数值")
            process = self._process(conn, batch["process_id"])
            windows = json.loads(process["quality_metrics_json"])
            missing = [name for name in windows if name not in values]
            if missing:
                raise ValidationError(f"缺少质量指标检测值: {','.join(sorted(missing))}")

            existing = conn.execute("SELECT * FROM pilot_inspections WHERE callback_id=?",
                                    (callback_id,)).fetchone()
            if existing:
                if existing["batch_id"] != batch_id:
                    raise ConflictError("回调编号已用于其他批次")
                if existing["metric_values_json"] != canonical_json(values):
                    raise ConflictError("相同回调编号携带了不同检测值，拒绝重复推进")
                from science_strategy_foundation.models import WriteReceipt
                return WriteReceipt(request_id, "inspection", existing["result_id"], True)

            if batch["status"] != D.BATCH_OPEN:
                raise ConflictError(f"批次状态为 {batch['status']}，检测已经闭环")

            failing: list[str] = []
            for name, bounds in windows.items():
                if not (bounds["min"] <= values[name] <= bounds["max"]):
                    failing.append(name)
            total = len(windows)
            if not failing:
                verdict = D.VERDICT_PASS
            elif len(failing) == total:
                verdict = D.VERDICT_FAIL
            else:
                verdict = D.VERDICT_PARTIAL

            def create():
                result_id = uuid.uuid4().hex
                conn.execute(
                    "INSERT INTO pilot_inspections(result_id,batch_id,callback_id,metric_values_json,"
                    "overall_verdict,partial_metrics_json,recorded_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (result_id, batch_id, callback_id, canonical_json(values), verdict,
                     canonical_json(sorted(failing)), actor_id, self._now()),
                )
                new_status = {D.VERDICT_PASS: D.BATCH_ACCEPTED,
                              D.VERDICT_PARTIAL: D.BATCH_PARTIAL,
                              D.VERDICT_FAIL: D.BATCH_REJECTED}[verdict]
                conn.execute("UPDATE pilot_batches SET status=? WHERE batch_id=?", (new_status, batch_id))
                for name in failing:
                    deviation_id = uuid.uuid4().hex
                    conn.execute(
                        "INSERT INTO pilot_deviations(deviation_id,batch_id,project_id,kind,metric,"
                        "observed_value,window_json,description,owner_party,disposition,"
                        "disposition_detail_json,refund_entry_id,status,created_at,decided_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (deviation_id, batch_id, batch["project_id"], "quality", name, values[name],
                         canonical_json(windows[name]),
                         f"指标 {name} 检测值 {values[name]} 超出窗口 "
                         f"[{windows[name]['min']},{windows[name]['max']}]",
                         None, None, None, None, D.DEVIATION_OPEN, self._now(), None),
                    )
                self._audit(conn, actor_id=actor_id, action="pilot.inspection.recorded",
                            resource_type="inspection", resource_id=result_id,
                            detail={"batch_id": batch_id, "callback_id": callback_id,
                                    "verdict": verdict, "failing_metrics": sorted(failing),
                                    "batch_status": new_status})
                return "inspection", result_id, {"result_id": result_id, "verdict": verdict,
                                                 "failing_metrics": sorted(failing),
                                                 "batch_status": new_status, "replayed": False}

            return self._idempotent(conn, request_id=request_id, action="pilot.record_inspection",
                                    payload=payload, create=create)

    # ------------------------------------------------------------------
    # 偏差处置
    # ------------------------------------------------------------------
    def decide_deviation(self, *, request_id: str, actor_id: str, deviation_id: str,
                         owner_party: str, disposition: str,
                         tech_share: float = 0.5, amount: float = 0.0, note: str = ""):
        """偏差责任由成果转化中心裁决；技术方承担份额生成返还义务，历史检测事实不变。"""
        payload = {"actor_id": actor_id, "deviation_id": deviation_id, "owner_party": owner_party,
                   "disposition": disposition, "tech_share": tech_share, "amount": amount, "note": note}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            row = conn.execute("SELECT * FROM pilot_deviations WHERE deviation_id=?",
                               (deviation_id,)).fetchone()
            if row is None:
                raise NotFoundError("偏差不存在")
            project = self._project(conn, row["project_id"])
            if self._party(actor, project) not in ("center", "admin"):
                raise PermissionDenied("偏差责任由成果转化中心裁决")
            if row["status"] != D.DEVIATION_OPEN:
                raise ConflictError("偏差已经裁决，不能改写结论")
            if owner_party not in (D.PARTY_TECH, D.PARTY_PRODUCER):
                raise ValidationError("责任方必须是 tech 或 producer")
            allowed = (D.DISPOSITION_TECH, D.DISPOSITION_PRODUCER, D.DISPOSITION_SHARED,
                       D.DISPOSITION_SCRAP, D.DISPOSITION_CONCESSION)
            if disposition not in allowed:
                raise ValidationError("处置方式非法")
            if not 0.0 <= tech_share <= 1.0:
                raise ValidationError("tech_share 必须在 0 到 1 之间")
            if amount < 0:
                raise ValidationError("amount 不能为负")
            currency = "CNY"
            # 技术方承担 → 全额返还；双方分担 → 按技术方份额返还；生产方承担 → 无返还。
            if owner_party == D.PARTY_TECH:
                refund_amount = float(amount)
            elif disposition == D.DISPOSITION_SHARED:
                refund_amount = float(amount) * tech_share
            else:
                refund_amount = 0.0
            refund_entry_id = None

            def create():
                nonlocal refund_entry_id
                if refund_amount > 0:
                    refund_entry_id = uuid.uuid4().hex
                    conn.execute(
                        "INSERT INTO pilot_ledger(entry_id,project_id,batch_id,stage,entry_type,amount,"
                        "currency,direction,status,ref_type,ref_id,detail_json,created_at,settled_by_entry_id) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (refund_entry_id, row["project_id"], row["batch_id"], None, D.LEDGER_REFUND,
                         refund_amount, currency, D.ENTRY_DIRECTION_OUTBOUND, D.LEDGER_PENDING,
                         "deviation", deviation_id,
                         canonical_json({"owner_party": owner_party, "disposition": disposition,
                                         "tech_share": tech_share, "note": note}),
                         self._now(), None),
                    )
                detail = {"owner_party": owner_party, "disposition": disposition,
                          "tech_share": tech_share, "amount": amount, "note": note,
                          "refund_entry_id": refund_entry_id}
                conn.execute(
                    "UPDATE pilot_deviations SET owner_party=?, disposition=?, disposition_detail_json=?, "
                    "refund_entry_id=?, status=?, decided_at=? WHERE deviation_id=?",
                    (owner_party, disposition, canonical_json(detail), refund_entry_id,
                     D.DEVIATION_DECIDED, self._now(), deviation_id),
                )
                # 处置结论落到批次状态：让步接收 → 可交付；报废 → 关闭；补救/承担/分担 → 保持在制挂起。
                batch_status_after = None
                pending_others = conn.execute(
                    "SELECT COUNT(*) AS c FROM pilot_deviations WHERE batch_id=? AND status=?",
                    (row["batch_id"], D.DEVIATION_OPEN),
                ).fetchone()["c"]
                if not pending_others:
                    if disposition == D.DISPOSITION_CONCESSION:
                        conn.execute("UPDATE pilot_batches SET status=? WHERE batch_id=?",
                                     (D.BATCH_ACCEPTED, row["batch_id"]))
                        batch_status_after = D.BATCH_ACCEPTED
                    elif disposition == D.DISPOSITION_SCRAP:
                        conn.execute("UPDATE pilot_batches SET status=?, closed_at=? WHERE batch_id=?",
                                     (D.BATCH_REJECTED, self._now(), row["batch_id"]))
                        batch_status_after = D.BATCH_REJECTED
                self._audit(conn, actor_id=actor_id, action="pilot.deviation.decided",
                            resource_type="deviation", resource_id=deviation_id,
                            detail={"project_id": row["project_id"], "batch_id": row["batch_id"],
                                    "metric": row["metric"], "batch_status_after": batch_status_after,
                                    **detail})
                return "deviation", deviation_id, {"deviation_id": deviation_id,
                                                   "owner_party": owner_party,
                                                   "disposition": disposition,
                                                   "refund_entry_id": refund_entry_id}

            return self._idempotent(conn, request_id=request_id, action="pilot.decide_deviation",
                                    payload=payload, create=create)

    # ------------------------------------------------------------------
    # 交付、召回、付款、终止
    # ------------------------------------------------------------------
    def deliver_batch(self, *, request_id: str, actor_id: str, batch_id: str,
                      quantity: float, unit: str):
        payload = {"actor_id": actor_id, "batch_id": batch_id, "quantity": quantity, "unit": unit}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            batch = conn.execute("SELECT * FROM pilot_batches WHERE batch_id=?", (batch_id,)).fetchone()
            if batch is None:
                raise NotFoundError("批次不存在")
            project = self._project(conn, batch["project_id"])
            self._require_active(project)
            if self._party(actor, project) not in ("producer", "admin"):
                raise PermissionDenied("交付由生产方执行")
            if batch["status"] != D.BATCH_ACCEPTED:
                raise ConflictError(f"批次状态为 {batch['status']}，只有全部达标批次可以交付")
            if not isinstance(quantity, (int, float)) or quantity <= 0:
                raise ValidationError("quantity 必须为正数")
            unit = self._text(unit, "unit", 20)

            def create():
                delivery_id = uuid.uuid4().hex
                delivered_at = self._now()
                conn.execute(
                    "INSERT INTO pilot_deliveries(delivery_id,batch_id,quantity,unit,delivered_by,delivered_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (delivery_id, batch_id, float(quantity), unit, actor_id, delivered_at),
                )
                conn.execute("UPDATE pilot_batches SET status=?, closed_at=? WHERE batch_id=?",
                             (D.BATCH_DELIVERED, delivered_at, batch_id))
                self._audit(conn, actor_id=actor_id, action="pilot.batch.delivered",
                            resource_type="delivery", resource_id=delivery_id,
                            detail={"batch_id": batch_id, "quantity": quantity, "unit": unit,
                                    "delivered_at": delivered_at})
                return "delivery", delivery_id, {"delivery_id": delivery_id, "batch_id": batch_id,
                                                 "quantity": quantity, "unit": unit}

            return self._idempotent(conn, request_id=request_id, action="pilot.deliver_batch",
                                    payload=payload, create=create)

    def recall_batch(self, *, request_id: str, actor_id: str, batch_id: str, reason: str,
                     refund_amount: float = 0.0):
        """召回插入新记录：批次转入召回并生成返还义务，已交付数量与付款事实不被改写。"""
        payload = {"actor_id": actor_id, "batch_id": batch_id, "reason": reason,
                   "refund_amount": refund_amount}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            batch = conn.execute("SELECT * FROM pilot_batches WHERE batch_id=?", (batch_id,)).fetchone()
            if batch is None:
                raise NotFoundError("批次不存在")
            project = self._project(conn, batch["project_id"])
            if self._party(actor, project) not in ("tech", "producer", "center", "admin"):
                raise PermissionDenied("项目各方均可发起召回")
            if batch["status"] not in (D.BATCH_ACCEPTED, D.BATCH_DELIVERED):
                raise ConflictError(f"批次状态为 {batch['status']}，不在可召回范围")
            if refund_amount < 0:
                raise ValidationError("refund_amount 不能为负")
            reason = self._text(reason, "reason")

            def create():
                recall_id = uuid.uuid4().hex
                recalled_at = self._now()
                refund_entry_id = None
                if refund_amount > 0:
                    refund_entry_id = uuid.uuid4().hex
                    conn.execute(
                        "INSERT INTO pilot_ledger(entry_id,project_id,batch_id,stage,entry_type,amount,"
                        "currency,direction,status,ref_type,ref_id,detail_json,created_at,settled_by_entry_id) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (refund_entry_id, batch["project_id"], batch_id, None, D.LEDGER_REFUND,
                         float(refund_amount), "CNY", D.ENTRY_DIRECTION_OUTBOUND, D.LEDGER_PENDING,
                         "recall", recall_id, canonical_json({"reason": reason}),
                         recalled_at, None),
                    )
                conn.execute(
                    "INSERT INTO pilot_recalls(recall_id,batch_id,project_id,reason,requested_by,"
                    "refund_entry_id,created_at) VALUES(?,?,?,?,?,?,?)",
                    (recall_id, batch_id, batch["project_id"], reason, actor_id,
                     refund_entry_id, recalled_at),
                )
                conn.execute("UPDATE pilot_batches SET status=?, recalled_at=? WHERE batch_id=?",
                             (D.BATCH_RECALLED, recalled_at, batch_id))
                self._audit(conn, actor_id=actor_id, action="pilot.batch.recalled",
                            resource_type="recall", resource_id=recall_id,
                            detail={"batch_id": batch_id, "reason": reason,
                                    "refund_entry_id": refund_entry_id,
                                    "delivery_preserved": True})
                return "recall", recall_id, {"recall_id": recall_id, "batch_id": batch_id,
                                             "refund_entry_id": refund_entry_id}

            return self._idempotent(conn, request_id=request_id, action="pilot.recall_batch",
                                    payload=payload, create=create)

    def settle_payment(self, *, request_id: str, actor_id: str, entry_id: str):
        """登记一笔义务的支付事实；支付后不可改写，也不能重复支付。"""
        payload = {"actor_id": actor_id, "entry_id": entry_id}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            row = conn.execute("SELECT * FROM pilot_ledger WHERE entry_id=?", (entry_id,)).fetchone()
            if row is None:
                raise NotFoundError("台账条目不存在")
            project = self._project(conn, row["project_id"])
            party = self._party(actor, project)
            if row["direction"] == D.ENTRY_DIRECTION_INBOUND and party not in ("producer", "admin"):
                raise PermissionDenied("阶段付款由生产方支付")
            if row["direction"] == D.ENTRY_DIRECTION_OUTBOUND and party not in ("tech", "admin"):
                raise PermissionDenied("返还款由技术方支付")
            if row["status"] == D.LEDGER_PAID:
                raise ConflictError("该条目已经支付，不能重复支付")
            if row["status"] == LEDGER_SETTLED:
                raise ConflictError("该条目已被终止结算结清")

            def create():
                conn.execute("UPDATE pilot_ledger SET status=? WHERE entry_id=?",
                             (D.LEDGER_PAID, entry_id))
                self._audit(conn, actor_id=actor_id, action="pilot.ledger.paid",
                            resource_type="ledger_entry", resource_id=entry_id,
                            detail={"project_id": row["project_id"], "entry_type": row["entry_type"],
                                    "amount": row["amount"], "currency": row["currency"],
                                    "direction": row["direction"]})
                return "ledger_entry", entry_id, {"entry_id": entry_id, "status": D.LEDGER_PAID}

            return self._idempotent(conn, request_id=request_id, action="pilot.settle_payment",
                                    payload=payload, create=create)

    def terminate_agreement(self, *, request_id: str, actor_id: str, project_id: str, reason: str):
        """协议终止：用一笔结算记录轧差全部未完成义务；已支付与已交付事实保持不变。"""
        payload = {"actor_id": actor_id, "project_id": project_id, "reason": reason}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            project = self._project(conn, project_id)
            if project["status"] != D.PROJECT_ACTIVE:
                raise ConflictError("项目已经终止")
            if self._party(actor, project) not in ("tech", "producer", "center", "admin"):
                raise PermissionDenied("项目各方可以发起终止")
            agreement = self._latest_agreement(conn, project_id)
            reason = self._text(reason, "reason")
            pending = conn.execute(
                "SELECT * FROM pilot_ledger WHERE project_id=? AND status=?",
                (project_id, D.LEDGER_PENDING),
            ).fetchall()
            inbound = sum(e["amount"] for e in pending if e["direction"] == D.ENTRY_DIRECTION_INBOUND)
            outbound = sum(e["amount"] for e in pending if e["direction"] == D.ENTRY_DIRECTION_OUTBOUND)
            net = round(inbound - outbound, 2)

            def create():
                now = self._now()
                settlement_id = uuid.uuid4().hex
                closed_ids = [e["entry_id"] for e in pending]
                if net >= 0:
                    direction, amount = D.ENTRY_DIRECTION_INBOUND, net
                else:
                    direction, amount = D.ENTRY_DIRECTION_OUTBOUND, -net
                # 先以新结算记录结清既有未完成义务，再落结算行本身（保持 pending）。
                if closed_ids:
                    conn.execute(
                        "UPDATE pilot_ledger SET status=?, settled_by_entry_id=? "
                        "WHERE entry_id IN (%s)" % ",".join("?" for _ in closed_ids),
                        [LEDGER_SETTLED, settlement_id, *closed_ids],
                    )
                conn.execute(
                    "INSERT INTO pilot_ledger(entry_id,project_id,batch_id,stage,entry_type,amount,currency,"
                    "direction,status,ref_type,ref_id,detail_json,created_at,settled_by_entry_id) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (settlement_id, project_id, None, None, D.LEDGER_SETTLEMENT, float(amount), "CNY",
                     direction, D.LEDGER_PENDING, "termination", None,
                     canonical_json({"inbound_total": inbound, "outbound_total": outbound,
                                     "net": net, "closed_entries": closed_ids,
                                     "reason": reason}),
                     now, None),
                )
                termination_id = uuid.uuid4().hex
                conn.execute(
                    "INSERT INTO pilot_terminations(termination_id,project_id,agreement_id,reason,"
                    "requested_by,settlement_entry_id,created_at) VALUES(?,?,?,?,?,?,?)",
                    (termination_id, project_id, agreement["agreement_id"], reason, actor_id,
                     settlement_id, now),
                )
                conn.execute("UPDATE pilot_projects SET status=? WHERE project_id=?",
                             (D.PROJECT_TERMINATED, project_id))
                conn.execute("UPDATE pilot_agreements SET status=? WHERE agreement_id=?",
                             (D.AGREEMENT_TERMINATED, agreement["agreement_id"]))
                self._audit(conn, actor_id=actor_id, action="pilot.agreement.terminated",
                            resource_type="termination", resource_id=termination_id,
                            detail={"project_id": project_id, "agreement_id": agreement["agreement_id"],
                                    "settlement_entry_id": settlement_id, "net": net,
                                    "closed_obligations": len(pending),
                                    "paid_and_delivered_preserved": True})
                return "termination", termination_id, {
                    "termination_id": termination_id, "settlement_entry_id": settlement_id,
                    "net": net, "direction": direction, "amount": amount,
                    "closed_obligations": len(pending)}

            return self._idempotent(conn, request_id=request_id, action="pilot.terminate_agreement",
                                    payload=payload, create=create)

    # ------------------------------------------------------------------
    # 查询：产品溯源、义务、变更波及
    # ------------------------------------------------------------------
    def _batch_row(self, batch_id: str):
        row = self.database.connection.execute(
            "SELECT * FROM pilot_batches WHERE batch_id=?", (batch_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("批次不存在")
        return row

    def product_provenance(self, batch_id: str) -> dict[str, Any]:
        """回答：这批产品依据哪套工艺与哪份授权生产。"""
        conn = self.database.connection
        batch = self._batch_row(batch_id)
        process = self._process(conn, batch["process_id"])
        gate = conn.execute("SELECT * FROM pilot_stage_gates WHERE gate_id=?",
                            (batch["gate_id"],)).fetchone()
        agreement = self._latest_agreement(conn, batch["project_id"])
        equipment = conn.execute("SELECT * FROM pilot_equipment WHERE equipment_id=?",
                                 (batch["equipment_id"],)).fetchone()
        materials = []
        for key, material_id in json.loads(batch["material_bindings_json"]).items():
            material = conn.execute("SELECT * FROM pilot_materials WHERE material_id=?",
                                    (material_id,)).fetchone()
            replacements = conn.execute(
                "SELECT replacement_id, old_material_id, effective_from, reason "
                "FROM pilot_supplier_replacements WHERE project_id=? AND material_key=? "
                "ORDER BY effective_from",
                (batch["project_id"], key),
            ).fetchall()
            materials.append({"material_key": key, "material_id": material_id,
                              "name": material["name"], "supplier_org_id": material["supplier_org_id"],
                              "supplier_batch_no": material["supplier_batch_no"],
                              "replacements": [dict(r) for r in replacements]})
        inspections = conn.execute(
            "SELECT callback_id, overall_verdict, partial_metrics_json, created_at "
            "FROM pilot_inspections WHERE batch_id=? ORDER BY created_at", (batch_id,)
        ).fetchall()
        delivery = conn.execute("SELECT * FROM pilot_deliveries WHERE batch_id=?",
                                (batch_id,)).fetchone()
        recall = conn.execute("SELECT * FROM pilot_recalls WHERE batch_id=?",
                              (batch_id,)).fetchone()
        return {
            "batch_id": batch_id,
            "project_id": batch["project_id"],
            "scale_stage": batch["scale_stage"],
            "status": batch["status"],
            "opened_at": batch["opened_at"],
            "process": {"process_id": process["process_id"], "version": process["version"],
                        "scale_stage": process["scale_stage"],
                        "specification_hash": process["specification_hash"],
                        "status": process["status"], "frozen_at": process["frozen_at"]},
            "authorization": {"agreement_id": agreement["agreement_id"],
                              "agreement_version": agreement["version"],
                              "ip_ownership": agreement["ip_ownership"],
                              "license_scope": json.loads(agreement["license_scope_json"]),
                              "gate_id": gate["gate_id"], "gate_status": gate["status"],
                              "tech_confirmed_by": gate["tech_confirmed_by"],
                              "producer_confirmed_by": gate["producer_confirmed_by"],
                              "activated_at": gate["activated_at"]},
            "equipment": {"equipment_id": equipment["equipment_id"], "name": equipment["name"],
                          "site_id": equipment["site_id"]},
            "materials": materials,
            "inspections": [{"callback_id": r["callback_id"], "verdict": r["overall_verdict"],
                             "partial_metrics": json.loads(r["partial_metrics_json"]),
                             "created_at": r["created_at"]} for r in inspections],
            "delivery": None if delivery is None else
                {"delivery_id": delivery["delivery_id"], "quantity": delivery["quantity"],
                 "unit": delivery["unit"], "delivered_at": delivery["delivered_at"]},
            "recall": None if recall is None else
                {"recall_id": recall["recall_id"], "reason": recall["reason"],
                 "created_at": recall["created_at"]},
        }

    def batch_deviations(self, batch_id: str) -> dict[str, Any]:
        """回答：偏差由谁处置、结论是什么、关联了多少返还义务。"""
        conn = self.database.connection
        batch = self._batch_row(batch_id)
        rows = conn.execute("SELECT * FROM pilot_deviations WHERE batch_id=? ORDER BY created_at",
                            (batch_id,)).fetchall()
        items = []
        for row in rows:
            refund = None
            if row["refund_entry_id"]:
                ledger = conn.execute("SELECT amount,currency,status FROM pilot_ledger WHERE entry_id=?",
                                      (row["refund_entry_id"],)).fetchone()
                refund = {"entry_id": row["refund_entry_id"], "amount": ledger["amount"],
                          "currency": ledger["currency"], "status": ledger["status"]}
            items.append({"deviation_id": row["deviation_id"], "kind": row["kind"],
                          "metric": row["metric"], "observed_value": row["observed_value"],
                          "window": json.loads(row["window_json"]) if row["window_json"] else None,
                          "description": row["description"], "owner_party": row["owner_party"],
                          "disposition": row["disposition"], "status": row["status"],
                          "created_at": row["created_at"], "decided_at": row["decided_at"],
                          "refund": refund})
        return {"batch_id": batch_id, "project_id": batch["project_id"],
                "batch_status": batch["status"], "deviations": items}

    def outstanding_obligations(self, project_id: str) -> dict[str, Any]:
        """回答：尚有哪些付款或返还义务（已支付/已交付事实不再出现为未完成项）。"""
        conn = self.database.connection
        project = self._project(conn, project_id)
        rows = conn.execute(
            "SELECT * FROM pilot_ledger WHERE project_id=? AND status=? ORDER BY created_at",
            (project_id, D.LEDGER_PENDING),
        ).fetchall()
        pending = [{"entry_id": r["entry_id"], "entry_type": r["entry_type"],
                    "amount": r["amount"], "currency": r["currency"], "direction": r["direction"],
                    "stage": r["stage"], "batch_id": r["batch_id"], "ref_type": r["ref_type"],
                    "ref_id": r["ref_id"], "detail": json.loads(r["detail_json"]),
                    "created_at": r["created_at"]} for r in rows]
        open_devs = conn.execute(
            "SELECT deviation_id,batch_id,metric,description,created_at FROM pilot_deviations "
            "WHERE project_id=? AND status=? ORDER BY created_at",
            (project_id, D.DEVIATION_OPEN),
        ).fetchall()
        recalls = conn.execute(
            "SELECT r.recall_id,r.batch_id,r.reason,r.created_at,r.refund_entry_id FROM pilot_recalls r "
            "WHERE r.project_id=? ORDER BY r.created_at", (project_id,)
        ).fetchall()
        paid_total = conn.execute(
            "SELECT COALESCE(SUM(amount),0) AS total FROM pilot_ledger WHERE project_id=? AND status=?",
            (project_id, D.LEDGER_PAID),
        ).fetchone()["total"]
        delivered = conn.execute(
            "SELECT batch_id,closed_at FROM pilot_batches WHERE project_id=? AND status=?",
            (project_id, D.BATCH_DELIVERED),
        ).fetchall()
        return {"project_id": project_id, "project_status": project["status"],
                "pending_obligations": pending,
                "open_deviations": [dict(r) for r in open_devs],
                "recalls": [dict(r) for r in recalls],
                "paid_total": paid_total,
                "delivered_batches": [dict(r) for r in delivered]}

    def change_impact(self, project_id: str, *, process_id: str | None = None,
                      material_id: str | None = None, replacement_id: str | None = None,
                      in_progress_only: bool = False) -> dict[str, Any]:
        """追踪一次工艺版本或供应商替换变更波及的全部在制批次。

        供应商替换的波及面包含两类批次：
        - 生效前开批、仍绑定旧来源的批次（需要处置或重新确认）；
        - 生效后开批、绑定新来源的批次（在变更状态下生产，需重点跟踪）。
        """
        conn = self.database.connection
        self._project(conn, project_id)
        batches = conn.execute(
            "SELECT * FROM pilot_batches WHERE project_id=? ORDER BY opened_at", (project_id,)
        ).fetchall()

        old_material_id = material_id
        new_material_id = None
        effective_from = None
        if replacement_id:
            rep = conn.execute("SELECT * FROM pilot_supplier_replacements WHERE replacement_id=?",
                               (replacement_id,)).fetchone()
            if rep is None:
                raise NotFoundError("替换记录不存在")
            if rep["project_id"] != project_id:
                raise ValidationError("替换记录不属于该项目")
            old_material_id = rep["old_material_id"]
            new_material_id = rep["new_material_id"]
            effective_from = rep["effective_from"]

        def binding_of(batch, mid: str) -> bool:
            return mid in json.loads(batch["material_bindings_json"]).values()

        items = []
        for batch in batches:
            reasons = []
            if process_id and batch["process_id"] == process_id:
                reasons.append("uses_process")
            if not replacement_id and old_material_id and binding_of(batch, old_material_id):
                reasons.append("uses_material")
            if replacement_id:
                if binding_of(batch, old_material_id) and \
                        (effective_from is None or batch["opened_at"] < effective_from):
                    reasons.append("bound_previous_source")
                if new_material_id and binding_of(batch, new_material_id) and \
                        (effective_from is None or batch["opened_at"] >= effective_from):
                    reasons.append("bound_replacement_source")
            if not reasons:
                continue
            in_progress = batch["status"] in IN_PROGRESS_STATUSES
            if in_progress_only and not in_progress:
                continue
            items.append({"batch_id": batch["batch_id"], "scale_stage": batch["scale_stage"],
                          "status": batch["status"], "process_id": batch["process_id"],
                          "material_bindings": json.loads(batch["material_bindings_json"]),
                          "in_progress": in_progress, "opened_at": batch["opened_at"],
                          "reasons": reasons})
        return {"project_id": project_id, "process_id": process_id,
                "material_id": old_material_id, "replacement_id": replacement_id,
                "affected_batches": items,
                "in_progress_batch_ids": [b["batch_id"] for b in items if b["in_progress"]]}

    def in_progress_batches(self, project_id: str) -> list[dict[str, Any]]:
        rows = self.database.connection.execute(
            "SELECT batch_id,scale_stage,status,process_id,opened_at FROM pilot_batches "
            "WHERE project_id=? AND status IN (?,?,?) ORDER BY opened_at",
            (project_id, *IN_PROGRESS_STATUSES),
        ).fetchall()
        return [dict(r) for r in rows]
