"""中试转化治理服务。

在基础 DomainService 之上实现：

* 协议与知识产权权属、许可范围登记；
* 工艺版本起草、放大前冻结（可执行工艺 + 前置证据齐备）；
* 技术方/生产方职责分离的签收门，双方确认才进入下一阶段；
* 原料谱系（停产/替代）、试生产批次、检测质量回调；
* 相同检测回调幂等，不重复推进批次状态；
* 偏差按类别确定责任方并结清为义务；
* 阶段付款、退款与返还义务台账；协议终止以新记录结清未完成义务；
* 产品血缘、偏差处置、付款/返还余额与变更波及批次的查询。

所有事实只追加：结清义务通过写入结算引用完成，已交付批次与已支付
付款永不被改写。
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from ..audit import append_event, canonical_json, digest
from ..errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from ..models import Actor
from ..storage import Database
from . import domain as D
from .storage import ensure_pilot_schema


class PilotService:
    """协调中试转化的权限、阶段门、批次、义务与审计。"""

    def __init__(self, database: Database, clock=None) -> None:
        self.database = database
        self.clock = clock
        ensure_pilot_schema(database)

    # ------------------------------------------------------------------ #
    # 内部工具
    # ------------------------------------------------------------------ #
    def _now(self) -> str:
        if self.clock is None:
            from ..clock import SystemClock

            return SystemClock().now().isoformat().replace("+00:00", "Z")
        return self.clock.now().isoformat().replace("+00:00", "Z")

    def _actor(self, connection, actor_id: str) -> Actor:
        row = connection.execute("SELECT * FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
        if row is None:
            raise NotFoundError("操作者不存在")
        actor = Actor(row["actor_id"], row["display_name"], row["role"],
                      row["organization_id"], bool(row["active"]))
        if not actor.active:
            raise PermissionDenied("操作者已停用")
        return actor

    def _id(self, value: str, field: str) -> str:
        value = str(value).strip()
        if not value or len(value) > 80:
            raise ValidationError(f"{field} 不能为空且不能超过 80 个字符")
        return value

    def _text(self, value: str, field: str, limit: int = 500) -> str:
        value = str(value).strip()
        if not value or len(value) > limit:
            raise ValidationError(f"{field} 不能为空且不能超过 {limit} 个字符")
        return value

    def _json_object(self, value: Any, field: str) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ValidationError(f"{field} 必须是对象")
        return value

    def _new_id(self) -> str:
        return uuid.uuid4().hex

    def _audit(self, connection, *, actor_id: str, action: str, resource_type: str,
               resource_id: str, detail: dict[str, Any]) -> None:
        append_event(connection, actor_id=actor_id, action=action, resource_type=resource_type,
                     resource_id=resource_id, detail=detail, occurred_at=self._now())

    def _idempotent(self, connection, *, request_id: str, action: str,
                    payload: dict[str, Any], create) -> dict[str, Any]:
        """复用基础服务的请求回执表实现幂等。"""

        request_id = self._id(request_id, "request_id")
        payload_hash = digest(payload)
        row = connection.execute(
            "SELECT * FROM request_receipts WHERE request_id=?", (request_id,)
        ).fetchone()
        if row:
            if row["action"] != action or row["payload_hash"] != payload_hash:
                raise ConflictError("request_id 已被不同内容使用")
            replay = json.loads(row["response_json"])
            replay["replayed"] = True
            return replay
        result = create()
        connection.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,resource_id,"
            "response_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (request_id, action, payload_hash, result["resource_type"], result["resource_id"],
             canonical_json({k: v for k, v in result.items() if k != "replayed"}), self._now()),
        )
        result["replayed"] = False
        return result

    def _replay_if_seen(self, connection, *, request_id: str, action: str,
                        payload: dict[str, Any]) -> dict[str, Any] | None:
        """状态推进类动作在状态校验前先识别回放，避免重复请求被当成冲突。"""

        try:
            request_id = self._id(request_id, "request_id")
        except ValidationError:
            return None
        row = connection.execute(
            "SELECT * FROM request_receipts WHERE request_id=?", (request_id,)
        ).fetchone()
        if row is None:
            return None
        if row["action"] != action or row["payload_hash"] != digest(payload):
            raise ConflictError("request_id 已被不同内容使用")
        replay = json.loads(row["response_json"])
        replay["replayed"] = True
        return replay

    def _agreement_row(self, connection, agreement_id: str):
        row = connection.execute(
            "SELECT * FROM pilot_agreements WHERE agreement_id=?", (agreement_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("协议不存在")
        return row

    def _require_active(self, agreement) -> None:
        if agreement["status"] != "active":
            raise ConflictError("协议已终止，不能再产生新的交付事实")

    def _party_of(self, actor: Actor, agreement) -> str:
        """根据操作者所属组织判定其在协议中的当事方。"""

        if actor.organization_id == agreement["tech_org_id"]:
            return D.PARTY_TECH
        if actor.organization_id == agreement["production_org_id"]:
            return D.PARTY_PRODUCTION
        if actor.role == "admin":
            return "admin"
        raise PermissionDenied("操作者不属于协议任何一方")

    def _require_party(self, actor: Actor, agreement, party: str) -> str:
        actual = self._party_of(actor, agreement)
        if actual != party and actual != "admin":
            raise PermissionDenied("该动作只能由职责对应的当事方执行")
        return actual

    def _latest_frozen_version(self, connection, agreement_id: str, stage: str):
        return connection.execute(
            "SELECT * FROM pilot_process_versions WHERE agreement_id=? AND stage=? AND status='frozen' "
            "ORDER BY seq DESC LIMIT 1",
            (agreement_id, stage),
        ).fetchone()

    def _gate_for_stage(self, connection, agreement_id: str, stage: str):
        return connection.execute(
            "SELECT * FROM pilot_scale_gates WHERE agreement_id=? AND stage=?",
            (agreement_id, stage),
        ).fetchone()

    # ------------------------------------------------------------------ #
    # 协议与知识产权
    # ------------------------------------------------------------------ #
    def create_agreement(self, *, request_id: str, actor_id: str, agreement_id: str, site_id: str,
                         tech_org_id: str, production_org_id: str, title: str,
                         license_scope: dict[str, Any]) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "agreement_id": agreement_id, "site_id": site_id,
                   "tech_org_id": tech_org_id, "production_org_id": production_org_id,
                   "title": title, "license_scope": license_scope}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            if actor.role not in ("admin", "operator"):
                raise PermissionDenied("当前角色不能创建协议")
            agreement_id = self._id(agreement_id, "agreement_id")
            title = self._text(title, "title")
            license_scope = self._json_object(license_scope, "license_scope")
            for org_id in (tech_org_id, production_org_id):
                if connection.execute("SELECT 1 FROM organizations WHERE organization_id=?",
                                      (org_id,)).fetchone() is None:
                    raise NotFoundError(f"组织不存在: {org_id}")
            if tech_org_id == production_org_id:
                raise ValidationError("技术方与生产方不能是同一组织")
            if connection.execute("SELECT 1 FROM sites WHERE site_id=?", (site_id,)).fetchone() is None:
                raise NotFoundError("场所不存在")

            def create():
                try:
                    connection.execute(
                        "INSERT INTO pilot_agreements(agreement_id,site_id,tech_org_id,production_org_id,"
                        "title,license_scope_json,status,created_by,created_at) VALUES(?,?,?,?,?,?, 'active',?,?)",
                        (agreement_id, site_id, tech_org_id, production_org_id, title,
                         canonical_json(license_scope), actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("协议编号已经存在") from exc
                self._audit(connection, actor_id=actor_id, action="pilot.agreement.created",
                            resource_type="pilot_agreement", resource_id=agreement_id,
                            detail={"tech_org_id": tech_org_id, "production_org_id": production_org_id,
                                    "license_scope": license_scope})
                return {"resource_type": "pilot_agreement", "resource_id": agreement_id,
                        "agreement_id": agreement_id, "status": "active"}

            return self._idempotent(connection, request_id=request_id,
                                    action="pilot.create_agreement", payload=payload, create=create)

    def register_ip_term(self, *, request_id: str, actor_id: str, agreement_id: str, ip_id: str,
                         ownership: str, scope: dict[str, Any]) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "agreement_id": agreement_id, "ip_id": ip_id,
                   "ownership": ownership, "scope": scope}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            agreement = self._agreement_row(connection, agreement_id)
            self._require_active(agreement)
            self._require_party(actor, agreement, D.PARTY_TECH)
            ip_id = self._id(ip_id, "ip_id")
            ownership = self._text(ownership, "ownership", 200)
            scope = self._json_object(scope, "scope")

            def create():
                term_id = self._new_id()
                try:
                    connection.execute(
                        "INSERT INTO pilot_ip_terms(term_id,agreement_id,ip_id,ownership,scope_json,"
                        "created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                        (term_id, agreement_id, ip_id, ownership, canonical_json(scope),
                         actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("该知识产权条款已经登记") from exc
                self._audit(connection, actor_id=actor_id, action="pilot.ip_term.registered",
                            resource_type="pilot_ip_term", resource_id=term_id,
                            detail={"agreement_id": agreement_id, "ip_id": ip_id, "ownership": ownership,
                                    "scope": scope})
                return {"resource_type": "pilot_ip_term", "resource_id": term_id,
                        "term_id": term_id, "agreement_id": agreement_id, "ip_id": ip_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="pilot.register_ip_term", payload=payload, create=create)

    # ------------------------------------------------------------------ #
    # 工艺版本与冻结
    # ------------------------------------------------------------------ #
    def draft_process_version(self, *, request_id: str, actor_id: str, agreement_id: str, stage: str,
                              formulation: dict[str, Any], scale_params: dict[str, Any],
                              scale_tolerance: dict[str, Any], allowed_material_specs: dict[str, Any],
                              equipment_capabilities: dict[str, Any],
                              version_id: str | None = None) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "agreement_id": agreement_id, "stage": stage,
                   "formulation": formulation, "scale_params": scale_params,
                   "scale_tolerance": scale_tolerance, "allowed_material_specs": allowed_material_specs,
                   "equipment_capabilities": equipment_capabilities, "version_id": version_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            agreement = self._agreement_row(connection, agreement_id)
            self._require_active(agreement)
            self._require_party(actor, agreement, D.PARTY_TECH)
            if stage not in D.STAGE_RANK:
                raise ValidationError("stage 必须是 lab/pilot/continuous")
            for name, value in (("formulation", formulation), ("scale_params", scale_params),
                                ("scale_tolerance", scale_tolerance),
                                ("allowed_material_specs", allowed_material_specs),
                                ("equipment_capabilities", equipment_capabilities)):
                self._json_object(value, name)
            if not formulation:
                raise ValidationError("formulation 必须包含核心配方")

            row = connection.execute(
                "SELECT COALESCE(MAX(seq),0) AS max_seq FROM pilot_process_versions WHERE agreement_id=? AND stage=?",
                (agreement_id, stage),
            ).fetchone()
            seq = row["max_seq"] + 1
            supersedes = self._latest_frozen_version(connection, agreement_id, stage)
            new_version_id = self._id(version_id, "version_id") if version_id else self._new_id()

            def create():
                try:
                    connection.execute(
                        "INSERT INTO pilot_process_versions(version_id,agreement_id,stage,seq,status,"
                        "formulation_hash,scale_params_json,scale_tolerance_json,allowed_material_specs_json,"
                        "equipment_capabilities_json,supersedes_version_id,frozen_at,created_by,created_at) "
                        "VALUES(?,?,?,?,'draft',?,?,?,?,?,?,NULL,?,?)",
                        (new_version_id, agreement_id, stage, seq, digest(formulation),
                         canonical_json(scale_params), canonical_json(scale_tolerance),
                         canonical_json(allowed_material_specs), canonical_json(equipment_capabilities),
                         supersedes["version_id"] if supersedes else None,
                         actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("工艺版本编号已经存在") from exc
                self._audit(connection, actor_id=actor_id, action="pilot.process_version.drafted",
                            resource_type="pilot_process_version", resource_id=new_version_id,
                            detail={"agreement_id": agreement_id, "stage": stage, "seq": seq,
                                    "supersedes_version_id": supersedes["version_id"] if supersedes else None,
                                    "formulation_hash": digest(formulation)})
                return {"resource_type": "pilot_process_version", "resource_id": new_version_id,
                        "version_id": new_version_id, "agreement_id": agreement_id,
                        "stage": stage, "seq": seq, "status": D.VERSION_DRAFT}

            return self._idempotent(connection, request_id=request_id,
                                    action="pilot.draft_process_version", payload=payload, create=create)

    def add_evidence(self, *, request_id: str, actor_id: str, agreement_id: str, stage: str,
                     category: str, external_ref: str, payload: dict[str, Any]) -> dict[str, Any]:
        payload_in = {"actor_id": actor_id, "agreement_id": agreement_id, "stage": stage,
                      "category": category, "external_ref": external_ref, "payload": payload}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            agreement = self._agreement_row(connection, agreement_id)
            self._require_active(agreement)
            if stage not in D.STAGE_RANK:
                raise ValidationError("stage 必须是 lab/pilot/continuous")
            category = self._id(category, "category")
            external_ref = self._text(external_ref, "external_ref", 300)
            payload = self._json_object(payload, "payload")

            def create():
                evidence_id = self._new_id()
                try:
                    connection.execute(
                        "INSERT INTO pilot_evidence(evidence_id,agreement_id,stage,category,external_ref,"
                        "payload_hash,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                        (evidence_id, agreement_id, stage, category, external_ref,
                         digest(payload), actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("该阶段同类证据已经存在") from exc
                self._audit(connection, actor_id=actor_id, action="pilot.evidence.added",
                            resource_type="pilot_evidence", resource_id=evidence_id,
                            detail={"agreement_id": agreement_id, "stage": stage,
                                    "category": category, "external_ref": external_ref,
                                    "payload_hash": digest(payload)})
                return {"resource_type": "pilot_evidence", "resource_id": evidence_id,
                        "evidence_id": evidence_id, "agreement_id": agreement_id,
                        "stage": stage, "category": category}

            return self._idempotent(connection, request_id=request_id,
                                    action="pilot.add_evidence", payload=payload_in, create=create)

    def freeze_process_version(self, *, request_id: str, actor_id: str, agreement_id: str,
                               version_id: str) -> dict[str, Any]:
        """放大前冻结可执行工艺：前置证据必须齐备，阶段顺序必须合法。"""

        payload = {"actor_id": actor_id, "agreement_id": agreement_id, "version_id": version_id}
        with self.database.transaction(immediate=True) as connection:
            replay = self._replay_if_seen(connection, request_id=request_id,
                                          action="pilot.freeze_process_version", payload=payload)
            if replay is not None:
                return replay
            actor = self._actor(connection, actor_id)
            agreement = self._agreement_row(connection, agreement_id)
            self._require_active(agreement)
            self._require_party(actor, agreement, D.PARTY_TECH)
            version = connection.execute(
                "SELECT * FROM pilot_process_versions WHERE agreement_id=? AND version_id=?",
                (agreement_id, version_id),
            ).fetchone()
            if version is None:
                raise NotFoundError("工艺版本不存在")
            if version["status"] == D.VERSION_FROZEN:
                raise ConflictError("工艺版本已经冻结，不能重复冻结")
            stage = version["stage"]
            required = D.REQUIRED_EVIDENCE[stage]
            present = {row["category"] for row in connection.execute(
                "SELECT category FROM pilot_evidence WHERE agreement_id=? AND stage=?",
                (agreement_id, stage),
            )}
            missing = sorted(required - present)
            if missing:
                raise ConflictError(f"前置证据不齐备，缺少: {','.join(missing)}")
            if stage != "lab":
                prior = D.STAGES[D.STAGE_RANK[stage] - 1]
                if self._latest_frozen_version(connection, agreement_id, prior) is None:
                    raise ConflictError(f"进入 {stage} 前必须先冻结 {prior} 阶段工艺")

            def create():
                now = self._now()
                connection.execute(
                    "UPDATE pilot_process_versions SET status='frozen', frozen_at=? WHERE version_id=?",
                    (now, version_id),
                )
                self._audit(connection, actor_id=actor_id, action="pilot.process_version.frozen",
                            resource_type="pilot_process_version", resource_id=version_id,
                            detail={"agreement_id": agreement_id, "stage": stage,
                                    "seq": version["seq"], "evidence_categories": sorted(present)})
                return {"resource_type": "pilot_process_version", "resource_id": version_id,
                        "version_id": version_id, "agreement_id": agreement_id,
                        "stage": stage, "status": D.VERSION_FROZEN, "frozen_at": now}

            return self._idempotent(connection, request_id=request_id,
                                    action="pilot.freeze_process_version", payload=payload, create=create)

    # ------------------------------------------------------------------ #
    # 职责分离的签收门
    # ------------------------------------------------------------------ #
    def open_scale_gate(self, *, request_id: str, actor_id: str, agreement_id: str,
                        stage: str, version_id: str) -> dict[str, Any]:
        """为进入 pilot/continuous 阶段开启签收门，绑定一套已冻结工艺。"""

        payload = {"actor_id": actor_id, "agreement_id": agreement_id, "stage": stage,
                   "version_id": version_id}
        with self.database.transaction(immediate=True) as connection:
            replay = self._replay_if_seen(connection, request_id=request_id,
                                          action="pilot.open_scale_gate", payload=payload)
            if replay is not None:
                return replay
            actor = self._actor(connection, actor_id)
            agreement = self._agreement_row(connection, agreement_id)
            self._require_active(agreement)
            if stage not in D.SCALE_GATE_STAGES:
                raise ValidationError("签收门只能在 pilot/continuous 放大前开启")
            version = connection.execute(
                "SELECT * FROM pilot_process_versions WHERE agreement_id=? AND version_id=?",
                (agreement_id, version_id),
            ).fetchone()
            if version is None:
                raise NotFoundError("工艺版本不存在")
            if version["stage"] != stage or version["status"] != D.VERSION_FROZEN:
                raise ConflictError("签收门必须绑定该阶段一套已冻结工艺")
            # 并发签收：同一阶段同一时刻最多一个未决门，门通过后才能为新版本开门。
            open_gate = connection.execute(
                "SELECT 1 FROM pilot_scale_gates WHERE agreement_id=? AND stage=? AND status='open'",
                (agreement_id, stage),
            ).fetchone()
            if open_gate is not None:
                raise ConflictError("该阶段已有未通过的签收门，并发签收最多绑定一个版本")

            def create():
                gate_id = self._new_id()
                now = self._now()
                connection.execute(
                    "INSERT INTO pilot_scale_gates(gate_id,agreement_id,stage,version_id,status,"
                    "tech_confirmation_id,production_confirmation_id,passed_at,created_at) "
                    "VALUES(?,?,?,?,'open',NULL,NULL,NULL,?)",
                    (gate_id, agreement_id, stage, version_id, now),
                )
                self._audit(connection, actor_id=actor_id, action="pilot.scale_gate.opened",
                            resource_type="pilot_scale_gate", resource_id=gate_id,
                            detail={"agreement_id": agreement_id, "stage": stage,
                                    "version_id": version_id})
                return {"resource_type": "pilot_scale_gate", "resource_id": gate_id,
                        "gate_id": gate_id, "agreement_id": agreement_id,
                        "stage": stage, "version_id": version_id, "status": D.GATE_OPEN}

            return self._idempotent(connection, request_id=request_id,
                                    action="pilot.open_scale_gate", payload=payload, create=create)

    def confirm_scale_gate(self, *, request_id: str, actor_id: str, agreement_id: str,
                           gate_id: str, party: str) -> dict[str, Any]:
        """技术方与生产方分别确认；双方确认后门通过，方可进入下一阶段。"""

        payload = {"actor_id": actor_id, "agreement_id": agreement_id, "gate_id": gate_id,
                   "party": party}
        with self.database.transaction(immediate=True) as connection:
            replay = self._replay_if_seen(connection, request_id=request_id,
                                          action="pilot.confirm_scale_gate", payload=payload)
            if replay is not None:
                return replay
            actor = self._actor(connection, actor_id)
            agreement = self._agreement_row(connection, agreement_id)
            self._require_active(agreement)
            if party not in D.PARTIES:
                raise ValidationError("party 必须是 tech/production")
            # 职责分离：技术方确认只能由技术方组织做出，生产方同理。
            self._require_party(actor, agreement, party)
            gate = connection.execute(
                "SELECT * FROM pilot_scale_gates WHERE agreement_id=? AND gate_id=?",
                (agreement_id, gate_id),
            ).fetchone()
            if gate is None:
                raise NotFoundError("签收门不存在")
            if gate["status"] == D.GATE_PASSED:
                raise ConflictError("签收门已经通过")
            already = connection.execute(
                "SELECT 1 FROM pilot_gate_confirmations WHERE gate_id=? AND party=?",
                (gate_id, party),
            ).fetchone()
            if already:
                raise ConflictError("该方已经确认，不能重复确认")

            def create():
                confirmation_id = self._new_id()
                now = self._now()
                connection.execute(
                    "INSERT INTO pilot_gate_confirmations(confirmation_id,gate_id,party,actor_id,created_at) "
                    "VALUES(?,?,?,?,?)",
                    (confirmation_id, gate_id, party, actor_id, now),
                )
                confirmations = {row["party"] for row in connection.execute(
                    "SELECT party FROM pilot_gate_confirmations WHERE gate_id=?", (gate_id,)
                )}
                passed = D.PARTIES <= confirmations
                detail = {"agreement_id": agreement_id, "stage": gate["stage"], "party": party,
                          "confirmations": sorted(confirmations)}
                if passed:
                    connection.execute(
                        "UPDATE pilot_scale_gates SET status='passed', tech_confirmation_id="
                        "(SELECT confirmation_id FROM pilot_gate_confirmations WHERE gate_id=? AND party='tech'), "
                        "production_confirmation_id=(SELECT confirmation_id FROM pilot_gate_confirmations "
                        "WHERE gate_id=? AND party='production'), passed_at=? WHERE gate_id=?",
                        (gate_id, gate_id, now, gate_id),
                    )
                    action = "pilot.scale_gate.passed"
                else:
                    action = "pilot.scale_gate.confirmed"
                self._audit(connection, actor_id=actor_id, action=action,
                            resource_type="pilot_scale_gate", resource_id=gate_id, detail=detail)
                return {"resource_type": "pilot_scale_gate", "resource_id": gate_id,
                        "gate_id": gate_id, "agreement_id": agreement_id, "stage": gate["stage"],
                        "status": D.GATE_PASSED if passed else D.GATE_OPEN,
                        "confirmations": sorted(confirmations), "confirmed_party": party}

            return self._idempotent(connection, request_id=request_id,
                                    action="pilot.confirm_scale_gate", payload=payload, create=create)

    # ------------------------------------------------------------------ #
    # 原料谱系
    # ------------------------------------------------------------------ #
    def register_material(self, *, request_id: str, actor_id: str, agreement_id: str, material_id: str,
                          code: str, name: str, maker: str, batch_no: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "agreement_id": agreement_id, "material_id": material_id,
                   "code": code, "name": name, "maker": maker, "batch_no": batch_no}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            agreement = self._agreement_row(connection, agreement_id)
            self._require_active(agreement)
            self._require_party(actor, agreement, D.PARTY_PRODUCTION)
            material_id = self._id(material_id, "material_id")
            code = self._id(code, "code")
            name = self._text(name, "name", 200)
            maker = self._text(maker, "maker", 200)
            batch_no = self._id(batch_no, "batch_no")

            def create():
                try:
                    connection.execute(
                        "INSERT INTO pilot_materials(material_id,agreement_id,code,name,maker,batch_no,"
                        "status,substituted_by_material_id,discontinued_at,created_by,created_at) "
                        "VALUES(?,?,?,?,?,?,'available',NULL,NULL,?,?)",
                        (material_id, agreement_id, code, name, maker, batch_no, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("该原料编码与批次已经登记") from exc
                self._audit(connection, actor_id=actor_id, action="pilot.material.registered",
                            resource_type="pilot_material", resource_id=material_id,
                            detail={"agreement_id": agreement_id, "code": code, "maker": maker,
                                    "batch_no": batch_no})
                return {"resource_type": "pilot_material", "resource_id": material_id,
                        "material_id": material_id, "agreement_id": agreement_id,
                        "code": code, "status": "available"}

            return self._idempotent(connection, request_id=request_id,
                                    action="pilot.register_material", payload=payload, create=create)

    def substitute_material(self, *, request_id: str, actor_id: str, agreement_id: str,
                            material_id: str, replacement_material_id: str,
                            justification: str) -> dict[str, Any]:
        """供应商/原料替换：旧料标记停产替代，不改写历史，形成待处置变更。"""

        payload = {"actor_id": actor_id, "agreement_id": agreement_id, "material_id": material_id,
                   "replacement_material_id": replacement_material_id, "justification": justification}
        with self.database.transaction(immediate=True) as connection:
            replay = self._replay_if_seen(connection, request_id=request_id,
                                          action="pilot.substitute_material", payload=payload)
            if replay is not None:
                return replay
            actor = self._actor(connection, actor_id)
            agreement = self._agreement_row(connection, agreement_id)
            self._require_active(agreement)
            self._require_party(actor, agreement, D.PARTY_PRODUCTION)
            old = connection.execute(
                "SELECT * FROM pilot_materials WHERE agreement_id=? AND material_id=?",
                (agreement_id, material_id),
            ).fetchone()
            if old is None:
                raise NotFoundError("被替换原料不存在")
            if old["status"] != "available":
                raise ConflictError("原料已经处于停产或替代状态")
            new = connection.execute(
                "SELECT * FROM pilot_materials WHERE agreement_id=? AND material_id=?",
                (agreement_id, replacement_material_id),
            ).fetchone()
            if new is None:
                raise NotFoundError("替代原料不存在，请先登记")
            if new["code"] != old["code"]:
                raise ValidationError("替代原料必须使用同一物料编码")
            justification = self._text(justification, "justification")
            affected = self._affected_batch_ids(connection, agreement_id, [material_id])

            def create():
                now = self._now()
                connection.execute(
                    "UPDATE pilot_materials SET status='substituted', substituted_by_material_id=? "
                    "WHERE material_id=?",
                    (replacement_material_id, material_id),
                )
                self._audit(connection, actor_id=actor_id, action="pilot.material.substituted",
                            resource_type="pilot_material", resource_id=material_id,
                            detail={"agreement_id": agreement_id, "replacement_material_id": replacement_material_id,
                                    "justification": justification, "affected_batch_ids": affected})
                return {"resource_type": "pilot_material", "resource_id": material_id,
                        "material_id": material_id, "agreement_id": agreement_id,
                        "status": "substituted", "substituted_by_material_id": replacement_material_id,
                        "affected_batch_ids": affected}

            return self._idempotent(connection, request_id=request_id,
                                    action="pilot.substitute_material", payload=payload, create=create)

    # ------------------------------------------------------------------ #
    # 试生产批次
    # ------------------------------------------------------------------ #
    def release_batch(self, *, request_id: str, actor_id: str, agreement_id: str, batch_no: str,
                      stage: str, version_id: str, material_ids: list[str]) -> dict[str, Any]:
        """按一套已授权且通过签收门的工艺与指定原料谱系放行试生产批次。"""

        payload = {"actor_id": actor_id, "agreement_id": agreement_id, "batch_no": batch_no,
                   "stage": stage, "version_id": version_id, "material_ids": material_ids}
        with self.database.transaction(immediate=True) as connection:
            replay = self._replay_if_seen(connection, request_id=request_id,
                                          action="pilot.release_batch", payload=payload)
            if replay is not None:
                return replay
            actor = self._actor(connection, actor_id)
            agreement = self._agreement_row(connection, agreement_id)
            self._require_active(agreement)
            self._require_party(actor, agreement, D.PARTY_PRODUCTION)
            if stage not in D.STAGE_RANK:
                raise ValidationError("stage 必须是 lab/pilot/continuous")
            version = connection.execute(
                "SELECT * FROM pilot_process_versions WHERE agreement_id=? AND version_id=?",
                (agreement_id, version_id),
            ).fetchone()
            if version is None:
                raise NotFoundError("工艺版本不存在")
            if version["stage"] != stage or version["status"] != D.VERSION_FROZEN:
                raise ConflictError("批次必须绑定该阶段一套已冻结工艺")
            gate = None
            if stage in D.SCALE_GATE_STAGES:
                gate = self._gate_for_stage(connection, agreement_id, stage)
                if gate is None or gate["version_id"] != version_id or gate["status"] != D.GATE_PASSED:
                    raise ConflictError("该阶段工艺尚未通过双方签收门，不能放行批次")
            if not isinstance(material_ids, list) or not material_ids:
                raise ValidationError("material_ids 必须是非空列表")
            if len(set(material_ids)) != len(material_ids):
                raise ValidationError("原料不能重复登记")
            allowed = set(json.loads(version["allowed_material_specs_json"]).keys())
            for material_id in material_ids:
                material = connection.execute(
                    "SELECT * FROM pilot_materials WHERE agreement_id=? AND material_id=?",
                    (agreement_id, material_id),
                ).fetchone()
                if material is None:
                    raise NotFoundError(f"原料不存在: {material_id}")
                if material["status"] != "available":
                    raise ConflictError(f"原料 {material_id} 已停产或被替代，不能用于新批次")
                if material["code"] not in allowed:
                    raise ConflictError(f"原料 {material_id} 不在工艺允许的物料规格内")
            batch_no = self._id(batch_no, "batch_no")

            def create():
                batch_id = self._new_id()
                now = self._now()
                connection.execute(
                    "INSERT INTO pilot_batches(batch_id,agreement_id,batch_no,stage,version_id,gate_id,"
                    "material_ids_json,status,callback_id,released_at,decided_at) "
                    "VALUES(?,?,?,?,?,?,?, 'released', NULL, ?, NULL)",
                    (batch_id, agreement_id, batch_no, stage, version_id,
                     gate["gate_id"] if gate else None, canonical_json(material_ids), now),
                )
                self._audit(connection, actor_id=actor_id, action="pilot.batch.released",
                            resource_type="pilot_batch", resource_id=batch_id,
                            detail={"agreement_id": agreement_id, "batch_no": batch_no,
                                    "stage": stage, "version_id": version_id,
                                    "material_ids": material_ids})
                return {"resource_type": "pilot_batch", "resource_id": batch_id,
                        "batch_id": batch_id, "agreement_id": agreement_id,
                        "batch_no": batch_no, "stage": stage, "version_id": version_id,
                        "status": D.BATCH_RELEASED}

            return self._idempotent(connection, request_id=request_id,
                                    action="pilot.release_batch", payload=payload, create=create)

    def start_batch(self, *, request_id: str, actor_id: str, agreement_id: str,
                    batch_id: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "agreement_id": agreement_id, "batch_id": batch_id}
        with self.database.transaction(immediate=True) as connection:
            replay = self._replay_if_seen(connection, request_id=request_id,
                                          action="pilot.start_batch", payload=payload)
            if replay is not None:
                return replay
            actor = self._actor(connection, actor_id)
            agreement = self._agreement_row(connection, agreement_id)
            self._require_active(agreement)
            self._require_party(actor, agreement, D.PARTY_PRODUCTION)
            batch = self._batch_row(connection, agreement_id, batch_id)
            if batch["status"] != D.BATCH_RELEASED:
                raise ConflictError("只有已放行批次可以开工")

            def create():
                connection.execute(
                    "UPDATE pilot_batches SET status='in_production' WHERE batch_id=?", (batch_id,)
                )
                self._audit(connection, actor_id=actor_id, action="pilot.batch.started",
                            resource_type="pilot_batch", resource_id=batch_id,
                            detail={"agreement_id": agreement_id, "batch_no": batch["batch_no"]})
                return {"resource_type": "pilot_batch", "resource_id": batch_id,
                        "batch_id": batch_id, "agreement_id": agreement_id,
                        "status": D.BATCH_IN_PRODUCTION}

            return self._idempotent(connection, request_id=request_id,
                                    action="pilot.start_batch", payload=payload, create=create)

    def _batch_row(self, connection, agreement_id: str, batch_id: str):
        batch = connection.execute(
            "SELECT * FROM pilot_batches WHERE agreement_id=? AND batch_id=?",
            (agreement_id, batch_id),
        ).fetchone()
        if batch is None:
            raise NotFoundError("批次不存在")
        return batch

    # ------------------------------------------------------------------ #
    # 检测回调：相同回调不得重复推进状态
    # ------------------------------------------------------------------ #
    def quality_callback(self, *, request_id: str, actor_id: str, agreement_id: str, batch_id: str,
                         callback_key: str, metrics: dict[str, float],
                         targets: dict[str, dict[str, float]]) -> dict[str, Any]:
        """登记检测结果并据此推进批次终态。

        callback_key 相同的重复回调原样回放首次结论，绝不二次推进状态。
        """

        payload = {"actor_id": actor_id, "agreement_id": agreement_id, "batch_id": batch_id,
                   "callback_key": callback_key, "metrics": metrics, "targets": targets}
        with self.database.transaction(immediate=True) as connection:
            replay = self._replay_if_seen(connection, request_id=request_id,
                                          action="pilot.quality_callback", payload=payload)
            if replay is not None:
                # 重放不产生第二次状态推进。
                replay["state_advanced"] = False
                return replay
            actor = self._actor(connection, actor_id)
            agreement = self._agreement_row(connection, agreement_id)
            self._require_active(agreement)
            batch = self._batch_row(connection, agreement_id, batch_id)
            callback_key = self._id(callback_key, "callback_key")
            self._validate_measurement(metrics, targets)
            duplicate = connection.execute(
                "SELECT * FROM pilot_quality_callbacks WHERE batch_id=? AND callback_key=?",
                (batch_id, callback_key),
            ).fetchone()
            if duplicate is not None:
                if duplicate["metrics_json"] != canonical_json(metrics) or \
                        duplicate["targets_json"] != canonical_json(targets):
                    raise ConflictError("相同 callback_key 不能携带不同检测数据")
                return {"resource_type": "pilot_quality_callback",
                        "resource_id": duplicate["callback_id"],
                        "callback_id": duplicate["callback_id"], "batch_id": batch_id,
                        "agreement_id": agreement_id, "callback_key": callback_key,
                        "result": duplicate["result"], "replayed": True, "state_advanced": False}

            def create():
                result, per_metric = self._evaluate(metrics, targets)
                now = self._now()
                callback_id = self._new_id()
                connection.execute(
                    "INSERT INTO pilot_quality_callbacks(callback_id,batch_id,callback_key,metrics_json,"
                    "targets_json,result,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (callback_id, batch_id, callback_key, canonical_json(metrics),
                     canonical_json(targets), result, actor_id, now),
                )
                advanced = False
                if batch["status"] in (D.BATCH_RELEASED, D.BATCH_IN_PRODUCTION):
                    connection.execute(
                        "UPDATE pilot_batches SET status=?, callback_id=?, decided_at=? WHERE batch_id=?",
                        (result, callback_id, now, batch_id),
                    )
                    advanced = True
                self._audit(connection, actor_id=actor_id, action="pilot.quality_callback.recorded",
                            resource_type="pilot_quality_callback", resource_id=callback_id,
                            detail={"agreement_id": agreement_id, "batch_id": batch_id,
                                    "batch_no": batch["batch_no"], "callback_key": callback_key,
                                    "result": result, "per_metric": per_metric,
                                    "batch_status_before": batch["status"], "state_advanced": advanced})
                return {"resource_type": "pilot_quality_callback", "resource_id": callback_id,
                        "callback_id": callback_id, "batch_id": batch_id, "agreement_id": agreement_id,
                        "callback_key": callback_key, "result": result, "per_metric": per_metric,
                        "state_advanced": advanced}

            return self._idempotent(connection, request_id=request_id,
                                    action="pilot.quality_callback", payload=payload, create=create)

    @staticmethod
    def _validate_measurement(metrics: Any, targets: Any) -> None:
        if not isinstance(metrics, dict) or not metrics:
            raise ValidationError("metrics 必须是非空检测指标对象")
        if not isinstance(targets, dict) or set(targets.keys()) != set(metrics.keys()):
            raise ValidationError("targets 必须为每个指标给出阈值")
        for name, value in metrics.items():
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ValidationError(f"指标 {name} 必须是数值")
            spec = targets[name]
            if not isinstance(spec, dict) or "min" not in spec or "max" not in spec:
                raise ValidationError(f"指标 {name} 的阈值必须包含 min/max")
            if not isinstance(spec["min"], (int, float)) or not isinstance(spec["max"], (int, float)):
                raise ValidationError(f"指标 {name} 的 min/max 必须是数值")
            if spec["min"] > spec["max"]:
                raise ValidationError(f"指标 {name} 的 min 不能大于 max")

    @staticmethod
    def _evaluate(metrics: dict[str, float], targets: dict[str, dict[str, float]]):
        per_metric: dict[str, str] = {}
        passes = 0
        for name, value in metrics.items():
            spec = targets[name]
            ok = spec["min"] <= value <= spec["max"]
            per_metric[name] = "pass" if ok else "fail"
            if ok:
                passes += 1
        if passes == len(metrics):
            result = D.BATCH_QUALIFIED
        elif passes == 0:
            result = D.BATCH_FAILED
        else:
            result = D.BATCH_PARTIAL
        return result, per_metric

    # ------------------------------------------------------------------ #
    # 偏差与责任
    # ------------------------------------------------------------------ #
    def open_deviation(self, *, request_id: str, actor_id: str, agreement_id: str, code: str,
                       classification: str, detail: dict[str, Any], batch_id: str | None = None
                       ) -> dict[str, Any]:
        """登记偏差；责任方依据偏差类别确定性判定。"""

        payload = {"actor_id": actor_id, "agreement_id": agreement_id, "code": code,
                   "classification": classification, "detail": detail, "batch_id": batch_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            agreement = self._agreement_row(connection, agreement_id)
            self._require_active(agreement)
            code = self._id(code, "code")
            detail = self._json_object(detail, "detail")
            try:
                responsible = D.responsible_party_for(classification)
            except ValueError as exc:
                raise ValidationError(str(exc)) from exc
            if batch_id is not None:
                self._batch_row(connection, agreement_id, batch_id)

            def create():
                deviation_id = self._new_id()
                connection.execute(
                    "INSERT INTO pilot_deviations(deviation_id,agreement_id,batch_id,code,classification,"
                    "detail_json,responsible_party,disposition,closed_at,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?, 'open', NULL, ?, ?)",
                    (deviation_id, agreement_id, batch_id, code, classification,
                     canonical_json(detail), responsible, actor_id, self._now()),
                )
                self._audit(connection, actor_id=actor_id, action="pilot.deviation.opened",
                            resource_type="pilot_deviation", resource_id=deviation_id,
                            detail={"agreement_id": agreement_id, "code": code,
                                    "classification": classification,
                                    "responsible_party": responsible, "batch_id": batch_id})
                return {"resource_type": "pilot_deviation", "resource_id": deviation_id,
                        "deviation_id": deviation_id, "agreement_id": agreement_id,
                        "code": code, "classification": classification,
                        "responsible_party": responsible, "disposition": "open"}

            return self._idempotent(connection, request_id=request_id,
                                    action="pilot.open_deviation", payload=payload, create=create)

    def resolve_deviation(self, *, request_id: str, actor_id: str, agreement_id: str,
                          deviation_id: str, disposition: str,
                          create_obligation: dict[str, Any] | None = None) -> dict[str, Any]:
        """以新记录结清偏差：接受/驳回/返工，可生成补偿或返工义务。

        已结清偏差不能改写；不同结论需要新的偏差记录。
        """

        payload = {"actor_id": actor_id, "agreement_id": agreement_id,
                   "deviation_id": deviation_id, "disposition": disposition,
                   "create_obligation": create_obligation}
        with self.database.transaction(immediate=True) as connection:
            replay = self._replay_if_seen(connection, request_id=request_id,
                                          action="pilot.resolve_deviation", payload=payload)
            if replay is not None:
                return replay
            actor = self._actor(connection, actor_id)
            agreement = self._agreement_row(connection, agreement_id)
            self._require_active(agreement)
            deviation = connection.execute(
                "SELECT * FROM pilot_deviations WHERE agreement_id=? AND deviation_id=?",
                (agreement_id, deviation_id),
            ).fetchone()
            if deviation is None:
                raise NotFoundError("偏差不存在")
            if deviation["disposition"] != "open":
                raise ConflictError("偏差已经结清，不能改写既有结论")
            if disposition not in ("accepted", "rejected", "reworked"):
                raise ValidationError("disposition 必须是 accepted/rejected/reworked")
            # 处置由责任方的相对方确认；admin 可代行。
            counterparty = D.PARTY_PRODUCTION if deviation["responsible_party"] == D.PARTY_TECH \
                else D.PARTY_TECH
            self._require_party(actor, agreement, counterparty)

            obligation_id = None
            if create_obligation is not None:
                if disposition == "rejected":
                    raise ValidationError("驳回偏差时不能附带义务")
                kind = create_obligation.get("kind")
                if kind not in (D.OBLIGATION_COMPENSATION, D.OBLIGATION_REWORK):
                    raise ValidationError("偏差结清只能生成 compensation/rework 义务")
                if kind == D.OBLIGATION_COMPENSATION:
                    amount = self._amount(create_obligation)
                    currency = self._currency(create_obligation)
                else:
                    amount, currency = 0.0, create_obligation.get("currency", "CNY")
                obligation_id = self._insert_obligation(
                    connection, agreement_id=agreement_id, kind=kind,
                    party=deviation["responsible_party"], amount=amount, currency=currency,
                    source_type="pilot_deviation", source_id=deviation_id, created_by=actor_id)

            def create():
                now = self._now()
                connection.execute(
                    "UPDATE pilot_deviations SET disposition=?, closed_at=? WHERE deviation_id=?",
                    (disposition, now, deviation_id),
                )
                self._audit(connection, actor_id=actor_id, action="pilot.deviation.resolved",
                            resource_type="pilot_deviation", resource_id=deviation_id,
                            detail={"agreement_id": agreement_id, "code": deviation["code"],
                                    "disposition": disposition, "obligation_id": obligation_id})
                return {"resource_type": "pilot_deviation", "resource_id": deviation_id,
                        "deviation_id": deviation_id, "agreement_id": agreement_id,
                        "code": deviation["code"], "disposition": disposition,
                        "closed_at": now, "obligation_id": obligation_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="pilot.resolve_deviation", payload=payload, create=create)

    # ------------------------------------------------------------------ #
    # 批次召回
    # ------------------------------------------------------------------ #
    def recall_batch(self, *, request_id: str, actor_id: str, agreement_id: str, batch_id: str,
                     reason: str) -> dict[str, Any]:
        """召回已放行批次：以新记录登记，不改写批次已交付事实，自动生成返还义务。"""

        payload = {"actor_id": actor_id, "agreement_id": agreement_id, "batch_id": batch_id,
                   "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            replay = self._replay_if_seen(connection, request_id=request_id,
                                          action="pilot.recall_batch", payload=payload)
            if replay is not None:
                return replay
            actor = self._actor(connection, actor_id)
            agreement = self._agreement_row(connection, agreement_id)
            self._require_active(agreement)
            # 任一方发现质量问题都可发起召回；外部组织无权。
            self._party_of(actor, agreement)
            batch = self._batch_row(connection, agreement_id, batch_id)
            reason = self._text(reason, "reason")
            existing = connection.execute(
                "SELECT 1 FROM pilot_obligations WHERE agreement_id=? AND source_type='pilot_batch_recall' "
                "AND source_id=?",
                (agreement_id, batch_id),
            ).fetchone()
            if existing:
                raise ConflictError("该批次已经召回")

            def create():
                # 生产方已交付的产品需返还/处置：记生产方对技术方的 return 义务（金额为 0，实物返还）。
                obligation_id = self._insert_obligation(
                    connection, agreement_id=agreement_id, kind=D.OBLIGATION_RETURN,
                    party=D.PARTY_PRODUCTION, amount=0.0, currency="CNY",
                    source_type="pilot_batch_recall", source_id=batch_id, created_by=actor_id)
                self._audit(connection, actor_id=actor_id, action="pilot.batch.recalled",
                            resource_type="pilot_batch", resource_id=batch_id,
                            detail={"agreement_id": agreement_id, "batch_no": batch["batch_no"],
                                    "reason": reason, "preserved_status": batch["status"],
                                    "obligation_id": obligation_id})
                return {"resource_type": "pilot_batch_recall", "resource_id": batch_id,
                        "batch_id": batch_id, "agreement_id": agreement_id,
                        "batch_no": batch["batch_no"], "recalled": True,
                        "preserved_status": batch["status"], "obligation_id": obligation_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="pilot.recall_batch", payload=payload, create=create)

    # ------------------------------------------------------------------ #
    # 阶段付款、退款与义务结算
    # ------------------------------------------------------------------ #
    def _amount(self, data: dict[str, Any]) -> float:
        amount = data.get("amount")
        if not isinstance(amount, (int, float)) or isinstance(amount, bool) or amount < 0:
            raise ValidationError("amount 必须是非负数值")
        return float(amount)

    def _currency(self, data: dict[str, Any]) -> str:
        currency = str(data.get("currency", "CNY")).strip()
        if not currency or len(currency) > 8:
            raise ValidationError("currency 不合法")
        return currency

    def _insert_obligation(self, connection, *, agreement_id: str, kind: str, party: str,
                           amount: float, currency: str, source_type: str, source_id: str,
                           created_by: str) -> str:
        obligation_id = self._new_id()
        connection.execute(
            "INSERT INTO pilot_obligations(obligation_id,agreement_id,kind,party,amount,currency,"
            "source_type,source_id,status,settled_by_type,settled_by_id,settled_at,created_by,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,'open',NULL,NULL,NULL,?,?)",
            (obligation_id, agreement_id, kind, party, amount, currency, source_type, source_id,
             created_by, self._now()),
        )
        self._audit(connection, actor_id=created_by, action="pilot.obligation.created",
                    resource_type="pilot_obligation", resource_id=obligation_id,
                    detail={"agreement_id": agreement_id, "kind": kind, "party": party,
                            "amount": amount, "currency": currency,
                            "source_type": source_type, "source_id": source_id})
        return obligation_id

    def schedule_payment(self, *, request_id: str, actor_id: str, agreement_id: str, milestone: str,
                         amount: float, currency: str = "CNY", stage: str | None = None
                         ) -> dict[str, Any]:
        """把阶段付款登记为生产方承担的到期付款义务。"""

        payload = {"actor_id": actor_id, "agreement_id": agreement_id, "milestone": milestone,
                   "amount": amount, "currency": currency, "stage": stage}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            agreement = self._agreement_row(connection, agreement_id)
            self._require_active(agreement)
            if actor.role not in ("admin", "operator"):
                raise PermissionDenied("当前角色不能排定阶段付款")
            self._party_of(actor, agreement)
            milestone = self._id(milestone, "milestone")
            if stage is not None and stage not in D.STAGE_RANK:
                raise ValidationError("stage 必须是 lab/pilot/continuous")
            amount = self._amount({"amount": amount})
            currency = self._currency({"currency": currency})
            if amount <= 0:
                raise ValidationError("amount 必须大于 0")

            def create():
                obligation_id = self._insert_obligation(
                    connection, agreement_id=agreement_id, kind=D.OBLIGATION_PAYMENT_DUE,
                    party=D.PARTY_PRODUCTION, amount=amount, currency=currency,
                    source_type="pilot_milestone", source_id=milestone, created_by=actor_id)
                return {"resource_type": "pilot_obligation", "resource_id": obligation_id,
                        "obligation_id": obligation_id, "agreement_id": agreement_id,
                        "kind": D.OBLIGATION_PAYMENT_DUE, "party": D.PARTY_PRODUCTION,
                        "milestone": milestone, "amount": amount, "currency": currency,
                        "status": "open"}

            return self._idempotent(connection, request_id=request_id,
                                    action="pilot.schedule_payment", payload=payload, create=create)

    def record_payment(self, *, request_id: str, actor_id: str, agreement_id: str, ref: str,
                       amount: float, direction: str, currency: str = "CNY",
                       milestone: str | None = None, stage: str | None = None) -> dict[str, Any]:
        """记录一笔不可改写的付款，并结清金额不超过它的未结义务。"""

        payload = {"actor_id": actor_id, "agreement_id": agreement_id, "ref": ref,
                   "amount": amount, "currency": currency, "direction": direction,
                   "milestone": milestone, "stage": stage}
        with self.database.transaction(immediate=True) as connection:
            replay = self._replay_if_seen(connection, request_id=request_id,
                                          action="pilot.record_payment", payload=payload)
            if replay is not None:
                return replay
            actor = self._actor(connection, actor_id)
            agreement = self._agreement_row(connection, agreement_id)
            # 协议终止后仍允许付款，以履行遗留的补偿/退款义务。
            ref = self._id(ref, "ref")
            amount = self._amount({"amount": amount})
            currency = self._currency({"currency": currency})
            if amount <= 0:
                raise ValidationError("amount 必须大于 0")
            if direction not in ("production_to_tech", "tech_to_production"):
                raise ValidationError("direction 不合法")
            payer_party = D.PARTY_PRODUCTION if direction == "production_to_tech" else D.PARTY_TECH
            self._require_party(actor, agreement, payer_party)
            if stage is not None and stage not in D.STAGE_RANK:
                raise ValidationError("stage 必须是 lab/pilot/continuous")

            def create():
                payment_id = self._new_id()
                now = self._now()
                try:
                    connection.execute(
                        "INSERT INTO pilot_payments(payment_id,agreement_id,milestone,stage,amount,"
                        "currency,direction,ref,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (payment_id, agreement_id, milestone or "", stage, amount, currency,
                         direction, ref, actor_id, now),
                    )
                except Exception as exc:
                    raise ConflictError("付款凭证号已经存在") from exc
                settled = self._settle_with_payment(connection, agreement_id=agreement_id,
                                                    payment_id=payment_id, amount=amount,
                                                    currency=currency, direction=direction,
                                                    created_by=actor_id, now=now)
                self._audit(connection, actor_id=actor_id, action="pilot.payment.recorded",
                            resource_type="pilot_payment", resource_id=payment_id,
                            detail={"agreement_id": agreement_id, "ref": ref, "amount": amount,
                                    "currency": currency, "direction": direction,
                                    "settled_obligation_ids": settled})
                return {"resource_type": "pilot_payment", "resource_id": payment_id,
                        "payment_id": payment_id, "agreement_id": agreement_id, "ref": ref,
                        "amount": amount, "currency": currency, "direction": direction,
                        "settled_obligation_ids": settled}

            return self._idempotent(connection, request_id=request_id,
                                    action="pilot.record_payment", payload=payload, create=create)

    def _settle_with_payment(self, connection, *, agreement_id: str, payment_id: str, amount: float,
                             currency: str, direction: str, created_by: str, now: str) -> list[str]:
        """按先到期先结（FIFO）用本笔付款结清同币种、同方向的未结义务。

        正向付款冲抵技术方应收（payment_due）；技术方退款冲抵技术方承担的
        compensation/refund。义务金额超过本笔付款时不予部分结清，留待后续付款。
        """

        if direction == "production_to_tech":
            # 生产方向技术方付款，冲抵生产方承担的阶段款。
            kinds = (D.OBLIGATION_PAYMENT_DUE,)
            party = D.PARTY_PRODUCTION
        else:
            # 技术方向生产方退款/补偿，冲抵技术方承担的补偿与退款。
            kinds = (D.OBLIGATION_COMPENSATION, D.OBLIGATION_REFUND)
            party = D.PARTY_TECH
        rows = connection.execute(
            "SELECT * FROM pilot_obligations WHERE agreement_id=? AND status='open' AND currency=? "
            "AND party=? AND kind IN (?,?) ORDER BY created_at, obligation_id",
            (agreement_id, currency, party, kinds[0], kinds[1] if len(kinds) > 1 else kinds[0]),
        ).fetchall()
        remaining = amount
        settled: list[str] = []
        for row in rows:
            if row["amount"] > remaining:
                continue
            connection.execute(
                "UPDATE pilot_obligations SET status='settled', settled_by_type='pilot_payment', "
                "settled_by_id=?, settled_at=? WHERE obligation_id=?",
                (payment_id, now, row["obligation_id"]),
            )
            remaining -= row["amount"]
            settled.append(row["obligation_id"])
            self._audit(connection, actor_id=created_by, action="pilot.obligation.settled",
                        resource_type="pilot_obligation", resource_id=row["obligation_id"],
                        detail={"agreement_id": agreement_id, "settled_by_type": "pilot_payment",
                                "settled_by_id": payment_id, "kind": row["kind"],
                                "amount": row["amount"]})
        return settled

    def settle_obligation(self, *, request_id: str, actor_id: str, agreement_id: str,
                          obligation_id: str, settlement_ref: str) -> dict[str, Any]:
        """以人工凭证结清非货币义务（返工/返还）。"""

        payload = {"actor_id": actor_id, "agreement_id": agreement_id,
                   "obligation_id": obligation_id, "settlement_ref": settlement_ref}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            agreement = self._agreement_row(connection, agreement_id)
            # 终止后仍可用新凭证结清遗留的返工/返还义务。
            replay = self._replay_if_seen(connection, request_id=request_id,
                                          action="pilot.settle_obligation", payload=payload)
            if replay is not None:
                return replay
            row = connection.execute(
                "SELECT * FROM pilot_obligations WHERE agreement_id=? AND obligation_id=?",
                (agreement_id, obligation_id),
            ).fetchone()
            if row is None:
                raise NotFoundError("义务不存在")
            if row["status"] == "settled":
                raise ConflictError("义务已经结清")
            if row["kind"] not in (D.OBLIGATION_REWORK, D.OBLIGATION_RETURN):
                raise PermissionDenied("货币义务只能由付款记录结清")
            settlement_ref = self._text(settlement_ref, "settlement_ref", 200)

            def create():
                now = self._now()
                connection.execute(
                    "UPDATE pilot_obligations SET status='settled', settled_by_type='manual', "
                    "settled_by_id=?, settled_at=? WHERE obligation_id=?",
                    (settlement_ref, now, obligation_id),
                )
                self._audit(connection, actor_id=actor_id, action="pilot.obligation.settled",
                            resource_type="pilot_obligation", resource_id=obligation_id,
                            detail={"agreement_id": agreement_id, "settled_by_type": "manual",
                                    "settled_by_id": settlement_ref, "kind": row["kind"]})
                return {"resource_type": "pilot_obligation", "resource_id": obligation_id,
                        "obligation_id": obligation_id, "agreement_id": agreement_id,
                        "kind": row["kind"], "status": "settled", "settlement_ref": settlement_ref}

            return self._idempotent(connection, request_id=request_id,
                                    action="pilot.settle_obligation", payload=payload, create=create)

    # ------------------------------------------------------------------ #
    # 协议终止：以新记录结清未完成义务
    # ------------------------------------------------------------------ #
    def terminate_agreement(self, *, request_id: str, actor_id: str, agreement_id: str,
                            reason: str) -> dict[str, Any]:
        """终止协议：冻结新交付，登记终止记录，并把未完成义务转换为返还/退款清单。

        已经交付的批次和已经支付的付款保持不变。
        """

        payload = {"actor_id": actor_id, "agreement_id": agreement_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            replay = self._replay_if_seen(connection, request_id=request_id,
                                          action="pilot.terminate_agreement", payload=payload)
            if replay is not None:
                return replay
            actor = self._actor(connection, actor_id)
            agreement = self._agreement_row(connection, agreement_id)
            if agreement["status"] != "active":
                raise ConflictError("协议已经终止")
            reason = self._text(reason, "reason")
            open_rows = connection.execute(
                "SELECT * FROM pilot_obligations WHERE agreement_id=? AND status='open' "
                "ORDER BY created_at, obligation_id",
                (agreement_id,),
            ).fetchall()

            def create():
                now = self._now()
                termination_id = self._new_id()
                surviving_obligation_ids: list[str] = []
                cancelled_obligation_ids: list[str] = []
                for row in open_rows:
                    if row["kind"] == D.OBLIGATION_PAYMENT_DUE:
                        # 尚未支付的阶段款随终止核减，不再支付；已支付事实保持不变。
                        connection.execute(
                            "UPDATE pilot_obligations SET status='settled', "
                            "settled_by_type='pilot_termination', settled_by_id=?, settled_at=? "
                            "WHERE obligation_id=?",
                            (termination_id, now, row["obligation_id"]),
                        )
                        cancelled_obligation_ids.append(row["obligation_id"])
                    else:
                        # 返工、返还、补偿、退款等未完成义务继续有效，快照进终止记录。
                        surviving_obligation_ids.append(row["obligation_id"])
                connection.execute(
                    "INSERT INTO pilot_terminations(termination_id,agreement_id,reason,"
                    "open_obligation_ids_json,created_by,created_at) VALUES(?,?,?,?,?,?)",
                    (termination_id, agreement_id, reason,
                     canonical_json(surviving_obligation_ids), actor_id, now),
                )
                connection.execute(
                    "UPDATE pilot_agreements SET status='terminated' WHERE agreement_id=?",
                    (agreement_id,),
                )
                self._audit(connection, actor_id=actor_id, action="pilot.agreement.terminated",
                            resource_type="pilot_agreement", resource_id=agreement_id,
                            detail={"reason": reason, "termination_id": termination_id,
                                    "surviving_obligation_ids": surviving_obligation_ids,
                                    "cancelled_payment_obligation_ids": cancelled_obligation_ids})
                return {"resource_type": "pilot_agreement_termination", "resource_id": termination_id,
                        "termination_id": termination_id, "agreement_id": agreement_id,
                        "status": "terminated", "reason": reason,
                        "open_obligation_ids": surviving_obligation_ids,
                        "cancelled_payment_obligation_ids": cancelled_obligation_ids}

            return self._idempotent(connection, request_id=request_id,
                                    action="pilot.terminate_agreement", payload=payload, create=create)

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #
    def _affected_batch_ids(self, connection, agreement_id: str, material_ids: list[str]) -> list[str]:
        affected: list[str] = []
        for row in connection.execute(
            "SELECT batch_id, material_ids_json FROM pilot_batches WHERE agreement_id=? "
            "AND status IN ('released','in_production') ORDER BY released_at, batch_id",
            (agreement_id,),
        ):
            used = set(json.loads(row["material_ids_json"]))
            if used.intersection(material_ids):
                affected.append(row["batch_id"])
        return affected

    def product_lineage(self, agreement_id: str, batch_id: str) -> dict[str, Any]:
        """说明一批产品依据哪套工艺、哪次授权（签收门）与哪些原料生产。"""

        with self.database.transaction() as connection:
            agreement = self._agreement_row(connection, agreement_id)
            batch = self._batch_row(connection, agreement_id, batch_id)
            version = connection.execute(
                "SELECT * FROM pilot_process_versions WHERE version_id=?", (batch["version_id"],)
            ).fetchone()
            gate = None
            if batch["gate_id"]:
                gate = connection.execute(
                    "SELECT * FROM pilot_scale_gates WHERE gate_id=?", (batch["gate_id"],)
                ).fetchone()
            materials = []
            for material_id in json.loads(batch["material_ids_json"]):
                row = connection.execute(
                    "SELECT * FROM pilot_materials WHERE material_id=?", (material_id,)
                ).fetchone()
                if row is not None:
                    materials.append({"material_id": row["material_id"], "code": row["code"],
                                      "name": row["name"], "maker": row["maker"],
                                      "batch_no": row["batch_no"], "status": row["status"],
                                      "substituted_by_material_id": row["substituted_by_material_id"]})
            ip_terms = [{"ip_id": r["ip_id"], "ownership": r["ownership"],
                         "scope": json.loads(r["scope_json"])}
                        for r in connection.execute(
                            "SELECT * FROM pilot_ip_terms WHERE agreement_id=? ORDER BY created_at",
                            (agreement_id,))]
            return {
                "agreement_id": agreement_id,
                "agreement_status": agreement["status"],
                "batch_id": batch_id,
                "batch_no": batch["batch_no"],
                "stage": batch["stage"],
                "batch_status": batch["status"],
                "process_version": {
                    "version_id": version["version_id"], "stage": version["stage"],
                    "seq": version["seq"], "status": version["status"],
                    "formulation_hash": version["formulation_hash"],
                    "scale_params": json.loads(version["scale_params_json"]),
                    "scale_tolerance": json.loads(version["scale_tolerance_json"]),
                    "allowed_material_specs": json.loads(version["allowed_material_specs_json"]),
                    "equipment_capabilities": json.loads(version["equipment_capabilities_json"]),
                    "supersedes_version_id": version["supersedes_version_id"],
                    "frozen_at": version["frozen_at"],
                },
                "authorization": None if gate is None else {
                    "gate_id": gate["gate_id"], "stage": gate["stage"],
                    "status": gate["status"], "version_id": gate["version_id"],
                    "tech_confirmation_id": gate["tech_confirmation_id"],
                    "production_confirmation_id": gate["production_confirmation_id"],
                    "passed_at": gate["passed_at"],
                },
                "materials": materials,
                "license_scope": json.loads(agreement["license_scope_json"]),
                "ip_terms": ip_terms,
            }

    def batch_dispositions(self, agreement_id: str, batch_id: str) -> dict[str, Any]:
        """说明一批产品的偏差由谁处置、处置结论与派生义务。"""

        with self.database.transaction() as connection:
            self._agreement_row(connection, agreement_id)
            batch = self._batch_row(connection, agreement_id, batch_id)
            items = []
            for row in connection.execute(
                "SELECT * FROM pilot_deviations WHERE agreement_id=? AND batch_id=? ORDER BY created_at",
                (agreement_id, batch_id),
            ):
                obligations = [{"obligation_id": r["obligation_id"], "kind": r["kind"],
                                "party": r["party"], "amount": r["amount"],
                                "currency": r["currency"], "status": r["status"]}
                               for r in connection.execute(
                                   "SELECT * FROM pilot_obligations WHERE source_type='pilot_deviation' "
                                   "AND source_id=?", (row["deviation_id"],))]
                items.append({"deviation_id": row["deviation_id"], "code": row["code"],
                              "classification": row["classification"],
                              "responsible_party": row["responsible_party"],
                              "disposition": row["disposition"], "closed_at": row["closed_at"],
                              "detail": json.loads(row["detail_json"]), "obligations": obligations})
            recall = connection.execute(
                "SELECT obligation_id FROM pilot_obligations WHERE agreement_id=? "
                "AND source_type='pilot_batch_recall' AND source_id=?",
                (agreement_id, batch_id),
            ).fetchone()
            return {"agreement_id": agreement_id, "batch_id": batch_id,
                    "batch_no": batch["batch_no"], "batch_status": batch["status"],
                    "deviations": items,
                    "recall": None if recall is None else {
                        "recalled": True, "return_obligation_id": recall["obligation_id"]}}

    def financial_position(self, agreement_id: str) -> dict[str, Any]:
        """汇总尚有哪些付款或返还义务，以及不可改写的付款流水。"""

        with self.database.transaction() as connection:
            agreement = self._agreement_row(connection, agreement_id)
            open_obligations = []
            totals: dict[str, dict[str, float]] = {}
            for row in connection.execute(
                "SELECT * FROM pilot_obligations WHERE agreement_id=? AND status='open' "
                "ORDER BY created_at, obligation_id", (agreement_id,)):
                open_obligations.append({"obligation_id": row["obligation_id"], "kind": row["kind"],
                                         "party": row["party"], "amount": row["amount"],
                                         "currency": row["currency"],
                                         "source_type": row["source_type"],
                                         "source_id": row["source_id"]})
                bucket = totals.setdefault(row["currency"], {"payment_due": 0.0, "compensation": 0.0,
                                                             "rework": 0.0, "return": 0.0,
                                                             "refund": 0.0})
                bucket[row["kind"]] += row["amount"]
            payments = [{"payment_id": r["payment_id"], "ref": r["ref"], "milestone": r["milestone"],
                         "stage": r["stage"], "amount": r["amount"], "currency": r["currency"],
                         "direction": r["direction"], "created_at": r["created_at"]}
                        for r in connection.execute(
                            "SELECT * FROM pilot_payments WHERE agreement_id=? ORDER BY created_at",
                            (agreement_id,))]
            termination = connection.execute(
                "SELECT * FROM pilot_terminations WHERE agreement_id=?", (agreement_id,)
            ).fetchone()
            return {"agreement_id": agreement_id, "agreement_status": agreement["status"],
                    "open_obligations": open_obligations, "open_totals": totals,
                    "payments": payments,
                    "terminated": termination is not None,
                    "termination_reason": None if termination is None else termination["reason"]}

    def change_impact(self, *, agreement_id: str, material_id: str | None = None,
                      version_id: str | None = None) -> dict[str, Any]:
        """追踪一次原料替换或工艺版本变更波及的全部在制批次。"""

        with self.database.transaction() as connection:
            self._agreement_row(connection, agreement_id)
            if not material_id and not version_id:
                raise ValidationError("必须提供 material_id 或 version_id")
            impacted: list[dict[str, Any]] = []
            rows = connection.execute(
                "SELECT * FROM pilot_batches WHERE agreement_id=? AND status IN ('released',"
                "'in_production') ORDER BY released_at, batch_id", (agreement_id,))
            for row in rows:
                reasons = []
                used_materials = set(json.loads(row["material_ids_json"]))
                if material_id and material_id in used_materials:
                    reasons.append("uses_changed_material")
                if version_id and row["version_id"] == version_id:
                    reasons.append("runs_on_changed_version")
                if not reasons:
                    superseded = connection.execute(
                        "WITH RECURSIVE ancestry(version_id) AS ("
                        "SELECT supersedes_version_id FROM pilot_process_versions WHERE version_id=? "
                        "UNION ALL SELECT p.supersedes_version_id FROM pilot_process_versions p "
                        "JOIN ancestry a ON p.version_id=a.version_id) "
                        "SELECT 1 FROM ancestry WHERE version_id=?",
                        (version_id, row["version_id"]),
                    ).fetchone() if version_id else None
                    if superseded:
                        reasons.append("runs_on_superseded_version_lineage")
                if reasons:
                    impacted.append({"batch_id": row["batch_id"], "batch_no": row["batch_no"],
                                     "stage": row["stage"], "version_id": row["version_id"],
                                     "status": row["status"], "reasons": reasons})
            return {"agreement_id": agreement_id, "material_id": material_id,
                    "version_id": version_id, "impacted_batches": impacted}
