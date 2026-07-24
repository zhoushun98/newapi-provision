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

**倍率除不尽时不要留循环小数**（会显示成 `0.024999` 之类）——改用 `billing_setting.billing_expr` 写绝对价。`deepseek-v4-pro` 就是这个原因（`fe82b11`）：`0.025 / 3` 除不尽。能整除就用纯倍率，不要多配表达式。

`billing_expr` 的变量：`p` 输入、`c` 输出、`cr` 缓存命中、`cc` 缓存写入、`len` 上下文长度；`tier("档名", 表达式)` 声明计费档。GPT 系列用它做 272K 长上下文分档。

**币种混用且系统不做换算**：国外模型（GPT / Claude / grok）填官方美元价，DeepSeek / 智谱填国内官方人民币价，数字直入。

## 数据纪律（历史上踩过的坑）

- **定价必须能从官方一手来源核实**，核不到的档位就不配，退回能核实的标准价。两次回退：`grok-4.5` 长上下文阶梯因高档价无法核实而撤销（`8a6e978`）；`glm-5.2` 误用了 GLM-5.1 的 ¥6/¥24 档（`6c998dc`）。查 Anthropic 价格用 `platform.claude.com/docs/en/about-claude/pricing.md`（注意 `/docs/en/pricing.md` 是 404）。

- **模型描述用代际表述**（「当前 / 上一代 / 旧版」），不写死「最强」这类绝对说法。新一代发布时只需把各档降一级，不用重写整组文案。

- **添加一个模型要动 4 处**，漏配会导致计费错误：`models` 数组 + `ModelRatio` + `CompletionRatio` + `CacheRatio`，Claude 系列再加 `CreateCacheRatio`。加完用脚本校验三张必需表对每个模型都有条目、且没有指向已删模型的孤儿键。

- `claude-sonnet-5` 倍率 1（$2/$10）是官方限时价，**2026-08-31 后改为 1.5**（$3/$15）。

## Git 约定

**直接在 `master` 上提交并推送，不开子分支、不走 PR**。提交信息用简体中文单行标题、无正文，说清改了哪个模型和为什么，例如：

```
修正 glm-5.2 定价：官方为平价 ¥8/¥28/命中¥2，撤掉误配的阶梯表达式（¥6/¥24 档属于 GLM-5.1）
```
