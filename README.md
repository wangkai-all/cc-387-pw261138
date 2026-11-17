# 平面网格融合与点面积统计 — 工作台

两个来源在同一**虚构平面坐标系**（无地图投影）中发布矩形网格观测，边界部分重叠、各自都有缺测单元。
本仓库从空目录起步，完成"接入 → 重叠审阅 → 重采样 → 点/区域统计 → 版本化报告"的完整链路，
并在网页上支持改目标分辨率、审阅重叠区、补交一片修订网格、中断计算。

```
data/          确定性 fixtures（ALPHA 4 m、BETA 3 m、修订补片、小网格基准）
gridmerge/     核心库（无第三方依赖，Python ≥ 3.11 标准库）
web/app.html   单页工作台（无外部资源，离线可用）
tools/         场景生成、验证、基准、预览渲染、UI 静态检查
tests/         34 个测试（几何 / 接入 / 重采样 / 引擎 / HTTP API）
reports/       生成的 HTML 报告、基准 JSON、预览图
```

## 快速开始

```bash
python3 tools/make_fixtures.py                  # 重建 data/（已提交，可跳过）
python3 -m unittest discover -s tests -v        # 34 项测试
python3 -m gridmerge.cli serve --port 8765      # 打开 http://127.0.0.1:8765
python3 tools/verify_fixture.py                 # 10 项守恒/语义断言
python3 tools/small_benchmark.py                # 小网格人工基准（可 --json）
python3 tools/check_ui.py                       # 页面结构/接线静态检查
node tools/client_smoke.js /tmp/wire.json       # 用真实 payload 跑页面渲染函数
python3 tools/render_preview.py                 # 无浏览器时的图面预览 PNG
python3 -m gridmerge.cli report --out reports/baseline.html
python3 -m gridmerge.cli bench  --out reports/bench.json
python3 -m gridmerge.cli demo   --out reports/session.json
```

## 语义契约（代码里被强制，而不是注释里被建议）

| 规则 | 实现位置 | 违反时 |
|---|---|---|
| 缺测 ≠ 0 | `_coerce_cell` / `CellRecord.value=None` / `weighted_sum` | 缺测不进入分母、不进入积分 |
| 同一块面积只统计一次 | `rect_union_area` + `area_fraction` | 支持面积取**并集**，重叠来源不叠加 |
| 计数（extensive）不能跨重叠来源相加 | `Request.validate` / `_merge_extensive` | 直接拒绝并说明会重复计数 |
| 计数不能用最近邻 | `Request.validate` | 最近邻会复制/丢失计数 |
| 最近邻不保任何全局量 | `nearest_neighbour` docstring + 报告 `conservation` | 报告显式列为 not preserved |
| 插值补洞必须被标记 | `Origin.ZONAL` / `Status.ZONAL` | 支撑面积为 0，默认排除在均值外，拒绝进入总量；实测开启补洞前后总量与覆盖率完全相同（仅缺测数下降） |
| 报告必须对应同一输入版 | `InputManifest`（版本 + 内容 sha256） | 输入变化 → 同一次运行的 HTML 打上 Stale 横幅 |
| 修订要么全成要么全不成 | `VersionStore.commit_batch` | 批内先校验后写入，读不到半更新状态 |
| 运行中被修订 → 不发布结果 | `generation` 对比 + `computations_are_current` | 状态 `superseded`，结果被扣留 |

### 各方法保持什么、不保持什么

- **面积加权（conservative，默认）**：保持"每个目标单元内按面积加权的均值"，
  支持面积取并集，因此共享地面只积一次。它**不**保持任一来源自身的面积积分——
  融合场是两个来源的混合，报告里的积分是这个混合场的积分，不是任一提供方的。
  不保持"每个目标单元的计数"，也不对无观测区域做外推；覆盖率单独报告。
- **priority**：保持**被选中来源**在目标单元内的观测面积积分与其支撑均值；
  代价是重叠区被丢弃的另一方（`overlap_summary` 量化这一代价）。
- **最近邻**：只适合类别/序数字段。不保持面积、总量、均值；报告
  `conservation.preserves = []`。若目标中心落在缺测源单元上，结果仍是缺测。
- **限定场景的 zone-of-influence 补洞**：仅在用户显式开启、且给出最大距离时使用；
  产出单元 `origin=zonal`、`area_fraction=0`，默认排除在均值外，**永远**不能进入总量
  （`weighted_sum(..., include_zonal=True)` 会抛 `ZoneConflict`）。

### 重叠策略

