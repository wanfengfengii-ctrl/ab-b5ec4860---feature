# Cold-Chain Exposure Service

冷链运输热暴露裁决服务。根据运输箱**不等间隔**的温度读数，裁决样本是否经历了
不可接受的热暴露。所有计算基于精确有理数（`fractions.Fraction`），因此
**录入顺序与十进制表示差异（`8.1` / `8.10`、`...:00Z` / `...:00.000+00:00`）
都不会改变结论**。

## 运行

```bash
docker compose up --build api            # 默认宿主机端口 8000
API_PORT=9000 docker compose up --build api   # 宿主机端口可配置
```

健康检查：`GET /healthz`，Compose 中 `depends_on: condition: service_healthy`
会等待 API 就绪。

## 一次性验证

```bash
docker compose up --build --exit-code-from verify --abort-on-container-exit verify
```

`verify` 服务依次执行并以退出码报告结果（0=通过，1=失败）：

1. **代码测试**：`pytest -q tests`
2. **应用构建**：字节码编译 + ASGI 应用导入检查
3. **业务冒烟请求**：提交一份同时包含**阈值交点**（6→10°C 穿越 8°C 阈值）
   和**覆盖缺口**（00:25→00:45 间隔 1200s > 600s 上限）的请求，断言裁决结果；
   并验证乱序 + 不同十进制/时区表示的请求得到完全相同的结论。
4. **分阶段温限冒烟请求**：对同一条运输轨迹提交带 `threshold_periods`
   的请求，同时包含**阈值切换**（00:30 由 8°C 切换为 6°C）、**交点**与
   **覆盖缺口**，断言连续超温区间、度分钟预算（27.5 + 10 = 37.5）与可定位
   422；并验证阶段乱序结论不变、阈值跳变边界处不重复/不漏计。

## API

### `POST /api/cold-chain/exposure`

```json
{
  "transport_start": "2026-01-01T00:00:00Z",
  "transport_end": "2026-01-01T00:55:00Z",
  "threshold_celsius": 8.0,
  "max_interval_seconds": 600,
  "max_single_excursion_seconds": 900,
  "degree_minute_budget": 30.0,
  "readings": [
    {"time": "2026-01-01T00:00:00Z", "celsius": 6.0},
    {"time": "2026-01-01T00:10:00Z", "celsius": 10.0}
  ]
}
```

| 字段 | 说明 |
| --- | --- |
| `transport_start` / `transport_end` | 运输起止时刻（RFC3339） |
| `threshold_celsius` | 温度阈值（°C），严格高于才算暴露 |
| `max_interval_seconds` | 最大采样间隔（>0）；相邻读数间隔超过它即形成覆盖缺口 |
| `max_single_excursion_seconds` | 单次超温时长上限（≥0） |
| `degree_minute_budget` | 度分钟预算（≥0） |
| `readings` | 2–500 条 `{time, celsius}`，时刻（按绝对时刻）唯一；首末读数须恰好位于运输边界 |
| `threshold_periods` | **可选**。1–16 个分阶段温限 `{start, end, threshold_celsius}`，用于装载/平衡/稳定运输等阶段采用不同温限的情形 |

#### `threshold_periods`（分阶段温限）

省略时完全沿用上述单阈值语义，响应内容与错误语义保持不变。提供时：

- 阶段按 `[start, end)` 各自携带恒定阈值，必须**首尾恰好覆盖** `[transport_start,
  transport_end]`、彼此**相邻且不重叠**（既不允许重叠，也不允许覆盖缺口）。
- **输入次序不影响结果**：服务按绝对时刻排序后裁决；跨阶段的校验错误仍定位到
  出错阶段在请求中的**原始下标**。
- **阶段边界处由新阶段阈值生效**。服务在每个有效采样段内部按阶段边界切分，
  温度在切分单元内仍按**线性轨迹**处理并**精确求交点**；严格超温时长与度分钟
  分别相对**各阶段自己的阈值**累计（度分钟 = ∫(温度 − 该阶段阈值)dt）。
