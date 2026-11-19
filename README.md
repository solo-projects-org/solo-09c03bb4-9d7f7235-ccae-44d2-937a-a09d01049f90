# 钻孔岩层对应与待送检样品 API

用 FastAPI + SQLite 实现两口钻孔的分层对应（order-preserving stratum alignment），
以及对按层贴标的待送检岩芯样品的登记 / 撤销 / 查询。

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
- 上传前会先拿新分层核查该孔**所有待送检样品**（见下节）。只要有样品会跨层、
  落入空档或与标签岩性不符，就**拒绝整版**：返回 `409`，`conflicts` 列出全部
  冲突样品的 `sample_no` 与 `reason`，原分层、现行修订号和所有样品状态照旧，
  该修订号也未被占用，可改好后再用。
- 全部样品都能通过时接修订版，并按原规则使旧对齐失效；`active_samples` 给出
  被重新核查过、仍然有效的样品数。若新分层只是改了层号，样品的 `layer_no`
  会在**同一事务内**改指新层号，登记依然有效。
- 受理新修订时，所有用到该孔旧修订的已发布结果立即置为失效
  （`invalidated_at` 非空），响应里的 `superseded_results` 给出失效条数。

### 2. 待送检样品登记 `POST /api/samples`

```json
{
  "sample_no": "S-2026-001",
  "hole_id": "ZK-01",
  "top_cm": 30,
  "bottom_cm": 95,
  "lithology": "粉质黏土"
}
```

- 深度为整数厘米，`bottom_cm > top_cm`；`lithology` 为标签上的岩性，比较时去掉首尾空白。
- 取芯区间 `[top_cm, bottom_cm]` 必须**完整落在现行分层的一个层内**，且该层岩性
  与标签相同。区间端点正好压在层界上是允许的（层界同属上下两层）；层界端点同时
  也是两层的内点，不会误判为跨层。
- 不满足时返回 `422`，`reason` 为机器可读的原因：
  - `crosses_layer_boundary`：区间跨越层界（覆盖两层或更多层）；
  - `lies_in_gap`：区间落在未记录分层的地段（含超出记录深度）；
  - `lithology_mismatch`：完整落在层内，但层岩性与标签不符。
- 同一样品号已有**有效**登记时返回 `409`（`sample_status: "active"`），必须先撤销。
- 孔不存在 / 尚无分层返回 `404`。
- 返回体中的 `layer_no`、`registered_revision`、`current_revision` 读自同一次
  已提交快照；有效样品的 `layer_no` 一定属于 `current_revision`。

### 3. 撤销 `DELETE /api/samples/{sample_no}`

- 只有有效登记可撤销，返回撤销后的记录（`status: "withdrawn"`，`withdrawn_at`、
  `withdrawn_revision` 有值）。
- 重复撤销或样品号不存在返回 `404`。
- 撤销后该样品号可重新登记（分层更正、样品重新贴标后）。重新登记会替换深度、
  岩性并重置 `registered_at`；旧的生命周期字段被清空。

### 4. 查询

- `GET /api/samples/{sample_no}`：查单个样品，`layer_no` 与 `current_revision`
  来自同一次已提交快照。
- `GET /api/samples?hole_id=ZK-01&status=active&limit=50`：默认只列有效登记；
  `status=withdrawn` 列已撤销，`status=all` 列全部。
- `GET /api/samples/verify?hole_id=ZK-01`：把所有有效登记重新对照各孔现行修订
  核查一遍。登记与每次换版都在同一事务内复查，正常数据库恒为
  `{"status": "ok", "checked": N, "conflicts": []}`；它是运维兜底，也是测试断言。

### 5. 计算对应 `POST /api/alignments`

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

### 6. 对齐查询

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

样品登记 / 撤销 / 换版三者共用同一把连接锁与 `BEGIN IMMEDIATE`：

- 登记在一个事务里完成“读现行分层 → 定位层 → 写登记”，要么看到某个修订，
  要么被该修订在其事务里重新核查；
- 换版在同一个事务里完成“核查全部有效样品 → 写修订 → 重指层号”，失败则整版回滚，
  修订号不被占用；
