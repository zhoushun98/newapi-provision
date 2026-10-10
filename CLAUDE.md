# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概况

把一套 new-api 的「供应商 + 模型元信息 + 计费配置」用一条命令灌进任意 new-api 实例。

- `seed.json`：权威数据，日常工作绝大多数是改它。三块：`vendors`（名称 + 图标）、`models`（字段对应 `/api/models/` 的列，但 `vendor` 写供应商名称，由脚本换成 `vendor_id`；**省略的字段按 `META_DEFAULTS` 的默认值写回实例，不是保持原样**）、`pricing`（只有 `billing_setting.billing_expr` 与 `billing_setting.billing_mode` 两张「模型名 → 值」表）。
- `provision.py`：把 seed 幂等地写进实例。纯标准库，用系统 `python3` 直接跑，**不要引入 uv / pyproject / 第三方依赖**。本机系统 Python 是 3.9，别用 `match`、`X | Y` 类型注解等 3.10+ 语法。
- `README.md` 面向使用者，有分类计数和逐模型的价格说明，改 seed 时同步。`AGENTS.md` 是本文件的软链，只改这里。

目标版本 **new-api v1.0.0-rc.41**（rc.40 也能用，更老的不支持）。

## 命令

```bash
python3 provision.py --check   # 离线自检 seed.json，本仓库唯一的"测试"；灌入前也会自动跑

python3 provision.py --base-url http://目标机:3000 --token <访问令牌> --dry-run   # 先预览
python3 provision.py --base-url http://目标机:3000 --token <访问令牌>             # 再灌入
# 全新系统加 --reset-pricing：连 seed 之外的模型定价也清空（有破坏性，务必先 --dry-run 看清单）
```

访问令牌：目标系统 控制台 → 个人资料 → 访问令牌。必须属于**超级管理员**（`/api/option/*` 走 `RootAuth`）；rc.41 的 `nap_` 令牌按权限授权，要勾选「模型」与「系统设置」的查看 + 编辑（`model:read/write`、`option:read/write`），缺了报 403 `ACCESS_TOKEN_SCOPE_DENIED`；只跑 `--dry-run` 有两个「查看」就够（预览接口只要 `option:read`）。rc.40 的旧令牌在实例升级后 30 天内仍可用。

`--check` 查结构和倍数：每个模型都有表达式且 mode 为 `tiered_expr`、没有旧定价键、没有孤儿键、Claude 每档写全 `cr/cc/cc1h`、缓存写入是输入价的 1.25x / 2x。**价格数字对不对它查不了**；表达式能否编译由 `--dry-run` 调后端预览接口把关。

## 架构

`seed.json → provision.py → new-api REST API`，按顺序三阶段（供应商须先于模型），每段幂等策略不同：

| 阶段 | 接口 | 策略 |
|---|---|---|
| 供应商 | `GET/POST /api/vendors/` | 按名称查重，已存在**不动** |
| 模型元信息 | `GET/POST/PUT /api/models/` | 按名称查重，已存在则逐字段对照 seed，**有差异就覆盖** |
| 定价 | `GET/PATCH /api/option/model_pricing` | seed 内的模型**整体替换**；`--reset-pricing` 另清空 seed 外的；全部变更合成一个 PATCH，单事务，任一不合法整批失败 |

读代码看不出来的约束：

- **分页**：`page_size` 被截断到 100，列表必须翻页（`list_all`），否则第 100 个之后的模型会被当成不存在，POST 时撞「模型名称已存在」。
- **模型 PUT 整行覆盖**：请求体要带齐所有列，`sync_official` 要原样带回，否则被清成 0。新建时显式传 `sync_official: 0`，免得「同步官方元数据」覆盖 seed 的文案。
- **定价只走 PATCH `model_pricing`，别改回 `PUT /api/option/`**：按单个 key 写有顺序死结（新增阶梯模型要先写 expr 再写 mode，删除则相反，否则报 `billing expression is required`）；且 `GET /api/option/` 返回的是混入后端内置表达式的生效值，回写会把内置条目固化进库。快照里的 `configured` 才是真正落库的值。
- **PATCH 细节**：每个模型带 `expected_version`（快照里没有的模型用 `empty_version`），409 表示读快照后有人改过定价，重跑即可。清空某模型的定价是提交 `pricing: {}`；`reset: true` 是恢复出厂默认，别用。
- **后端内置表达式**：`gpt-image-2`、`gpt-image-2.5-flare`、`gpt-image-2.5-sunburst`、`gpt-6-astra` 自带默认表达式，不落库（`configured` 为空，reset 不用管）。seed 给它们都配了定价，库里有值时优先于内置。
- **网络重试**：urllib 每个请求都新建 TLS 连接，偶有握手被掐或连接被断。`api()` 最多尝试 `NET_RETRIES` 次：请求没发出去（`URLError`）任何方法都重发；已发出但没收到响应的只重发 GET / PUT / 预览，POST 新建和 PATCH 定价直接报错让人重跑（脚本幂等）。
- **不在范围内**：渠道（含上游密钥）需在实例上手工加。从 seed 删掉的模型和供应商不会从实例删除，定价只有 `--reset-pricing` 才会清。