- 边界**两侧均严格超温**时保持为**同一次超温区间**；任一侧未严格超温
  （恰好等于阈值或低于阈值）则在边界处结束/开启区间——阈值跳变处不会重复计时，
  也不会漏计暴露。
- 覆盖缺口（采样间隔超限）依旧不跨越插值、不计暴露；缺口穿过阶段边界时同样
  中断区间，**绝不跨缺口计算**。
- 单阶段（`[transport_start, transport_end]` 一条）与省略该字段、使用同一阈值时
  的裁决完全一致。非法阶段时刻、起止倒置、非有限阈值、首尾不覆盖/重叠/缺口或
  条数越界均返回可定位的 422（`loc` 形如
  `["body", "threshold_periods", i, "start"]`）。

### 裁决规则

- 读数按**绝对时刻**排序（时区偏移会归一化）。
- 相邻读数间隔 ≤ `max_interval_seconds` 时按**线性变化**插值，精确求解阈值
  交点；**等于阈值不计暴露**。
- 间隔超过上限形成**覆盖缺口**：不跨越缺口插值，缺口会中断进行中的超温区间。
- 暴露区间内对（温度 − 阈值）积分得到度分钟（梯形/三角形精确面积）。
- 在共享读数处相邻的暴露段合并为同一超温区间。
- **分阶段温限**（可选 `threshold_periods`）：采样段先按阶段边界切分，再相对
  各阶段阈值分别求交点、累计严格超温时长与度分钟；边界两侧均严格超温时区间
  保持连续，否则在边界处结束/开启。

### 成功响应（200）

```json
{
  "verdict": "fail",
  "reasons": [
    {"code": "coverage_gap", "gap_index": 0, "message": "..."},
    {"code": "single_excursion_exceeded", "excursion_index": 0, "duration_seconds": "1050", "limit_seconds": "900", "message": "..."}
  ],
  "excursions": [
    {"start": "2026-01-01T00:05:00Z", "end": "2026-01-01T00:22:30Z",
     "duration_seconds": "1050", "degree_minutes": "27.5"}
  ],
  "total_degree_minutes": "27.5",
  "coverage_gaps": [
    {"start": "2026-01-01T00:25:00Z", "end": "2026-01-01T00:45:00Z", "duration_seconds": "1200"}
  ]
}
```

- `verdict`：`pass` 或 `fail`。任一覆盖缺口、单次超温时长越界或总度分钟
  超预算都会导致 `fail`，并在 `reasons` 中给出对应原因
  （`coverage_gap` / `single_excursion_exceeded` / `degree_minute_budget_exceeded`）。
- 数值以十进制字符串返回，避免浮点误差；可精确表示时即为精确值。

### 错误响应（422，可定位）

非法时刻、重复时刻（同一绝对时刻的不同写法也算重复）、非有限数值
（`NaN` / `Infinity`）、边界不符、条数越界等均返回 422，`loc` 指向出错位置：

```json
{"detail": [{"loc": ["body", "readings", 1, "time"],
             "msg": "duplicate timestamp: same absolute instant as readings[0]",
             "type": "value_error.duplicate"}]}
```

## 本地开发

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest -q tests
.venv/bin/python -m uvicorn app.main:app --port 8000
API_BASE_URL=http://127.0.0.1:8000 .venv/bin/python -m verify.run
```

## 结构

```
app/timeutil.py   RFC3339 精确解析（→ Fraction 纪元秒）与格式化
app/core.py       暴露裁决引擎（线性插值、阈值交点、度分钟、缺口）
app/main.py       FastAPI 入口、请求校验（可定位 422）、裁决与响应组装
tests/            pytest 单元与 API 测试
verify/run.py     一次性验证：测试 + 构建 + 冒烟请求，退出码报告结果
Dockerfile / docker-compose.yml
```
