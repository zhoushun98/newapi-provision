# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概况

把一套 new-api 的「供应商 + 模型元信息 + 计费配置」用一条命令幂等地灌进任意 new-api 实例。目标版本 **v1.0.0-rc.41**（rc.40 也能用，更老的不支持）。

- `seed.json`：权威数据，日常工作基本是改它。`vendors`（名称 + 图标）；`models`（`/api/models/` 的列，`vendor` 写名称由脚本换成 id；**省略的字段按 `META_DEFAULTS` 写回实例，即清空，不是保持原样**）；`pricing`（只有 `billing_setting.billing_expr` / `billing_setting.billing_mode` 两张「模型名 → 值」表）。
- `provision.py`：纯标准库，用系统 `python3`（3.9）直接跑。**不引入 uv / 第三方依赖，不用 3.10+ 语法**。
- `README.md` 面向使用者，含分类计数和逐模型价格说明，改 seed 时同步。`AGENTS.md` 是本文件的软链。

## 命令

```bash
python3 provision.py --check   # 离线自检，本仓库唯一的"测试"；灌入前也会自动跑
python3 provision.py --base-url http://目标机:3000 --token <访问令牌> --dry-run   # 预览（会调后端编译表达式）
python3 provision.py --base-url http://目标机:3000 --token <访问令牌>             # 灌入
# --reset-pricing：连 seed 外的模型定价也清空，有破坏性，先 --dry-run
```

令牌须属于**超级管理员**；rc.41 的 `nap_` 令牌要勾「模型」「系统设置」的查看 + 编辑（缺了报 403 `ACCESS_TOKEN_SCOPE_DENIED`），只跑 `--dry-run` 两个「查看」就够。

`--check` 只查结构和倍数（mode 为 `tiered_expr`、无旧定价键 / 孤儿键、Claude 每档写全 `cr/cc/cc1h`、缓存写入 = 输入价 1.25x / 2x），**查不了价格对不对**。没有实例时，可在 rc.41 源码的 `pkg/billingexpr` 里写个临时 `_test.go` 调 `billingexpr.RunExpr` 验证表达式能否编译、分档是否正确。

## 架构

三阶段按顺序执行，幂等策略各不相同：

| 阶段 | 接口 | 策略 |
|---|---|---|
| 供应商 | `GET/POST /api/vendors/` | 按名称查重，已存在不动 |
| 模型元信息 | `GET/POST/PUT /api/models/` | 按名称查重，与 seed 有差异就覆盖 |
| 定价 | `GET/PATCH /api/option/model_pricing` | seed 内模型整体替换，合成一个 PATCH 单事务提交 |

读代码看不出来的约束：

- **分页**：`page_size` 上限 100，必须翻页（`list_all`），否则后面的模型被当成不存在、POST 撞重名。
- **模型 PUT 整行覆盖**：要带齐所有列，`sync_official` 原样带回；新建时传 `sync_official: 0`，免得上游元数据同步覆盖 seed 文案。
- **定价别改回 `PUT /api/option/`**：逐 key 写有顺序死结（`billing expression is required`），且 `GET /api/option/` 混入了后端内置表达式，回写会固化进库。快照的 `configured` 才是落库值。
- **PATCH**：每个模型带 `expected_version`（没有的用 `empty_version`），409 = 有人并发改过，重跑即可。清空定价提交 `pricing: {}`；`reset: true` 是恢复出厂，别用。
- **后端内置表达式**：`gpt-image-2`、`gpt-image-2.5-flare`、`gpt-image-2.5-sunburst`、`gpt-6-astra` 自带默认值、不落库；seed 配了就以 seed 为准。
- **网络重试**：请求没发出（`URLError`）任何方法都重发；发出但没响应的只重发 GET / PUT / 预览，POST 和 PATCH 直接报错让人重跑。
- **不在范围内**：渠道（含密钥）手工加；从 seed 删掉的模型和供应商不会从实例删除。

接口行为以源码为准（文档跟不上代码），克隆到带版本号的目录，免得误用残留旧版；单个文件可取 `raw.githubusercontent.com/QuantumNous/new-api/v1.0.0-rc.41/<路径>`：

```bash
git clone --depth 1 --branch v1.0.0-rc.41 https://github.com/QuantumNous/new-api.git /tmp/new-api-rc.41
```

| 要查的事 | 看哪里 |
|---|---|
| 路由、鉴权、令牌 scope | `router/api-router.go`、`middleware/access_token_routes.go` |
| 按模型定价的读写与版本号 | `controller/model_pricing_config.go`、`model/model_pricing_config.go` |
| 表达式语法、时间函数、token 归一化 | `pkg/billingexpr/expr.md`、`service/tiered_settle.go` |
| 内置表达式 | `setting/billing_setting/builtin_billing.go` |
| 模型元信息列、端点推断 | `controller/model_meta.go`、`model/pricing.go` |

