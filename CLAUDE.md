# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目性质

把一套 new-api 的「供应商 + 模型元信息 + 计费配置」用一条命令灌进任意 new-api 实例。**没有构建、测试框架和依赖**——`provision.py` 是纯标准库脚本，`seed.json` 是权威数据。日常工作 95% 是编辑 `seed.json` 的数据，而不是改代码。脚本对齐 **new-api v1.0.0-rc.40** 的接口，更老的实例不支持。

## 命令

```bash
# 离线自检 seed.json（无实例也能跑，本仓库唯一的"测试"；灌入前也会自动跑）
python3 provision.py --check

# 灌入配置（务必先 --dry-run 预览）
python3 provision.py --base-url http://目标机:3000 --token <访问令牌> --dry-run
python3 provision.py --base-url http://目标机:3000 --token <访问令牌>

# 全新系统：清掉出厂自带的过时模型定价
python3 provision.py --base-url http://目标机:3000 --token <访问令牌> --reset-pricing --dry-run
```

访问令牌来自目标系统：控制台 → 个人资料 → 生成访问令牌（需**超级管理员**，`/api/option/*` 走 `RootAuth`）。

`--check` 覆盖：三张必需倍率表（Claude 另加 `CreateCacheRatio`）是否齐全、孤儿键、`billing_mode` 与 `billing_expr` 是否成对、表达式 standard 档的绝对价与倍率反算是否一致。**它查不了价格对不对**——逐行对照官方价目表仍是人工的活，见下文。

## 架构

单向数据流，`provision.py` 只是 `seed.json` 的幂等执行器：

```
seed.json ──> provision.py ──> new-api REST API
             （三阶段，供应商必须先于模型）
```

三个阶段的幂等策略各不相同，这是读代码才能看出的关键差异：

| 阶段 | 接口 | 幂等策略 |
|---|---|---|
| `vendors` | `GET/POST /api/vendors/` | 按名称查重；已存在则**完全不动**（不覆盖手工修改） |
| `models` | `GET/POST/PUT /api/models/` | 按名称查重；已存在则逐字段对照 seed（描述/图标/标签/端点/状态/匹配规则/供应商），**有差异就覆盖** |
| `pricing` | `GET/PATCH /api/option/model_pricing` | seed 内的模型**按模型整体替换**；`--reset-pricing` 另把 seed 外的模型清空；全部变更合成一个 PATCH |

**分页**：rc.40 把 `page_size` 截断到 100，列表必须翻页（`list_all`），否则超过 100 个模型的实例会把后面的当成不存在、POST 时撞上「模型名称已存在」。

**模型更新**：`PUT /api/models/` 会整行覆盖 `model_name / description / icon / tags / vendor_id / endpoints / status / sync_official / name_rule` 这些列，请求体必须带齐，**`sync_official` 要原样带回**，否则被清成 0。新建时显式传 `sync_official: 0`：seed 是元信息的权威，关掉「同步官方元数据」以免被上游覆盖。

**为什么定价不走 `PUT /api/option/`**（rc.40 仍兼容，但有两个坑，全新实例或有残留时才暴露）：

- rc.40 把定价类 key 的 PUT 也路由到统一的 `UpdateModelPricingOptions`，对「新旧两张表涉及的每个模型」做整体校验。按单个 key 覆盖写就有**顺序死结**：新增阶梯模型要先写 `billing_expr` 再写 `billing_mode`，删掉阶梯模型却要反过来，否则 mode 还标着 `tiered_expr` 而表达式没了，报 `billing expression is required`。实例上残留 seed 已删的阶梯模型（如 `gpt-5.4`）时，旧脚本的 `--reset-pricing` 就是这样整批失败的，已在测试环境复现。
- `GET /api/option/` 返回的 `billing_setting.*` 是**生效值**，混入了后端内置表达式；拿它合并再 PUT 回去会把内置条目固化进库。`model_pricing` 快照的 `configured` 只含真正落库的值。

