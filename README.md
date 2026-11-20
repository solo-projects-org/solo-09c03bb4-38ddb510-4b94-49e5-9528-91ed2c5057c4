# 港池疏浚验收计量系统

测绘员在网页上沿 1 米格线框选矩形，系统基于前、后两期测深数据核量
**两期共测面积（㎡）** 与 **净挖方（m³，后测减前测，负值照计）**。

## 业务规则

| 规则 | 实现 |
| --- | --- |
| 同期同格取最高测次号 | `readings` 以 `(phase,x,y)` 为主键，低测次号跳过 |
| 空水深也覆盖旧值 | `depth_cm=NULL` 正常 upsert，该格退出计量但保留读数 |
| 同号异值整批退回 | 批内逐格校验，冲突即抛错回滚，一行不写，返回 409 并留痕 |
| 前后都有读数才计量 | 按 `(x,y)` JOIN 两期且两期 `depth_cm` 均非空 |
| 净挖方 | Σ(后测水深 − 前测水深) cm ÷ 100，每格 1 ㎡，负值照计 |
| 框内整块只读汇总 | 完整块直接读 `block_stats` 的配对指标 |
| 切开的块按细格算 | 边缘格回 `readings` 逐格 JOIN |
| 补测只更新受影响块 | 仅重算本批写入格所在块 |
| 读数与统计一起提交 | 同一 SQLite 事务，失败整体回滚，原结果保留 |
| 并发补测不漏算 | 进程写锁 + `BEGIN IMMEDIATE` + WAL，写事务串行 |
| 一次核量取同一份数据 | 整块与边缘查询在同一个只读快照事务内，返回 `data_version` |
| 网页增量刷新格网 | 初次全量加载；之后 `版本 > 已显示版本` 的格变更，未改动读数不传 |
| 增量不随补测次数膨胀 | `cell_changes` 每 `(期,格)` 一行，反复补测只改同一行 |
| 同一格只传最新值 | 变更日志按格唯一，响应带该格最后写入的读数 |
| 空水深进增量 | `depth_cm=NULL` 照常写入并下发，网页据此清掉旧显示 |
| 退回批次不进增量 | 退回不写 `cell_changes`、不推进 `data_version` |
| 刷新失败保留原图 | 前端分页缓冲后原子落地，失败不改版本，重试从原版本续起 |
| 迟到响应不盖新图 | 每次刷新带递增序号，过期响应直接丢弃 |
| 服务重启后可续刷 | 版本号存库，原页面保留在内存的版本即可继续增量 |

## 目录结构

```
app/
  config.py     # 块大小/数据库路径等环境变量配置
  database.py   # 连接、建表、WAL、读写事务、数据版本、增量日志
  ingest.py     # CSV 解析、测次号规则、整批提交/退回、批次留痕、写变更日志
  stats.py      # 块坐标、受影响块重算（含两期配对指标）
  query.py      # 矩形选区；全量格网与增量变更，均取自同一快照
  main.py       # FastAPI 路由与静态网页
web/            # 原生 HTML/JS 画布，沿格线拖选矩形、刷新格网增量
tests/          # unittest 套件（40 个用例，含并发、迁移与增量）
scripts/        # 端到端试算、增量试算、随机交叉校验、并发压测脚本
Dockerfile, docker-compose.yml
```

## 启动方式

### Docker Compose（交付方式）

```bash
docker compose up --build
# 浏览器打开 http://localhost:8000
```

数据持久化在命名卷 `dredging-data`（容器内 `/data/dredging.db`，WAL 模式）。
可用环境变量调整：`BLOCK_SIZE`（默认 16）、`BUSY_TIMEOUT_S`、`DB_PATH`。

### 本机直接运行

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
DB_PATH=./data/dredging.db uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## CSV 格式

表头 `x,y,survey_no,depth_cm`（支持中文表头别名：格X/格Y/测次号/厘米水深）。
`depth_cm` 留空表示空水深。每行一个 1 米方格的整数坐标、测次号、厘米水深。

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/surveys/{before|after}` | 上传一期 CSV（multipart 字段 `file`） |
| GET | `/api/selection?x0=&y0=&x1=&y1=&cellwise=` | 矩形核量，`cellwise=true` 为全细格校验 |
| GET | `/api/grid` | 初次全量格网：范围 + 两期全部读数 + 版本（同一快照） |
| GET | `/api/grid/changes?since=&limit=` | 增量：`版本 > since` 的格变更；分页用 `next_cursor` 续传 |
| GET | `/api/cells` | 矩形内各格前后水深（旧接口，调试用） |
| GET | `/api/extent` | 两期坐标范围 |
| GET | `/api/blocks` | 分块统计全表 |
| GET | `/api/batches` | 批次留痕（含退回原因） |
| GET | `/health` | 健康检查与数据版本 |

## 网页刷新格网

页面初次打开只加载一次全图（`/api/grid`），之后点「刷新格网」都走
`/api/grid/changes?since=<已显示版本>`，只拉别的窗口新提交、或被覆盖的格：

- 同一期同一格只回传最新值；空水深（`depth_cm: null`）照样覆盖旧显示。
- 增量中的读数与响应里的 `data_version` 由后端同一个只读事务返回，
  被整批退回的批次不写变更日志，因此不会出现在增量里。
- 刷新失败（网络/超时）保留原图与原版本；再次刷新从原版本继续补齐。
- 每次刷新带递增序号，迟到的旧响应直接丢弃，不会盖过较新的格网。
- 已显示版本存于页面内存；后端重启后原页面继续点刷新即可从该版本增量，
  无需重载全图。库被重置（版本倒退）时后端回 `full_reload`，网页自动整图重载。
- 增量按 `next_cursor` 分页，全部取完才一次性落地并推进版本，超大一批也不会截断。

## 测试

```bash
python -m unittest discover -s tests -v          # 40 个用例
python scripts/e2e_trial.py                       # 对运行中服务的 HTTP 试算
python scripts/incremental_trial.py               # 增量刷新：只传变更/覆盖/退回/存储不累积
python scripts/random_check.py                    # 500 个随机矩形两算法交叉校验
python scripts/concurrency_trial.py               # 1200 次并发补测压测
```
（HTTP 试算脚本会往库中写数据，建议对空库或专用测试库运行。）

## 关键设计说明

- **配对指标入库**：`block_stats` 冗余存 `n_matched`（两期均非空的格数）与
  `diff_cm`（后减前差值和），重算块时用 readings JOIN 得出。不能用两期
  各自总和直接相减——任一期出现空水深/缺测时格集合不一致会算错。
- **同快照核量**：WAL 模式下读不阻塞写；一次核量的整块查询与边缘查询包在
  同一个 `BEGIN` 只读事务里，看到的是事务开始时的已提交版本，响应中带
  `data_version` 可供调用方核对。
- **补测原子性**：读数 upsert、块重算、留痕、版本号递增在一个
  `BEGIN IMMEDIATE` 事务内；任何失败全部回滚。退回批次的留痕用独立事务写入，
  不推进数据版本。
- **增量变更日志**：`cell_changes(phase, x, y)` 以格为主键，值为该格最后一次
  变更所属的 `version` 与当时的读数。每次补测在写读数的事务里 upsert 同一行，
  因此行数上界是出现过的格数，不随补测次数增长；写入的版本号与读数同属一个
  提交快照，增量查询与 `data_version` 在同一个只读事务内读取，二者必然一致。
  旧库升级时把存量读数按当前版本补登一次，保证增量不丢格。
