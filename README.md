# 钻孔岩层对应 API

用 FastAPI + SQLite 实现两口钻孔的分层对应（order-preserving stratum alignment）。

## 启动（Docker Compose）

```bash
docker compose up --build -d
```

- Swagger UI：<http://localhost:8000/docs>
- OpenAPI JSON：<http://localhost:8000/openapi.json>
- 健康检查：<http://localhost:8000/health>
- 数据库存放在命名卷 `borehole-data`（容器内 `/data/boreholes.sqlite3`），
  容器重启或重建后修订单与结果仍在，可直接对现行两孔重算。

本地不带 Docker 运行：

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload
pytest -q
```

## 接口

### 1. 上传某孔的一整版分层 `POST /api/holes/revisions`

```json
{
  "hole_id": "ZK-01",
  "revision": 1,
  "layers": [
    {"layer_no": 1, "top_cm": 0,   "bottom_cm": 120, "lithology": "粉质黏土"},
    {"layer_no": 2, "top_cm": 120, "bottom_cm": 260, "lithology": "细砂"},
    {"layer_no": 3, "top_cm": 260, "bottom_cm": 400, "lithology": "强风化泥岩"}
  ]
}
```

- 深度为整数厘米，`bottom_cm > top_cm`，同孔内层号唯一。
- 分层必须按深度排列且互不重叠（顶部可相接，不可交叠）；接口会校验。
- `revision` 必须严格大于该孔当前修订号，否则 `409`。
- 每次上传是**整版替换**：该修订号下的分层集合就是该孔在该修订的完整分层。
- 受理新修订时，所有用到该孔旧修订的已发布结果立即置为失效
  （`invalidated_at` 非空），响应里的 `superseded_results` 给出失效条数。

### 2. 计算对应 `POST /api/alignments`

```json
{
  "left_hole_id": "ZK-01",
  "right_hole_id": "ZK-02",
  "skip_penalty": 30,
  "confirmed_matches": [{"left_layer_no": 1, "right_layer_no": 1}]
}
```

- `left_revision` / `right_revision` 可省略，省略时取该孔**现行**修订
  （重启后重算沿用此方式即可）。
- `skip_penalty` 必须是正整数。
- 目标函数：所有对应层的厚度差绝对值之和 + 跳层罚分 ×（左孔未配层数 + 右孔未配层数）。
- 约束：只能同岩性配对；对应不能交叉（左、右层序同时严格递增）；
  `confirmed_matches` 中的对应必须出现在结果中。
- 以下情况返回 `201` 且 `status = "pending_review"`，`matches` 为空、`total_cost` 为 `null`，
  并在 `review_reasons` 说明原因，**不给出唯一对齐**：
  - `confirmed_lithology_mismatch`：确认项两侧岩性不同，不可行；
  - `confirmed_matches_cross`：确认项之间顺序交叉，不可行；
  - `no_layers_matched`：没有任何可配出的层；
  - `multiple_optimal_alignments`：存在多个不同的最优对应。
- 请求的修订已不是现行修订时返回 `409`，不会用旧修订发布结果。
- 确认项引用了不存在的层号时返回 `404`。

### 3. 查询

- `GET /api/alignments/{result_id}`：按 id 查询结果。结果不可变；若其后任一方上传了新修订，
  `is_current=false` 且 `invalidated_at` 有值。刚计算出的结果返回 `is_current=true`。
- `pending_review` 的结果不发布任何对应：`matches=[]`、`total_cost=null`、
  `unmatched_*_layer_nos=null`，只给出 `review_reasons`。
- `GET /api/alignments?hole_id=ZK-01&current_only=true&limit=50`：列出结果，可按孔过滤。
- `GET /api/holes/{hole_id}`：查看该孔现行修订及分层。
- `GET /health`：健康检查。

## 修订交错与重启语义

计算分三步：读取当前分层 → 求解（不持锁）→ 在 `BEGIN IMMEDIATE` 事务中重新确认两孔修订仍为
请求所指定的修订，然后才写入结果。因此：

- 若计算期间有孔上传了新修订，发布前检查失败，返回 `409`，不会发布旧版对应；
- 若上传恰好发生在本结果写入之后，该上传的失效语句会覆盖本结果（`invalidated_at` 置位）；
- 两者由同一把连接锁串行化，不会出现“旧版对应仍标记为现行”的窗口。

SQLite 采用 WAL，数据落在卷上；进程失败或重启后，修订单和已发布结果都保留，
可用省略修订号的方式直接对现行两孔重算。

## 测试

`pytest -q` 覆盖：

- 目标函数/跳层/确认项强制纳入、确认项岩性冲突、确认项交叉、无层可配、多解判待核；
- 上传校验（重叠、乱序、重复层号、修订号必须递增）；
- 计算时修订已过期返回 409；
- 新修订使旧结果立即失效、列表默认隐藏失效结果；
- 按现行修订重算；
- 模拟“计算进行中来了新修订”的交错场景，确认不会发布旧结果；
- 重启后（同一数据库文件新建应用实例）结果可查、可重算；
- OpenAPI 可用。

另用独立脚本把求解器与穷举法在随机小规模用例上逐一对拍（见实现说明）。

## 实现说明

- `app/matching.py`：最小权不交叉链 DP。扣除常量项后，配对权重为
  `|厚度差| - 2 * skip_penalty`。DP 同时精确统计（封顶为 2）达到最小值的不同链数：
  前缀矩形保存“最小权重、达到该值的格子数、其中是否已有多个链”，再做二维容斥合并，
  因此无需枚举即可判定唯一性。确认项把问题切成若干独立矩形分别求最优。
- `app/store.py`：SQLite 持久化、修订号校验、失效处理和发布前修订复核。
- `app/schemas.py` / `app/main.py`：Pydantic 校验与 FastAPI 路由（Swagger 自动生成）。