接口行为以 new-api 源码为准（文档跟不上代码），升级目标版本时拉对应 tag 对照。目录名带版本号，免得误用 `/tmp` 里残留的旧版本或克隆失败留下的空目录；只看个别文件可直接取 `https://raw.githubusercontent.com/QuantumNous/new-api/v1.0.0-rc.41/<路径>`：

```bash
git clone --depth 1 --branch v1.0.0-rc.41 https://github.com/QuantumNous/new-api.git /tmp/new-api-rc.41
```

| 要查的事 | 看哪里 |
|---|---|
| 路由与鉴权 | `router/api-router.go` |
| 访问令牌各路由要求的 scope | `middleware/access_token_routes.go`、`service/access_token_scope.go` |
| 按模型定价的读写、校验、版本号 | `controller/model_pricing_config.go`、`model/model_pricing_config.go` |
| 表达式语法、变量、token 归一化 | `pkg/billingexpr/expr.md`、`service/tiered_settle.go`（`BuildTieredTokenParams`） |
| 后端内置表达式 | `setting/billing_setting/builtin_billing.go` |
| 分页上限 | `common/page_info.go` |
| 模型元信息的新建 / 更新列 | `controller/model_meta.go`、`model/model_meta.go` |

## 定价表达式

rc.40 起倍率 / 按次两种旧模式已弃用，seed 只写计费表达式，系数直接是官方美元价（$/MTok）；`--check` 会拒绝 `ModelRatio` 等旧键。

```
claude-opus-5-5  tier("standard", p * 4 + c * 20 + cr * 0.2 + cc * 5 + cc1h * 8)
gpt-6-sol        len <= 272000 ? tier("standard", ...) : tier("long_context", ...)
gpt-image-2      tier("image", fixed(0.1)) * image_count
```

变量：`p` 输入、`c` 输出、`cr` 缓存命中、`cc` 缓存写入（5 分钟）、`cc1h` 缓存写入（1 小时，仅 Claude）、`len` 上下文长度。

**缓存 token 的归一化两家不同**，最容易写错：

- **Claude**：`p` 不含缓存。没写 `cr` 时命中 token 并入 `p` 按输入价收；没写 `cc` / `cc1h` 则这部分**完全不计费**。所以 Claude 每档都要写全 `p c cr cc cc1h`。
- **OpenAI**：`p` 含全部缓存。写了 `cr` / `cc` 就从 `p` 里扣出来单独计价，没写的留在 `p` 按输入价收。所以 GPT-5.5（不收写入费）不写 `cc` 是对的。

**价格规律**：缓存写入 = 输入价 × 1.25（5 分钟）/ × 2（1 小时），两家一样。缓存命中价**逐型号不同**，不是全系 0.1x（如 `claude-fable-5-1` 0.025x，`claude-opus-5-5`、`claude-sonnet-5-5`（官方，seed 不跟）、`gpt-6.1-sol` 0.05x），新增模型别照抄上一代——`gpt-6.1-sol` 与 `gpt-6-sol` 只差命中价。

**长上下文**：整单按高档计费（不是超出部分才涨），阈值边界各家不同——GPT 输入**超过** 272K 才涨（短档 `len <= 272000`），Grok **达到** 200K 就涨（短档 `len < 200000`），Claude 只有 `claude-haiku-5-5` 分档（输入**超过** 100K 才涨，短档 `len <= 100000`；Claude 的 `len` 含缓存读写，与官方口径一致），其余 1M 内不分档。

**峰谷价**（DeepSeek）：高峰为北京时间周一至周五 9:00–12:00、14:00–18:00，空闲半价，用 `weekday("Asia/Shanghai")` / `hour("Asia/Shanghai")` 分 `peak` / `off_peak` 两档。表达式识别不了法定节假日，工作日节假日会按高峰收，接受这个偏差。官方调用名是 `deepseek-flash`，seed 用 `deepseek-v4.1-flash`，靠渠道的模型映射转过去。

