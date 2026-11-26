# SQLite 只肯让一个人写

> 一棵没人读的 B 树，一个永远等不到的 idle，和三个不存在的接口。
> 它们都在说同一句话：你以为它在那儿，其实它不在。

上一篇结尾我写的是：

> 最后一篇，收官之战。我们将用 EXPLAIN 拯救慢查询，聊聊 SQLite 的写锁天花板，并看 Playwright 是如何在自动化录屏中扒出系统最后 3 个缺失 API 的。

这句话基本准确。不准确的是比重。

我以为 D7 的主线是“压测”和“优化”。实际跑下来，压测只占了不到三分之一，剩下的时间在做三件更不体面的事：

- 把自己写的一套路由删掉，因为仓库另一条线已经挂了一套；
- 给三个“前端在调、后端没有”的接口补洞；
- 跟一个 200 毫秒的入场动画较劲，因为机器不会等人。

D7 不是继续加功能。D7 是给一个已经“能用”的系统加最后一道保险，然后亲手去撞它。

先摆盘子。

| 维度 | 交付 | 关键数字 |
| :--- | :--- | :--- |
| 后端 · 管理链路 | 用户 CRUD / 重置密码 / 启停 / 角色权限只读 / 审计日志 | 4 角色、16 权限码、6 类审计动作 |
| 后端 · 跨项目批注 | 不传 `project_id` 时按成员关系收敛；`assignee=me`；状态与严重度组合筛选 | 设计师 14 条跨 2 项目；非成员 0 条 |
| 后端 · 工作台 | 9 个标量指标压成 1 条 SQL | 7 次往返 → 1 次 |
| 后端 · 索引 | 迁移 `d7a1b2c3e4f5`：替换 1 条、新增 2 条 | 2.86ms → 0.027ms |
| 后端 · 压测 | locust，20 并发 30s，8 个 uvicorn worker | P95 **31 / 46 / 74ms**，0 失败 |
| 前端 · 管理三页 | 用户管理 / 角色权限矩阵 / 审计日志 | 初始包 gzip **93.49 kB** |
| 前端 · 守卫与反馈 | 页面级权限守卫、Toast 去重、空态与错误态 | 18 条手工链路全绿 |
| E2E | Playwright 一条脚本跑完整条演示 | 1920×1080，真实后端、真实 PDF |

这张表里有一行是假的：**“18 条手工链路全绿”**。

它不是谎报。它只证明了人走的那条路是通的。等我把同样的路交给机器，机器在四个地方停了下来，还顺手扒出三个后端不存在的接口。

先讲五个瞬间：

1. 在 `commit` 和补写审计之间杀掉进程——**项目删了，审计表 0 行**；
2. 任务清单让加三条索引，`EXPLAIN` 说两条都不该加——**其中一条，数据库一次都没选过**；
3. 20 个并发打进来，登录先 500 了——**`database is locked`**；
4. Playwright 在预审中心死等 30 秒——**`networkidle` 永远不会发生**；
5. 录屏点到“采纳为批注”，界面没反应——**接口根本不存在**。

---

## 第一块 · 后端：把“能跑”做成“可交付”

### 1.1 审计写在 commit 之后，等于项目删了但没留痕

**【现象】**

第一版把审计当成日志。删除项目的服务里先提交业务，路由层再开一个 `AsyncSession` 补写审计：

```python
# ❌ 两次提交：中间那段窗口，谁也补不上
await project_service.delete_project(db, project_id, user)
async with AsyncSessionLocal() as audit_db:
    audit_db.add(AuditLog(action="PROJECT_DELETE", ...))
    await audit_db.commit()
```

验收时我故意在两步之间杀掉进程。数据库停在最糟的状态：

- `projects.is_deleted = true`，图纸已经级联不可见；
- `audit_logs` 里干干净净，没有任何人删过这个项目的证据。

反向的场景同样存在。补写失败后如果重试，会出现“业务已经回滚、审计却写着删除成功”的幽灵记录。一个删了没留痕，一个没删却留了痕，两种都比报错难查。

**【排查与解决】**

先不写代码，先选语义。审计日志有两种，必须先选一种：

- **可观测日志**：允许丢，失败不能影响业务；
- **业务凭证**：不能丢，宁可不做也不能不留痕。

`prd-05 §8` 要的是后者。“谁在什么时候删了什么”，是事后追责的依据。只要拆成两次提交，无论怎么加补偿，都存在一个“业务已生效、审计未生效”的窗口。进程被杀、连接断开、磁盘满，都会命中。

所以把审计拉回同一个事务。`audit_service.record()` 只 `add()`，不 `commit()`：

```python
project.is_deleted = True
audit_service.record(
    db,
    action=AuditAction.PROJECT_DELETE,
    target_type="PROJECT",
    user=user,
    target_id=project.id,
    target_name=project.name,
    detail={"cascade": "drawings_soft_deleted"},
)
await db.commit()
```

业务和审计同生共死。提交成功，两条都在；提交失败，两条都不在。

顺手收掉三个细节。

**一，IP 和 UA 不该污染 Service 签名。** 补写用新 session 时，`request.client` 已经拿不到了；把 `Request` 塞进每个 Service 参数，又会让批量调用无法复用。改用 `ContextVar`，在中间件里写一次：

```python
_client_ip.set(request.client.host if request.client else None)
_user_agent.set(request.headers.get("user-agent"))
```

`record()` 从上下文里取，Service 完全不知道 HTTP 的存在。

**二，脱敏放在入口，不靠调用方自觉。** 键名里带 `password / token / hash / secret` 的字段，直接丢弃。验收时专门断言了“审计详情里搜不到初始密码和新密码”——这种事不能指望每个调用点都记得。

**三，时间戳显式写 UTC 微秒。** SQLite 的 `CURRENT_TIMESTAMP` 只有秒精度。同一秒内的“改角色 → 重置密码”会失去先后顺序，而审计的第一价值就是顺序。

代价也要写进注释：**审计插入失败会连带业务失败。** 对“删项目 / 删用户”这类低频高危操作，这是正确取舍。如果 V2 要接外部审计中心，应该改成 outbox 事件，而不是退回“事后补写”。

**【原理与沉淀】**

审计的写入时机，比审计的字段设计重要。

字段少一个，补一个迁移就好；时机错了，丢的是整段时间里的事实，而且丢得没有任何痕迹。日志可以丢，凭证不能丢。这两个词看起来只是命名差别，落到代码上是“两次提交”和“一次提交”的差别。

还有一个更普遍的判断：

> **任何“事后补”的设计，都要问一句：中间那段窗口，谁来负责？**

审计补写、缓存回填、消息重发、索引异步重建，都是同一族。答案通常是“没人”。那就不要留窗口。

---

### 1.2 索引不是按字段清单加的，是按执行计划加的

**【现象】**

任务给的索引清单很具体：

- `(user_id, is_read)`——未读数；
- `(project_id, status)`——项目内批注；
- `(assignee_id, status)`——跨项目批注。

照单全加，是最省事的做法。但索引这个东西，加错了不是“没收益”，是每次写入都要维护一棵没人读的 B 树。所以我先造了合成数据：2 万条通知、5000 条批注，跑 `EXPLAIN QUERY PLAN`。

结果两条都不对。

