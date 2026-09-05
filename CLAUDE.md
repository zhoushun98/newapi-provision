# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目性质

把一套 new-api 的「供应商 + 模型元信息 + 计费配置」用一条命令灌进任意 new-api 实例。**没有构建、测试框架和依赖**——`provision.py` 是纯标准库脚本，`seed.json` 是权威数据。日常工作 95% 是编辑 `seed.json` 的数据，而不是改代码。

## 命令

```bash
# 灌入配置（务必先 --dry-run 预览）
python3 provision.py --base-url http://目标机:3000 --token <访问令牌> --dry-run
python3 provision.py --base-url http://目标机:3000 --token <访问令牌>

# 全新系统：清掉出厂自带的过时模型定价
python3 provision.py --base-url http://目标机:3000 --token <访问令牌> --reset-pricing --dry-run
```

访问令牌来自目标系统：控制台 → 个人资料 → 生成访问令牌（需管理员账号）。

改完 `seed.json` 后的自检（无实例也能跑，本仓库唯一的"测试"）：

```bash
python3 -c "import json; json.load(open('seed.json'))"   # 语法
```

更有价值的是校验**覆盖完整性**与**反算单价**——见下文两节。

## 架构

单向数据流，`provision.py` 只是 `seed.json` 的幂等执行器：

```
seed.json ──> provision.py ──> new-api REST API
             （三阶段，顺序有依赖）
```

三个阶段的幂等策略各不相同，这是读代码才能看出的关键差异：

| 阶段 | 幂等策略 |
|---|---|
| `vendors` | 按名称查重；已存在则**完全不动**（不覆盖手工修改） |
| `models` | 按名称查重；已存在但 `vendor_id` 与 seed 不符则**补绑定** |
| `options_merge` | 默认只增改 seed 内的键；`--reset-pricing` 则整体替换 |

**顺序依赖**：供应商必须先建，因为模型需要 `vendor_id`。首跑时若供应商尚不存在，模型会以未绑定状态创建，重跑时由补绑定逻辑修正（这是 `d80d871` 的由来）。补绑定走 `PUT /api/models/`，需先 `GET` 完整对象、再 `pop` 掉 `bound_channels` / `enable_groups` / `quota_types` / 时间戳等只读字段，否则接口报错。

**渠道（含上游密钥）不在种子范围内**，需在目标系统手工添加；模型与渠道的绑定会自动关联。

**`--reset-pricing` 有破坏性**：目标系统上手工配过、但没进 `seed.json` 的定价会被一并清掉，`options_reset_extra` 列出的键（图片/音频倍率）会清空。务必先 `--dry-run` 看清单。

## 定价：倍率反算是核心心智模型

new-api 的 `ModelRatio` 以 **$2/MTok 为 1 倍**，即：

```
输入价 $/MTok      = ModelRatio × 2
输出价 $/MTok      = 输入价 × CompletionRatio
缓存命中价 $/MTok  = 输入价 × CacheRatio
缓存写入价 $/MTok  = 输入价 × CreateCacheRatio   （5 分钟档）
```

改动任何定价后，用这个关系反算并**逐行对照官方价目表**。例：`claude-opus-5` 倍率 `2.5 / 5 / 0.1 / 1.25` → `$5 / $25 / $0.5 / $6.25`，与 Anthropic 官方表吻合。

**倍率除不尽时不要留循环小数**（会显示成 `0.024999` 之类）——改用 `billing_setting.billing_expr` 写绝对价。判断方法：拿官方价除以输入价，除不尽就别硬凑倍率。能整除就用纯倍率，不要多配表达式。

`billing_expr` 的变量：`p` 输入、`c` 输出、`cr` 缓存命中、`cc` 缓存写入、`len` 上下文长度；`tier("档名", 表达式)` 声明计费档。GPT 系列用它做 272K 长上下文分档。

**统一填官方美元价**：当前收录的 OpenAI / Anthropic / xAI 全为美元计价，数字直入。系统不做汇率换算，将来若纳入非美元计价的厂商，得先定好折算口径。

## 数据纪律（历史上踩过的坑）

- **定价必须能从官方一手来源核实**，核不到的档位就不配，退回能核实的标准价。典型回退：`grok-4.5` 长上下文阶梯因高档价无法核实而撤销（`8a6e978`）。另一类常犯的错是**把同系列上一个小版本的价错配到新版本上**——同系列相邻版本可能完全不同价，逐位对着目标版本那一行抄。**核不到只是当时的状态，厂商补上文档后要回来补配**——Grok 的 200K 阶梯就是这样在 `docs.x.ai/docs/pricing` 列出完整高档价后补回的。查价用官方文档页：Anthropic 是 `platform.claude.com/docs/en/about-claude/pricing.md`（注意 `/docs/en/pricing.md` 是 404），OpenAI 是 `developers.openai.com/api/docs/pricing`（`platform.openai.com/docs/pricing` 会 301 过去），xAI 是 `docs.x.ai/docs/pricing`。

- **长上下文阈值各家不同**，别套用：GPT 是 272K，Grok 是 200K，Claude 1M 内不分档。Grok 还是"整个请求按高档计费"（提示词达 200K，全量按 $4/$12），不是超出部分才涨。

- **模型描述用代际表述**（「当前 / 上一代 / 旧版」），不写死「最强」这类绝对说法。新一代发布时只需把各档降一级，不用重写整组文案。

- **添加一个模型要动 4 处**，漏配会导致计费错误：`models` 数组 + `ModelRatio` + `CompletionRatio` + `CacheRatio`，Claude 系列再加 `CreateCacheRatio`。加完用脚本校验三张必需表对每个模型都有条目、且没有指向已删模型的孤儿键。

- **`CacheRatio` 不是全系 0.1**：Anthropic 从 Fable 5.1 起把缓存命中改成 **0.025x**（`claude-fable-5-1` 命中 $0.25，而 `claude-fable-5` 仍是 0.1x 的 $1）。新增 Claude 模型时别照抄上一代的 0.1，去价目表确认那一行的命中价。

- **改一个已有模型的价，四张表要一起改**：厂商降价时 `ModelRatio` / `CompletionRatio` / `CacheRatio` 和 `billing_expr` 里的绝对价必须同步，只改一处会让分档与兜底倍率打架。`gpt-5.6-terra`（$2.5/$15 → $2/$12）与 `gpt-5.6-luna`（$1/$6 → $0.2/$1.2）跟进降价时就是整组同步改的。

- **`gpt-5.6-sol` 是有意偏离官方价**：官方已降到 $4/$20（长档 $8/$30、写入 $5/$10），本仓库按决策**保留旧价 $5/$30**（长档 $10/$45、写入 $6.25/$12.5）。这不是过期数据，别当成漏更新顺手「修正」；要跟进时倍率改 `2 / 5 / 0.1`，`billing_expr` 同步换成新价。

- `claude-sonnet-5` 倍率 1（$2/$10）**已是官方标准价**：原定 2026-09-01 涨到 $3/$15 的计划被 Anthropic 明确取消，不要再按限时价处理。

## Git 约定

**直接在 `master` 上提交并推送，不开子分支、不走 PR**。提交信息用简体中文单行标题、无正文，说清改了哪个模型和为什么，例如：

```
修正 grok-4.5 缓存命中价：官方为 $0.3（倍率 0.15），此前误配成 $0.5
```