**`fixed()`** 是按次 / 按张的完整价格，所在的档不能再加 token 项，只能乘 `image_count`（取请求顶层 `n`）。

## 数据纪律

- **价格必须能从官方一手来源核实**，核不到的档位不配、退回标准价；厂商补上文档后要回来补配。同系列相邻版本可能完全不同价，逐行对着目标型号抄。改价时短档和长档（含 `cr`、`cc`）一起改。价目页：
  - Anthropic：`platform.claude.com/docs/en/about-claude/pricing.md`（`/docs/en/pricing.md` 是 404）
  - OpenAI：`developers.openai.com/api/docs/pricing`；历史调价看 `developers.openai.com/api/docs/changelog`
  - xAI：`docs.x.ai/developers/pricing`
  - DeepSeek：`api-docs.deepseek.com/zh-cn/quick_start/pricing`（国内人民币价；英文页是美元价，不用）；发布公告在 `api-docs.deepseek.com/zh-cn/news/`
- **加 / 删一个模型**：seed 里动 3 处——`models` + `billing_expr` + `billing_mode`（`tiered_expr`）。新条目插在同系列相邻型号旁，两张定价表的键序跟 `models` 一致。再同步 README 的分类计数和价格说明，最后跑 `--check`。
- **不写 `tags`**（脚本会清掉实例上的标签）。
- **描述一句话写「定位 + 擅长场景」**，以官方模型页的一句话介绍为准（new-api 上游元数据 `basellm.github.io/llm-metadata/api/newapi/models.json` 可参考句式，但内容要能在官方核实）。型号名看不出档位的先写档位（如「GPT-6 旗舰」「GPT Image 2.5 快速型」）。**不写**「当前 / 上一代 / 旧版」这类会过时的相对说法，不写「最强」「最快」，不写价格、缓存比例、上下文长度。新模型发布时不用改老模型的描述。
- **`endpoints` 用数组形式**：GPT `["openai", "openai-response"]`，Claude `["anthropic", "openai"]`，Grok `["openai"]`，DeepSeek `["openai", "openai-response", "anthropic"]`，GPT Image `["image-generation"]`。`icon` 跟所属供应商的图标一致。UI 的「转换为计费表达式」只认 map 形式，对这些模型会报 `The model routing configuration could not be verified`，不用管，别为此改成 map（map 是自定义端点路径，会覆盖定价页上该端点类型的全局路径）。
- **币种**：系统不做汇率换算。OpenAI / Anthropic / xAI 填美元价；DeepSeek 按决策**把国内人民币价的数字直接当美元填**（不折算，额度消耗约为美元口径的 7 倍），别"纠正"成美元价或折算价。再纳入其他非美元厂商前先问清口径。

**有意偏离官方价的自定价**（按决策，别对着价目表"纠正"）：

| 模型 | 定价 | 说明 |
|---|---|---|
| `gpt-5.6-sol` | $5/$30，长档 $10/$45 | 发布价；官方现价 $4/$20 是促销价（至少到 2026-11-21） |
| `gpt-5.6-luna` | $1/$6，长档 $2/$9 | 发布价；官方 2026-07-30 降到 $0.2/$1.2 |
| `claude-sonnet-5-5` | 缓存命中 $0.2（0.1x） | 发布价；官方 2026-10-07 降到 $0.1（0.05x），其余价格跟官方 |
| `codex-auto-review` | 跟 `gpt-5.6-terra` 同价 | 不是公开的模型 ID，无官方价可核（[openai/codex#20981](https://github.com/openai/codex/issues/20981)） |
| `gpt-6-luna` | $0.5/$2.5，长档 $1/$3.75 | 官方价的 5 倍，与 5.6-luna 发布价相对现价的倍数一致；输出**不是** 5.6-luna 的一半而是 5/12 |
| GPT Image 三个 | 每张 $0.1 | 按张不按请求，别改成 `tier("request", ...)`，否则 `n=4` 只收一张的钱 |

`gpt-5.6-terra` 跟官方现价。`claude-sonnet-5` 的 $2/$10 已是官方标准价（原定的涨价被取消）。

## Git 约定

直接在 `master` 上提交并推送，不开分支、不走 PR。提交信息用简体中文单行标题、无正文，说清改了哪个模型、为什么，例如：

```
修正 grok-4.5 缓存命中价：官方为 $0.3，此前误配成 $0.5
```
