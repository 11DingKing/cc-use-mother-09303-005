# 国际实习岗位履约

本项目维护国际实习岗位履约的领域约定、角色边界与样例数据，并在此基础上提供一套
**零三方依赖**（Python 3.11+ 标准库 + SQLite）的录取履约后端，供服务、接口和自动化
验证统一使用。领域覆盖企业导师、合作院校、实习学生三类角色，保证岗位容量、资格快照、
导师占用、履约证据四类约束。

## 解决的核心问题

一批跨境实习学生提前退出后，企业释放了岗位却没有释放导师容量，下一轮录取出现名额账实
不符。后端从模型层根治：

- **资源成对占用 / 成对释放**：岗位名额（`seat_ledger`）与导师负荷
  （`mentor_load_ledger`）是两套独立台账，但只在同一事务内同时变动——授予岗位时一次
  性占用，退出 / 延期结转 / 导师替换时一次性结算，代码中不存在"只释放其一"的路径。
- **统一状态链结算**：录取 → 履约 → 评估 → 完成 / 部分完成；延期、退出、导师替换、
  延期结转全部沿同一条占位状态链迁移，每次迁移写只追加事件。
- **机构严格隔离**：资料仅对申请院校与对口企业可见，跨机构访问一律返回 404（不暴露
  资源是否存在）。
- **重复申请 / 服务重启不重复占位**：持久化幂等键 + 数据库部分唯一索引 +
  `BEGIN IMMEDIATE` 写事务三重保证；并发录取由 SQLite 写锁串行化，容量检查与占用在
  同一事务内。
- **任何名额可追溯**：座位按 `批次 + 座位号` 可查当前学生与历次占用；占位有完整事件流
  （录取、开工、评估、完成、退出、延期、换导师、结转）；`/reconcile` 提供账实核对。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/internship/db.py`：SQLite schema、索引与连接管理。
- `src/internship/service.py`：领域服务（事务、状态机、台账、隔离、追溯、核对）。
- `src/internship/errors.py`：业务错误与 HTTP 状态码映射。
- `src/internship/app.py`：HTTP API 与启动入口。
- `tools/check_contract.py`：命令行摘要检查。
- `tests/`：契约回归、领域服务（31 例）、HTTP 端到端（含真实端口与重启测试）。

## 状态机

```
                 ┌────────────── 延期 ──────────────┐
                 ▼                                   │
录取 admitted → 履约 performing → 评估 assessing → 完成 completed / 部分完成 partial
     │                │                  │
     └── 退出 withdrawn ◄────────────────┘
延期 deferred → 恢复 performing / 退出 withdrawn / 结转 carried（在新批次重新 admitted）
导师替换 mentor_reassigned：履约中任意活跃状态，容量在同事务内换挂
```

延期期间名额与导师容量继续占用；退出释放二者（座位号可被下一轮复用）；结转在旧批次
成对释放、在新批次成对占用，新旧占位通过事件双向关联。

## 启动

```bash
PYTHONPATH=src python3 -m internship.app --db internship.db --host 127.0.0.1 --port 8080
# 或安装后：internship-server
```

请求头：`X-Org-Id`（操作机构）、`X-Actor-Id`（操作人，记入事件流）、
`Idempotency-Key`（创建类请求的幂等键）。

### 主要接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/orgs`、`/orgs/{id}/people`、`/partnerships` | 机构、人员、对口授权 |
| POST | `/companies/{id}/batches` | 企业开放岗位批次 |
| PUT | `/batches/{id}/mentor-capacities` | 配置导师带教容量（不得低于当前负荷） |
| POST | `/schools/{id}/applications` | 院校提交申请，资格材料即时不可变快照 |
| POST | `/applications/{id}/confirm` `/reject` | 企业确认 / 驳回（录取前置） |
| POST | `/schools/{id}/placements` | **录取：单事务占用名额+导师容量** |
| POST | `/placements/{id}/start` `/assess` `/complete[?partial=true]` | 履约状态链 |
| POST | `/placements/{id}/defer` `/resume` `/withdraw` | 延期 / 恢复 / 退出 |
| POST | `/placements/{id}/reassign-mentor` `/carry-over` | 导师替换 / 延期结转 |
| POST/GET | `/placements/{id}/evidences` | 履约证据（完成结算的必要条件） |
| GET | `/placements/{id}/timeline` | 占位 + 事件流 + 证据 |
| GET | `/batches/{id}/account` | 名额账实核对（容量、占用/释放/结转、导师负荷） |
| GET | `/batches/{id}/seats/{seat_no}` | 座位当前学生与历史占用 |
| GET | `/reconcile` | 全局账实核对（名额台账↔占位↔导师负荷） |

## 验证

```bash
# 全部测试（契约 + 31 个领域服务用例 + 5 个 HTTP 端到端用例，含并发超卖与重启）
python3 -m unittest discover -s tests -v

# 编译检查
python3 -m compileall -q src tools tests

# 契约摘要
python3 tools/check_contract.py domain/contract.json
```
