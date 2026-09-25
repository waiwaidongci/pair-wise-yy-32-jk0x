# 批次放行复核台

质量部在同一处登记偏差、检验、稳定性、供应商变更和返工；复核台为每个批次生成一张待办清单，放行前能直接看出还缺哪份材料。Python 标准库 + SQLite，无外部依赖。

## 结构（规则、记录、页面分离）

- `rules.py`：复核规则，纯函数。由记录生成待办清单并汇总结论，不依赖数据库。
- `app.py`：记录层。SQLite 存储、角色校验、审计日志、HTTP 接口；每次资料变更令原复核失效并按新版本重算，旧结论留存在 `reviews` 表可查询。
- `static/index.html`：复核台页面。可筛出卡住的批次，展开查看待办缺项、复核历史和放行决定。

## 复核规则

- 待办五类：未关闭偏差、最新检验、稳定性、供应商变更、返工。
- 关键偏差始终阻止放行；一般偏差只有例外在有效期内才允许有条件放行（且必须提供例外编号）。
- 关闭偏差前：有失败检验的项目必须有失败后的合格复测；已安排返工的须等返工完成。
- 供应商变更未做影响处置（无影响 / 可接受 / 不可接受）会一直留在待办；处置为不可接受则阻塞放行。
- 无检验记录、无稳定性数据、最新检验不合格、稳定性不合格均构成缺项。
- 结论分四档：可放行 / 可有条件放行 / 待补齐 / 卡住。资料一改，原复核失效并按新版本重算，旧结论仍可查；放行决定携带批次修订号，版本不符即拒绝。

## 运行

```bash
python3 app.py --init --seed   # 建库并写入五个不同状态的演示批次
python3 app.py                 # 默认端口 8214，打开 http://127.0.0.1:8214 即复核台
```

身份通过 `X-Actor` 与 `X-Role` 模拟，角色为 `operator`、`inspector`、`lab`、`qa`。工厂人员只能修改本工厂批次。可用 `--port`、`--db` 覆盖。

## 主要接口

- `POST /api/factories`、`POST /api/batches`：登记工厂和批次。
- `POST /api/batches/{id}/deviations`、`POST /api/deviations/{id}/close`：记录和关闭偏差（关闭前校验合格复测与返工完成）。
- `POST /api/deviations/{id}/exception`：为一般偏差批准有期限例外。
- `POST /api/batches/{id}/tests`：记录检验和复测轮次。
- `POST /api/batches/{id}/rework`、`POST /api/rework/{id}/complete`：计划和完成返工。
- `POST /api/batches/{id}/supplier-changes`、`POST /api/supplier-changes/{id}/disposition`：登记供应商变更并做影响处置。
- `POST /api/batches/{id}/stability`：记录稳定性考察数据。
- `POST /api/batches/{id}/decide`：质量决定（release / conditional / reject / resample），按当前复核结论把关并做并发修订号检查。
- `GET /api/batches/{id}`：批次详情，含当前复核（`review`）与历史复核快照（`reviews`，旧结论标 `superseded`）。
- `GET /api/state`、`GET /api/health`：复核台列表数据与健康检查。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

`tests/test_rules.py` 覆盖规则纯函数，`tests/test_flow.py` 覆盖登记到放行的完整流程。

当前为原型：规则以最新检验、未关闭偏差、例外有效期、影响处置和返工状态为核心，不等同于真实 GMP 质量体系、电子签名、验证或监管提交规范。
