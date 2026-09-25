# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目性质

把一套 new-api 的「供应商 + 模型元信息 + 计费配置」用一条命令灌进任意 new-api 实例。**没有构建、测试框架和依赖**——`provision.py` 是纯标准库脚本（系统自带的 `python3` 3.9+ 即可，用到了 `str.removeprefix`；不要为它引入 uv / pyproject），`seed.json` 是权威数据。日常工作 95% 是编辑 `seed.json` 的数据，而不是改代码。脚本对齐 **new-api v1.0.0-rc.40** 的接口，更老的实例不支持。`AGENTS.md` 是指向本文件的软链，只改这里。

`seed.json` 三块：`vendors`（名称 + 图标）、`models`（字段与 `/api/models/` 的列一一对应，**省略的字段按空值处理**，见 `META_DEFAULTS`）、`pricing`（只有 `billing_setting.billing_expr` 与 `billing_setting.billing_mode` 两张「模型名 → 值」的表）。

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

`--check` 覆盖：每个模型都有表达式且 mode 为 `tiered_expr`、没有旧定价模式的键、孤儿键、Claude 每档都写全 `cr / cc / cc1h`、缓存写入价是否为输入价的 1.25x（5 分钟）/ 2x（1 小时）。**它查不了价格对不对**——逐行对照官方价目表仍是人工的活，见下文。

`check_expr` 的解析方式：按括号配对切档（`split_tiers`，档内可以有 `fixed()`、`max()` 等嵌套括号）；一档里没有任何 token 变量（纯按次，如 `tier("request", fixed(0.04))`）就跳过 token 检查；缓存写入的倍数只在能读出「变量 * 数字」或「数字 * 变量」这种简单项时才校验。表达式能否编译由 `--dry-run` 的后端预览把关。

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

**后端内置表达式**：rc.40 自带 `gpt-image-2` / `gpt-image-2.5-flare` / `gpt-image-2.5-sunburst`（图像计费，用 `img`、`img_cr` 变量）和 `gpt-6-astra` 四条，只作默认值、不落库，快照里 `configured` 为空，reset 不会也不需要清它们。

**接口行为以 new-api 源码为准**，升级目标版本时先拉对应 tag 对照（文档跟不上代码）：

```bash
git clone --depth 1 --branch v1.0.0-rc.40 https://github.com/QuantumNous/new-api.git /tmp/new-api-src
```

| 要查的事 | 看哪里 |
|---|---|
| 路由与鉴权（哪些接口要 root） | `router/api-router.go` |
| 按模型定价的读写、校验、版本号 | `controller/model_pricing_config.go`、`model/model_pricing_config.go`（`validateModelPricing`、`UpdateModelPricing`） |
| 表达式变量与 token 归一化（缓存 token 是否从 `p` 扣除） | `pkg/billingexpr/expr.md`、`service/tiered_settle.go`（`BuildTieredTokenParams`） |
| 后端内置表达式 | `setting/billing_setting/builtin_billing.go` |
| 分页上限 | `common/page_info.go`（`GetPageQuery`） |
| 模型元信息的新建 / 更新列 | `controller/model_meta.go`、`model/model_meta.go`（`Insert`、`Update`） |

**渠道（含上游密钥）不在种子范围内**，需在目标系统手工添加；模型与渠道的绑定会自动关联。

**`--reset-pricing` 有破坏性**：目标系统上手工配过、但没进 `seed.json` 的定价（含图片/音频倍率）会被一并清掉。务必先 `--dry-run` 看清单。

## 定价：一律写计费表达式（美元绝对价）

rc.40 已把「按 Token（倍率）」「按次」两种旧定价模式标为**已弃用**；表达式模式下计费完全不读倍率表（`relay/helper/price.go` 的 `modelPriceHelperTiered`）。所以 seed 的 `pricing` 只有 `billing_expr` + `billing_mode` 两张表，系数直接是官方的 $/MTok，不再有倍率换算和循环小数的问题。例：

```
claude-opus-5-5  tier("standard", p * 4 + c * 20 + cr * 0.2 + cc * 5 + cc1h * 8)
gpt-6-sol        len <= 272000 ? tier("standard", ...) : tier("long_context", ...)
```

变量：`p` 输入、`c` 输出、`cr` 缓存命中、`cc` 缓存写入（5 分钟）、`cc1h` 缓存写入（1 小时，Claude 专用）、`len` 上下文长度；`tier("档名", 表达式)` 声明计费档。改价后**逐行对照官方价目表**。

两种用量格式对缓存 token 的处理不同，这是写表达式最容易出错的地方：

- **Claude 格式**：输入 token 本身不含缓存，缓存 token 不会并入 `p`。漏写 `cc` / `cc1h` 这部分**完全不计费**，漏写 `cr` 则按输入价收。所以 Claude 每一档都要写全 `p c cr cc cc1h`。
- **OpenAI 格式**：`prompt_tokens` 含全部子类，表达式用了 `cr` / `cc` 后端就从 `p` 里扣掉，不会重复计费（OpenAI 的缓存写入价是**替代**普通输入价）。没写的子类留在 `p` 里按输入价收，所以 GPT-5.5（不收写入费）不写 `cc` 是对的。

两家官方的缓存写入价都是输入价的固定倍数：5 分钟 **1.25x**、1 小时 **2x**（`--check` 会校验）。缓存命中价则各型号不同，见下文。

