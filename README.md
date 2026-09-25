# 批次放行复核台（原型）

Python 标准库 + SQLite，无第三方依赖。质量部在**批次放行复核台**上看到"每批一张待办"：未关闭偏差、最新检验、稳定性、供应商变更与返工的缺项逐项展开，可直接筛出卡住的批次。

## 架构：规则 / 记录 / 页面分离

| 文件 | 职责 |
| --- | --- |
| `rules.py` | 纯函数规则引擎：不碰数据库与 HTTP。计算每批的缺项清单、复核结论（ready/conditional/blocked）、偏差关闭前置条件、例外有效期。 |
| `records.py` | SQLite 记录层：表结构、旧库迁移（供应商影响处置列）、只读字典。不含任何业务规则。 |
| `app.py` | 编排层：角色鉴权、事务、资料修订号（乐观锁）、复核留痕失效、HTTP 接口、演示种子数据。 |
| `static/index.html`、`static/app.js` | 复核台页面：待办卡片、卡住筛选、缺项展开、复核历史、动作弹窗。 |

## 复核规则（`rules.py`）

- **关键偏差**未关闭 → 始终 `blocked`，不允许批准例外，有条件放行也不行。
- **一般偏差**未关闭：无有效例外 → `blocked`；有 QA 批准且未过有效期的例外 → `conditional`，只能有条件放行（须填例外编号）。
- **检验**按项目只看最新一轮：无任何检验或最新轮不合格 → 缺项，需合格复测。
- **稳定性**按考察条件只看最新时间点：最新时间点不合格 → 缺项；无数据仅提示，不硬挡。
- **返工**已安排（planned）未完成 → 缺项。
- **供应商变更**未完成影响处置（`disposition != assessed`）→ 缺项。
- **关闭偏差前置**（QA 关闭时强校验）：
  1. 同一检验项目存在"早期轮次失败、其后复测合格"的证据；
  2. 若该批次已安排返工，必须先完成返工。

## 复核结论与资料版本

- 每次资料变更（偏差/检验/返工/供应商/稳定性）都会把批次 `revision + 1`，并把该版本上的有效复核标记为 `superseded`——**资料一改，原复核失效，复核台按新版本重算**。
- 复核历史保留全部旧结论（含当时快照），在批次详情"历史复核结论"中可查。
- 放行/有条件放行要求存在**当前修订号**的有效复核结论，否则 409 拒绝并提示重新复核。

## 运行

```bash
python3 app.py --init --seed   # 建库并灌入 7 个演示批次
python3 app.py                 # 默认 http://127.0.0.1:8214
```

身份通过请求头 `X-Actor` / `X-Role` 模拟：`operator`、`inspector`、`lab`、`qa`；工厂人员只能操作本工厂批次；所有写操作需携带当前 `expected_revision`。

演示批次覆盖：完整闭环已放行、关键偏差挡住、一般偏差+例外可条件放行、缺合格复测、等返工完成、供应商变更未处置、稳定性失败。

## 主要接口

- `GET /api/desk?verdict=blocked|conditional|ready&status=&product=&factory_id=`：复核台（每批一张待办+缺项）。
- `GET /api/batches/{id}`：批次详情 + 规则重算结果 + 当前有效复核。
- `GET /api/batches/{id}/reviews`：复核历史（含已失效旧结论与快照）。
- `POST /api/reviews`：按当前资料版本登记复核结论。
- `POST /api/deviations/{id}/close`：关闭偏差（强校验复测/返工前置）。
- `POST /api/deviations/{id}/exception`：一般偏差例外批准（关键偏差拒绝）。
- `POST /api/supplier-changes/{id}/dispose`：供应商变更影响处置。
- 其余：`/api/factories`、`/api/batches`、`/api/batches/{id}/{deviations,tests,rework,supplier-changes,stability}`、`/api/rework/{id}/complete`、`/api/batches/{id}/decide`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

覆盖端到端闭环、关键偏差硬挡、关闭前置（复测+返工）、例外有效期、供应商处置、复核失效与历史可查、复核台筛选等 14 个用例。

> 原型免责声明：规则为演示用途，不等同于真实 GMP 质量体系、电子签名、计算机化系统验证或监管提交要求。