未读数：D5 已经有 `(user_id, is_read, created_at)`，规划器明确走它。我又按清单加了一条部分索引 `WHERE is_read = false AND merged_into_id IS NULL`，SQLite **一次都没选中**。

跨项目批注：查询同时带 `assignee_id + status` 和 `ORDER BY updated_at DESC`。旧计划里有一行很刺眼：

```text
USE TEMP B-TREE FOR ORDER BY
```

过滤条件有索引，排序没有。数据库先把命中的行找出来，再在内存里排一遍。

**【排查与解决】**

把两条查询拆开看，问题不在“字段有没有索引”，在“整条访问路径有没有被一条索引覆盖”。

数据库选索引，看的是这个顺序：

1. 等值过滤列；
2. 范围过滤列；
3. 排序键。

`(assignee_id, status)` 只解决了第 1 步。`updated_at` 不在索引里，排序只能临时建 B 树。

合成数据上的对比：

| 场景 | 旧计划 | 新计划 | P50 / P95（200 次） |
| :--- | :--- | :--- | :--- |
| 未读 count（2 万通知） | `ix_notifications_user_type_created (user_id=?)` | `ix_notifications_user_read_created (user_id=? AND is_read=?)` | 10.67 / 12.86ms → **4.61 / 5.38ms** |
| 跨项目批注（5000 条） | `ix_annotations_project_severity` + `USE TEMP B-TREE FOR ORDER BY` | `ix_annotations_assignee_status_updated`，无临时 B 树 | 2.86 / 3.27ms → **0.027 / 0.039ms** |

最终迁移 `d7a1b2c3e4f5` 只做三件事：

1. **替换** `(assignee_id, status)` → `(assignee_id, status, updated_at)`。旧索引是新索引的前缀，留着只增加写放大；
2. **新增** `(project_id, status, updated_at)`。项目内视图同样是“状态过滤 + 时间排序”；
3. **新增** `drawings (project_id, status, is_deleted)`。工作台的“待审图纸数”不再走 `ix_drawings_status` 全表扫后回表。

没有新增未读索引。原因和 `EXPLAIN` 证据一起写进了迁移的 docstring：

```python
"""D7 跨项目索引。

不新增未读部分索引：
  D5 的 ix_notifications_user_read_created (user_id, is_read, created_at)
  已覆盖 unread-count；合成 2 万条数据下，部分索引
  WHERE is_read = false AND merged_into_id IS NULL 从未被规划器选中，
  只增加写放大。

降级：恢复 (assignee_id, status)，删除本迁移新增的两条复合索引。
"""
```

半年后的人只会看到索引，不会看到今天的 `EXPLAIN`。所以“为什么不加”必须和“为什么加”写在一起。

**【原理与沉淀】**

索引优化的第一反应不该是“这个字段查得多，加个索引”，而是“这条查询的访问路径长什么样”。

一个更实用的判断顺序：

1. 先看 `EXPLAIN QUERY PLAN`，确认现在走的是哪条索引；
2. 看有没有 `TEMP B-TREE`，有就说明排序没被覆盖；
3. 看等值列、范围列、排序列能不能塞进同一条复合索引；
4. 最后才决定加、换、还是删。

还有两个方言差异值得记住：SQLite 面对部分索引可能直接忽略，PostgreSQL 则可能因为选择率低而选它。**同一条索引，在两个数据库里不是同一个东西。** 所以索引决策必须绑定执行计划和数据分布，不能绑定字段清单。

---

### 1.3 20 个并发打进来：先分清“慢 / 排队 / CPU 满”

**【现象】**

第一次 `locust -u 20 -r 10 -t 30s` 打完，报告里同时出现三件事：

- 一个 `POST /auth/login` 返回 **500**，服务端栈是 `sqlite3.OperationalError: database is locked`，卡在 `UPDATE users SET last_login_at = ...`；
- 三个目标接口 P95 分别是 **350 / 660 / 780ms**，P99 冲到 **5~12s**；
- `/health` 能跑 **398 rps**，而所有碰数据库的接口加起来只有 **~37 rps**。

如果只看“P95 780ms”，很容易得出“SQL 慢”的结论，然后去调查询、加索引。但 `/health` 和数据库接口之间 10 倍的差距，说明问题不在框架，在“每个请求做了什么”。

**【排查与解决】**

拆成三层。

**第一层：错误。** SQLite 的写锁默认不等待，`busy_timeout = 0`。20 个登录同时更新 `last_login_at`，第一个拿到锁，其余立刻报错。WAL 只解决读写并发，不解决写写。

先让它排队，而不是失败：

```python
@event.listens_for(engine.sync_engine, "connect")
def _set_sqlite_pragma(dbapi_conn, _):
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA busy_timeout=5000")
    cur.close()
```

登录 500 消失。

**第二层：每个请求的固定成本。** 单看每条查询 1~3ms，叠在 20 并发上就是排队：

- 鉴权 `select(User)` 触发 selectin 加载 roles + permissions，共 **3 次查询**；
- 工作台 7 个 `count` 是 **7 次往返**；
- 跨项目批注为了项目名、图纸名、版本号又额外跑 **2~3 条**。

对应收口：

```python
# 鉴权：一次 join 取回角色与权限
stmt = (
    select(User)
    .options(joinedload(User.roles).joinedload(Role.permissions))
    .where(User.id == user_id)
)
```

```sql
-- 工作台：9 个标量，一次往返
SELECT
  SUM(CASE WHEN d.status = 'PENDING_REVIEW' THEN 1 ELSE 0 END) AS pending_drawings,
  SUM(CASE WHEN a.assignee_id = :uid AND a.status <> 'CLOSED' THEN 1 ELSE 0 END) AS my_open_annotations,
  ...
FROM drawings d
LEFT JOIN annotations a ON a.drawing_id = d.id
WHERE d.is_deleted = 0;
```

批注列表则把项目名、图纸名、版本号并进主查询，总查询从 6 条降到 3 条（鉴权 / count / 列表）。

**第三层：进程。** 单进程事件循环是单核的。`/health` 400 rps 说明框架没问题；数据库接口 37 rps 说明每个请求约 20ms CPU，单核吞吐就是 50 rps 左右。加并发只增加切换开销，不提高总吞吐。

压测用 8 个 uvicorn worker，同时 `OUTBOX_ENABLED=false`：

```bash
OUTBOX_ENABLED=false uvicorn app.main:app --workers 8 --port 8000
```

为什么要关 outbox：每个 worker 都跑消费者，会放大轮询和锁竞争。生产环境的 outbox 消费者应该是**独立单例进程**，不能跟着 worker 一起复制。

效果：

| 层 | 改动 | 效果 |
| :--- | :--- | :--- |
| SQLite 写锁 | `PRAGMA busy_timeout=5000` | 登录 500 消失；写事务排队而不是失败 |
| 连接池 | `pool_pre_ping` 仅 PostgreSQL 开启；池 10+20 | 单查询 P50 13.2ms → **5.6ms**（10 并发） |
| 鉴权 | `joinedload(roles → permissions)` | 每请求 3 查询 → **1**；P95 37ms → **27ms**（5 并发） |
| 工作台 | 9 个标量压成 1 条 SQL | 7 次往返 → **1**；P95 337ms → **105ms**（5 并发） |
| 跨项目批注 | 名称与版本并进主查询 | 6 → **3** 条；P95 358ms → **239ms**（5 并发） |
| 进程 | 8 个 worker | 总吞吐 37 → **144 rps** |