UI 的「转换为计费表达式」按钮对 `endpoints` 用数组形式的模型会报 `The model routing configuration could not be verified`（rc.40 的转换代码只认 map 形式），不用管它，脚本直接写表达式。

**统一填官方美元价**：当前收录的 OpenAI / Anthropic / xAI 全为美元计价，数字直入。系统不做汇率换算，将来若纳入非美元计价的厂商，得先定好折算口径。

## 数据纪律（历史上踩过的坑）

- **定价必须能从官方一手来源核实**，核不到的档位就不配，退回能核实的标准价。典型回退：`grok-4.5` 长上下文阶梯因高档价无法核实而撤销（`8a6e978`）。另一类常犯的错是**把同系列上一个小版本的价错配到新版本上**——同系列相邻版本可能完全不同价，逐位对着目标版本那一行抄。**核不到只是当时的状态，厂商补上文档后要回来补配**——Grok 的 200K 阶梯就是这样在官方价目页列出完整高档价后补回的。查价用官方文档页：Anthropic 是 `platform.claude.com/docs/en/about-claude/pricing.md`（注意 `/docs/en/pricing.md` 是 404），OpenAI 是 `developers.openai.com/api/docs/pricing`（`platform.openai.com/docs/pricing` 会 301 过去），xAI 是 `docs.x.ai/docs/pricing`（会 308 到 `docs.x.ai/developers/pricing`）。

- **长上下文阈值和边界各家不同**，别套用：GPT 是输入**超过** 272K 才涨（`len <= 272000` 为短档），Grok 是提示词**达到** 200K 就涨（`len < 200000` 为短档，恰好 200,000 属高档），Claude 1M 内不分档。两家都是"整个请求按高档计费"，不是超出部分才涨。

- **不设模型标签**：按决策 seed 里所有模型都不写 `tags`（省略即空值），脚本会把实例上的标签清掉。新增模型时别顺手补标签。

- **模型描述用代际表述**（「当前 / 上一代 / 旧版」），不写死「最强」「最快」这类绝对说法。新一代发布时只需把各档降一级，不用重写整组文案。

- **添加一个模型要动 3 处**：`models` 数组 + `billing_expr` + `billing_mode`（`tiered_expr`）。加完跑 `python3 provision.py --check`。

- **缓存命中价不是全系 0.1x**：Anthropic 从 Fable 5.1 起逐型号单独定命中价——`claude-fable-5-1` 是 **0.025x**（$0.25），`claude-opus-5-5` 是 **0.05x**（$0.2），而 `claude-fable-5`、`claude-opus-5` 仍是 0.1x。新增 Claude 模型时别照抄上一代的 `cr`，去价目表脚注确认那一行的命中价。

- **改价时各档要一起改**：带长上下文档的模型，短档和长档（含 `cr`、`cc`）都要按官方表逐项换，只改一档会让两档对不上。`gpt-5.6-terra` 跟进降价（$2.5/$15 → $2/$12）时就是两档整组同步改的。

- **`gpt-5.6-sol` / `gpt-5.6-luna` 按发布价配，有意不跟官方降价**（2026-09-26 决策，2026-09-24 曾一度改跟现价后撤回）：sol $5/$30（长档 $10/$45），官方现价 $4/$20 是至少持续到 2026-11-21 的促销价；luna $1/$6（长档 $2/$9、命中 $0.1、写入 $1.25），官方 2026-07-30 降价 80% 到 $0.2/$1.2。**别对着价目表把它俩"纠正"成现价**。terra 仍跟官方现价，`codex-auto-review` 跟 sol 同价。`gpt-6-luna` 也是自定价：官方价的 5 倍（$0.5/$2.5，长档 $1/$3.75），与 5.6-luna 发布价相对其现价的倍数一致，从而保持官方两者的比例——输入与缓存是 5.6-luna 的一半，输出**不是一半**而是 5/12，别简单地整体减半。价目页只列现价，历史发布价去 `developers.openai.com/api/docs/changelog` 找调价公告反推。

- **`codex-auto-review` 没有官方定价可核**：官方文档里 Auto-review 是 Codex 的一个功能（`approvals_reviewer = "auto_review"`，由审核子代理代替人工审批），不是公开的模型 ID（[openai/codex#20981](https://github.com/openai/codex/issues/20981) 问过它的计费身份，至今无官方回复）。本仓库按决策让它与 `gpt-5.6-sol` 同价（当前 $5/$30，长档 $10/$45），**这是自定价，不是抄来的官方价**——改它时不必去找官方表，跟着 sol 走即可。注意实际成本取决于渠道把它转发到哪个真实模型，而渠道不在种子范围内。

- `claude-sonnet-5` 的 $2/$10 **已是官方标准价**：原定 2026-09-01 涨到 $3/$15 的计划被 Anthropic 明确取消，不要再按限时价处理。

- **`endpoints` 用数组形式声明协议**：GPT 全系声明 `["openai", "openai-response"]`（官方同时支持 Chat Completions 与 Responses，Codex 走 Responses），Claude 是 `["anthropic", "openai"]`，Grok 是 `["openai"]`。数组形式只供前端展示；定价页的端点由渠道能力推断，只有 map 形式（自定义路径）才会参与。

## Git 约定

**直接在 `master` 上提交并推送，不开子分支、不走 PR**。提交信息用简体中文单行标题、无正文，说清改了哪个模型和为什么，例如：

```
修正 grok-4.5 缓存命中价：官方为 $0.3，此前误配成 $0.5
```
