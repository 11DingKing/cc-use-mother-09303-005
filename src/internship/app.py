"""HTTP API（标准库 http.server，零三方依赖）。

鉴权约定（演示后端）：
- 请求头 X-Org-Id 标识操作机构；所有资料按机构隔离，服务层对越权访问返回 404。
- 请求头 X-Actor-Id 记录操作人，写入事件流。
- 创建类请求可用 Idempotency-Key 头实现持久化幂等。

路由：
POST /orgs                                 登记院校/企业
POST /orgs/{id}/people                     登记学生/导师
POST /partnerships                         企业-院校对口授权
POST /companies/{id}/batches              企业开放批次
PUT  /batches/{id}/mentor-capacities       配置导师容量
POST /batches/{id}/close                   关闭批次
POST /schools/{id}/applications           院校提交申请（资格快照）
POST /applications/{id}/confirm            企业确认
POST /applications/{id}/reject             企业驳回
POST /schools/{id}/placements              录取（一次性占名额+导师容量）
GET  /placements/{id}                      占位详情
POST /placements/{id}/start                开始履约
POST /placements/{id}/assess               进入评估
POST /placements/{id}/complete             完成结算（?partial=true 部分完成；需证据）
POST /placements/{id}/defer                延期
POST /placements/{id}/resume               延期后恢复履约
POST /placements/{id}/withdraw             退出（成对释放资源）
POST /placements/{id}/reassign-mentor      导师替换（容量同事务换挂）
POST /placements/{id}/carry-over           延期结转下一批次
POST /placements/{id}/evidences            提交履约证据
GET  /placements/{id}/evidences            证据清单
GET  /placements/{id}/timeline             完整变更链
GET  /batches/{id}/placements              批次占位列表
GET  /batches/{id}/account                 名额账实核对
GET  /batches/{id}/seats/{seat_no}         座位追溯
GET  /reconcile                            全局账实核对（运维）
"""
from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from .db import connect, init_db
from .errors import DomainError, ValidationError
from .service import PlacementService


def _require(body: dict, key: str):
    if key not in body:
        raise ValidationError(f"缺少必填字段：{key}")
    return body[key]