PATCH 的要点：每个模型要带 `expected_version`（快照里的 `version`，快照里没有的模型用 `empty_version`），冲突返回 409，说明读快照后有人改过定价，重跑即可。**清空某模型的定价是提交 `pricing: {}`**，别用 `reset: true`，那是恢复出厂默认倍率。`--dry-run` 会对每份草稿调 `POST /api/option/model_pricing/preview`（无副作用）做后端校验。

**后端内置表达式**：rc.40 自带 `gpt-image-2` / `gpt-image-2.5-flare` / `gpt-image-2.5-sunburst`（图像计费要用 `img`、`img_cr` 变量，纯倍率表达不了）和 `gpt-6-astra` 四条，只作默认值、不落库，快照里 `configured` 为空，reset 不会也不需要清它们。

**渠道（含上游密钥）不在种子范围内**，需在目标系统手工添加；模型与渠道的绑定会自动关联。

**`--reset-pricing` 有破坏性**：目标系统上手工配过、但没进 `seed.json` 的定价（含图片/音频倍率）会被一并清掉。务必先 `--dry-run` 看清单。

## 定价：倍率反算是核心心智模型

new-api 的 `ModelRatio` 以 **$2/MTok 为 1 倍**，即：

```
输入价 $/MTok      = ModelRatio × 2
输出价 $/MTok      = 输入价 × CompletionRatio
缓存命中价 $/MTok  = 输入价 × CacheRatio
缓存写入价 $/MTok  = 输入价 × CreateCacheRatio   （5 分钟档；Claude 1 小时档由后端按 ×1.6 自动推出）
```

改动任何定价后，用这个关系反算并**逐行对照官方价目表**。例：`claude-opus-5` 倍率 `2.5 / 5 / 0.1 / 1.25` → `$5 / $25 / $0.5 / $6.25`，与 Anthropic 官方表吻合。

**倍率除不尽时不要留循环小数**（会显示成 `0.024999` 之类）——改用 `billing_setting.billing_expr` 写绝对价。判断方法：拿官方价除以输入价，除不尽就别硬凑倍率。能整除就用纯倍率，不要多配表达式。

`billing_expr` 的变量：`p` 输入、`c` 输出、`cr` 缓存命中、`cc` 缓存写入、`len` 上下文长度；`tier("档名", 表达式)` 声明计费档。GPT 系列用它做 272K 长上下文分档。表达式一旦用了 `cr` / `cc`，后端会从 `p` 里扣掉对应 token，所以 `p * X + cc * Y` 不会重复计费（OpenAI 的缓存写入价是**替代**普通输入价，不是叠加）。

**统一填官方美元价**：当前收录的 OpenAI / Anthropic / xAI 全为美元计价，数字直入。系统不做汇率换算，将来若纳入非美元计价的厂商，得先定好折算口径。

## 数据纪律（历史上踩过的坑）

- **定价必须能从官方一手来源核实**，核不到的档位就不配，退回能核实的标准价。典型回退：`grok-4.5` 长上下文阶梯因高档价无法核实而撤销（`8a6e978`）。另一类常犯的错是**把同系列上一个小版本的价错配到新版本上**——同系列相邻版本可能完全不同价，逐位对着目标版本那一行抄。**核不到只是当时的状态，厂商补上文档后要回来补配**——Grok 的 200K 阶梯就是这样在官方价目页列出完整高档价后补回的。查价用官方文档页：Anthropic 是 `platform.claude.com/docs/en/about-claude/pricing.md`（注意 `/docs/en/pricing.md` 是 404），OpenAI 是 `developers.openai.com/api/docs/pricing`（`platform.openai.com/docs/pricing` 会 301 过去），xAI 是 `docs.x.ai/docs/pricing`（会 308 到 `docs.x.ai/developers/pricing`）。