最终 20 并发：**31 / 46 / 74ms P95，0 失败。**

还有一个容易被误判的点：登录不在验收目标内。bcrypt 12 轮在 20 个用户同时登录时平均 **1366ms**，P95 **2100ms**。这是有意为之的慢哈希，不是接口退化。**压测报告里必须区分“设计如此”和“性能退化”，否则每次都会被同一个数字吓一跳。**

**【原理与沉淀】**

压测第一步不是调 SQL，是分清三件事：

- **查询慢**：单条查询本身就慢，`EXPLAIN` 能看出来；
- **排队**：单条很快，但并发上来后在等锁、等连接、等线程；
- **CPU 满**：代码没问题，单核就是这么多吞吐，加并发只会更慢。

`/health` 和业务接口的对比，是最便宜的判别实验。一个不碰数据库的接口能跑 400 rps，说明框架、网络、事件循环都没问题；碰数据库的接口只有 37 rps，问题一定在每个请求的固定成本上。

---

### 1.4 单进程天花板：86ms 这一格，我压不下去

**【现象】**

8 个 worker 之后，三个目标接口都达标了。但我又跑了一组“不体面”的实验：单进程、紧密循环、不加思考时间。

结果未读数 P95 是 **86ms**。5 个等待者时是 **99ms**。

同样的条件，绕过 HTTP 直接查 SQLite，P95 是 **0.5ms**。

差了 170 多倍。

报表上 31ms 很好看，但 86ms 那一格我盯了很久——它不是“还差一点”，是“怎么压都不动”。

**【排查与解决】**

先确认不是查询问题。直接 SQLite 0.5ms，说明索引、SQL、数据量都没有问题。也不是锁：未读数是纯读，不涉及写锁。

剩下的是固定成本：单核 Python 事件循环 + aiosqlite 的线程切换。压测期间 `_poll` 占了 15% CPU，就是线程池在等结果。每个请求约 20ms CPU，单核吞吐上限就在 50 rps 左右。20 个紧密循环同时打，不是在“并行处理”，是在“排队等同一个核”。

这一格我没有压下去。写进复盘的原话是：

> 单进程 + 紧密循环压测仍打不到未读 50ms（5 等待者时 P95 99ms；20 个紧密循环时 86ms）。这不是查询问题——直接 SQLite 同条件查询 P95 0.5ms——而是单核 Python + aiosqlite 的固定成本。

**【原理与沉淀】**

“达标”要写条件，“没达标”也要写条件。

- 达标的是：20 并发、8 worker、30 秒、三个目标接口；
- 没达标的是：单进程、紧密循环、无思考时间。

如果把这两组数字混在一起说“P95 31ms”，是吹牛；如果说“单进程也能 31ms”，是撒谎。**性能数字离开条件就没有意义。**

更重要的是，86ms 不是“差一点”。它不会因为再优化一条 SQL 变成 30ms，因为瓶颈不在 SQL。它也不会因为再加并发变好，因为总吞吐已经封顶。**它是一个天花板，不是一段距离。**

所以结论要写死：SQLite 只是开发库；生产必须 PostgreSQL + 多 worker；outbox 消费者独立单例。V1.0 的验收口径是“20 并发下达标”，不是“任何条件下都达标”。

---

### 1.5 撞车：另一条线写了重复路由，我删了自己的

**【现象】**

按 `prd-05` 的接口清单，我先落了一套 admin 路由：`POST /users` 返回 `UserOut`，请求字段叫 `roles`，重置密码返回 `{username, password}`。与此同时，仓库里另一条线落了一套功能重叠的 `system` 路由。`main.py` 最终只挂载了一套。

结果是：前端页面能编译、能渲染，但所有写操作都 422 或解析错误。

因为我的 model 是照着 PRD 手写的，不是照着真实响应生成的。

**【排查与解决】**

把两套契约并排放，冲突一目了然：

| 语义 | 我的版本 | 已挂载版本 |
| :--- | :--- | :--- |
| 创建请求里的角色 | `roles` | `role_codes` |
| 初始密码 | 前端传 `password` | 后端生成，响应里 `initial_password` 只出现一次 |
| 重置密码响应 | `{username, password}` | `{user_id, new_password}` |
| 权限列表 | 扁平 16 条 | 按 6 个模块分组，带 `module_text` |

字段都“说得通”。这正是它危险的地方：review 时看不出谁对，TypeScript 也不会报错，只有真调一次接口才会炸。

决策很简单，也很难受：**以已挂载的契约为准，删掉自己重复的路由、服务和 schema。**

然后把前端三处对齐：

- `UserCreateRequest.role_codes` + 可选 `password`（留空由后端 `secrets` 生成）；
- `ResetPasswordOut { user_id, new_password }`；
- `/permissions` 直接返回分组，前端不再维护 `PERMISSION_MODULE_LABEL`。

验证方式也改了。不再“读代码觉得对”，而是三步：

1. `GET /openapi.json` 对一遍字段；
2. 浏览器里真点一遍写操作；
3. 看审计日志有没有落库。

**【原理与沉淀】**

并行开发里，代码归属要让位于契约唯一。

两套都能跑的代码，不是“双保险”，是“双真相”。它们今天字段名不同，明天就会行为不同；等到联调那天，你没有任何依据判断哪套是对的。

所以判断标准不是“谁写得更早”“谁写得更全”，而是“哪套已经挂载、已经被调用、已经产生数据”。**删掉自己能跑的代码，比留下两套各自能跑的代码更专业。**

还有一句要记住：**前端要对真实响应编程，不要对 PRD 里的字段名编程。** PRD 写的是设计意图，实现会给更严格的形态；TypeScript 的类型是断言，不是运行时验证。

---

## 第二块 · 前端与 E2E：把演示交给机器

手工测试 18 条链路全绿之后，我以为前端可以收了。剩下的是“把流程录成视频”。

结果录屏脚本跑了七八遍才过。每一次挂掉，都不是脚本写错，而是“人类会自动补偿、机器不会”的界面契约。

但在把界面交给机器之前，我先花了半天，把三处“假逻辑”收干净。因为自动化的第一价值不是发现新 bug，是让旧 bug 无处可藏。

### 2.0 在交给机器之前，先把三处“假逻辑”收干净

**一，多选角色：一个控件，两个真值源。**

新建用户弹窗里，用户名 / 邮箱 / 初始密码走 `ReactiveFormsModule`，角色是四个 checkbox。第一版把角色也塞进表单，于是三处同时出问题：

- 用 `FormControl<string[]>` + `(change)` 手动 `setValue`，`form.invalid` 永远为 false——“至少选一个角色”这条规则根本不在表单体系里；
- 编辑弹窗用 `patchValue` 回填，但 `selectedRoles` 是另一个 signal，`patchValue` 不会碰它。复选框视觉上全空，提交却带着旧角色；
- 角色接口慢于弹窗打开时，`roles()` 还是空数组，弹窗里一个选项都没有。

后面还踩到字段名：前端 model 写的是 `roles`，后端契约是 `role_codes`。TypeScript 编译通过，单测不存在，只有真点一次才 422。

修法是承认两个真值源的存在，并明确“角色不进表单”：

