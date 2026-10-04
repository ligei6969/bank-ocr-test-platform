# SQLite 写锁并发失败：修复与验证（2026-10-03）

修复对象：[压力测试报告](../reports/test-artifacts/pressure/20260929-压力测试报告.md) 结论 1 与其「下一步」第一条 ——
mock OCR 下 40 / 80 并发的 `sqlite3.OperationalError: database is locked`。
本报告记录**复现、根因、改动、修复后同脚本复测**，以及本方案没有解决的部分。

全量回归：**1581 passed / 0 failed**（修复前 1570，新增 11 条，无回归）。

---

## 一、先把缺陷复现出来

用压力脚本原样重跑一遍 fix 前的代码（`--mode mock --concurrency 10 40 80 --seconds 15`）：

| 并发 | 请求数 | 失败数 | 失败率 | 成功 RPS | P50 ms | P95 ms |
|---:|---:|---:|---:|---:|---:|---:|
| 10 | 819 | 0 | 0.00% | 53.76 | 168.32 | 343.94 |
| 40 | 822 | **41** | **4.99%** | 46.31 | 241.12 | 5073.68 |
| 80 | 852 | **32** | **3.76%** | 48.49 | 972.67 | 5026.34 |

失败类型 `HTTP_500: 33` + `ReadError: 8`（40 并发档），服务端日志里
`database is locked` 出现 **112 行**、`sqlite3.OperationalError` **56 行**。

> ⚠️ **这次的失败率比 2026-09-29 那份报告更高**（那份是 2.57% / 2.58%）。
> 两次是**不同的运行**，机器状态不同，不能把 4.99% 当成「缺陷变严重了」。
> 引用时请说「40 / 80 并发 2.6%–5.0%」，并注明是短窗口本机观测。

**失败点不是建表，是 INSERT 本身** —— 这一点值得单独记下来，因为
「每请求跑 DDL」只是让写锁变拥挤的原因，真正超时的是业务插入：

```
File "app/main.py", line 140, in _save_audit_record
File "app/review_records.py", line 108, in save_review_record
    connection.execute(
sqlite3.OperationalError: database is locked
```

### 一次没复现出来的尝试（留着，因为它说明复现要贴着真实负载）

先写过一个直接压落库路径的最小复现
（[repro_write_lock.py](../reports/test-artifacts/sqlite-concurrency/repro_write_lock.py)）：
8 / 40 / 80 个线程各写 12 条，**0 失败**
（[输出](../reports/test-artifacts/sqlite-concurrency/before.txt)）。

差在哪：它绕过了 HTTP 与质检，库也一直很小，写事务短、锁窗口窄，
所以复现不出来。**这不是「缺陷不存在」，是复现条件不对。**
结论是：**复现必须用与线上一致的那条负载路径**（本项目自带
`performance.run_local_pressure`），而不是自己造一个更轻的替身 ——
否则很容易得出「压不出来，所以没问题」。

## 二、根因

不是 SQLite 慢，是**同一个进程里几十个线程抢同一个写锁，而且每次抢之前
还要先抢两次建表语句的锁**：

1. `save_review_record` 每次都先调 `initialize_review_database()`；
   这个函数是 `CREATE TABLE IF NOT EXISTS` + 7 项 `ALTER` 检查 +
   `CREATE INDEX IF NOT EXISTS` —— **2 条写语句 + 1 条读**；
2. `get_review_record` / `list_review_records` / 每次用户查询同样各跑一遍；
3. 默认的 rollback journal 下，写事务要等到没有读者才拿得到排他锁，
   每次 commit 还要 fsync；
4. `connect_database` 的 `timeout=5.0` 是唯一的退避手段。

于是 80 个并发写者退化成「几十个线程 × 每请求 3 个写事务」的锁风暴，
部分线程等满 5 秒，直接冒泡成 HTTP 500。

复现前后确认了库文件的 journal 模式：修复前那次运行是
`journal_mode=delete`（即 rollback journal）。

## 三、改了什么（三层都在机制层）

| # | 改动 | 落点 |
| --- | --- | --- |
| 1 | **建库语句每个库文件只跑一次** | `sqlite_connection.setup_database_once()` |
| 2 | **写连接在进程内排队** | `connect_database(..., write=True)` |
| 3 | **新库切到 WAL + busy timeout 5s → 30s** | `sqlite_connection.enable_wal_mode()` |

**（1）建库只跑一次** —— 把每请求的写事务从 3 个降到 1 个。
标记是 `(库文件, 用途)`：`review_records` 与 `users` 共用一个库文件，
两张表各自建过一次，谁先跑都不会漏掉另一张。标记里带**「文件还在不在」**的
判断 —— 库文件被删掉（测试 teardown 会这么做）之后必须重来，
否则重建的空库会缺表，调用方拿到的是 `no such table` 而不是「建库没跑」。

**（2）写连接排队** —— SQLite 本来就只允许一个写者，所以进程内排队
**不损失并发度**，只是把「抢锁 → 超时」换成「先进先出」。锁覆盖到
commit 与 close 之后才释放：不覆盖提交的话，两个线程仍会在提交那一刻撞在一起。
只读连接**不排队** —— 否则 WAL 换来的读并发会被写队列逐个串起来。

**（3）WAL** —— 写在库文件头里，是持久属性，所以只在建库那一次设置。
`enable_wal_mode()` 返回**实际生效**的模式而不是假定成功：部分文件系统
（网络盘、某些容器挂载）不支持 WAL，此时 SQLite 静默返回旧模式，
假定它成功就会让人对着一个「以为开了 WAL」的库排查并发问题。