- **长上下文阈值和边界各家不同**，别套用：GPT 是输入**超过** 272K 才涨（`len <= 272000` 为短档），Grok 是提示词**达到** 200K 就涨（`len < 200000` 为短档，恰好 200,000 属高档），Claude 1M 内不分档。两家都是"整个请求按高档计费"，不是超出部分才涨。

- **不设模型标签**：按决策 seed 里所有模型都不写 `tags`（省略即空值），脚本会把实例上的标签清掉。新增模型时别顺手补标签。

- **模型描述用代际表述**（「当前 / 上一代 / 旧版」），不写死「最强」「最快」这类绝对说法。新一代发布时只需把各档降一级，不用重写整组文案。

- **添加一个模型要动 4 处**，漏配会导致计费错误：`models` 数组 + `ModelRatio` + `CompletionRatio` + `CacheRatio`，Claude 系列再加 `CreateCacheRatio`；带阶梯的再加 `billing_expr` + `billing_mode`。加完跑 `python3 provision.py --check`。

- **`CacheRatio` 不是全系 0.1**：Anthropic 从 Fable 5.1 起逐型号单独定缓存命中价——`claude-fable-5-1` 是 **0.025x**（$0.25），`claude-opus-5-5` 是 **0.05x**（$0.2），而 `claude-fable-5`、`claude-opus-5` 仍是 0.1x。新增 Claude 模型时别照抄上一代，去价目表脚注确认那一行的命中价。

- **改一个已有模型的价，四张表要一起改**：厂商降价时 `ModelRatio` / `CompletionRatio` / `CacheRatio` 和 `billing_expr` 里的绝对价必须同步，只改一处会让分档与兜底倍率打架（`--check` 会拦住 standard 档不一致）。`gpt-5.6-terra` 跟进降价（$2.5/$15 → $2/$12）时就是整组同步改的。

- **`gpt-5.6` 全系已跟进官方现价**（2026-09-24）：此前 sol 保留 $5/$30、luna 保留 $1/$6 的有意偏离已取消。注意 **sol 的官方现价 $4/$20 是至少持续到 2026-11-21 的促销价**，到期后要回来复核；若官方回到 $5/$30（长档 $10/$45），倍率改回 `2.5 / 6 / 0.1`，`billing_expr` 的绝对价（含 `cc` 缓存写入档 $6.25 / $12.5）要一起换，`codex-auto-review` 也跟着改。

- **`codex-auto-review` 没有官方定价可核**：官方文档里 Auto-review 是 Codex 的一个功能（`approvals_reviewer = "auto_review"`，由审核子代理代替人工审批），不是公开的模型 ID（[openai/codex#20981](https://github.com/openai/codex/issues/20981) 问过它的计费身份，至今无官方回复）。本仓库按决策让它与 `gpt-5.6-sol` 同价（当前 $4/$20，长档 $8/$30），**这是自定价，不是抄来的官方价**——改它时不必去找官方表，跟着 sol 走即可。注意实际成本取决于渠道把它转发到哪个真实模型，而渠道不在种子范围内。

- `claude-sonnet-5` 倍率 1（$2/$10）**已是官方标准价**：原定 2026-09-01 涨到 $3/$15 的计划被 Anthropic 明确取消，不要再按限时价处理。

- **`endpoints` 用数组形式声明协议**：GPT 全系声明 `["openai", "openai-response"]`（官方同时支持 Chat Completions 与 Responses，Codex 走 Responses），Claude 是 `["anthropic", "openai"]`，Grok 是 `["openai"]`。数组形式只供前端展示；定价页的端点由渠道能力推断，只有 map 形式（自定义路径）才会参与。

## Git 约定

**直接在 `master` 上提交并推送，不开子分支、不走 PR**。提交信息用简体中文单行标题、无正文，说清改了哪个模型和为什么，例如：

```
修正 grok-4.5 缓存命中价：官方为 $0.3（倍率 0.15），此前误配成 $0.5
```
