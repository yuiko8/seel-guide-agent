# M4 报告：运营台（Ops Console）

日期：2026-11-21 · 测试：57 passed（MockLLM）· 地址：`/console`

运营台是产品实习生日常工作的主界面：看 trace 定位 badcase → 标注 → 导出给 M5 评测集；
同时可以手动开/关熔断器、注入故障，现场验证降级路径。

## 1. 功能
| 页签 | 内容 | 接口 |
|---|---|---|
| Traces | 顶部 8 个指标（轮数、降级率、错误率、护栏拦截率、重试率、P50/P95、单轮成本、标注数）；筛选 all/degraded/errors/guard/unlabelled/bad + 搜索；按会话查看 | `GET /api/stats`、`GET /api/traces?only=&q=&session_id=` |
| Trace 详情 | 对话、节点路径、降级、token/成本、槽位、UI 卡片；span 瀑布图（node/llm/tool/retrieval/guard/degrade/context），点开看 attempts（每次耗时/错误/退避）与参数 | `GET /api/traces/{trace_id}` |
| 标注 | Good/Bad；Bad 需选失败层（routing/slots/retrieval/generation/guard/tool/harness/ux/other）、期望意图、期望行为（即评测断言）、备注 | `POST /api/labels`、`DELETE /api/labels/{sid}/{turn}` |
| Breakers & chaos | 每个依赖：熔断状态、窗口错误率、调用/失败/拒绝数；强制开/关；一键注入故障（Down / 30% 错误 / 30% 超时 / 格式错误 / +800ms）；熔断事件流 | `GET /api/health`、`POST /api/admin/breakers/{name}/{open\|closed}`、`POST/DELETE /api/admin/faults/{dep}` |
| Tickets | 工单列表，可跳到对应会话的 trace | `GET /api/tickets` |
| Labels | 标注列表；导出 JSONL（bad 或全部） | `GET /api/labels`、`GET /api/labels/export?verdict=bad\|all` |

导出格式（M5 评测输入）：每行一个用例，`turns` 是到被标注那轮为止的全部用户消息，
`expect` 是期望意图/行为，`observed` 记录标注时 agent 的实际表现（意图、路径、降级、回复），`prompt_version` 用于回归对比。

## 2. Harness 侧改动
- **LLM span 带 token/成本/模型/任务**：之前只有整轮汇总，现在能看出是 route 还是 respond 贵、是否走了修复/备用模型。
- **trace 带 `meta`**（prompt_version、llm_mode、models）：badcase 可以对应到产生它的版本。
- **管理接口鉴权**：设置 `ADMIN_TOKEN` 后，运营台接口要求 `X-Admin-Token`（常量时间比较）；购物端 `/api/sessions/{id}` 不受影响。`ADMIN_CHAOS=0` 时所有故障/熔断操作返回 403。
- 故障注入/强制熔断只接受已知依赖名（或 `*` 通配），避免打错名字造出一个"永远打开"的幽灵熔断器；故障参数做了范围校验。
- Service Worker 不再拦截 `/console`（之前离线兜底会把运营台替换成购物 App）。

## 3. 本轮发现并修复的问题
1. **trace JSONL 被 PII 脱敏写坏**（M1 以来就存在）：`mask_pii` 作用在整个 JSON 字符串上，浮点时间戳
   `started_at` 被当成电话号码替换成未加引号的 `[phone]`，每一行 JSONL 都不是合法 JSON。
   修复：新增 `mask_obj`，只脱敏字符串值；运营台接口也用它。已加测试。
2. `DELETE /api/admin/faults` 之前没检查 `ADMIN_CHAOS`，关闭混沌开关后仍可调用。
3. 手机端详情页是 `position: fixed` 的 grid 子元素，`align-self: start` 让它按内容高度收缩，超出屏幕后点不到下方按钮（Playwright 发现）。

## 4. 验证
- `tests/test_console.py`：8 个用例（trace 列表/详情/LLM span 用量、混沌→降级→筛选→统计、强制熔断、标注 upsert/导出、鉴权、混沌开关、JSONL 合法性、PII）。
- Playwright：iPhone 13 与 1440px 桌面，跑通"打开 trace → 展开 span → 标注 → 注入故障 → 强制熔断 → 工单 → 标注列表"，无 JS 报错、无横向溢出。
  截图：`docs/screenshots/m4-*.png`。

## 5. 仍未做 / 下一步
- 真实 Claude API 仍未跑过（本环境没有 `ANTHROPIC_API_KEY`），成本列目前全是 $0。
- M5：用导出的 JSONL + golden/adversarial 集做评测 runner 与回归门禁；chaos 评测（固定故障配置批量重跑）可直接复用这里的 `FaultInjector` 接口。