### 刻意**没有**做的事

- **没有把 `synchronous` 降成 `NORMAL`。** 这是审核审计库，默认的 `FULL`
  在 WAL 下每次 commit 仍 fsync。拿「掉电可能丢最后几条记录」换吞吐
  不在本项目的取舍范围内 —— 这条比吞吐重要。
- **没有引入 Redis / 无界队列 / 换存储。** 与既有立场一致。
- **没有改表结构、没有改接口契约、没有改原因码。** `app/main.py` 主链路一行未动。

## 四、修复后：同脚本、同图片、同档位复测

| 并发 | 请求数 | 失败数 | 失败率 | 成功 RPS | P50 ms | P95 ms | P99 ms |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 10 | 987 | **0** | 0.00% | 65.35 | 151.66 | 182.44 | 195.16 |
| 40 | 1019 | **0** | 0.00% | 66.99 | 574.56 | 711.94 | 979.83 |
| 80 | 1034 | **0** | 0.00% | 66.15 | 1161.18 | 1458.97 | 1958.33 |

对比：

| 指标 | 修复前 | 修复后 |
| --- | --- | --- |
| 失败请求 | 73 / 2493（2.93%） | **0 / 3040** |
| `database is locked` 日志行 | 112 | **0** |
| 40 并发成功 RPS | 46.31 | **66.99**（+45%） |
| 40 并发 P95 | 5073.68 ms | **711.94 ms**（-86%） |
| 80 并发 P95 | 5026.34 ms | **1458.97 ms**（-71%） |
| 库文件 journal 模式 | `delete` | `wal` |

**落库核对（修复后）：**

```json
{"rows": 3041, "acknowledged_unique_requests": 3041,
 "missing_acknowledged_records": 0, "extra_records": 0}
```

即：返回 200 的每一条都有对应记录，没有多余记录。
（修复前那份报告写的是「成功响应没有发现缺失记录；不能由此推断失败请求也有
完整审计记录」—— 修复后失败请求数为 0，这个不确定性消失了。）

证据：修复前 [report.json](../reports/test-artifacts/pressure/20261003-023642-mock-cpu/report.json) ·
[server.log](../reports/test-artifacts/pressure/20261003-023642-mock-cpu/server.log)；
修复后 [report.json](../reports/test-artifacts/pressure/20261003-024200-mock-cpu/report.json) ·
[server.log](../reports/test-artifacts/pressure/20261003-024200-mock-cpu/server.log)。

## 五、新增的 11 条测试守的是什么

`tests/test_sqlite_concurrency.py`。断言**刻意落在机制上**，而不是
「跑一遍并发看会不会红」—— 后者受机器和调度影响，这次绿下次红：

| 测试 | 守的东西 |
| --- | --- |
| `test_setup_database_once_runs_setup_only_once` | 建库只跑一次（本次修复的核心） |
| `test_setup_database_once_reruns_after_database_file_is_removed` | 删库后必须重建，不能只认路径 |
| `test_review_and_user_schema_setup_are_independent` | 两张表共用库文件，标记不能互相顶掉 |
| `test_normalize_database_path_is_case_and_separator_insensitive` | 路径大小写 / 分隔符不同必须映射到同一把写锁 |
| `test_write_connections_are_mutually_exclusive` | 统计**重叠**：改坏排队就一定红 |
| `test_read_only_connection_does_not_wait_for_the_write_queue` | 读路径没有拿写锁 |
| `test_new_database_is_switched_to_wal` | WAL 真的生效 |
| `test_saving_a_record_does_not_rerun_schema_setup` | 读写多次，建库语句只跑一次 |
| `test_read_path_still_creates_schema_for_a_fresh_database` | 行为保持：空库查询仍建表返回空 |
| `test_concurrent_review_saves_all_persist` | 并发落库零异常且条数对得上 |
| `test_write_queue_serializes_contention_instead_of_failing` | 拉长写事务制造竞争，仍不失败 |

**没有删除或放宽任何既有断言。** 既有迁移测试
`test_existing_review_database_is_migrated`（老库缺列要自动补上）原样保留并通过 ——
这正是「建库只跑一次」最容易踩坏的地方。

## 六、这份修复没有解决什么（诚实边界）

1. **排队只覆盖单进程。** 本次压测是单 Uvicorn worker。多 worker /
   多进程时各进程有自己的写锁，仍然只能靠 SQLite 的 busy timeout。
   要真正多写者，得上 WAL + 应用层写队列外置，或者换掉 SQLite。
2. **这是短窗口本机压测，不是容量承诺。** 15 秒一档、合成图片、
   mock OCR、关闭大模型；不代表真实 OCR 或真实用户分布。
3. **Paddle OCR 侧的问题一个都没碰。** 报告结论 2–4（CPU 吞吐约
   0.16–0.17 次/秒、共享推理实例被全局锁串行、CPU 路径出现
   `access violation` 与进程异常退出）**仍然未解决**，优先级仍高于存储。
4. **WAL 会带来 `-wal` / `-shm` 边车文件**，备份与拷贝时要一起考虑。
5. **写队列是可观测性上的一个盲点**：排队时间目前没有单独计时，
   高并发下 P95 上升里有多少是排队、多少是 SQLite 本身，暂时分不开。

## 七、复跑命令

```cmd
conda activate bank
cd /d J:\job\bank-ocr-test-platform

python -m pytest tests/test_sqlite_concurrency.py -q
python -m pytest -q

python -m performance.run_local_pressure --mode mock --concurrency 10 40 80 --seconds 15
```