```ts
// 唯一真值源：selectedRoles signal；表单只管文本字段
protected readonly selectedRoles = signal<GlobalRole[]>([]);

submit(): void {
  this.form.markAllAsTouched();
  if (this.form.invalid || this.passwordError() !== null) return;
  // 表单体系覆盖不到的规则，手写一次
  if (this.selectedRoles().length === 0) {
    this.roleError.set('至少选择一个角色');
    return;
  }
  const payload = { ..., role_codes: this.selectedRoles() };   // 对齐后端契约
}
```

配套三条：页面维护 `rolesLoading / rolesError / roles` 三态，只有 `rolesError` 才提示失败；自己的 ADMIN 锁定在 UI，提交时再兜底补齐；字段名以后端为准，只在一处映射。

这里最通用的教训不是 Angular 表单，是：**“空数组”是一个被重载的语义。** 它同时表示“还在加载”“加载失败”和“确实是空”。用空值判错，必然误报。

**二，权限守卫：刷新内页时 `currentUser()` 是 null。**

`/admin/*` 三页加了页面级守卫，逻辑直接写成“读 `currentUser()`，没有权限就跳 403”。开发时点菜单进去一切正常。

但刷新 `/admin/users` 时，守卫执行早于 `/auth/me` 返回，`currentUser()` 是 `null`，`hasPermission()` 全 false。有权限的管理员被送去 403。

反过来，如果守卫什么都不判断直接放行，设计师又能看到页面骨架，再被接口 403 打回，体验更差。

修法是让守卫自己恢复会话，并且只返回 `UrlTree`：

```ts
export function permissionGuard(...required: string[]): CanActivateFn {
  return () => {
    const auth = inject(AuthService);
    const router = inject(Router);
    return auth.ensureSession().pipe(
      map((session) => {
        if (session !== 'authenticated') {
          return router.createUrlTree(['/login'], { queryParams: { redirect: router.url } });
        }
        return required.every((p) => auth.hasPermission(p)) ? true : router.createUrlTree(['/403']);
      }),
    );
  };
}
```

三条边界：不依赖“父级守卫先跑完”，因为懒加载 chunk 下载和守卫是两条链；跳转返回 `UrlTree`，不用 `router.navigate` 和当前导航打架；`/403` 必须放在 `authGuard` 子树之外，否则无权限的人会在 403 上无限循环。

实测：管理员刷新正常进入；设计师直接输地址 → `/403`；未登录刷新 → `/login?redirect=%2Fadmin%2Fusers`。

**三，Toast 与裸表单：误报、重复，以及“看起来没生效”。**

演示打磨时逐个点按钮，发现三类反馈问题：

- **误报**：刚进用户管理页就点“新建用户”，弹出“角色清单加载失败”——其实只是接口还没返回；
- **重复**：一批并发请求同时失败，同一条错误连弹两三条；
- **看起来没生效**：项目“重命名”弹窗点保存后整页刷新，改名没生效。原因是这个弹窗没有 `[formGroup]`，`(ngSubmit)` 在裸 `<form>` 上不会阻止浏览器默认提交——**原生 GET 提交把整页刷掉了**。

修法三条：

```ts
// Toast 去重：同 kind + 同文案，直接吞掉
const duplicated = this.items().some((i) => i.kind === kind && i.message === message);
if (duplicated) return 0;
```

```html
<!-- 不依赖表单指令是否存在，先关掉原生提交这条路 -->
<form (submit)="$event.preventDefault(); submit()">
```

以及一条分层约定，写进拦截器注释：拦截器只负责登录态与 403 换页，**不弹 Toast**；页面负责把错误翻译成“内联提示”或“Toast”二选一，表单类错误一律内联，避免遮挡输入。

这三个坑看起来分散，其实是一句话：**同一件事不能有两个真值源、两个时机、两个责任人。**

---

### 2.1 SSE 页面永远等不到 `networkidle`

**【现象】**

脚本按惯例进预审中心：

```ts
await page.goto(reviewUrl, { waitUntil: 'networkidle' });
```

30 秒稳定超时。

手工测试时从没感觉——因为人点进去就是看，页面早就渲染完了，谁也没盯着“网络空闲”这个信号。

更尴尬的是第二遍、第三遍跑，超时时间还不一样：Mock 引擎整轮 1 秒跑完，真实引擎要十几秒。

**【排查与解决】**

先看 `networkidle` 的定义：**500ms 内不超过 2 个进行中的请求。**

预审中心用的是 SSE，`text/event-stream` 是一条常驻连接，不会关闭，心跳还会定期写帧。这条连接让“进行中的请求”永远大于 0。所以 `networkidle` 不是“慢”，是**永远不会发生**。

另外两个问题叠在一起：

- 预审中心收到 `report_interrupt` 会自动跳转。你在这儿等 idle 的时候，页面已经换了；
- 骨架屏会骗过可见性断言。问题清单先渲染 8 行占位，`toBeVisible` 对占位行同样成立。

固定 `waitForTimeout(5000)` 也不是解法，它只是把“赌网络快”换成“赌引擎快”。Mock 1 秒、真实 10 秒，5 秒两边都不对。

改成 `domcontentloaded`，剩下的交给**内容锚点**——等业务状态的外在证据，而不是等网络：

```ts
// 1) 任务存在：思考面板只在有 task 之后渲染
//    host 元素是零尺寸，锚点落到内部 section
const thoughtPanel = page.locator('app-thought-panel');
await expect(thoughtPanel.locator('section').first()).toBeVisible({ timeout: 60_000 });
await expect(thoughtPanel.getByText('Agent 思考链路')).toBeVisible();

// 2) 事件真的来了：面板右上角是“{{ lines().length }} 行”，0 行不算
await expect(thoughtPanel.getByText(/[1-9]\d*\s*行/).first()).toBeVisible({ timeout: 30_000 });
```

配套三条：

1. 阶段文案 `正在解析图纸 | 正在核对规范 | 正在归纳问题` 给 8 秒**短超时且允许错过**——Mock 全速时一闪而过，等不到不该让整段录像失败；
2. 超时按“业务最长耗时”给：思考链路 60s、报告就绪 180s，不是按“我印象里多快”给；
3. 轮询的业务结局要分叉：风险弹窗 / 已自动跳报告页 / 还在跑，三种都要能接住，不能只等其中一种。

**【原理与沉淀】**

自动化里的“等”，不是等网络，是等业务状态的外在证据。

`networkidle` 是一个代理指标：它假设“网络安静了，页面就好了”。这个假设在普通页面上成立，在 SSE 页面上不成立。**用一个不成立的代理指标做等待，超时时间设多大都没用。**

内容锚点则直接问业务问题：

- 任务存在吗？→ 思考面板的 `section` 出现了吗？
- 事件到了吗？→ 右上角的“行数”从 0 变成非 0 了吗？
- 报告就绪了吗？→ 报告页的标题出现了吗？

还有一个细节值得单说：**骨架屏是“可见”的，但不是“已加载”的。** `toBeVisible` 只回答“在不在屏幕上”，不回答“是不是真的数据”。所以锚点必须选那种“只有真实数据到了才会出现”的元素，而不是任何占位元素。

---

### 2.2 遮罩层与入场动画：机器没有那 200 毫秒

**【现象】**

风险弹窗一出现，脚本就点“提交并继续执行”，稳定报错：