def _as_int(value, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{name} 必须是整数")
    return value


class _Handler(BaseHTTPRequestHandler):
    db_path: str
    _tls = threading.local()

    @property
    def service(self) -> PlacementService:
        # 每个工作线程独享一个 sqlite 连接；写操作由 BEGIN IMMEDIATE 串行化，
        # 多连接通过 WAL + busy_timeout 协作，避免跨线程共用游标。
        conn = getattr(self._tls, "conn", None)
        if conn is None:
            conn = connect(self.db_path)
            init_db(conn)
            self._tls.conn = conn
        return PlacementService(conn)

    # 仅静音默认日志，访问日志由 api 层统一格式输出到 stderr
    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        pass

    # ------------------------------------------------------------- 请求辅助

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise DomainError(f"请求体不是合法 JSON：{exc}") from exc
        if not isinstance(value, dict):
            raise DomainError("请求体必须是 JSON 对象")
        return value

    def _send(self, status: int, payload) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _actor(self, body: dict) -> str:
        return body.pop("actor_id", None) or self.headers.get("X-Actor-Id") or "system"

    def _require_org(self) -> str:
        org = self.headers.get("X-Org-Id")
        if not org:
            raise DomainError("缺少 X-Org-Id 请求头")
        return org

    # ------------------------------------------------------------- 入口分发

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._dispatch("PUT")

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parsed.query
        try:
            status, payload = self._route(method, path, query)
            self._send(status, payload)
        except DomainError as exc:
            self._send(exc.http_status, exc.to_dict())
        except Exception as exc:  # noqa: BLE001 - 兜底，避免连接被静默断开
            self._send(500, {"error": "internal_error", "message": str(exc),
                             "details": {}})

    def _route(self, method: str, path: str, query: str):
        s = self.service
        body = self._body() if method in ("POST", "PUT") else {}
        idem = self.headers.get("Idempotency-Key")
        m = method

        def match(pattern: str):
            return re.fullmatch(pattern, path)

        if m == "POST" and path == "/orgs":
            return 201, s.create_organization(body.get("kind", ""), body.get("name", ""))

        if r := match(r"/orgs/([^/]+)/people"):
            if m == "POST":
                return 201, s.create_person(r.group(1), body.get("kind", ""),
                                            body.get("name", ""))

        if m == "POST" and path == "/partnerships":
            return 200, s.add_partnership(_require(body, "company_id"),
                                          _require(body, "school_id"))

        if r := match(r"/companies/([^/]+)/batches"):
            if m == "POST":
                return 201, s.create_batch(r.group(1), body.get("title", ""),
                                           _as_int(body.get("seat_capacity"),
                                                   "seat_capacity"))

        if r := match(r"/batches/([^/]+)/mentor-capacities"):
            if m == "PUT":
                return 200, s.set_mentor_capacity(_require(body, "mentor_id"),
                                                  r.group(1),
                                                  _as_int(body.get("capacity"),
                                                          "capacity"))

        if r := match(r"/batches/([^/]+)/close"):
            if m == "POST":
                return 200, s.close_batch(r.group(1))

        if r := match(r"/schools/([^/]+)/applications"):
            if m == "POST":
                return 201, s.create_application(
                    r.group(1), _require(body, "batch_id"),
                    _require(body, "student_id"),
                    body.get("qualification", {}), idem)

        if r := match(r"/applications/([^/]+)/(confirm|reject)"):
            if m == "POST":
                org = self._require_org()
                action = r.group(2)
                if action == "confirm":
                    return 200, s.company_confirm(org, r.group(1), self._actor(body),
                                                  body.get("decision_note", ""))
                return 200, s.company_reject(org, r.group(1), self._actor(body),
                                             body.get("reason", ""))

        if r := match(r"/schools/([^/]+)/placements"):
            if m == "POST":
                return 201, s.admit(r.group(1), _require(body, "application_id"),
                                    _require(body, "mentor_id"), idem)

        if r := match(r"/placements/([^/]+)/(start|assess|complete|defer|resume|"
                      r"withdraw|reassign-mentor|carry-over|evidences|timeline)"):
            pid, action = r.group(1), r.group(2)
            org = self._require_org()
            if m == "POST" and action == "start":
                return 200, s.start_performance(org, pid, self._actor(body))
            if m == "POST" and action == "assess":
                return 200, s.request_assessment(org, pid, self._actor(body))
            if m == "POST" and action == "complete":
                partial = bool(body.get("partial", False)) or "partial=true" in query
                return 200, s.complete(org, pid, self._actor(body), partial)
            if m == "POST" and action == "defer":
                return 200, s.defer(org, pid, self._actor(body),
                                    body.get("reason", ""))
            if m == "POST" and action == "resume":
                return 200, s.resume(org, pid, self._actor(body))
            if m == "POST" and action == "withdraw":
                return 200, s.withdraw(org, pid, self._actor(body),
                                       body.get("reason", ""))
            if m == "POST" and action == "reassign-mentor":
                return 200, s.reassign_mentor(org, pid, _require(body, "new_mentor_id"),
                                              self._actor(body), body.get("reason", ""))
            if m == "POST" and action == "carry-over":
                return 200, s.carry_over(org, pid, _require(body, "target_batch_id"),
                                         self._actor(body),
                                         body.get("new_mentor_id"))
            if m == "POST" and action == "evidences":
                return 201, s.add_evidence(org, pid, body.get("kind", "other"),
                                           body.get("content", ""), self._actor(body))
            if m == "GET" and action == "evidences":
                return 200, s.list_evidences(org, pid)
            if m == "GET" and action == "timeline":
                return 200, s.placement_timeline(org, pid)

        if r := match(r"/placements/([^/]+)"):
            if m == "GET":
                return 200, s.get_placement(self._require_org(), r.group(1))

        if r := match(r"/batches/([^/]+)/seats/([0-9]+)"):
            if m == "GET":
                return 200, s.seat_trace(self._require_org(), r.group(1),
                                         int(r.group(2)))

        if r := match(r"/batches/([^/]+)/(placements|account)"):
            bid, action = r.group(1), r.group(2)
            org = self._require_org()
            if m == "GET" and action == "placements":
                return 200, s.list_batch_placements(org, bid)
            if m == "GET" and action == "account":
                return 200, s.batch_account(org, bid)

        if m == "GET" and path == "/reconcile":
            return 200, s.reconcile()

        return 404, {"error": "not_found", "message": f"无此路由：{method} {path}",
                     "details": {}}


def build_server(db_path: str, host: str = "127.0.0.1",
                 port: int = 8080) -> ThreadingHTTPServer:
    # 启动连接只负责初始化 schema；请求连接由各工作线程自行建立。
    init_conn = connect(db_path)
    init_db(init_conn)
    init_conn.close()

    handler = type("Handler", (_Handler,), {"db_path": db_path})
    return ThreadingHTTPServer((host, port), handler)


def main() -> None:
    import argparse
    import os

    parser = argparse.ArgumentParser(description="国际实习岗位履约后端")
    parser.add_argument("--db", default=os.environ.get("INTERNSHIP_DB", "internship.db"))
    parser.add_argument("--host", default=os.environ.get("INTERNSHIP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("INTERNSHIP_PORT", "8080")))
    args = parser.parse_args()

    server = build_server(args.db, args.host, args.port)
    print(f"实习履约后端已启动：http://{args.host}:{args.port}  数据库={args.db}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
