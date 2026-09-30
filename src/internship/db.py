"""数据库连接与 schema 管理（SQLite，标准库实现）。

设计要点：
- 所有写操作走 IMMEDIATE 事务（BEGIN IMMEDIATE），第一时间获取写锁，
  把"检查容量 -> 占用"的竞态串行化，杜绝并发重复占位。
- schema 版本记录在 user_version，启动时幂等迁移。
- 外键约束强制开启。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1

SCHEMA = """
-- 机构：合作院校与企业都登记为机构，通过 kind 区分
CREATE TABLE IF NOT EXISTS organizations (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL CHECK (kind IN ('school', 'company')),
    name        TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- 学生（属于院校）与企业导师（属于企业）
CREATE TABLE IF NOT EXISTS people (
    id          TEXT PRIMARY KEY,
    org_id      TEXT NOT NULL REFERENCES organizations(id),
    kind        TEXT NOT NULL CHECK (kind IN ('student', 'mentor')),
    name        TEXT NOT NULL,
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_people_org ON people(org_id, kind);

-- 岗位批次：企业开放的一轮岗位
CREATE TABLE IF NOT EXISTS batches (
    id              TEXT PRIMARY KEY,
    company_id      TEXT NOT NULL REFERENCES organizations(id),
    title           TEXT NOT NULL,
    seat_capacity   INTEGER NOT NULL CHECK (seat_capacity > 0),
    -- 每个学生在该批次默认占用的导师容量（恒为 1，列出来便于表达约束）
    mentor_per_seat INTEGER NOT NULL DEFAULT 1,
    status          TEXT NOT NULL DEFAULT 'open'
                    CHECK (status IN ('open', 'closed')),
    opened_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    closed_at       TEXT
);

-- 导师容量：某导师在某批次上的带教上限（企业一次性为批次配置）
CREATE TABLE IF NOT EXISTS mentor_capacities (
    mentor_id       TEXT NOT NULL REFERENCES people(id),
    batch_id        TEXT NOT NULL REFERENCES batches(id),
    capacity        INTEGER NOT NULL CHECK (capacity >= 0),
    PRIMARY KEY (mentor_id, batch_id)
);

-- 企业对院校的对口授权：只有被授权院校的学生能看到/申请该企业的批次
CREATE TABLE IF NOT EXISTS company_partnerships (
    company_id      TEXT NOT NULL REFERENCES organizations(id),
    school_id       TEXT NOT NULL REFERENCES organizations(id),
    PRIMARY KEY (company_id, school_id)
);

-- 学生申请。资格信息在创建时快照，之后不随学生档案变化
CREATE TABLE IF NOT EXISTS applications (
    id              TEXT PRIMARY KEY,
    org_id          TEXT NOT NULL REFERENCES organizations(id),   -- 申请院校（隔离属主之一）
    batch_id        TEXT NOT NULL REFERENCES batches(id),
    student_id      TEXT NOT NULL REFERENCES people(id),
    status          TEXT NOT NULL DEFAULT 'submitted'
                    CHECK (status IN ('submitted', 'confirmed', 'rejected', 'withdrawn')),
    -- 资格快照：录取依据的是这份不可变材料
    qualification_snapshot TEXT NOT NULL,
    idempotency_key TEXT,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (org_id, batch_id, student_id)                          -- 同院校同批次同一学生只能一条
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_applications_idem
    ON applications(org_id, idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_applications_batch ON applications(batch_id, status);

-- 企业确认（录取前置条件）。一个申请至多一条确认
CREATE TABLE IF NOT EXISTS company_confirmations (
    application_id  TEXT PRIMARY KEY REFERENCES applications(id),
    company_id      TEXT NOT NULL REFERENCES organizations(id),
    confirmed_by    TEXT NOT NULL,           -- 导师/操作人 id
    decision_note   TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- 名额台账：批次的每个座位的每次占用一行（追加，保留全部历史）
-- 状态：held(占位/录取) / released(学生退出，号码可复用) / carried(延期结转，号码可复用)
-- 同一 (批次, 座位号) 至多一行处于 held
CREATE TABLE IF NOT EXISTS seat_ledger (
    id              TEXT PRIMARY KEY,
    batch_id        TEXT NOT NULL REFERENCES batches(id),
    seat_no         INTEGER NOT NULL,        -- 批次内从 1 开始，释放/结转后可复用
    status          TEXT NOT NULL CHECK (status IN ('held', 'released', 'carried')),
    current_application_id TEXT REFERENCES applications(id),  -- 该次占用对应的申请（释放后仍保留作历史）
    current_student_id     TEXT REFERENCES people(id),        -- 该次占用的学生；当前占用以 status='held' 判定
    occupied_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    released_at     TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_seat_held_no
    ON seat_ledger(batch_id, seat_no) WHERE status = 'held';
CREATE INDEX IF NOT EXISTS idx_seat_batch_no ON seat_ledger(batch_id, seat_no);

-- 占位（录取后沿统一状态链流转的主体）
-- 状态链：admitted(录取) -> performing(履约) -> assessing(评估) -> completed(完成)
--         旁支：deferred(延期) / withdrawn(退出)；carried 表示已结转至下一批次
CREATE TABLE IF NOT EXISTS placements (
    id              TEXT PRIMARY KEY,
    org_id          TEXT NOT NULL REFERENCES organizations(id),   -- 院校属主
    company_id      TEXT NOT NULL REFERENCES organizations(id),   -- 企业属主（两方隔离可见）
    application_id  TEXT NOT NULL REFERENCES applications(id),
    batch_id        TEXT NOT NULL REFERENCES batches(id),
    student_id      TEXT NOT NULL REFERENCES people(id),
    mentor_id       TEXT NOT NULL REFERENCES people(id),
    seat_id         TEXT NOT NULL REFERENCES seat_ledger(id),
    status          TEXT NOT NULL
                    CHECK (status IN ('admitted', 'performing', 'assessing',
                                      'completed', 'partial', 'deferred',
                                      'withdrawn', 'carried')),
    -- 该占位对应的导师容量在退出/结转时是否已归还（防止漏释放的账实核对位）
    mentor_released INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
-- 一个座位同一时刻至多一个活跃占位
CREATE UNIQUE INDEX IF NOT EXISTS idx_placements_seat_active
    ON placements(seat_id) WHERE status IN ('admitted','performing','assessing','completed','partial','deferred');
-- 一个学生在一批次至多一个活跃占位（延期结转前不能在同批重复录取）
CREATE UNIQUE INDEX IF NOT EXISTS idx_placements_student_batch_active
    ON placements(student_id, batch_id) WHERE status IN ('admitted','performing','assessing','completed','partial','deferred');
CREATE INDEX IF NOT EXISTS idx_placements_org ON placements(org_id);
CREATE INDEX IF NOT EXISTS idx_placements_company ON placements(company_id);
CREATE INDEX IF NOT EXISTS idx_placements_mentor_batch ON placements(mentor_id, batch_id);

-- 导师负荷台账：占位对导师容量的每一次占用/归还一行，只追加
-- action: held(占用 1) / released(归还 1)
CREATE TABLE IF NOT EXISTS mentor_load_ledger (
    id              TEXT PRIMARY KEY,
    mentor_id       TEXT NOT NULL REFERENCES people(id),
    batch_id        TEXT NOT NULL REFERENCES batches(id),
    placement_id    TEXT NOT NULL REFERENCES placements(id),
    action          TEXT NOT NULL CHECK (action IN ('held', 'released')),
    delta           INTEGER NOT NULL CHECK (delta IN (1, -1)),
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_mload_mentor_batch ON mentor_load_ledger(mentor_id, batch_id);

-- 履约证据
CREATE TABLE IF NOT EXISTS evidences (
    id              TEXT PRIMARY KEY,
    org_id          TEXT NOT NULL REFERENCES organizations(id),
    company_id      TEXT NOT NULL REFERENCES organizations(id),
    placement_id    TEXT NOT NULL REFERENCES placements(id),
    kind            TEXT NOT NULL CHECK (kind IN ('report', 'attendance', 'evaluation', 'other')),
    content         TEXT NOT NULL,
    submitted_by    TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_evidences_placement ON evidences(placement_id);

-- 占位状态事件流：只追加，任何名额都能据此追到当前学生与全部历史变更
CREATE TABLE IF NOT EXISTS placement_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    placement_id    TEXT NOT NULL REFERENCES placements(id),
    event_type      TEXT NOT NULL
                    CHECK (event_type IN ('admitted','started','assessment_requested',
                                          'completed','partial_completed','withdrawn',
                                          'deferred','mentor_reassigned','carried_over')),
    from_status     TEXT,
    to_status       TEXT NOT NULL,
    actor_id        TEXT NOT NULL,
    payload         TEXT NOT NULL DEFAULT '{}',   -- JSON：原因、证据 id、新旧导师、目标批次等
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_events_placement ON placement_events(placement_id, id);

-- 幂等键：重启后仍能识别重复请求
CREATE TABLE IF NOT EXISTS idempotency_keys (
    key             TEXT NOT NULL,
    org_id          TEXT NOT NULL,
    scope           TEXT NOT NULL,              -- 例如 'application.create'
    request_hash    TEXT NOT NULL,
    resource_id     TEXT,                       -- 成功后回填所创建资源
    result_status   INTEGER,                    -- 首次响应状态码
    result_body     TEXT,                       -- 首次响应体（JSON 字符串）
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY (org_id, scope, key)
);

-- 账实核对视图：每批次名额与导师负荷的账面占用
CREATE VIEW IF NOT EXISTS v_batch_account AS
SELECT b.id AS batch_id,
       b.seat_capacity,
       (SELECT COUNT(*) FROM seat_ledger s
         WHERE s.batch_id = b.id AND s.status = 'held') AS seats_held,
       (SELECT COUNT(*) FROM seat_ledger s
         WHERE s.batch_id = b.id AND s.status = 'released') AS seats_released,
       (SELECT COUNT(*) FROM seat_ledger s
         WHERE s.batch_id = b.id AND s.status = 'carried') AS seats_carried
FROM batches b;
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), isolation_level=None, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """创建表并记录 schema 版本。幂等，可在每次启动时调用。

    全部 DDL 使用 IF NOT EXISTS；executescript 会自行处理事务边界，
    无需外层再包 BEGIN。
    """
    conn.executescript(SCHEMA)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