| 策略 | 用途 | 关键点 |
|---|---|---|
| `priority` | 一方权威 | 按顺序取**在该单元内有数据**的最高优先级来源；缺测不会把单元变空，而是落到下一来源 |
| `area` | 双方平等 | 按**相交面积**加权平均；两来源覆盖不全时按各自覆盖面积加权 |
| `error_weighted` | 有误差方差 | 权重 `面积 / 方差`，缺方差直接报错 |
| `sum` | 计数、互斥总体 | 只允许 extensive，且两来源 footprint 重叠即拒绝 |

## 数据加工链

```
来源 payload (JSON, 行优先/列优先/稀疏行)
  └─ from_rows / from_columns     校验形状、缺测 token、误差方差配对
       └─ VersionStore.commit_batch   原子提交，分配 version + sha256
            ├─ apply_patch            补片：只改列出的单元，缺省=不变
            └─ FusionEngine.run       逐目标单元：覆盖→并集支撑→按策略合并
                 ├─ CellRecord         value/status/origin/support/observed
                 ├─ plan_overlaps      四类重叠对账（both/a_only/b_only/neither）
                 └─ stats              加权均值、面积积分、分块、覆盖率
                      └─ report / export   HTML 报告 + 紧凑 JSON 制品
```

## 网页工作台

- **左栏**：来源与版本、补片入口、目标网格（改 cell size / nx / ny / 原点，实时显示单元数）、
  语义选择器（字段类型、重采样、重叠策略、优先级、补洞）、Check request / Compute / Stop。
- **中栏**：融合制品栅格图（值 / 归属 / 缺测 / 分歧 四种图层，悬停提示，来源 footprint 虚线框）、
  指标卡、保持/不保持清单、分块统计、值分布直方图、小网格人工基准。
- **右栏**：输入溯源（版本 + sha256）、报告指纹、修订 diff、警告。

关键交互都对应一条明确契约：

- **改目标分辨率** → 重新计算，指纹变化；同一输入版本下可对比。
- **审阅重叠区** → 每个相交来源单元对给出四类状态与共享面积、B−A 差值，可先看再选策略。
- **补交一片修订** → 补片只含变更单元；缺省单元保持原值（与"整表提交"语义区分开）。
- **中断计算** → 令牌在每个单元检查；取消后无结果、无报告，状态 `cancelled`。
- **报告版本身份** → 报告固定列出输入版本与内容哈希；输入被修订后，**同一份**报告页
  会显示 Stale 横幅，而报告内的数字仍属于它声明的版本。

## 小网格人工基准与误差来源

`tools/small_benchmark.py` 用 4×3 @4 m → 8×6 @2 m 的对齐重采样，每格期望值可手算：

```
coarse row 0 [10, 12, ×, 16]
fine row 0   10 10 | 12 12 | × × | 16 16
```

验证项：单元值逐格匹配、对齐网格上最近邻与面积加权一致、缺测不被邻居填充、
积分 `Σv·4 = 2272` 与粗网格 `Σv·16` 相等。

**误差来源清单**（在报告中逐项可见，而不是混入一个总数）：

1. **混合支撑**：目标单元部分被观测覆盖时，support 权重让小面积观测不放大；
   覆盖不足在 `coverage` 中单列，不能当作均衡采样。
2. **重叠分歧**：换策略会改变结果——6 m 目标网格上 priority 均值 14.384、面积加权
   14.507（+0.85%），积分 20 943 → 22 775。差异全部来自重叠区：priority 只认一方，
   面积加权把 BETA 在 ALPHA 缺测处提供的小块支撑也计入。`overlap_summary` 给出
   平均/最大单元格差值，供先审阅再选策略。
3. **单元尺度效应**：粗目标单元会吞掉小块支撑。6 m 时 priority 的支撑面积只有
   1 456 m²，3 m 时 1 528 m²，2 m 及以下达到真实并集 1 570 m²；积分同步从
   20 943 收敛到 22 725（+8.5%），而**均值**几乎不变（14.384 → 14.474）。
   这正是"总量随分辨率收敛、均值稳定"的预期行为，也说明引用积分时必须同时给分辨率。
4. **边界半格**：footprint 边缘的目标单元支撑只有半格；按支持面积计权，不比整格。
5. **最近邻/补洞**：会引入结构性偏差，只在明确标注时出现，且不进入守恒量。

## 约束与取舍

- **不处理地图投影**：坐标就是平面米制，面积 = 解析矩形相交，无重投影误差。
- **无第三方依赖**：服务端 `http.server`，页面原生 JS/SVG；便于在空目录直接运行。
- **单机内存态**：`VersionStore` 不落盘，重启回到 fixtures；版本历史在进程内可查。
- **无浏览器环境**：`tools/check_ui.py` 做结构/接线/JS 语法检查，
  `tools/render_preview.py` 按页面同款几何与配色输出 PNG；版式仍需人工打开确认。
