# new-api 配置种子

把一套 new-api 的「供应商 + 模型元信息 + 计费配置」用一条命令灌进任何新实例，不用在 UI 里一个个点。

## 文件

- `seed.json` — 权威配置数据（从线上实例导出，可按需修改）：
  - 5 个供应商（OpenAI、Anthropic、DeepSeek、智谱、xAI，含图标）
  - 18 个模型的元信息：名称、供应商归属、图标、标签、简介、端点、匹配类型
  - 计费：`ModelRatio` / `CompletionRatio` / `CacheRatio` / `CreateCacheRatio`（14 个模型的倍率），以及 5 个 GPT 模型的 272K 长上下文阶梯表达式（`billing_setting.*`）
- `provision.py` — 幂等灌入脚本，标准库实现，无依赖

## 用法

```bash
# 1. 在目标系统：控制台 → 个人资料 → 生成访问令牌（需管理员账号）
# 2. 灌入（先 --dry-run 预览）
python3 provision.py --base-url http://目标机:3000 --token <访问令牌> --dry-run
python3 provision.py --base-url http://目标机:3000 --token <访问令牌>

# 全新系统建议加 --reset-pricing：清掉出厂自带的一大堆过时模型定价
python3 provision.py --base-url http://目标机:3000 --token <访问令牌> --reset-pricing --dry-run
python3 provision.py --base-url http://目标机:3000 --token <访问令牌> --reset-pricing
```

两种定价写入模式：

- **默认（合并）**：只增改 seed 里的条目，目标系统已有的其他模型定价原样保留。适合往已经在用的系统里补配置。
- **`--reset-pricing`（清空重置）**：模型倍率 / 补全倍率 / 缓存倍率 / 固定定价等整体替换为 seed 的精确状态，seed 之外的旧条目（ERNIE、BLOOMZ、mj_* 等出厂默认）全部清掉，图片/音频倍率清为空表。适合初始化全新系统。⚠️ 目标系统上手工配过、但没进 seed 的定价也会被清掉——先 `--dry-run` 看清单。

**可反复执行（幂等）**：模型按名称查重跳过；两种模式下选项都是先比对、无差异就跳过，重复跑 N 遍结果一致，不会重复创建或反复写库。

## 注意

- **供应商自动创建**：全新系统的供应商列表是空的，脚本会按 seed 自动补齐（按名称查重，已存在的不动、不覆盖你的手工修改）；早前以未绑定状态创建的模型会在重跑时自动补上供应商归属。
- **渠道不在种子范围内**（含上游密钥，不适合进仓库）——新系统需自行添加渠道，模型与渠道的绑定会自动关联。
- `claude-sonnet-5` 倍率 1（$2/$10）是官方限时价，**2026-08-31 后改为 1.5**（$3/$15），届时更新 `seed.json` 并重跑即可（合并逻辑会检测到差异并写入）。
- 分组倍率（GroupRatio）、按次计费模型（如 gpt-image-2）不在种子内；需要时往 `seed.json` 的 `options_merge` 里加对应键即可。
- 配置有变化时，重新从线上导出或直接改 `seed.json`，然后对所有实例重跑一遍。