## 定价表达式

只写计费表达式（倍率 / 按次旧模式已弃用），系数直接是官方单价（每百万 token）。

```
claude-opus-5-5  tier("standard", p * 4 + c * 20 + cr * 0.2 + cc * 5 + cc1h * 8)
gpt-6-sol        len <= 272000 ? tier("standard", ...) : tier("long_context", ...)
gpt-image-2      tier("image", fixed(0.1)) * image_count
```

变量：`p` 输入、`c` 输出、`cr` 缓存命中、`cc` 缓存写入（5 分钟）、`cc1h`（1 小时，仅 Claude）、`len` 上下文长度。

- **缓存归一化两家不同**：Claude 的 `p` 不含缓存，漏写 `cc` / `cc1h` 这部分**完全不计费**，所以每档写全 `p c cr cc cc1h`。OpenAI 格式（含 Grok、DeepSeek）的 `p` 含缓存，写了 `cr` / `cc` 才扣出来单独计价，不收写入费的就不写 `cc`。
- **缓存价**：写入 = 输入价 × 1.25（5 分钟）/ × 2（1 小时）。命中价**逐型号不同**（如 `claude-fable-5-1` 0.025x，`gpt-6.1-sol` 0.05x），新模型别照抄上一代。
- **长上下文整单按高档**，边界各家不同：GPT **超过** 272K（`len <= 272000`），Grok **达到** 200K（`len < 200000`），Claude 仅 `claude-haiku-5-5` **超过** 100K（`len <= 100000`，含缓存读写），其余 1M 内不分档。
- **`fixed()`** 是按次 / 按张的完整价，所在档不能再加 token 项，只能乘 `image_count`。

## 数据纪律

- **价格必须能在官方一手来源核实**，核不到的档位不配。同系列相邻版本可能完全不同价，逐行对着目标型号抄；短档、长档、`cr`、`cc` 一起改。价目页：
  - Anthropic：`platform.claude.com/docs/en/about-claude/pricing.md`
  - OpenAI：`developers.openai.com/api/docs/pricing`（调价历史看同站 `changelog`）
  - xAI：`docs.x.ai/developers/pricing`
  - DeepSeek：`api-docs.deepseek.com/zh-cn/quick_start/pricing`（用国内人民币价；公告在同站 `/zh-cn/news/`）
- **加 / 删模型**：seed 动 3 处（`models`、`billing_expr`、`billing_mode`），插在同系列相邻型号旁，两张定价表键序跟 `models` 一致；同步 README 计数和价格说明；跑 `--check`。
- **不写 `tags` 和 `endpoints`**（脚本会清空实例上的值）。端点由 new-api 按渠道类型推断；`endpoints` 只认 map 形式的自定义路径，数组写法被忽略。`icon` 跟供应商一致。
- **描述**一句话「定位 + 擅长场景」，以官方介绍为准；型号名看不出档位的先写档位（如「GPT-6 旗舰」）。不写「当前 / 上一代」「最强 / 最快」，不写价格、上下文长度。
- **币种**：系统不做汇率换算。DeepSeek 按决策**把人民币数字直接当美元填**，别折算或改成美元价；再纳入非美元厂商前先问口径。
- **DeepSeek 调用名**：官方 API 只认 `deepseek-flash`，seed 用 `deepseek-v4.1-flash`，需渠道配模型映射。

**有意偏离官方价的自定价**（按决策，别对着价目表"纠正"）：

| 模型 | 定价 | 说明 |
|---|---|---|
| `gpt-5.6-sol` | $5/$30，长档 $10/$45 | 发布价；官方 $4/$20 是促销价（至少到 2026-11-21） |
| `gpt-5.6-luna` | $1/$6，长档 $2/$9 | 发布价；官方 2026-07-30 降到 $0.2/$1.2 |
| `gpt-6-luna` | $0.5/$2.5，长档 $1/$3.75 | 官方价的 5 倍 |
| `codex-auto-review` | 跟 `gpt-5.6-terra` 同价 | 非公开模型 ID，无官方价 |
| GPT Image 三个 | 每张 $0.1 | 按张不按请求，别改成 `tier("request", ...)`，否则 `n=4` 只收一张 |
| `claude-sonnet-5-5` | 命中 $0.2 | 发布价；官方 2026-10-07 降到 $0.1，其余跟官方 |
| `deepseek-v4.1-flash` | 输入 1 / 命中 0.02 / 输出 4 | 全天按国内空闲价；官方高峰时段（北京时间工作日 9–12、14–18 点）双倍 |

## Git 约定

直接在 `master` 提交并推送，不开分支、不走 PR。提交信息为简体中文单行标题、无正文，说清改了哪个模型、为什么，如：`修正 grok-4.5 缓存命中价：官方为 $0.3，此前误配成 $0.5`。
