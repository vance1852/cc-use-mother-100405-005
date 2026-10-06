"""中试转化治理领域的 HTTP/JSON 边界，复用基础库的服务器形态。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from science_strategy_foundation.errors import DomainError, ValidationError
from science_strategy_foundation.service import DomainService
from science_strategy_foundation.storage import Database

from .service import PilotGovernanceService

_FOUNDATION_POST_ROUTES = {
    "/organizations": "register_organization",
    "/actors": "register_actor",
    "/sites": "register_site",
    "/domain-records": "record_domain_data",
}

_POST_ROUTES = {
    "/pilot/projects": "create_project",
    "/pilot/agreements": "register_agreement",
    "/pilot/processes": "create_process",
    "/pilot/processes/freeze": "freeze_process",
    "/pilot/equipment": "register_equipment",
    "/pilot/materials": "register_material",
    "/pilot/materials/discontinue": "mark_material_discontinued",
    "/pilot/supplier-replacements": "replace_supplier",
    "/pilot/gates": "open_gate",
    "/pilot/gates/confirm": "confirm_gate",
    "/pilot/batches": "open_batch",
    "/pilot/inspections": "record_inspection",
    "/pilot/deviations/decide": "decide_deviation",
    "/pilot/deliveries": "deliver_batch",
    "/pilot/recalls": "recall_batch",
    "/pilot/payments/settle": "settle_payment",
    "/pilot/terminations": "terminate_agreement",
}


def route(service: PilotGovernanceService, method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    """把一个 HTTP 语义请求分派到中试治理服务。"""

    headers = headers or {}
    body = body or {}
    parsed = urlparse(path)
    actor_id = headers.get("X-Actor-Id", "")
    query = parse_qs(parsed.query)
    foundation = DomainService(service.database, service.clock)
    try:
        if method == "GET" and parsed.path == "/health":
            from science_strategy_foundation.audit import verify_chain
            ok, events = verify_chain(service.database.connection)
            return 200, {"status": "ok", "audit_valid": ok, "audit_events": events}
        if method == "POST" and parsed.path in _FOUNDATION_POST_ROUTES:
            receipt = getattr(foundation, _FOUNDATION_POST_ROUTES[parsed.path])(
                actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path in _POST_ROUTES:
            method_name = _POST_ROUTES[parsed.path]
            receipt = getattr(service, method_name)(actor_id=actor_id, **body)
            row = service.database.connection.execute(
                "SELECT response_json FROM request_receipts WHERE request_id=?",
                (receipt.request_id,),
            ).fetchone()
            payload = dict(receipt.__dict__)
            if row is not None:
                payload.update(json.loads(row["response_json"]))
            return 200 if receipt.replayed else 201, payload
        if method == "GET" and parsed.path == "/pilot/products/provenance":
            batch_id = query.get("batch_id", [""])[0]
            if not batch_id:
                raise ValidationError("batch_id 不能为空")
            return 200, service.product_provenance(batch_id)
        if method == "GET" and parsed.path == "/pilot/deviations":
            batch_id = query.get("batch_id", [""])[0]
            if not batch_id:
                raise ValidationError("batch_id 不能为空")
            return 200, service.batch_deviations(batch_id)
        if method == "GET" and parsed.path == "/pilot/obligations":
            project_id = query.get("project_id", [""])[0]
            if not project_id:
                raise ValidationError("project_id 不能为空")
            return 200, service.outstanding_obligations(project_id)
        if method == "GET" and parsed.path == "/pilot/change-impact":
            project_id = query.get("project_id", [""])[0]
            if not project_id:
                raise ValidationError("project_id 不能为空")
            return 200, service.change_impact(
                project_id,
                process_id=query.get("process_id", [None])[0],
                material_id=query.get("material_id", [None])[0],
                replacement_id=query.get("replacement_id", [None])[0],
                in_progress_only=query.get("in_progress_only", ["false"])[0] == "true",
            )
        if method == "GET" and parsed.path == "/pilot/batches":
            project_id = query.get("project_id", [""])[0]
            if not project_id:
                raise ValidationError("project_id 不能为空")
            return 200, {"items": service.in_progress_batches(project_id)}
        return 404, {"error": "route_not_found", "message": "接口不存在"}
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


class Handler(BaseHTTPRequestHandler):
    """把标准库 HTTP 请求转换为治理路由调用。"""

    service: PilotGovernanceService

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(400, {"error": "invalid_json", "message": "请求体必须是 UTF-8 JSON"})
            return
        status, payload = route(self.service, self.command, self.path, body,
                                {"X-Actor-Id": self.headers.get("X-Actor-Id", "")})
        self._write(status, payload)

    def _write(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> int:
    """启动中试转化治理 HTTP 服务。"""

    parser = argparse.ArgumentParser(description="启动中试转化治理服务")
    parser.add_argument("--database", default="pilot_governance.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    args = parser.parse_args()
    database = Database(args.database)
    Handler.service = PilotGovernanceService(database)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