- 因此不存在“样品按旧分层判为有效、而新分层已是现行”的窗口。撤销也是单事务，
  与换版交错时按提交顺序串行。

SQLite 采用 WAL，数据落在卷上；进程失败或重启后，修订单、已发布结果和样品登记都保留，
可用省略修订号的方式直接对现行两孔重算，样品查询与 `verify` 也照常工作。

## 测试

`pytest -q` 覆盖：

- 目标函数/跳层/确认项强制纳入、确认项岩性冲突、确认项交叉、无层可配、多解判待核；
- 上传校验（重叠、乱序、重复层号、修订号必须递增）；
- 计算时修订已过期返回 409；
- 新修订使旧结果立即失效、列表默认隐藏失效结果；
- 按现行修订重算；
- 模拟“计算进行中来了新修订”的交错场景，确认不会发布旧结果；
- 重启后（同一数据库文件新建应用实例）结果可查、可重算；
- 样品登记返回层号与修订号、三种拒绝原因、端点贴层界、重复登记 409、撤销与重登；
- 一个冲突样品即拒绝整版并列出全部冲突样品号，原分层与样品状态不变；
- 兼容换版（层号变化）后样品仍在、`layer_no` 已更新、`verify` 为 ok；
- 换版产生空档时拒绝跨空档样品；被拒修订号未被占用，撤销后可复用；
- 登记 / 撤销 / 换版随机交错的压力用例，每一步都断言 `verify` 为 ok；
- 重启后样品可查、样品号仍被占用、撤销仍可用；
- OpenAPI 可用。

另用独立脚本把求解器与穷举法在随机小规模用例上逐一对拍（见实现说明）。

## Swagger 演示（登记与换版）

服务起来后执行：

```bash
python3 -I scripts/swagger_demo.py            # 默认 http://localhost:8000
python3 -I scripts/swagger_demo.py http://127.0.0.1:8000
```

脚本只用标准库，按顺序调用真实 HTTP 接口并在终端打印每一步的请求与要点：

1. 上传一对演示孔（孔号带本次运行的时间后缀）的第一版分层；
2. 登记一个落在单层内的样品，打印返回的 `layer_no` 与 `current_revision`；
3. 打印三个被拒绝的登记（跨层 / 空档 / 岩性不符）及其 `reason`；
4. 上传一版会把该样品劈成两层的分层，展示 `409` 与全部冲突样品号，并确认现行修订未变；
5. 撤销样品，再用同一版分层换版成功；
6. 在换版后的分层里重新登记，打印新的 `layer_no` 与 `current_revision`；
7. 上传一版几何相同、仅层号不同的分层，确认登记仍有效且 `layer_no` 已跟随换版；
8. 调用 `GET /api/samples/verify` 收尾，断言 `status == "ok"`。

孔号与样品号都带运行后缀，可在同一数据卷上反复运行。

同一流程也可以直接在 <http://localhost:8000/docs> 里逐条点开对应接口执行。

## 实现说明

- `app/matching.py`：最小权不交叉链 DP。扣除常量项后，配对权重为
  `|厚度差| - 2 * skip_penalty`。DP 同时精确统计（封顶为 2）达到最小值的不同链数：
  前缀矩形保存“最小权重、达到该值的格子数、其中是否已有多个链”，再做二维容斥合并，
  因此无需枚举即可判定唯一性。确认项把问题切成若干独立矩形分别求最优。
- `app/samples.py`：`locate_sample` 是“区间落在单一同岩性层内”的唯一判定实现，
  登记与换版都调用它，规则不会在两处漂移。
- `app/store.py`：SQLite 持久化、修订号校验、失效处理、换版时的全员样品核查与
  层号重指、发布前修订复核。
- `app/schemas.py` / `app/main.py`：Pydantic 校验与 FastAPI 路由（Swagger 自动生成）。

## 已知取舍

- 撤销不会让已发布的**对齐结果**失效。对齐的结果是对两张分层表的对应关系，
  不依赖样品是否存在；撤销只改变送检流程的状态。反过来，样品登记也不改动分层，
  所以不会触发已有对齐失效。
- 样品号是全局主键（不按孔分组），跨孔复用一个号码同样会得到 `409`。
