"""HTTP API 端到端测试：真实启动 ThreadingHTTPServer，用 urllib 走完整请求。"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from internship.app import build_server


class ApiTestBase(unittest.TestCase):
    def setUp(self) -> None:
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.server: ThreadingHTTPServer = build_server(self.db_path, "127.0.0.1", 0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        for suffix in ("", "-wal", "-shm"):
            p = Path(self.db_path + suffix)
            if p.exists():
                p.unlink()

    def call(self, method: str, path: str, body: dict | None = None,
             org: str | None = None, actor: str | None = None,
             idem: str | None = None):
        """返回 (status, payload)。"""
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if org:
            req.add_header("X-Org-Id", org)
        if actor:
            req.add_header("X-Actor-Id", actor)
        if idem:
            req.add_header("Idempotency-Key", idem)
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))


class TestEndToEnd(ApiTestBase):
    def _bootstrap(self):
        _, school = self.call("POST", "/orgs", {"kind": "school", "name": "甲方学院"})
        _, company = self.call("POST", "/orgs", {"kind": "company", "name": "星辰科技"})
        _, other_school = self.call("POST", "/orgs",
                                    {"kind": "school", "name": "外人学院"})
        _, stu = self.call("POST", f"/orgs/{school['id']}/people",
                           {"kind": "student", "name": "学生一"})
        _, stu2 = self.call("POST", f"/orgs/{school['id']}/people",
                            {"kind": "student", "name": "学生二"})
        _, mentor = self.call("POST", f"/orgs/{company['id']}/people",
                              {"kind": "mentor", "name": "导师M"})
        self.call("POST", "/partnerships",
                  {"company_id": company["id"], "school_id": school["id"]})
        _, batch = self.call("POST", f"/companies/{company['id']}/batches",
                             {"title": "2026秋批次", "seat_capacity": 1})
        self.call("PUT", f"/batches/{batch['id']}/mentor-capacities",
                  {"mentor_id": mentor["id"], "capacity": 1})
        return {
            "school": school["id"], "company": company["id"],
            "other_school": other_school["id"],
            "stu": stu["id"], "stu2": stu2["id"],
            "mentor": mentor["id"], "batch": batch["id"],
        }

    def test_full_flow_over_http(self) -> None:
        ctx = self._bootstrap()
        # 申请（带幂等键，重放返回同一 id）
        payload = {"batch_id": ctx["batch"], "student_id": ctx["stu"],
                   "qualification": {"gpa": 3.6, "language": "B2"}}
        s1, app1 = self.call("POST", f"/schools/{ctx['school']}/applications",
                             payload, org=ctx["school"], idem="apply-1")
        s2, app2 = self.call("POST", f"/schools/{ctx['school']}/applications",
                             payload, org=ctx["school"], idem="apply-1")
        self.assertEqual((s1, s2), (201, 201))
        self.assertEqual(app1["id"], app2["id"])

        # 企业确认 -> 录取
        self.assertEqual(self.call("POST", f"/applications/{app1['id']}/confirm",
                                   {}, org=ctx["company"], actor=ctx["mentor"])[0], 200)
        status, placement = self.call("POST", f"/schools/{ctx['school']}/placements",
                                      {"application_id": app1["id"],
                                       "mentor_id": ctx["mentor"]},
                                      org=ctx["school"], idem="admit-1")
        self.assertEqual(status, 201)
        pid = placement["id"]
        self.assertEqual(placement["seat_no"], 1)

        # 第二个学生录取失败：名额与导师容量都只有 1
        _, app_b = self.call("POST", f"/schools/{ctx['school']}/applications",
                             {"batch_id": ctx["batch"], "student_id": ctx["stu2"],
                              "qualification": {"gpa": 3.0}},
                             org=ctx["school"])
        self.call("POST", f"/applications/{app_b['id']}/confirm", {},
                  org=ctx["company"])
        status, err = self.call("POST", f"/schools/{ctx['school']}/placements",
                                {"application_id": app_b["id"],
                                 "mentor_id": ctx["mentor"]}, org=ctx["school"])
        self.assertEqual(status, 409)
        self.assertEqual(err["error"], "conflict")

        # 履约 -> 证据 -> 评估 -> 完成
        self.assertEqual(self.call("POST", f"/placements/{pid}/start", {},
                                   org=ctx["school"], actor=ctx["stu"])[0], 200)
        self.assertEqual(self.call("POST", f"/placements/{pid}/evidences",
                                   {"kind": "report", "content": "报告通过"},
                                   org=ctx["company"], actor=ctx["mentor"])[0], 201)
        self.assertEqual(self.call("POST", f"/placements/{pid}/assess", {},
                                   org=ctx["school"])[0], 200)
        status, done = self.call("POST", f"/placements/{pid}/complete", {},
                                 org=ctx["company"])
        self.assertEqual(status, 200)
        self.assertEqual(done["status"], "completed")

        # 账实核对
        status, account = self.call("GET", f"/batches/{ctx['batch']}/account",
                                    org=ctx["school"])
        self.assertEqual(status, 200)
        self.assertEqual(account["seats_held"], 1)
        self.assertEqual(account["mentor_loads"][0]["used"], 1)

        # 时间线
        _, timeline = self.call("GET", f"/placements/{pid}/timeline",
                                org=ctx["school"])
        self.assertEqual([e["event_type"] for e in timeline["events"]],
                         ["admitted", "started", "assessment_requested", "completed"])

        # 座位追溯
        _, trace = self.call("GET", f"/batches/{ctx['batch']}/seats/1",
                             org=ctx["company"])
        self.assertEqual(trace["current_student_id"], ctx["stu"])
        self.assertEqual(len(trace["history"]), 1)

        # 全局核对
        status, rec = self.call("GET", "/reconcile")
        self.assertEqual(status, 200)
        self.assertTrue(rec["ok"], rec)

    def test_withdraw_then_refill_over_http(self) -> None:
        ctx = self._bootstrap()
        _, app = self.call("POST", f"/schools/{ctx['school']}/applications",
                           {"batch_id": ctx["batch"], "student_id": ctx["stu"],
                            "qualification": {"gpa": 3.2}}, org=ctx["school"])
        self.call("POST", f"/applications/{app['id']}/confirm", {},
                  org=ctx["company"])
        _, p = self.call("POST", f"/schools/{ctx['school']}/placements",
                         {"application_id": app["id"], "mentor_id": ctx["mentor"]},
                         org=ctx["school"])
        self.call("POST", f"/placements/{p['id']}/start", {}, org=ctx["school"])
        status, _ = self.call("POST", f"/placements/{p['id']}/withdraw",
                              {"reason": "提前退出"}, org=ctx["school"])
        self.assertEqual(status, 200)

        _, account = self.call("GET", f"/batches/{ctx['batch']}/account",
                               org=ctx["company"])
        self.assertEqual(account["seats_available"], 1)
        self.assertEqual(account["mentor_loads"][0]["used"], 0)

        # 新人补位
        _, app2 = self.call("POST", f"/schools/{ctx['school']}/applications",
                            {"batch_id": ctx["batch"], "student_id": ctx["stu2"],
                             "qualification": {"gpa": 3.8}}, org=ctx["school"])
        self.call("POST", f"/applications/{app2['id']}/confirm", {},
                  org=ctx["company"])
        _, p2 = self.call("POST", f"/schools/{ctx['school']}/placements",
                          {"application_id": app2["id"], "mentor_id": ctx["mentor"]},
                          org=ctx["school"])
        self.assertEqual(p2["seat_no"], 1)
        _, trace = self.call("GET", f"/batches/{ctx['batch']}/seats/1",
                             org=ctx["school"])
        self.assertEqual(trace["current_student_id"], ctx["stu2"])
        self.assertEqual(len(trace["history"]), 2)

    def test_org_isolation_over_http(self) -> None:
        ctx = self._bootstrap()
        _, app = self.call("POST", f"/schools/{ctx['school']}/applications",
                           {"batch_id": ctx["batch"], "student_id": ctx["stu"],
                            "qualification": {"gpa": 3.2}}, org=ctx["school"])
        self.call("POST", f"/applications/{app['id']}/confirm", {},
                  org=ctx["company"])
        _, p = self.call("POST", f"/schools/{ctx['school']}/placements",
                         {"application_id": app["id"], "mentor_id": ctx["mentor"]},
                         org=ctx["school"])
        # 未对口院校：查批次账目、查占位都得到 404
        self.assertEqual(self.call("GET", f"/batches/{ctx['batch']}/account",
                                   org=ctx["other_school"])[0], 404)
        self.assertEqual(self.call("GET", f"/placements/{p['id']}",
                                   org=ctx["other_school"])[0], 404)

    def test_restart_keeps_idempotency(self) -> None:
        ctx = self._bootstrap()
        # 关闭旧服务，用同一 db 文件重启（模拟进程重启）
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.server = build_server(self.db_path, "127.0.0.1", self.port)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

        _, app1 = self.call("POST", f"/schools/{ctx['school']}/applications",
                            {"batch_id": ctx["batch"], "student_id": ctx["stu"],
                             "qualification": {"gpa": 3.2}},
                            org=ctx["school"], idem="persist-key")
        _, app2 = self.call("POST", f"/schools/{ctx['school']}/applications",
                            {"batch_id": ctx["batch"], "student_id": ctx["stu"],
                             "qualification": {"gpa": 3.2}},
                            org=ctx["school"], idem="persist-key")
        self.assertEqual(app1["id"], app2["id"])

    def test_validation_errors(self) -> None:
        # 非法 JSON
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/orgs",
                                     data=b"{bad json", method="POST")
        req.add_header("Content-Type", "application/json")
        try:
            urllib.request.urlopen(req)
            self.fail("应返回 400")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)

        _, school = self.call("POST", "/orgs", {"kind": "school", "name": "S"})
        # 缺少必填字段
        status, err = self.call("POST", f"/schools/{school['id']}/applications",
                                {"batch_id": "x"}, org=school["id"])
        self.assertEqual(status, 400)
        self.assertEqual(err["error"], "validation_error")

        # 缺少 X-Org-Id
        self.assertEqual(self.call("GET", "/reconcile")[0], 200)  # 运维接口不需要
        # 未知路由
        self.assertEqual(self.call("GET", "/nope")[0], 404)


if __name__ == "__main__":
    unittest.main()