```text
<div class="fixed inset-0 z-[70] ... backdrop-blur-sm">
  from <app-risk-confirm-modal> subtree intercepts pointer events
```

手工点从来不会遇到。我甚至在旁边看着脚本点，位置分毫不差，那个按钮就在那儿，明明看得见。

同一次调试里还撞到三个变体：

- 上传弹窗里“关闭”有两个：右上角图标按钮 `aria-label="关闭"`，footer 里还有一个按钮。`getByRole('button', { name: '关闭' })` 直接 strict mode violation；
- 对 `app-upload-modal` 本体做 `toBeVisible()` 永远 false；
- “稍后处理”是两步确认，点一次只展开“确认稍后再处理？”，第二次才真正挂起。

**【排查与解决】**

先把“人为什么点得到”说清楚。

人手一次点击是有时间预算的：眼睛看到目标（约 100ms）→ 决定点（约 100ms）→ 手移过去、按下去（100ms 以上）。加起来 200~300ms 是最保守的估计。而弹窗的 `animate-fade-in` 只有 150~200ms。等手指落下去，动画早跑完了。

人还有一个机器没有的反馈环：按下去之前，如果发现目标还在动、被遮住、或者还没变灰，会停一下。机器不会。Playwright 只会在 30 秒里反复重试，然后把重试日志摔在你脸上——**重试不是等待，是把同一个错误重复三十次。**

所以报错不是“点歪了”，是**机器在动画还没结束时就开始点了，而遮罩层在这段时间里确实接收指针事件**。

Playwright 的 actionability 检查有四项：可见、稳定、能接收事件、可用。这里的坑主要藏在“稳定”和“能接收事件”的缝隙里：

- **稳定**要求元素连续两帧的包围盒一致。入场动画里面板在 `translate-y` / `scale`，脚本会等；
- 等它稳定了，**遮罩仍然盖在上面**。半透明不等于不接收指针事件——`bg-black/40 backdrop-blur-sm` 的 `pointer-events` 还是 `auto`；
- 点击坐标落在面板还没覆盖到的区域，命中的就是铺满全屏的遮罩。报错信息里的 “from ... subtree intercepts pointer events”，说的就是这件事。

还有两个 Angular 特有的坑。

**其一，组件的 host 元素是零尺寸盒子。** `<app-upload-modal>` 默认 `display: inline`，里面的弹窗 `fixed inset-0` 铺满屏幕，host 自己的 `getBoundingClientRect()` 全是 0。`toBeVisible()` 基于盒子尺寸判断，所以对它永远返回 false。**锚点必须落到组件内部的语义元素上。**

**其二，按钮文案会随状态变化。** 提交按钮在 busy 时从“提交并继续执行”变成“正在恢复流程…”。按文案定位的 locator，会在成功的那一瞬间失配——你等的那句话已经不存在了。

**修法。**

把“作用域”和“锚点”两件事固定下来：

```ts
// 作用域收窄到对话框内部：同名按钮问题自然消失
const dialog = page.locator('[role="alertdialog"]');
await expect(dialog).toBeVisible();
await dialog.getByRole('button', { name: '确认采纳' }).click();

// 同名按钮显式声明意图
await uploadModal.locator('footer').getByRole('button', { name: '关闭' }).click();
await riskDialog.getByRole('button', { name: '稍后处理' }).first().click();   // 展开确认条
await riskDialog.getByRole('button', { name: '稍后处理' }).last().click();    // 真正挂起
```

`await expect(dialog).toBeVisible()` 不只是“等一下”。它是一条契约断言：**我即将点击的东西，已经在屏幕上，并且是可交互的。** 如果它超时，那不是脚本写错了，是界面在告诉你“这个状态不该被点”。

四条纪律：

1. **断言锚点一律落在组件内部的语义元素**（`role="dialog"` / `section` / `nav[aria-label="面包屑"]`），不对零尺寸 host 做可见性断言；
2. **会变文案的按钮用结构定位**：`footer button.btn-primary`，并在“弹窗已关闭”时容错跳过（`isVisible().catch(() => false)`）；
3. **`force: true` 只在明确知道遮罩语义时用**。它会跳过可操作性检查，把“点不到”变成“点了没反应”，查起来更难；
4. **遮罩类的坑本质是产品交互问题**。如果弹窗必须在动画结束前不可交互，那就该在 DOM 上 `pointer-events: none`，而不是让每个自动化客户端自己猜。

顺带说一句：`page.waitForTimeout(300)` 不是解法。它只是把“赌网络快”换成“赌动画刚好 300ms 结束”，换一台机器、换一个 `prefers-reduced-motion` 设置，又会挂。**等状态，不要等时间。**

**【原理与沉淀】**

这一节最值得记住的不是 locator 写法，是那句话：

> **人手抬起再放下，天然有 200~300ms 的容错；机器没有。**

人会自动补偿界面的时序缺陷：等一等、看一眼、挪一下。机器不会。所以同一个界面，人点一百次都过，机器第一次就挂。这不是机器笨，是**这个界面的操作契约只对人成立**。

再往前一步：一个只有在人手速度下才能操作的界面，通常也意味着——

- 键盘用户按 Tab 过去时，焦点可能落在遮罩上；
- 开了 `prefers-reduced-motion` 的用户，动画没了，但 `pointer-events` 的时序假设还在；
- 屏幕阅读器读到的可交互元素，和视觉上的层级可能不一致。

所以自动化在这里不是“替人点一遍”，是**替人证明这个界面不依赖人的耐心**。它挂掉的地方，往往也是真实用户在某台慢机器上会骂人的地方。

---

### 2.3 1080p 录出来少一条：视频尺寸不等于可见尺寸

**【现象】**

按“1080p”把 Playwright 视口设成 1920×1080。录出来的视频是完整的，但“边看边录”时底部的 Agent 思考面板永远看不到——浏览器自己的标题栏、标签栏要吃掉 100 多像素，窗口总高超过屏幕，最下面一截被物理裁掉。同时页面整体看着“比例不对”。

**【排查与解决】**

三个概念被混在了一起：

- **`100vh` 是视口高度，不是屏幕高度。** 预审中心主体是 `height: calc(100vh - 104px)`，视口 1080 时它算 976，页面自己没毛病；问题是**窗口**比屏幕高；
- **Windows 125% 缩放会二次缩放。** 不加 `--force-device-scale-factor=1`，1920 CSS 像素的窗口会被放大成 2400 物理像素，超出屏幕后 Chrome 反手把页面缩一遍——这就是“比例不对”的来源；
- **视频尺寸 ≠ 可见尺寸。** `recordVideo` 抓的是页面帧，所以“录像完整、人看不到”是完全可能的，两者不是一回事。

修法：

```ts
// 1920 CSS px 与物理像素 1:1，绕开系统缩放
const [vw, vh] = (process.env.DEMO_VIEWPORT ?? '1920x1080').split('x').map(Number);

test.use({
  viewport: { width: vw, height: vh },
  deviceScaleFactor: 1,
  video: { mode: 'on', size: { width: vw, height: vh } },
  launchOptions: {
    args: ['--force-device-scale-factor=1', `--window-size=${vw},${vh}`, '--window-position=0,0'],
  },
});
```

配套三条：

