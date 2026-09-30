"""领域服务：录取履约的全部业务规则。

核心承诺：
1. 授予岗位（admit）在单事务内一次性占用全部相关资源：批次名额 + 导师容量。
2. 退出 / 结转 / 导师替换在单事务内成对结算名额台账与导师负荷台账，
   系统中不存在"只释放其一"的代码路径。
3. 状态链唯一：admitted -> performing -> assessing -> completed/partial，
   旁支 deferred（可 resume 或 carry_over）、withdrawn；所有迁移写只追加事件。
4. 机构隔离：院校与对口企业之外的任何主体看不到资料。
5. 幂等：创建类请求支持持久化幂等键；叠加数据库唯一约束，
   重复申请与服务重启都不会产生重复占位。
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any, Iterable

from .errors import (
    ConflictError,
    IdempotencyConflictError,
    NotFoundError,
    PermissionError,
    ValidationError,
)

# 活跃（仍占用名额与导师）状态；与 placements 部分唯一索引保持一致
ACTIVE_STATUSES = (
    "admitted", "performing", "assessing", "completed", "partial", "deferred",
)
SETTLED_STATUSES = ("withdrawn", "carried")

# 允许的状态迁移
TRANSITIONS: dict[str, set[str]] = {
    "admitted": {"performing", "deferred", "withdrawn"},
    "performing": {"assessing", "deferred", "withdrawn"},
    "assessing": {"completed", "partial", "deferred", "withdrawn"},
    "deferred": {"performing", "withdrawn", "carried"},
    "completed": set(),
    "partial": set(),
    "withdrawn": set(),
    "carried": set(),
}


def _uid() -> str:
    return uuid.uuid4().hex


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class PlacementService:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # ------------------------------------------------------------------ 工具

    def _one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, list(params)).fetchone()

    def _execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        try:
            return self.conn.execute(sql, list(params))
        except sqlite3.IntegrityError as exc:
            raise ConflictError(f"数据约束冲突：{exc}") from exc

    def _begin(self) -> None:
        self.conn.execute("BEGIN IMMEDIATE")

    def _commit(self) -> None:
        self.conn.execute("COMMIT")

    def _rollback(self) -> None:
        self.conn.execute("ROLLBACK")

    def _get_or_404(self, table: str, obj_id: str) -> sqlite3.Row:
        row = self._one(f"SELECT * FROM {table} WHERE id = ?", (obj_id,))
        if row is None:
            raise NotFoundError(f"{table} 不存在：{obj_id}")
        return row

    def _idempotent_start(self, org_id: str, scope: str,
                          key: str | None, request_hash: str) -> dict | None:
        """返回已缓存响应表示重放；None 表示首次请求。"""
        if not key:
            return None
        row = self._one(
            "SELECT request_hash, result_body FROM idempotency_keys "
            "WHERE org_id = ? AND scope = ? AND key = ?",
            (org_id, scope, key),
        )
        if row is not None:
            if row["request_hash"] != request_hash:
                raise IdempotencyConflictError("幂等键已被不同的请求体使用")
            return json.loads(row["result_body"])
        return None

    def _idempotent_finish(self, org_id: str, scope: str, key: str | None,
                           request_hash: str, resource_id: str, result: dict) -> None:
        if not key:
            return
        self._execute(
            "INSERT INTO idempotency_keys "
            "(key, org_id, scope, request_hash, resource_id, result_status, result_body) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (key, org_id, scope, request_hash, resource_id, 200, _json_dumps(result)),
        )

    # ----------------------------------------------------------- 基础数据维护

    def create_organization(self, kind: str, name: str) -> dict:
        if kind not in ("school", "company"):
            raise ValidationError("机构类型必须是 school 或 company")
        if not name:
            raise ValidationError("机构名称不能为空")
        org_id = _uid()
        self._execute(
            "INSERT INTO organizations (id, kind, name) VALUES (?, ?, ?)",
            (org_id, kind, name),
        )
        self.conn.commit()
        return {"id": org_id, "kind": kind, "name": name}

    def create_person(self, org_id: str, kind: str, name: str) -> dict:
        if kind not in ("student", "mentor"):
            raise ValidationError("人员类型必须是 student 或 mentor")
        org = self._get_or_404("organizations", org_id)
        expected = "school" if kind == "student" else "company"
        if org["kind"] != expected:
            raise ValidationError(f"{kind} 必须登记在{expected}机构下")
        person_id = _uid()
        self._execute(
            "INSERT INTO people (id, org_id, kind, name) VALUES (?, ?, ?, ?)",
            (person_id, org_id, kind, name),
        )
        self.conn.commit()
        return {"id": person_id, "org_id": org_id, "kind": kind, "name": name}

    def add_partnership(self, company_id: str, school_id: str) -> dict:
        company = self._get_or_404("organizations", company_id)
        school = self._get_or_404("organizations", school_id)
        if company["kind"] != "company" or school["kind"] != "school":
            raise ValidationError("对口关系必须是 企业-院校")
        self._execute(
            "INSERT OR IGNORE INTO company_partnerships (company_id, school_id) VALUES (?, ?)",
            (company_id, school_id),
        )
        self.conn.commit()
        return {"company_id": company_id, "school_id": school_id}

    # ------------------------------------------------------------------ 批次

    def create_batch(self, company_id: str, title: str, seat_capacity: int) -> dict:
        company = self._get_or_404("organizations", company_id)
        if company["kind"] != "company":
            raise ValidationError("只有企业能开放岗位批次")
        if not isinstance(seat_capacity, int) or seat_capacity <= 0:
            raise ValidationError("岗位容量必须是正整数")
        batch_id = _uid()
        self._execute(
            "INSERT INTO batches (id, company_id, title, seat_capacity) VALUES (?, ?, ?, ?)",
            (batch_id, company_id, title, seat_capacity),
        )
        self.conn.commit()
        return {"id": batch_id, "company_id": company_id, "title": title,
                "seat_capacity": seat_capacity, "status": "open"}

    def set_mentor_capacity(self, mentor_id: str, batch_id: str,
                            capacity: int) -> dict:
        mentor = self._get_or_404("people", mentor_id)
        batch = self._get_or_404("batches", batch_id)
        if mentor["kind"] != "mentor" or mentor["org_id"] != batch["company_id"]:
            raise ValidationError("导师必须是本批次企业的导师")
        if capacity < 0:
            raise ValidationError("导师容量不能为负")
        # 不得把容量调到当前负荷之下
        used = self._mentor_load(mentor_id, batch_id)
        if capacity < used:
            raise ConflictError(f"导师当前负荷 {used}，容量不能下调到 {capacity}")
        self._execute(
            "INSERT INTO mentor_capacities (mentor_id, batch_id, capacity) VALUES (?, ?, ?) "
            "ON CONFLICT(mentor_id, batch_id) DO UPDATE SET capacity = excluded.capacity",
            (mentor_id, batch_id, capacity),
        )
        self.conn.commit()
        return {"mentor_id": mentor_id, "batch_id": batch_id, "capacity": capacity}

    def close_batch(self, batch_id: str) -> dict:
        self._get_or_404("batches", batch_id)
        self._execute(
            "UPDATE batches SET status = 'closed', "
            "closed_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ?",
            (batch_id,),
        )
        self.conn.commit()
        return {"id": batch_id, "status": "closed"}

    def _assert_partnership(self, company_id: str, school_id: str) -> None:
        row = self._one(
            "SELECT 1 FROM company_partnerships WHERE company_id = ? AND school_id = ?",
            (company_id, school_id),
        )
        if row is None:
            # 对未授权院校不暴露企业批次是否存在
            raise NotFoundError("岗位批次不存在或未对该院校开放")

    # ------------------------------------------------------------- 申请与确认

    def create_application(self, org_id: str, batch_id: str, student_id: str,
                           qualification: dict, idempotency_key: str | None = None) -> dict:
        if not isinstance(qualification, dict) or not qualification:
            raise ValidationError("资格材料必须是非空对象，录取时将原样快照")
        school = self._get_or_404("organizations", org_id)
        if school["kind"] != "school":
            raise PermissionError("只有合作院校能提交申请")
        batch = self._get_or_404("batches", batch_id)
        student = self._get_or_404("people", student_id)
        if student["kind"] != "student" or student["org_id"] != org_id:
            raise ValidationError("学生必须属于申请院校")
        self._assert_partnership(batch["company_id"], org_id)
        if batch["status"] != "open":
            raise ConflictError("该批次已关闭，不能再申请")

        snapshot = _json_dumps(qualification)
        request_hash = f"{batch_id}|{student_id}|{snapshot}"
        cached = self._idempotent_start(org_id, "application.create",
                                        idempotency_key, request_hash)
        if cached is not None:
            return cached

        self._begin()
        try:
            dup = self._one(
                "SELECT id FROM applications WHERE org_id = ? AND batch_id = ? AND student_id = ?",
                (org_id, batch_id, student_id),
            )
            if dup is not None:
                raise ConflictError("该学生在本批次已有申请，禁止重复申请",
                                    details={"application_id": dup["id"]})
            app_id = _uid()
            self._execute(
                "INSERT INTO applications (id, org_id, batch_id, student_id, "
                "qualification_snapshot, idempotency_key) VALUES (?, ?, ?, ?, ?, ?)",
                (app_id, org_id, batch_id, student_id, snapshot, idempotency_key),
            )
            result = {"id": app_id, "status": "submitted", "batch_id": batch_id,
                      "student_id": student_id, "qualification_snapshot": qualification}
            self._idempotent_finish(org_id, "application.create", idempotency_key,
                                    request_hash, app_id, result)
            self._commit()
            return result
        except Exception:
            self._rollback()
            raise

    def company_confirm(self, company_id: str, application_id: str,
                        confirmed_by: str, decision_note: str = "") -> dict:
        app = self._get_application_for_company(company_id, application_id)
        if app["status"] != "submitted":
            raise ConflictError(f"申请当前状态为 {app['status']}，无法确认")
        self._begin()
        try:
            self._execute(
                "INSERT INTO company_confirmations "
                "(application_id, company_id, confirmed_by, decision_note) "
                "VALUES (?, ?, ?, ?)",
                (application_id, company_id, confirmed_by, decision_note),
            )
            self._execute("UPDATE applications SET status = 'confirmed', "
                          "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ?",
                          (application_id,))
            self._commit()
        except Exception:
            self._rollback()
            raise
        return {"application_id": application_id, "status": "confirmed"}

    def company_reject(self, company_id: str, application_id: str,
                       confirmed_by: str, reason: str = "") -> dict:
        app = self._get_application_for_company(company_id, application_id)
        if app["status"] != "submitted":
            raise ConflictError(f"申请当前状态为 {app['status']}，无法驳回")
        self._execute(
            "UPDATE applications SET status = 'rejected', "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ?",
            (application_id,),
        )
        self.conn.commit()
        return {"application_id": application_id, "status": "rejected", "reason": reason}

    def _get_application_for_company(self, company_id: str,
                                     application_id: str) -> sqlite3.Row:
        app = self._get_or_404("applications", application_id)
        batch = self._get_or_404("batches", app["batch_id"])
        if batch["company_id"] != company_id:
            # 企业之间严格隔离
            raise NotFoundError("申请不存在")
        return app

    def _get_application_for_school(self, org_id: str,
                                    application_id: str) -> sqlite3.Row:
        app = self._get_or_404("applications", application_id)
        if app["org_id"] != org_id:
            raise NotFoundError("申请不存在")
        return app

    # ----------------------------------------------------------- 导师负荷台账

    def _mentor_load(self, mentor_id: str, batch_id: str) -> int:
        """某导师在某批次的当前占用（held 累计 - released 累计）。"""
        row = self._one(
            "SELECT COALESCE(SUM(delta), 0) AS used FROM mentor_load_ledger "
            "WHERE mentor_id = ? AND batch_id = ?",
            (mentor_id, batch_id),
        )
        return int(row["used"])

    def _mentor_capacity(self, mentor_id: str, batch_id: str) -> int:
        row = self._one(
            "SELECT capacity FROM mentor_capacities WHERE mentor_id = ? AND batch_id = ?",
            (mentor_id, batch_id),
        )
        if row is None:
            raise ConflictError("导师尚未在该批次配置带教容量")
        return int(row["capacity"])

    def _hold_mentor(self, placement_id: str, mentor_id: str, batch_id: str) -> None:
        used = self._mentor_load(mentor_id, batch_id)
        capacity = self._mentor_capacity(mentor_id, batch_id)
        if used + 1 > capacity:
            raise ConflictError(
                f"导师容量不足：上限 {capacity}，已占用 {used}",
                details={"mentor_id": mentor_id, "capacity": capacity, "used": used},
            )
        self._execute(
            "INSERT INTO mentor_load_ledger (id, mentor_id, batch_id, placement_id, action, delta) "
            "VALUES (?, ?, ?, ?, 'held', 1)",
            (_uid(), mentor_id, batch_id, placement_id),
        )

    def _release_mentor(self, placement_id: str, mentor_id: str, batch_id: str) -> None:
        """归还导师容量。台账余额为 0 时再释放属于编程错误，直接拒绝。"""
        if self._mentor_load(mentor_id, batch_id) <= 0:
            raise ConflictError("导师负荷台账余额为 0，不能重复归还",
                                details={"mentor_id": mentor_id, "batch_id": batch_id})
        self._execute(
            "INSERT INTO mentor_load_ledger (id, mentor_id, batch_id, placement_id, action, delta) "
            "VALUES (?, ?, ?, ?, 'released', -1)",
            (_uid(), mentor_id, batch_id, placement_id),
        )

    # ----------------------------------------------------------- 名额台账

    def _held_seats(self, batch_id: str) -> int:
        row = self._one(
            "SELECT COUNT(*) AS n FROM seat_ledger WHERE batch_id = ? AND status = 'held'",
            (batch_id,),
        )
        return int(row["n"])

    def _occupy_seat(self, batch_id: str, application_id: str,
                     student_id: str) -> str:
        """占用批次座位：优先复用已释放/结转的最小座位号，否则开新号。

        座位号取值恒在 [1, seat_capacity] 内，容量检查与号码分配在同一写事务。
        """
        batch = self._get_or_404("batches", batch_id)
        held = self._held_seats(batch_id)
        if held >= batch["seat_capacity"]:
            raise ConflictError(
                f"批次名额已满：容量 {batch['seat_capacity']}，已占用 {held}",
                details={"batch_id": batch_id, "capacity": batch["seat_capacity"],
                         "held": held},
            )
        # 取 1..capacity 中第一个未 held 的号码（释放/结转腾出的小号优先复用）
        held_nos = {
            r["seat_no"] for r in self.conn.execute(
                "SELECT seat_no FROM seat_ledger WHERE batch_id = ? AND status = 'held'",
                (batch_id,),
            ).fetchall()
        }
        seat_no = next(n for n in range(1, batch["seat_capacity"] + 1)
                       if n not in held_nos)
        seat_id = _uid()
        self._execute(
            "INSERT INTO seat_ledger (id, batch_id, seat_no, status, "
            "current_application_id, current_student_id) "
            "VALUES (?, ?, ?, 'held', ?, ?)",
            (seat_id, batch_id, seat_no, application_id, student_id),
        )
        return seat_id

    def _release_seat(self, seat_id: str) -> None:
        # 保留占用人信息：该行代表一次已结束的占用，历史可追溯；当前占用人以 status='held' 判定
        self._execute(
            "UPDATE seat_ledger SET status = 'released', "
            "released_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
            "WHERE id = ? AND status = 'held'",
            (seat_id,),
        )

    def _carry_seat(self, seat_id: str) -> None:
        self._execute(
            "UPDATE seat_ledger SET status = 'carried', "
            "released_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
            "WHERE id = ? AND status = 'held'",
            (seat_id,),
        )

    # ==================================================================
    # 录取：一次性占用所有相关资源（名额 + 导师容量 + 占位 + 事件），同生共死
    # ==================================================================

    def admit(self, org_id: str, application_id: str, mentor_id: str,
              idempotency_key: str | None = None) -> dict:
        app = self._get_application_for_school(org_id, application_id)
        if app["status"] != "confirmed":
            raise ConflictError("只有企业已确认的申请才能录取",
                                details={"application_status": app["status"]})
        batch = self._get_or_404("batches", app["batch_id"])
        if batch["status"] != "open":
            raise ConflictError("批次已关闭，不能录取")
        mentor = self._get_or_404("people", mentor_id)
        if mentor["kind"] != "mentor" or mentor["org_id"] != batch["company_id"]:
            raise ValidationError("导师必须是开放批次企业的在岗导师")
        student = self._get_or_404("people", app["student_id"])
        if not student["active"]:
            raise ValidationError("学生档案已停用")

        request_hash = f"admit|{application_id}|{mentor_id}"
        cached = self._idempotent_start(org_id, "placement.admit",
                                        idempotency_key, request_hash)
        if cached is not None:
            return cached

        self._begin()
        try:
            # 重新加锁后读取，排除并发确认回退等情况
            app = self._one("SELECT * FROM applications WHERE id = ?", (application_id,))
            if app["status"] != "confirmed":
                raise ConflictError("只有企业已确认的申请才能录取",
                                    details={"application_status": app["status"]})
            # 同一申请在同一批次至多产生一个占位（退出后重新录取须重新走申请/确认；
            # 延期结转在新批次创建占位，不受此限）
            prior = self._one(
                "SELECT 1 FROM placements WHERE application_id = ? AND batch_id = ?",
                (application_id, batch["id"]),
            )
            if prior is not None:
                raise ConflictError("该申请在本批次已有占位结算记录，不能重复录取")

            # 1) 名额（检查容量在同一写事务内）
            seat_id = self._occupy_seat(batch["id"], app["id"], app["student_id"])
            # 2) 占位主体
            placement_id = _uid()
            self._execute(
                "INSERT INTO placements (id, org_id, company_id, application_id, "
                "batch_id, student_id, mentor_id, seat_id, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'admitted')",
                (placement_id, org_id, batch["company_id"], app["id"],
                 batch["id"], app["student_id"], mentor_id, seat_id),
            )
            # 3) 导师容量（容量不足会抛错，整个事务回滚：名额不会被白占）
            self._hold_mentor(placement_id, mentor_id, batch["id"])
            # 4) 事件流
            self._add_event(placement_id, "admitted", None, "admitted", org_id,
                            {"batch_id": batch["id"], "seat_id": seat_id,
                             "mentor_id": mentor_id,
                             "qualification_snapshot": json.loads(app["qualification_snapshot"])})
            result = self._placement_dict(placement_id)
            self._idempotent_finish(org_id, "placement.admit", idempotency_key,
                                    request_hash, placement_id, result)
            self._commit()
            return result
        except Exception:
            self._rollback()
            raise

    # ------------------------------------------------------------- 状态机迁移

    def _get_placement_for_owner(self, requester_org: str, placement_id: str) -> sqlite3.Row:
        p = self._get_or_404("placements", placement_id)
        if requester_org not in (p["org_id"], p["company_id"]):
            raise NotFoundError("占位不存在")  # 对无关联机构不暴露
        return p

    def _assert_transition(self, current: str, target: str) -> None:
        if target not in TRANSITIONS[current]:
            raise ConflictError(f"非法状态迁移：{current} -> {target}")

    def _add_event(self, placement_id: str, event_type: str, from_status: str | None,
                   to_status: str, actor_id: str, payload: dict) -> None:
        self._execute(
            "INSERT INTO placement_events (placement_id, event_type, from_status, "
            "to_status, actor_id, payload) VALUES (?, ?, ?, ?, ?, ?)",
            (placement_id, event_type, from_status, to_status,
             actor_id, _json_dumps(payload)),
        )

    def _move(self, placement_id: str, target: str, event_type: str,
              actor_id: str, payload: dict) -> dict:
        self._begin()
        try:
            p = self._one("SELECT * FROM placements WHERE id = ?", (placement_id,))
            self._assert_transition(p["status"], target)
            self._execute(
                "UPDATE placements SET status = ?, updated_at = "
                "strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ?",
                (target, placement_id),
            )
            self._add_event(placement_id, event_type, p["status"], target,
                            actor_id, payload)
            self._commit()
            return self._placement_dict(placement_id)
        except Exception:
            self._rollback()
            raise

    def start_performance(self, requester_org: str, placement_id: str,
                          actor_id: str) -> dict:
        p = self._get_placement_for_owner(requester_org, placement_id)
        return self._move(p["id"], "performing", "started", actor_id, {})

    def request_assessment(self, requester_org: str, placement_id: str,
                           actor_id: str) -> dict:
        p = self._get_placement_for_owner(requester_org, placement_id)
        return self._move(p["id"], "assessing", "assessment_requested", actor_id, {})

    def complete(self, requester_org: str, placement_id: str,
                 actor_id: str, partial: bool = False) -> dict:
        p = self._get_placement_for_owner(requester_org, placement_id)
        target = "partial" if partial else "completed"
        event_type = "partial_completed" if partial else "completed"
        # 完成必须有履约证据支撑
        count = self._one("SELECT COUNT(*) AS n FROM evidences WHERE placement_id = ?",
                          (placement_id,))["n"]
        if count == 0:
            raise ConflictError("缺少履约证据，不能进入完成结算")
        return self._move(p["id"], target, event_type, actor_id,
                          {"evidence_count": count})

    def defer(self, requester_org: str, placement_id: str, actor_id: str,
              reason: str) -> dict:
        p = self._get_placement_for_owner(requester_org, placement_id)
        if not reason:
            raise ValidationError("延期必须填写原因")
        # 延期期间资源继续占用；学生沿同一状态链稍后 resume 或结转
        return self._move(p["id"], "deferred", "deferred", actor_id, {"reason": reason})

    def resume(self, requester_org: str, placement_id: str, actor_id: str) -> dict:
        p = self._get_placement_for_owner(requester_org, placement_id)
        return self._move(p["id"], "performing", "started", actor_id,
                          {"resumed": True})

    # ========================================================= 退出：成对释放

    def withdraw(self, requester_org: str, placement_id: str, actor_id: str,
                 reason: str) -> dict:
        """学生提前退出：在同一事务内释放名额与导师容量。"""
        if not reason:
            raise ValidationError("退出必须填写原因")
        p = self._get_placement_for_owner(requester_org, placement_id)
        self._begin()
        try:
            p = self._one("SELECT * FROM placements WHERE id = ?", (placement_id,))
            self._assert_transition(p["status"], "withdrawn")
            if p["mentor_released"]:
                raise ConflictError("导师容量已归还，不能重复结算")
            # 1) 名额释放（可再用于下一轮录取）
            self._release_seat(p["seat_id"])
            # 2) 导师容量释放——与名额在同一事务，杜绝"只释放岗位不释放导师"
            self._release_mentor(p["id"], p["mentor_id"], p["batch_id"])
            self._execute(
                "UPDATE placements SET status = 'withdrawn', mentor_released = 1, "
                "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ?",
                (placement_id,),
            )
            self._add_event(placement_id, "withdrawn", p["status"], "withdrawn",
                            actor_id, {"reason": reason, "seat_id": p["seat_id"],
                                       "mentor_id": p["mentor_id"]})
            self._commit()
            return self._placement_dict(placement_id)
        except Exception:
            self._rollback()
            raise

    # ================================================== 导师替换：容量同事务换挂

    def reassign_mentor(self, requester_org: str, placement_id: str,
                        new_mentor_id: str, actor_id: str, reason: str) -> dict:
        if not reason:
            raise ValidationError("导师替换必须填写原因")
        p = self._get_placement_for_owner(requester_org, placement_id)
        if p["status"] not in ACTIVE_STATUSES or p["status"] in ("completed", "partial"):
            raise ConflictError("仅履约中的占位可以替换导师")
        new_mentor = self._get_or_404("people", new_mentor_id)
        if new_mentor["kind"] != "mentor" or new_mentor["org_id"] != p["company_id"]:
            raise ValidationError("新导师必须属于同一企业")
        if new_mentor_id == p["mentor_id"]:
            raise ValidationError("新导师与当前导师相同")

        self._begin()
        try:
            old_mentor_id = p["mentor_id"]
            # 先占用新导师容量（不足则整体回滚，旧导师不受影响）
            self._hold_mentor(p["id"], new_mentor_id, p["batch_id"])
            # 再归还旧导师容量
            self._release_mentor(p["id"], old_mentor_id, p["batch_id"])
            self._execute(
                "UPDATE placements SET mentor_id = ?, updated_at = "
                "strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ?",
                (new_mentor_id, placement_id),
            )
            self._add_event(placement_id, "mentor_reassigned", p["status"],
                            p["status"], actor_id,
                            {"old_mentor_id": old_mentor_id,
                             "new_mentor_id": new_mentor_id, "reason": reason})
            self._commit()
            return self._placement_dict(placement_id)
        except Exception:
            self._rollback()
            raise

    # ========================================== 延期结转：旧批次结算 + 新批次占用

    def carry_over(self, requester_org: str, placement_id: str,
                   target_batch_id: str, actor_id: str,
                   new_mentor_id: str | None = None) -> dict:
        """把延期占位结转至同一企业的下一批次：旧资源成对结算，新资源成对占用。"""
        p = self._get_placement_for_owner(requester_org, placement_id)
        target = self._get_or_404("batches", target_batch_id)
        if p["status"] != "deferred":
            raise ConflictError("只有延期状态的占位可以结转")
        if target["company_id"] != p["company_id"]:
            raise ValidationError("只能结转至同一企业的批次")
        if target["id"] == p["batch_id"]:
            raise ValidationError("目标批次不能与当前批次相同")
        if target["status"] != "open":
            raise ConflictError("目标批次已关闭")
        self._assert_partnership(p["company_id"], p["org_id"])
        mentor_id = new_mentor_id or p["mentor_id"]
        mentor = self._get_or_404("people", mentor_id)
        if mentor["kind"] != "mentor" or mentor["org_id"] != p["company_id"]:
            raise ValidationError("导师必须属于该企业")

        self._begin()
        try:
            # --- 旧批次结算 ---
            old = self._one("SELECT * FROM placements WHERE id = ?", (placement_id,))
            if old["status"] != "deferred":
                raise ConflictError("占位状态已变化，结转中止")
            self._carry_seat(old["seat_id"])
            self._release_mentor(old["id"], old["mentor_id"], old["batch_id"])
            self._execute(
                "UPDATE placements SET status = 'carried', mentor_released = 1, "
                "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ?",
                (placement_id,),
            )

            # --- 新批次占用（沿用原申请，资格快照不变）---
            new_seat_id = self._occupy_seat(target["id"], old["application_id"],
                                            old["student_id"])
            new_id = _uid()
            self._execute(
                "INSERT INTO placements (id, org_id, company_id, application_id, "
                "batch_id, student_id, mentor_id, seat_id, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'admitted')",
                (new_id, old["org_id"], old["company_id"], old["application_id"],
                 target["id"], old["student_id"], mentor_id, new_seat_id),
            )
            self._hold_mentor(new_id, mentor_id, target["id"])
            self._add_event(placement_id, "carried_over", "deferred", "carried",
                            actor_id, {"target_batch_id": target["id"],
                                       "new_placement_id": new_id,
                                       "new_seat_id": new_seat_id,
                                       "mentor_id": mentor_id})
            self._add_event(new_id, "admitted", None, "admitted", actor_id,
                            {"carried_from_placement": placement_id,
                             "carried_from_batch": old["batch_id"],
                             "seat_id": new_seat_id, "mentor_id": mentor_id})
            self._commit()
            return self._placement_dict(new_id)
        except Exception:
            self._rollback()
            raise

    # ------------------------------------------------------------- 履约证据

    def add_evidence(self, requester_org: str, placement_id: str, kind: str,
                     content: str, submitted_by: str) -> dict:
        if kind not in ("report", "attendance", "evaluation", "other"):
            raise ValidationError("证据类型非法")
        if not content:
            raise ValidationError("证据内容不能为空")
        p = self._get_placement_for_owner(requester_org, placement_id)
        evidence_id = _uid()
        self._execute(
            "INSERT INTO evidences (id, org_id, company_id, placement_id, kind, "
            "content, submitted_by) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (evidence_id, p["org_id"], p["company_id"], placement_id,
             kind, content, submitted_by),
        )
        self.conn.commit()
        return {"id": evidence_id, "placement_id": placement_id, "kind": kind,
                "content": content, "submitted_by": submitted_by}

    def list_evidences(self, requester_org: str, placement_id: str) -> list[dict]:
        self._get_placement_for_owner(requester_org, placement_id)
        rows = self.conn.execute(
            "SELECT * FROM evidences WHERE placement_id = ? ORDER BY id",
            (placement_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------- 查询与追溯

    def _placement_dict(self, placement_id: str) -> dict:
        p = self._get_or_404("placements", placement_id)
        seat = self._one("SELECT seat_no, status AS seat_status FROM seat_ledger WHERE id = ?",
                         (p["seat_id"],))
        mentor_load = self._mentor_load(p["mentor_id"], p["batch_id"])
        d = dict(p)
        d["seat_no"] = seat["seat_no"]
        d["seat_status"] = seat["seat_status"]
        d["mentor_current_load"] = mentor_load
        return d

    def get_placement(self, requester_org: str, placement_id: str) -> dict:
        return self._placement_dict(self._get_placement_for_owner(requester_org,
                                                                  placement_id)["id"])

    def placement_timeline(self, requester_org: str, placement_id: str) -> dict:
        """任何名额的完整变更链：占位 + 只追加事件流 + 证据清单。"""
        p = self._get_placement_for_owner(requester_org, placement_id)
        events = self.conn.execute(
            "SELECT event_type, from_status, to_status, actor_id, payload, created_at "
            "FROM placement_events WHERE placement_id = ? ORDER BY id",
            (placement_id,),
        ).fetchall()
        evidences = self.conn.execute(
            "SELECT id, kind, submitted_by, created_at FROM evidences "
            "WHERE placement_id = ? ORDER BY id",
            (placement_id,),
        ).fetchall()
        return {
            "placement": self._placement_dict(placement_id),
            "events": [
                {**dict(e), "payload": json.loads(e["payload"])} for e in events
            ],
            "evidences": [dict(e) for e in evidences],
        }

    def list_batch_placements(self, requester_org: str, batch_id: str) -> list[dict]:
        batch = self._get_or_404("batches", batch_id)
        if requester_org != batch["company_id"]:
            self._assert_partnership(batch["company_id"], requester_org)
        rows = self.conn.execute(
            "SELECT id FROM placements WHERE batch_id = ? ORDER BY created_at",
            (batch_id,),
        ).fetchall()
        return [self._placement_dict(r["id"]) for r in rows]

    def batch_account(self, requester_org: str, batch_id: str) -> dict:
        """名额账实核对：批次容量、占用/释放/结转、导师负荷分项。"""
        batch = self._get_or_404("batches", batch_id)
        if requester_org != batch["company_id"]:
            self._assert_partnership(batch["company_id"], requester_org)
        acc = self._one("SELECT * FROM v_batch_account WHERE batch_id = ?",
                        (batch_id,))
        mentor_rows = self.conn.execute(
            "SELECT mc.mentor_id, pe.name AS mentor_name, mc.capacity, "
            "COALESCE(SUM(ml.delta), 0) AS used "
            "FROM mentor_capacities mc "
            "JOIN people pe ON pe.id = mc.mentor_id "
            "LEFT JOIN mentor_load_ledger ml "
            "  ON ml.mentor_id = mc.mentor_id AND ml.batch_id = mc.batch_id "
            "WHERE mc.batch_id = ? GROUP BY mc.mentor_id ORDER BY mc.mentor_id",
            (batch_id,),
        ).fetchall()
        return {
            "batch_id": batch_id,
            "title": batch["title"],
            "status": batch["status"],
            "seat_capacity": acc["seat_capacity"],
            "seats_held": acc["seats_held"],
            "seats_released": acc["seats_released"],
            "seats_carried": acc["seats_carried"],
            "seats_available": acc["seat_capacity"] - acc["seats_held"],
            "mentor_loads": [dict(r) for r in mentor_rows],
        }

    def seat_trace(self, requester_org: str, batch_id: str, seat_no: int) -> dict:
        """按批次+座位号追踪当前学生与该号码的完整占用历史（限企业/对口院校）。"""
        batch = self._get_or_404("batches", batch_id)
        if requester_org != batch["company_id"]:
            self._assert_partnership(batch["company_id"], requester_org)
        rows = self.conn.execute(
            "SELECT * FROM seat_ledger WHERE batch_id = ? AND seat_no = ? ORDER BY occupied_at, id",
            (batch_id, seat_no),
        ).fetchall()
        if not rows:
            raise NotFoundError(f"批次 {batch_id} 不存在座位号 {seat_no}")
        current = next((r for r in rows if r["status"] == "held"), None)
        history: list[dict] = []
        for r in rows:
            entry = {
                "seat_ledger_id": r["id"],
                "status": r["status"],
                "student_id": r["current_student_id"],
                "application_id": r["current_application_id"],
                "occupied_at": r["occupied_at"],
                "released_at": r["released_at"],
            }
            placement = self._one("SELECT id FROM placements WHERE seat_id = ?",
                                  (r["id"],))
            if placement is not None:
                entry["placement_id"] = placement["id"]
            history.append(entry)
        return {
            "batch_id": batch_id,
            "seat_no": seat_no,
            "current_student_id": current["current_student_id"] if current else None,
            "current_application_id": current["current_application_id"] if current else None,
            "history": history,
        }

    # ============================================================ 全局账实核对

    def reconcile(self) -> dict:
        """核对名额台账、导师负荷台账与占位状态是否一致。无差异返回 ok。"""
        discrepancies: list[dict] = []

        # 1) 名额状态 <-> 占位状态
        seat_rows = self.conn.execute(
            "SELECT batch_id, status, COUNT(*) AS n FROM seat_ledger GROUP BY batch_id, status"
        ).fetchall()
        seat_counts = {(r["batch_id"], r["status"]): r["n"] for r in seat_rows}
        p_rows = self.conn.execute(
            "SELECT batch_id, status, COUNT(*) AS n FROM placements GROUP BY batch_id, status"
        ).fetchall()
        expected = {"held": set(ACTIVE_STATUSES), "released": {"withdrawn"},
                    "carried": {"carried"}}
        per_batch: dict[str, dict[str, int]] = {}
        for r in p_rows:
            slot = next(s for s, sts in expected.items() if r["status"] in sts)
            per_batch.setdefault(r["batch_id"], {"held": 0, "released": 0, "carried": 0})
            per_batch[r["batch_id"]][slot] += r["n"]
        batch_ids = {b for b, _ in seat_counts} | set(per_batch)
        for bid in batch_ids:
            actual = {s: seat_counts.get((bid, s), 0) for s in expected}
            want = per_batch.get(bid, {"held": 0, "released": 0, "carried": 0})
            if actual != want:
                discrepancies.append({"type": "seat_mismatch", "batch_id": bid,
                                      "ledger": actual, "placements": want})

        # 2) 导师负荷余额 <-> 活跃占位数
        load_rows = self.conn.execute(
            "SELECT mentor_id, batch_id, COALESCE(SUM(delta),0) AS used "
            "FROM mentor_load_ledger GROUP BY mentor_id, batch_id"
        ).fetchall()
        for lr in load_rows:
            active = self._one(
                "SELECT COUNT(*) AS n FROM placements "
                "WHERE mentor_id = ? AND batch_id = ? AND status IN (%s)"
                % ",".join("?" for _ in ACTIVE_STATUSES),
                (lr["mentor_id"], lr["batch_id"], *ACTIVE_STATUSES),
            )["n"]
            if lr["used"] != active:
                discrepancies.append({
                    "type": "mentor_load_mismatch",
                    "mentor_id": lr["mentor_id"], "batch_id": lr["batch_id"],
                    "ledger_used": lr["used"], "active_placements": active,
                })

        # 3) mentor_released 标志位与状态是否自洽
        bad = self.conn.execute(
            "SELECT id, status FROM placements WHERE "
            "(status IN ('withdrawn','carried') AND mentor_released = 0) "
            "OR (status NOT IN ('withdrawn','carried') AND mentor_released = 1)"
        ).fetchall()
        for r in bad:
            discrepancies.append({"type": "release_flag_mismatch",
                                  "placement_id": r["id"], "status": r["status"]})

        # 4) held 名额不得超过批次容量
        over = self.conn.execute(
            "SELECT b.id, b.seat_capacity, COUNT(s.id) AS held FROM batches b "
            "JOIN seat_ledger s ON s.batch_id = b.id AND s.status = 'held' "
            "GROUP BY b.id HAVING held > b.seat_capacity"
        ).fetchall()
        for r in over:
            discrepancies.append({"type": "capacity_exceeded", "batch_id": r["id"],
                                  "capacity": r["seat_capacity"], "held": r["held"]})

        return {"ok": not discrepancies, "discrepancies": discrepancies}
