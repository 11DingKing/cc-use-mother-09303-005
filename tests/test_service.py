"""领域服务集成测试：直接驱动 PlacementService，覆盖全部业务承诺。"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from internship.db import connect, init_db
from internship.errors import (
    ConflictError,
    IdempotencyConflictError,
    NotFoundError,
    PermissionError,
    ValidationError,
)
from internship.service import PlacementService


class World:
    """构造测试世界：2 院校、2 企业、学生/导师/批次/对口关系。"""

    def __init__(self, path: str) -> None:
        self.svc = PlacementService(connect(path))
        self.school_a = self.svc.create_organization("school", "甲方学院")["id"]
        self.school_b = self.svc.create_organization("school", "乙方学院")["id"]
        self.company_x = self.svc.create_organization("company", "星辰科技")["id"]
        self.company_y = self.svc.create_organization("company", "远洋集团")["id"]

        self.stu1 = self.svc.create_person(self.school_a, "student", "学生一")["id"]
        self.stu2 = self.svc.create_person(self.school_a, "student", "学生二")["id"]
        self.stu3 = self.svc.create_person(self.school_b, "student", "学生三")["id"]
        self.mentor_m = self.svc.create_person(self.company_x, "mentor", "导师M")["id"]
        self.mentor_n = self.svc.create_person(self.company_x, "mentor", "导师N")["id"]
        self.mentor_y = self.svc.create_person(self.company_y, "mentor", "导师Y")["id"]

        self.svc.add_partnership(self.company_x, self.school_a)
        # school_b 与 company_x 没有对口关系

    def open_batch(self, capacity: int = 3, mentor_caps: dict | None = None) -> str:
        bid = self.svc.create_batch(self.company_x, "跨境实习批次", capacity)["id"]
        for mentor_id, cap in (mentor_caps or {self.mentor_m: 5, self.mentor_n: 5}).items():
            self.svc.set_mentor_capacity(mentor_id, bid, cap)
        return bid

    def apply_confirm(self, batch_id: str, student_id: str,
                      qual: dict | None = None) -> str:
        qual = qual or {"gpa": 3.5, "language": "B2", "term": "2026秋"}
        app = self.svc.create_application(self.school_a, batch_id, student_id, qual)
        self.svc.company_confirm(self.company_x, app["id"], self.mentor_m)
        return app["id"]


class ServiceTestBase(unittest.TestCase):
    def setUp(self) -> None:
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        init_db(connect(self.db_path))
        self.w = World(self.db_path)

    def tearDown(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            p = Path(self.db_path + suffix)
            if p.exists():
                p.unlink()

    def fresh_service(self) -> PlacementService:
        """模拟服务重启：用同一数据库文件建立新连接与新服务实例。"""
        return PlacementService(connect(self.db_path))


class TestHappyPath(ServiceTestBase):
    def test_full_chain_admit_to_complete(self) -> None:
        w = self.w
        bid = w.open_batch()
        app_id = w.apply_confirm(bid, w.stu1)
        p = w.svc.admit(w.school_a, app_id, w.mentor_m)

        self.assertEqual(p["status"], "admitted")
        self.assertEqual(p["seat_no"], 1)
        self.assertEqual(p["seat_status"], "held")

        acc = w.svc.batch_account(w.company_x, bid)
        self.assertEqual(acc["seats_held"], 1)
        self.assertEqual(acc["seats_available"], acc["seat_capacity"] - 1)
        mentor = next(m for m in acc["mentor_loads"] if m["mentor_id"] == w.mentor_m)
        self.assertEqual(mentor["used"], 1)

        w.svc.start_performance(w.school_a, p["id"], w.stu1)
        w.svc.add_evidence(w.school_a, p["id"], "attendance", "考勤全勤", w.stu1)
        w.svc.add_evidence(w.company_x, p["id"], "report", "结题报告通过", w.mentor_m)
        w.svc.request_assessment(w.school_a, p["id"], w.stu1)
        done = w.svc.complete(w.company_x, p["id"], w.mentor_m)
        self.assertEqual(done["status"], "completed")

        timeline = w.svc.placement_timeline(w.school_a, p["id"])
        types = [e["event_type"] for e in timeline["events"]]
        self.assertEqual(types, ["admitted", "started", "assessment_requested",
                                 "completed"])
        self.assertEqual(len(timeline["evidences"]), 2)
        self.assertTrue(w.svc.reconcile()["ok"])

    def test_partial_completion_needs_evidence(self) -> None:
        w = self.w
        bid = w.open_batch()
        app_id = w.apply_confirm(bid, w.stu1)
        p = w.svc.admit(w.school_a, app_id, w.mentor_m)
        w.svc.start_performance(w.school_a, p["id"], w.stu1)
        with self.assertRaises(ConflictError):
            w.svc.complete(w.school_a, p["id"], w.stu1, partial=True)
        w.svc.add_evidence(w.school_a, p["id"], "report", "完成部分任务清单", w.stu1)
        w.svc.request_assessment(w.school_a, p["id"], w.stu1)
        result = w.svc.complete(w.school_a, p["id"], w.stu1, partial=True)
        self.assertEqual(result["status"], "partial")
        self.assertTrue(w.svc.reconcile()["ok"])


class TestAtomicOccupation(ServiceTestBase):
    def test_mentor_full_rolls_back_seat(self) -> None:
        """导师容量不足时，名额不得被白占（原子占用的关键回归）。"""
        w = self.w
        bid = w.open_batch(capacity=2, mentor_caps={w.mentor_m: 1, w.mentor_n: 5})
        app1 = w.apply_confirm(bid, w.stu1)
        p1 = w.svc.admit(w.school_a, app1, w.mentor_m)
        self.assertEqual(p1["seat_no"], 1)

        app2 = w.apply_confirm(bid, w.stu2)
        with self.assertRaises(ConflictError) as ctx:
            w.svc.admit(w.school_a, app2, w.mentor_m)
        self.assertIn("导师容量不足", str(ctx.exception))

        acc = w.svc.batch_account(w.company_x, bid)
        self.assertEqual(acc["seats_held"], 1)  # 没有残留名额
        self.assertEqual(acc["seats_released"], 0)

    def test_seat_capacity_full(self) -> None:
        w = self.w
        bid = w.open_batch(capacity=1)
        app1 = w.apply_confirm(bid, w.stu1)
        w.svc.admit(w.school_a, app1, w.mentor_m)
        app2 = w.apply_confirm(bid, w.stu2)
        with self.assertRaises(ConflictError) as ctx:
            w.svc.admit(w.school_a, app2, w.mentor_n)
        self.assertIn("批次名额已满", str(ctx.exception))

    def test_concurrent_admits_never_oversell_seats(self) -> None:
        w = self.w
        bid = w.open_batch(capacity=3)
        students = [w.svc.create_person(w.school_a, "student", f"并发学生{i}")["id"]
                    for i in range(8)]
        apps = [w.apply_confirm(bid, s) for s in students]

        results: list = []
        barrier = threading.Barrier(len(apps))

        def worker(app_id: str) -> None:
            svc = PlacementService(connect(self.db_path))
            barrier.wait()
            try:
                svc.admit(w.school_a, app_id, w.mentor_m)
                results.append("ok")
            except ConflictError:
                results.append("conflict")

        threads = [threading.Thread(target=worker, args=(a,)) for a in apps]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(results.count("ok"), 3)
        self.assertEqual(results.count("conflict"), 5)
        acc = w.svc.batch_account(w.company_x, bid)
        self.assertEqual(acc["seats_held"], 3)
        self.assertTrue(w.svc.reconcile()["ok"])

    def test_concurrent_admits_never_oversell_mentor(self) -> None:
        w = self.w
        bid = w.open_batch(capacity=8, mentor_caps={w.mentor_m: 2, w.mentor_n: 8})
        students = [w.svc.create_person(w.school_a, "student", f"导师并发{i}")["id"]
                    for i in range(6)]
        apps = [w.apply_confirm(bid, s) for s in students]
        results: list = []
        barrier = threading.Barrier(len(apps))

        def worker(app_id: str) -> None:
            svc = PlacementService(connect(self.db_path))
            barrier.wait()
            try:
                svc.admit(w.school_a, app_id, w.mentor_m)
                results.append("ok")
            except ConflictError:
                results.append("conflict")

        threads = [threading.Thread(target=worker, args=(a,)) for a in apps]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(results.count("ok"), 2)
        acc = w.svc.batch_account(w.company_x, bid)
        self.assertEqual(acc["seats_held"], 2)  # 导师满导致失败，同样不残留名额
        self.assertTrue(w.svc.reconcile()["ok"])


class TestWithdraw(ServiceTestBase):
    def test_withdraw_releases_both_seat_and_mentor(self) -> None:
        """复现并根治线上事故：退出后岗位与导师容量必须同时释放。"""
        w = self.w
        bid = w.open_batch(capacity=1, mentor_caps={w.mentor_m: 1, w.mentor_n: 1})
        app1 = w.apply_confirm(bid, w.stu1)
        p = w.svc.admit(w.school_a, app1, w.mentor_m)
        w.svc.start_performance(w.school_a, p["id"], w.stu1)

        w.svc.withdraw(w.school_a, p["id"], w.stu1, "提前退出回国")

        acc = w.svc.batch_account(w.company_x, bid)
        self.assertEqual(acc["seats_held"], 0)
        self.assertEqual(acc["seats_released"], 1)
        self.assertEqual(acc["seats_available"], 1)
        mentor = next(m for m in acc["mentor_loads"] if m["mentor_id"] == w.mentor_m)
        self.assertEqual(mentor["used"], 0)
        self.assertTrue(w.svc.reconcile()["ok"])

        # 下一轮录取：新学生重新申请确认后顶上，座位号复用
        stu_new = w.svc.create_person(w.school_a, "student", "补位学生")["id"]
        app2 = w.apply_confirm(bid, stu_new)
        p2 = w.svc.admit(w.school_a, app2, w.mentor_m)
        self.assertEqual(p2["seat_no"], 1)
        acc = w.svc.batch_account(w.company_x, bid)
        self.assertEqual(acc["seats_held"], 1)
        self.assertTrue(w.svc.reconcile()["ok"])

    def test_double_withdraw_rejected(self) -> None:
        w = self.w
        bid = w.open_batch()
        p = w.svc.admit(w.school_a, w.apply_confirm(bid, w.stu1), w.mentor_m)
        w.svc.withdraw(w.school_a, p["id"], w.stu1, "原因")
        with self.assertRaises(ConflictError):
            w.svc.withdraw(w.school_a, p["id"], w.stu1, "再次退出")
        # 导师容量只能归还一次
        acc = w.svc.batch_account(w.company_x, bid)
        self.assertEqual(acc["mentor_loads"][0]["used"], 0)

    def test_readmit_same_application_rejected(self) -> None:
        w = self.w
        bid = w.open_batch()
        app_id = w.apply_confirm(bid, w.stu1)
        p = w.svc.admit(w.school_a, app_id, w.mentor_m)
        w.svc.withdraw(w.school_a, p["id"], w.stu1, "退出")
        with self.assertRaises(ConflictError):
            w.svc.admit(w.school_a, app_id, w.mentor_m)


class TestDeferAndCarryOver(ServiceTestBase):
    def test_defer_keeps_resources_then_resume(self) -> None:
        w = self.w
        bid = w.open_batch()
        p = w.svc.admit(w.school_a, w.apply_confirm(bid, w.stu1), w.mentor_m)
        w.svc.start_performance(w.school_a, p["id"], w.stu1)
        w.svc.defer(w.school_a, p["id"], w.stu1, "签证延迟")
        acc = w.svc.batch_account(w.company_x, bid)
        self.assertEqual(acc["seats_held"], 1)  # 延期继续占用
        w.svc.resume(w.school_a, p["id"], w.stu1)
        self.assertEqual(w.svc.get_placement(w.school_a, p["id"])["status"],
                         "performing")

    def test_carry_over_settles_and_reoccupies(self) -> None:
        w = self.w
        bid1 = w.open_batch()
        p1 = w.svc.admit(w.school_a, w.apply_confirm(bid1, w.stu1), w.mentor_m)
        w.svc.defer(w.school_a, p1["id"], w.stu1, "延期一轮")

        bid2 = w.svc.create_batch(w.company_x, "第二批", 3)["id"]
        w.svc.set_mentor_capacity(w.mentor_m, bid2, 2)
        p2 = w.svc.carry_over(w.school_a, p1["id"], bid2, w.stu1)

        self.assertEqual(p2["status"], "admitted")
        self.assertEqual(p2["batch_id"], bid2)
        self.assertEqual(p2["seat_no"], 1)
        self.assertEqual(p2["student_id"], w.stu1)

        old = w.svc.get_placement(w.school_a, p1["id"])
        self.assertEqual(old["status"], "carried")

        acc1 = w.svc.batch_account(w.company_x, bid1)
        self.assertEqual(acc1["seats_held"], 0)
        self.assertEqual(acc1["seats_carried"], 1)
        self.assertEqual(acc1["mentor_loads"][0]["used"], 0)
        acc2 = w.svc.batch_account(w.company_x, bid2)
        self.assertEqual(acc2["seats_held"], 1)
        self.assertEqual(acc2["mentor_loads"][0]["used"], 1)
        self.assertTrue(w.svc.reconcile()["ok"])

        # 事件链双向可追
        tl1 = w.svc.placement_timeline(w.school_a, p1["id"])
        self.assertEqual(tl1["events"][-1]["event_type"], "carried_over")
        self.assertEqual(tl1["events"][-1]["payload"]["new_placement_id"], p2["id"])
        tl2 = w.svc.placement_timeline(w.school_a, p2["id"])
        self.assertEqual(tl2["events"][0]["payload"]["carried_from_placement"], p1["id"])

    def test_carry_over_rolls_back_when_target_full(self) -> None:
        w = self.w
        bid1 = w.open_batch()
        p1 = w.svc.admit(w.school_a, w.apply_confirm(bid1, w.stu1), w.mentor_m)
        w.svc.defer(w.school_a, p1["id"], w.stu1, "延期")
        bid2 = w.svc.create_batch(w.company_x, "满员批次", 1)["id"]
        w.svc.set_mentor_capacity(w.mentor_m, bid2, 5)
        filler_app = w.apply_confirm(bid2, w.stu2)
        w.svc.admit(w.school_a, filler_app, w.mentor_m)

        with self.assertRaises(ConflictError):
            w.svc.carry_over(w.school_a, p1["id"], bid2, w.stu1)
        # 旧批次资源原样保留
        self.assertEqual(w.svc.get_placement(w.school_a, p1["id"])["status"],
                         "deferred")
        acc1 = w.svc.batch_account(w.company_x, bid1)
        self.assertEqual(acc1["seats_held"], 1)
        self.assertTrue(w.svc.reconcile()["ok"])


class TestMentorReassign(ServiceTestBase):
    def test_reassign_moves_load_atomically(self) -> None:
        w = self.w
        bid = w.open_batch(mentor_caps={w.mentor_m: 1, w.mentor_n: 1})
        p = w.svc.admit(w.school_a, w.apply_confirm(bid, w.stu1), w.mentor_m)
        w.svc.start_performance(w.school_a, p["id"], w.stu1)
        w.svc.reassign_mentor(w.school_a, p["id"], w.mentor_n, w.stu1, "原导师休假")

        acc = w.svc.batch_account(w.company_x, bid)
        loads = {m["mentor_id"]: m["used"] for m in acc["mentor_loads"]}
        self.assertEqual(loads[w.mentor_m], 0)
        self.assertEqual(loads[w.mentor_n], 1)
        tl = w.svc.placement_timeline(w.school_a, p["id"])
        ev = tl["events"][-1]
        self.assertEqual(ev["event_type"], "mentor_reassigned")
        self.assertEqual(ev["payload"]["old_mentor_id"], w.mentor_m)
        self.assertEqual(ev["payload"]["new_mentor_id"], w.mentor_n)
        self.assertTrue(w.svc.reconcile()["ok"])

    def test_reassign_to_full_mentor_changes_nothing(self) -> None:
        w = self.w
        bid = w.open_batch(mentor_caps={w.mentor_m: 1, w.mentor_n: 1})
        p1 = w.svc.admit(w.school_a, w.apply_confirm(bid, w.stu1), w.mentor_n)
        p2 = w.svc.admit(w.school_a, w.apply_confirm(bid, w.stu2), w.mentor_m)
        with self.assertRaises(ConflictError):
            w.svc.reassign_mentor(w.school_a, p2["id"], w.mentor_n, w.stu2, "替换")
        # p2 仍挂在 m 名下
        self.assertEqual(w.svc.get_placement(w.school_a, p2["id"])["mentor_id"],
                         w.mentor_m)


class TestIdempotencyAndRestart(ServiceTestBase):
    def test_same_key_returns_same_application(self) -> None:
        w = self.w
        bid = w.open_batch()
        qual = {"gpa": 3.9}
        a1 = w.svc.create_application(w.school_a, bid, w.stu1, qual,
                                      idempotency_key="key-1")
        a2 = w.svc.create_application(w.school_a, bid, w.stu1, qual,
                                      idempotency_key="key-1")
        self.assertEqual(a1["id"], a2["id"])

    def test_same_key_different_body_conflicts(self) -> None:
        w = self.w
        bid = w.open_batch()
        w.svc.create_application(w.school_a, bid, w.stu1, {"gpa": 3.9},
                                 idempotency_key="key-2")
        with self.assertRaises(IdempotencyConflictError):
            w.svc.create_application(w.school_a, bid, w.stu1, {"gpa": 2.0},
                                     idempotency_key="key-2")

    def test_duplicate_without_key_still_rejected(self) -> None:
        w = self.w
        bid = w.open_batch()
        w.svc.create_application(w.school_a, bid, w.stu1, {"gpa": 3.9})
        with self.assertRaises(ConflictError):
            w.svc.create_application(w.school_a, bid, w.stu1, {"gpa": 3.9})

    def test_idempotency_survives_restart(self) -> None:
        w = self.w
        bid = w.open_batch()
        qual = {"gpa": 3.9}
        a1 = w.svc.create_application(w.school_a, bid, w.stu1, qual,
                                      idempotency_key="restart-key")
        restarted = self.fresh_service()
        a2 = restarted.create_application(w.school_a, bid, w.stu1, qual,
                                          idempotency_key="restart-key")
        self.assertEqual(a1["id"], a2["id"])

        # 重启后重复录取同样被挡：已存在的申请记录被唯一约束拦截
        with self.assertRaises(ConflictError):
            restarted.create_application(w.school_a, bid, w.stu1, {"gpa": 4.0})

    def test_admit_idempotency_survives_restart(self) -> None:
        w = self.w
        bid = w.open_batch()
        app_id = w.apply_confirm(bid, w.stu1)
        p1 = w.svc.admit(w.school_a, app_id, w.mentor_m, idempotency_key="admit-key")
        restarted = self.fresh_service()
        p2 = restarted.admit(w.school_a, app_id, w.mentor_m,
                             idempotency_key="admit-key")
        self.assertEqual(p1["id"], p2["id"])
        acc = w.svc.batch_account(w.company_x, bid)
        self.assertEqual(acc["seats_held"], 1)


class TestIsolation(ServiceTestBase):
    def test_unauthorized_school_cannot_see_or_apply(self) -> None:
        w = self.w
        bid = w.open_batch()
        with self.assertRaises(NotFoundError):
            w.svc.create_application(w.school_b, bid, w.stu3, {"gpa": 3.0})
        with self.assertRaises(NotFoundError):
            w.svc.list_batch_placements(w.school_b, bid)
        with self.assertRaises(NotFoundError):
            w.svc.batch_account(w.school_b, bid)

    def test_other_company_cannot_touch_placement(self) -> None:
        w = self.w
        bid = w.open_batch()
        p = w.svc.admit(w.school_a, w.apply_confirm(bid, w.stu1), w.mentor_m)
        with self.assertRaises(NotFoundError):
            w.svc.get_placement(w.company_y, p["id"])
        with self.assertRaises(NotFoundError):
            w.svc.add_evidence(w.company_y, p["id"], "report", "越权", w.mentor_y)

    def test_company_cannot_confirm_other_companys_application(self) -> None:
        w = self.w
        bid = w.open_batch()
        app = w.svc.create_application(w.school_a, bid, w.stu1, {"gpa": 3.0})
        with self.assertRaises(NotFoundError):
            w.svc.company_confirm(w.company_y, app["id"], w.mentor_y)

    def test_student_must_belong_to_applying_school(self) -> None:
        w = self.w
        bid = w.open_batch()
        with self.assertRaises(ValidationError):
            w.svc.create_application(w.school_a, bid, w.stu3, {"gpa": 3.0})


class TestSnapshotAndRules(ServiceTestBase):
    def test_qualification_is_snapshotted_immutably(self) -> None:
        w = self.w
        bid = w.open_batch()
        qual = {"gpa": 3.2, "language": "B2"}
        app = w.svc.create_application(w.school_a, bid, w.stu1, qual)
        qual["gpa"] = 1.0  # 事后篡改原对象
        qual["new_field"] = "x"
        app_id = app["id"]
        w.svc.company_confirm(w.company_x, app_id, w.mentor_m)
        p = w.svc.admit(w.school_a, app_id, w.mentor_m)
        tl = w.svc.placement_timeline(w.school_a, p["id"])
        snap = tl["events"][0]["payload"]["qualification_snapshot"]
        self.assertEqual(snap["gpa"], 3.2)
        self.assertNotIn("new_field", snap)

    def test_admit_requires_company_confirmation(self) -> None:
        w = self.w
        bid = w.open_batch()
        app = w.svc.create_application(w.school_a, bid, w.stu1, {"gpa": 3.0})
        with self.assertRaises(ConflictError):
            w.svc.admit(w.school_a, app["id"], w.mentor_m)

    def test_illegal_transitions_rejected(self) -> None:
        w = self.w
        bid = w.open_batch()
        p = w.svc.admit(w.school_a, w.apply_confirm(bid, w.stu1), w.mentor_m)
        with self.assertRaises(ConflictError):
            w.svc.request_assessment(w.school_a, p["id"], w.stu1)  # 未开始履约
        with self.assertRaises(ConflictError):
            w.svc.complete(w.school_a, p["id"], w.stu1)

    def test_closed_batch_rejects_new_application_and_admit(self) -> None:
        w = self.w
        bid = w.open_batch()
        app_id = w.apply_confirm(bid, w.stu1)
        w.svc.close_batch(bid)
        with self.assertRaises(ConflictError):
            w.svc.create_application(w.school_a, bid, w.stu2, {"gpa": 3.0})
        with self.assertRaises(ConflictError):
            w.svc.admit(w.school_a, app_id, w.mentor_m)

    def test_cannot_lower_capacity_below_load(self) -> None:
        w = self.w
        bid = w.open_batch(mentor_caps={w.mentor_m: 3})
        w.svc.admit(w.school_a, w.apply_confirm(bid, w.stu1), w.mentor_m)
        with self.assertRaises(ConflictError):
            w.svc.set_mentor_capacity(w.mentor_m, bid, 0)


class TestTraceability(ServiceTestBase):
    def test_seat_trace_follows_reuse_history(self) -> None:
        w = self.w
        bid = w.open_batch(capacity=1)
        p1 = w.svc.admit(w.school_a, w.apply_confirm(bid, w.stu1), w.mentor_m)
        w.svc.withdraw(w.school_a, p1["id"], w.stu1, "退出")
        stu_new = w.svc.create_person(w.school_a, "student", "补位")["id"]
        app2 = w.apply_confirm(bid, stu_new)
        p2 = w.svc.admit(w.school_a, app2, w.mentor_m)

        trace = w.svc.seat_trace(w.school_a, bid, 1)
        self.assertEqual(trace["current_student_id"], stu_new)
        self.assertEqual(len(trace["history"]), 2)
        self.assertEqual(trace["history"][0]["status"], "released")
        self.assertEqual(trace["history"][0]["student_id"], w.stu1)
        self.assertEqual(trace["history"][1]["status"], "held")
        self.assertEqual(trace["history"][1]["student_id"], stu_new)
        self.assertEqual(trace["history"][1]["placement_id"], p2["id"])

    def test_seat_trace_unknown_number_404(self) -> None:
        w = self.w
        bid = w.open_batch()
        with self.assertRaises(NotFoundError):
            w.svc.seat_trace(w.school_a, bid, 99)


if __name__ == "__main__":
    unittest.main()