1. 画幅可配置：默认 1920×1080 出成片；`DEMO_VIEWPORT=1920x940` 用于“边看边录”（实测思考面板底边 916 < 940，完整可见）；
2. 测试开头自检并打印真实视口（`实际视口 1920×940 @1x`），被缩放污染立刻告警，而不是录完才发现；
3. 产品侧的长期解：预审中心底部面板给 `max-height` + 内部滚动，别让 `100vh` 成为唯一的布局依据——1280×720 的笔记本上同样会挤。

**【原理与沉淀】**

“录制尺寸”和“可见尺寸”是两份契约。

自动化能证明视频是 1080p，但不能证明人眼看到了全部内容。反过来，人手拖一下窗口就能解决，所以这个坑只会在自动化里暴露——**机器不会帮你把窗口拖小。**

`100vh` 也是一样：它描述的是视口，不是设备，不是窗口，不是“用户实际能看到的区域”。把布局挂在一个不等于可见区域的单位上，迟早会在某个屏幕上少一条。

---

### 2.4 照妖镜：三个“前端调了、后端没有”的接口

**【现象】**

手工 18 条链路全绿。把同样的流程交给 Playwright，第一次完整跑就挂在三个地方：

1. 上传完图纸点“查看”进预审中心，正常；但**刷新页面**（或从站内信深链回来）就变成“这张图纸还没有预审任务”。实测 `GET /drawings/{id}/review-tasks` 返回 **405**——同名路径后端只注册了 POST。
2. 问题清单里点“采纳为批注”没反应。`POST /issues/{id}/adopt` 与 `POST /issues/batch-adopt` 都是 **404**。整条演示链路里“AI 问题 → 批注”这一段其实是断的，录屏时只能绕道。
3. 风险弹窗上写着“确认后会自动转为批注草稿”，实测 `annotations = []`。文案承诺了，代码里没有。

**【排查与解决】**

四个原因，每一个都值得单独记：

- **前端主动降级把缺口藏起来了。** `review.service.ts` 的注释写着“契约未覆盖时按 `[]` 收敛，页面退化成‘发起预审’空态，而不是白屏”。这个决定是对的（不白屏），但副作用是**“接口 405”和“这张图确实没审过”在 UI 上长得一模一样**；
- **404 只落到控制台。** 按 D7 的约定拦截器不弹 Toast，页面自己也没处理，于是“点了没反应”被归因为手滑；
- **手工测试的路径盲区。** 主流程是“新建 → 上传 → 预审 → 报告”，二次刷新、单条采纳、批量采纳都不在主路径上，人工点一遍自然全绿；
- **同路径不同方法最难发现。** POST 和 GET 挂在同一个 `/drawings/{id}/review-tasks`，OpenAPI 里不展开方法根本看不出少了一条。

**修法。**

补三个**新增**接口，不改既有契约：

```python
# 1) 任务发现：给前端 listTasks() 补权威来源
@router.get("/drawings/{drawing_id}/review-tasks", response_model=Page[ReviewTaskBrief])
async def list_review_tasks(
    drawing_id, db, user=Depends(require("review:read")), ...
):
    return await svc.list_tasks(db, drawing_id, user, page=page, page_size=page_size)


# 2) 单条 / 批量采纳：整批成功或整批回滚
@router.post("/issues/{issue_id}/adopt", response_model=IssueOut)
@router.post("/issues/batch-adopt", response_model=BatchAdoptOut)
```

Service 里仍然 `ensure_project_member`：**数据权限不依赖路由挂没挂依赖。**

采纳的三件事必须同事务：

1. 建 `Annotation(source=AI, status=PENDING_CONFIRM)`；
2. 回填 `review_issues.annotation_id`；
3. `open_annotation_count` 走数据库端 `+1`。

```python
db.add(annotation)
await db.flush()

issue.annotation_id = annotation.id
await db.execute(
    update(Drawing)
    .where(Drawing.id == drawing_id)
    .values(open_annotation_count=Drawing.open_annotation_count + 1)
)
```

第 3 条是 D2 的教训：ORM 里 `drawing.open_annotation_count += 1` 是读-改-写，并发下会丢更新。批量版先全量校验（存在性 / 成员 / 状态都是 `PENDING`）再统一写，任何一条不满足就整批放弃。

风险确认那条按 `prd-06 §8.3` 补在 `hitl_service.resume_risk` 里，**顺序有讲究**：先 `db.add` + `flush` 批注，再调 `resolve_if_pending()`——它内部那次 commit 会把“插批注 / 改 interrupt 状态 / 回填问题”一起提交；没抢到恢复权就 `rollback`，不会留下“弹窗关了但图上没框”。

顺手记一个**自己引入的 bug**：给 `ReviewTaskBrief` 加 schema 时，编辑用的 `old_string` 太短，把 `ReviewTaskSnapshot` 的尾巴（`run_status_text / current_node / review_mode / waiting_interrupt_id / report_id / last_seq`）整体切到了新类上。首次 GET 直接 500，pydantic 报 `run_status_text Field required`。**新增类时 `old_string` 必须包含完整类定义**，否则字段会被“下溢”到下一个类。

验证口径：`GET` 返回 200 且带 `items / total / page`；`batch-adopt` 返回 `{"created": 2, "annotations": [...]}`；两条风险各生成一条 AI 批注，`annotation_id` 全部 linked。

**【原理与沉淀】**

Playwright 在这里不是录制工具，是照妖镜：它把“前端以为存在”和“后端实际提供”放进同一张网络日志里对照。

**手工测试测的是主流程，自动化测的是用户路径。这两件事不是一回事。**

用户路径里包含：刷新、从消息深链回来、单条采纳、批量采纳、失败后重试。主流程里一个都没有。所以“18 条全绿”不是系统通，是“主流程通”。

还有一条更隐蔽的：

> **降级必须可见。**

前端把 405 收敛成 `[]`，是一个正确的降级；但它同时抹掉了“坏”和“空”的区别。正确的做法不是不降级，而是降级时留下证据：日志、指标、或者界面上一个“数据可能不完整”的提示。否则用户看到的是空态，你看到的是全绿，只有网络面板知道出过事。

最后一条：**文案也是规格。** 弹窗上写“确认后会自动转为批注草稿”，后端就必须真的生成批注，或者改掉这句话。文案承诺、实现缺失，只有真跑一遍才会发现。

**附：E2E 不该死等异步副作用。**

下发批注后切设计师查站内信，脚本有时要轮询到 45 秒才看到“新批注待处理”，有几次直接超时，重跑就过。

原因是 outbox 天生异步：业务写库与事件入表同事务，消费者**稍后**投递，中间还有 60 秒合并窗口、失败重试、租约回收。再加上未读角标是 30 秒轮询，“批注已下发”和“消息可查”之间必然有延迟，而且**延迟不是常数**。

解法不是缩短等待，是加一条产品自己支持的降级路径：

```ts
try {
  await page.getByText(/新批注待处理/).first().click({ timeout: 45_000 });
  await page.waitForURL(/annotation=/, { timeout: 20_000 });
} catch {
  // 消息还没投递（或已被合并）：直接走批注列表，终点一致
  await page.goto(`${BASE}/annotations?assignee=me&status=ISSUED`);
  await page.getByRole('button', { name: '定位' }).first().click();
}
```

三条口径：

1. **断言最终状态，不断言中间过程**：等的是“标记已修改”按钮可见，说明批注确实派到了这个人头上，而不是“消息列表里必须有这一条”；
2. **降级路径必须是产品支持的路径**：`/annotations?assignee=me&status=ISSUED` 正好是合并消息的 deeplink 目标，所以兜底不是投机，是走同一条契约；
3. **产品口径写死**：站内信是**通知**，不是**凭据**。业务状态以批注 / 任务表为准。

**附：E2E 会往真实库里塞脏数据。**

反复调试脚本，同一套演示流程跑了七八遍。等录屏成功时查库：

- `projects` **20** 条（全是“演示项目-自动化录制-*”）；
- `drawings` 5 张同名 `sample.pdf`；
- `review_issues` 16 条；
- `notifications` 55 条；
- `uploads/` 19 个文件，464KB。

列表页开始出现重复项，工作台数字被撑大，最后不得不整库重置。

清理不是删一行。链条是：图纸 → 任务 → 问题 → 报告草案 → 中断 → `agent_events` / `agent_state_checkpoints` → 批注 → 上传文件。而且 `interrupt_tasks` **不挂在 drawing 上**，按图纸删会漏；残留的 PENDING 中断会让下一次“待处理中断”接口返回上一个世界的工单——我确实被这个坑到过：报告页报“当前链接缺少中断信息”，就是因为 pending 接口返回了已删任务的旧中断，`thread_id` 对不上被守卫丢掉。

还有 SQLite 的 `-wal` / `-shm`：只删 `.db` 会把旧库一起带回来。

两条清理路径都写进了 README：一套是回到纯演示基线（停后端 → 删 `.db` 连同 `-wal/-shm` → 重建迁移 → 幂等 seed），一套是按依赖倒序的局部清理加孤儿扫描。下一步是给 E2E 一个**独立库**，跑完就 drop，永远不碰演示基线。

---

### 2.5 防盲签：`disabled` 是最便宜的业务规则说明书

**【现象】**

报告页点“确认生成”没反应——按钮是 disabled 的，因为没勾“我已阅读并确认报告内容”。风险弹窗同理：只选了决策没填豁免依据，提交按钮保持禁用；两条风险只处理了一条，提交仍然禁用。

脚本最初的写法是“找到按钮 → 点”，于是在三处稳定失败，而且失败信息很有误导性：`element is not enabled`，看起来像页面坏了。

**【排查与解决】**

先接受一件事：**这些 disabled 不是 bug，是产品有意为之。**

- 防盲签：必须勾“我已阅读”才能确认报告；
- 防敷衍：豁免依据 ≥5 字、退回意见 ≥10 字；
- 防漏项：每条风险都要给决策，`decidedCount === items.length` 才允许提交。

校验分散在三层：前端 `[disabled]` → 后端 Pydantic（`RiskItemDecision` 的 `model_validator`）→ 业务规则。脚本只绕过 UI 那层，就会在 API 层吃 422；而 422 的 `code` 又和并发冲突、参数校验混在一起，很难分辨。

更坏的是 `force: true` 对 disabled 无效。Playwright 确实会把点击事件派发过去，但浏览器不会触发 `click`。脚本“成功”了，界面毫无变化——**这是最难查的一类假绿。**

脚本要写成**一个真实用户的完整动作序列**，不是“点击器”：

```ts
// 勾选 → 断言按钮从 disabled 变 enabled → 再点
await page.locator('input[name="acknowledge-report"]').check();
await expect(page.getByRole('button', { name: '确认生成' })).toBeEnabled();
await page.getByRole('button', { name: '确认生成' }).click();

// 风险逐条处置：每条都点决策，豁免分支填足 5 字
for (const card of await riskDialog.locator('article').all()) {
  await card.getByText(decision, { exact: true }).click();
  if (decision === '标记为特殊豁免') {
    await card.locator('input[id^="remark-"]').fill('已取得规划部门超限批复，消防审查意见编号 XF-2026-018');
  }
}
```

三条配套：

1. **断言状态迁移，不断言“点了”**：`已确认，正式文档已生成`（页面内联提示 + Toast 两处，用 `.first()` 避免 strict mode）、状态 chip 变 `已修改待复核`；
2. **提交前先断言“已选 N/N”**，数量不对就不点提交——把业务规则显式写进脚本；
3. **禁止用 `force` 绕过 disabled**。disabled 是最便宜的业务规则说明书，绕过它等于把校验推迟到线上。

**【原理与沉淀】**

`disabled` 不是“按钮坏了”，是“现在还不能做这件事”。它把业务规则写在了用户动手之前，比任何错误提示都早。

这也解释了为什么手工测试发现不了：**人不会去点一个明显灰掉的按钮。** 所以这类规则在人工测试里“永远是对的”，只有在脚本无脑点击时才会暴露。

自动化在这里的价值不是“点得快”，是**它真的会去点那个不该点的按钮**，然后告诉你：规则在，且规则生效了。

---

## 第三块 · 合流：六份“声明”，六份“事实”

把前面所有的坑排在一起，会发现它们不是六件事，是同一张表。

| **声明** | 谁说的 | **事实** | 谁发现的 |
| :--- | :--- | :--- | :--- |
| **“审计会记下来”** | 代码顺序：先 commit，再补写 | 中间有窗口，kill 就丢 | 在两步之间杀进程 |
| **“这条索引会被用”** | 任务清单 / 字段直觉 | 规划器一次都没选，纯写放大 | `EXPLAIN QUERY PLAN` |
| **“这个接口是通的”** | 前端调用 / PRD 清单 | 405 / 404，被降级成空态 | Playwright 网络日志 |
| **“这个按钮能点”** | 人眼 / 手工测试 | 遮罩 + disabled，机器点不到 | actionability 重试日志 |
| **“这条查询很快”** | 单条 1~3ms | 20 并发排队后 780ms | locust P95 |
| **“这条规则有保护”** | 代码里写了 | 正常路径永远触发不到 | 构造一个非 ADMIN 的操作人 |

六行，六个不同的层：事务、索引、接口、交互、性能、权限。它们不是六个 bug，是同一种错误：**把声明当事实。**

第六行值得单独说。“至少保留一名启用的管理员”这条规则，正常路径永远触发不到：

- 用最后一个管理员禁用自己 → 先被“不能禁用自己”拦下，422 文案不是“至少保留一名”；
- 再建一个管理员去禁用他 → 此时目标已经不是“最后一个”，守卫按定义不成立；
- 降级自己的 ADMIN 角色 → 先被“不能移除自己的管理员角色”拦下。

验收时临时给 `DESIGNER` 补上 `system:user:manage`，构造出“有权限、但不是 ADMIN”的操作人，才真正打到这条分支：

```text
PATCH /users/{原 admin}  role_codes=["REVIEWER"]   → 200，qa.admin 成为唯一 ADMIN
POST  /users/{qa.admin}/status {DISABLED}          → 422 "系统必须保留至少一名启用的管理员"
```

三条规则用三个身份、三条路径覆盖，断言文案而不只是状态码。测试如果只断言 422，可能命中的是另一条规则，看着全绿其实没测到。

**可达性也是正确性。** 一段永远不会执行的守卫，和没写没有区别；一个永远走不到的分支，不是“防御性编程”，是“防御性注释”。

---

### 三条心法

**一，把声明当事实，是万恶之源。**

TypeScript 的类型是声明，不是运行时验证；PRD 是设计意图，不是实现；字段清单是人的直觉，不是执行计划；“我印象里很快”是记忆，不是 P95；“代码里写了”是存在，不是可达。

这张表上的六行，全部来自这句话。反面教材就是“照单加索引”：三条都合理，两条都不该加。

验收动作也很具体：

- 索引 → 跑 `EXPLAIN QUERY PLAN`；
- 审计 → 在两步之间 kill 进程；
- 接口 → 用 Playwright 从 UI 真调一遍；
- 规则 → 构造一个能走到那条分支的身份。

**一个系统里最危险的不是未知，是“已知但没验证”。**

**二，失败必须可见。**

审计不能丢，截断要标 `truncated`，405 不能收敛成空态，disabled 不是 bug 是说明书。

失败有三种长相，一种比一种贵：

- 报错：最便宜，至少有栈；
- 静默错误：数据错了但不报错，次贵；
- 看起来正常：界面显示空态、全绿、按钮灰着，最贵——因为它把所有线索都擦干净了。

反面教材是 `catch { return [] }`：它让页面不白屏，也让故障不可见。正确的降级要留下证据：日志、指标、或者界面上一个“数据可能不完整”的提示。

**失败不可见，比失败本身更贵。**

**三，用自动化兜底。**

自动化不是替代人，是去人不去的地方：二次刷新、非主路径、300ms 的动画窗口、20 并发、两步确认、批量采纳。

人会被主流程骗过去，机器不会；但机器也会被骗——如果你把 405 翻译成空态，把 disabled 当成脚本写错，把异步副作用当成同步来等。所以最后还是要回到人：

> **人负责定义什么叫“对”，机器负责证明它。**

三条心法之外，还有一个习惯：**把代价写进代码。**

审计同事务会连带业务失败；替换索引会让降级多一步；8 个 worker 必须关 outbox；E2E 会脏库。这些“不写就会忘”的代价，全部写进注释、docstring、README。半年后的人不会看到今天的权衡，只会看到代码。

---

## 实战避坑清单

九行，每一行都是这一天真实花掉的时间。

| 主题 | 后端 · 这一天写下的规矩 | 前端与 E2E · 这一天写下的规矩 |
| :--- | :--- | :--- |
| **事务** | 审计与业务同事务，`record()` 只 add 不 commit；高危操作宁可不做也不留痕 | 错误翻译只有一个责任人；Toast 去重；裸 `<form>` 显式 `preventDefault` |
| **索引** | 先 `EXPLAIN` 再迁移；排序列进复合索引；迁移写明“为什么不加” | 首屏按路由懒加载；初始包 gzip 93.49 kB |
| **压测** | 先分“慢 / 排队 / CPU 满”；`/health` 与 DB 接口对照；8 worker，outbox 关 | 录屏前打印真实视口；`deviceScaleFactor=1`；画幅可配置 |
| **契约** | 以已挂载 OpenAPI 为准；删重复路由；字段名只留一套 | 对真实响应编程；`role_codes` 只映射一次；文案即规格 |
| **等待** | 长任务超时按业务最长耗时给；outbox 延迟写进文档 | `domcontentloaded` + 内容锚点；不用 `networkidle`，不用固定 sleep |
| **定位** | 同路径不同方法分开核对（GET vs POST） | 作用域收窄到 dialog；锚点落内部语义元素；不对零尺寸 host 断言 |
| **断言** | 断言文案，不只状态码；422 要区分是哪条规则 | 断言状态迁移与“已选 N/N”；`force` 不绕 disabled |
| **录制** | 审计时间戳写 UTC 微秒，顺序才可回放 | 视频尺寸 ≠ 可见尺寸；窗口高度与屏幕高度分开算 |
| **数据** | seed 幂等；清理含 `-wal/-shm`，按依赖倒序 | E2E 独立库；录完回基线；临时项目删除 |

这张表可以缩成三个词：**同事务、看计划、等状态。** 剩下的都是这三行的推论。

---

## 最后：七篇之后，留下什么

七篇，一条线：鉴权 → 并发 → 文件 → AI 引擎 → 事件 → 真实渲染 → 压测兜底。

#### D1《从零搭建 AI 预审平台地基：11 个鉴权与 ORM 的连环坑，我一个个踩给你看》

从一条登录链路、一套权限码、23 张表开始。坑最多，但大多是“不报错但结果是错的”。

**心法：权限不是页面上少一个按钮，是每次请求都要重新回答“你是谁”。**

#### D2《5 个并发打进来，3 个返回 200，库里只剩最后一条》

条件更新、行锁、隔离级别。三个 200 背后，是一条被覆盖的数据。

**心法：返回 200 不等于成功；状态迁移必须原子，否则“成功”只是最后一个写入者的错觉。**

#### D3《12 个分片都在飞，服务端只收到 1 个》

分片乱序、重复、合并竞态。最后收在幂等和收据上。

**心法：客户端的编排不可信；服务端要能对同一件事重复说“是”，且只生效一次。**

#### D4《弹窗刚关掉，它又自己弹了出来》

状态机重放、网络重放。同一个风险确认被推了两遍。

**心法：“暂停”不是停止，状态必须被持久化；恢复不是重放，要先卡住恢复权。**

#### D5《从已下发，改到已下发》

条件更新、秒级时间戳、一枚永远不会变脏的 `computed`。三次“变化发生了，但没人转达”。

**心法：最终一致不是“最终会自动对”，是有人负责转达、有人负责重试、有人负责收尾。**

#### D6《那个框，正好偏了一半》

三套像素、一个写窄的类型、一个编造的数字。

**心法：屏幕坐标只是渲染结果，数据库里永远是图纸坐标；同一个数只能有一个出处。**

#### D7《SQLite 只肯让一个人写》

写锁、单核、索引、自动化。最后收在“有条件达标”和“失败可见”上。

**心法：性能有天花板，验收要有条件；系统有盲区，兜底要交给机器。**

---

七篇走完，主线其实只有一条：

> **每一篇都在把上一轮的“看起来对”，换成“可验证的对”。**

D1 的验证是人点一遍，D7 的验证是机器点一遍。人会被主流程骗过去，机器不会；但机器也会被骗——如果你把失败翻译成空态，把 405 收敛成 `[]`，把 disabled 当成“脚本写错了”。所以最后还是要回到人：**人负责定义什么叫“对”，机器负责证明它。**

如果这一篇只留一句话，我想是这句：

> **“能交付”不是没有坑，是每个坑都有一条能复现、能验证、能回滚的路。**

- **能复现**：`EXPLAIN`、locust、Playwright、在两步之间 kill 进程；
- **能验证**：P95 有数字、审计有记录、状态有迁移、接口有响应；
- **能回滚**：迁移有 `downgrade`、降级路径是产品支持的路径、E2E 有独立库。

这三件事，比“功能全”更能说明一个系统能不能交出去。

---

**GitHub 仓库**：[arch-harness](https://github.com/your-org/arch-harness)
<!-- TODO: 替换为真实仓库链接（上面的 https://github.com/your-org/arch-harness 是占位符） -->

**演示视频**：[D7 全链路演示（1920×1080）](https://example.com/d7-demo)
<!-- TODO: 替换为真实视频链接（上面的 https://example.com/d7-demo 是占位符） -->

> 系列完。感谢你一路看到这里。
> 如果这套复盘让你在自己的项目里少踩一个坑，那它就不算白写。
