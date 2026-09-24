#!/usr/bin/env python3
"""new-api 一键配置脚本：按 seed.json 灌入 供应商 → 模型元信息 → 计费配置。

适配 new-api v1.0.0-rc.40 及以上：定价走按模型的 /api/option/model_pricing 接口。

幂等设计，可反复执行：
  - 供应商：按名称查重，缺失则连同图标一起创建；已存在的不做修改
  - 模型元信息：按名称查重，缺失则创建；已存在则以 seed 为准，描述 / 图标 / 标签 /
    端点 / 状态 / 匹配规则 / 供应商绑定有差异才更新
  - 定价：seed 只用计费表达式（rc.40 已弃用倍率 / 按次两种旧模式）；seed 内的模型整体替换为
    seed 的定价（该模型在 seed 里没配的键会被清掉，例如旧的倍率）；seed 之外的模型默认不动
  - 加 --reset-pricing 则连 seed 之外的模型定价也全部清空（出厂默认的一大堆过时倍率、
    手工配过的其他模型），只留 seed 的精确状态；后端内置的计费表达式（gpt-image-* 等）
    本就不落库，不受影响
  - 全部定价变更合成一个请求提交：后端单事务写入、逐模型整体校验，任一失败整批不生效

用法：
  python3 provision.py --base-url http://目标机:3000 --token <超级管理员访问令牌> [--user-id 1] [--dry-run]
  python3 provision.py --check      # 只离线校验 seed.json，不连实例

访问令牌：目标系统 控制台 → 个人资料 → 生成访问令牌（定价接口要求超级管理员）。
"""

import argparse
import json
import math
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

# rc.40 已弃用「按 Token（倍率）」「按次」两种旧定价模式，seed 只用计费表达式
EXPR_KEY, MODE_KEY = "billing_setting.billing_expr", "billing_setting.billing_mode"
META_DEFAULTS = {"description": "", "icon": "", "tags": "", "endpoints": "", "status": 1, "name_rule": 0}


class ApiError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def api(base, token, user_id, method, path, body=None):
    req = urllib.request.Request(
        base.rstrip("/") + path,
        data=json.dumps(body, ensure_ascii=False).encode() if body is not None else None,
        method=method,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "New-Api-User": str(user_id),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise ApiError(f"HTTP {e.code} {method} {path}: {e.read().decode()[:300]}", e.code)
    if not data.get("success", False):
        raise ApiError(f"API 失败 {method} {path}: {data.get('message')}")
    return data.get("data")


def check_seed(seed):
    """离线校验 seed.json，返回错误列表。覆盖 CLAUDE.md「数据纪律」里能机器检查的部分。"""
    errors = []
    vendors = {v["name"] for v in seed.get("vendors", [])}
    names = [m["model_name"] for m in seed["models"]]
    dup = sorted({n for n in names if names.count(n) > 1})
    if dup:
        errors.append(f"models 里有重名: {', '.join(dup)}")
    for m in seed["models"]:
        if m.get("vendor") and m["vendor"] not in vendors:
            errors.append(f"{m['model_name']}: 供应商 {m['vendor']} 不在 vendors 里")
        try:
            json.loads(m.get("endpoints") or "[]")
        except ValueError:
            errors.append(f"{m['model_name']}: endpoints 不是合法 JSON")

    pricing = seed["pricing"]
    for key in pricing:
        if key not in (EXPR_KEY, MODE_KEY):
            errors.append(f"pricing 里有 {key}：旧定价模式已弃用，价格一律写进 {EXPR_KEY}")
    exprs, modes = pricing.get(EXPR_KEY, {}), pricing.get(MODE_KEY, {})
    for name in sorted((set(exprs) | set(modes)) - set(names)):
        errors.append(f"pricing.{name}: 指向 models 里不存在的模型（孤儿键）")
    for name in names:
        expr = exprs.get(name)
        if modes.get(name) != "tiered_expr" or not isinstance(expr, str) or not expr.strip():
            errors.append(f"{name}: 需要 {EXPR_KEY}，且 {MODE_KEY} 为 tiered_expr")
            continue
        errors += check_expr(name, expr)
    return errors


def check_expr(name, expr):
    """逐档检查缓存相关的系数。表达式能否编译交给后端预览接口（--dry-run）。"""
    tiers = re.findall(r'tier\("(\w+)",([^)]*)\)', expr)
    if not tiers:
        return [f"{name}: billing_expr 里没有 tier(...)"]
    errors = []
    for tier, body in tiers:
        coef = {v: float(x) for v, x in re.findall(r"\b(p|c|cr|cc|cc1h)\s*\*\s*([\d.]+)", body)}
        if "p" not in coef or "c" not in coef:
            errors.append(f"{name}: {tier} 档缺少 p 或 c")
            continue
        # Claude 格式的用量里缓存 token 不并入 p：漏写 cc / cc1h 这部分就不计费，漏写 cr 会按输入价收
        missing = [v for v in ("cr", "cc", "cc1h") if v not in coef] if name.startswith("claude-") else []
        if missing:
            errors.append(f"{name}: {tier} 档缺少 {', '.join(missing)}")
        # OpenAI 与 Anthropic 官方的缓存写入价都是输入价的固定倍数：5 分钟 1.25x，1 小时 2x
        for v, times in (("cc", 1.25), ("cc1h", 2)):
            if v in coef and not math.isclose(coef[v], coef["p"] * times, rel_tol=1e-9):
                errors.append(f"{name}: {tier} 档 {v} * {coef[v]:g} 应为输入价的 {times:g} 倍（{coef['p'] * times:g}）")
    return errors


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url")
    ap.add_argument("--token")
    ap.add_argument("--user-id", default="1")
    ap.add_argument("--seed", default=str(Path(__file__).parent / "seed.json"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--check", action="store_true", help="只离线校验 seed.json，不连接实例")
    ap.add_argument("--reset-pricing", action="store_true",
                    help="连 seed 之外的模型定价也全部清空，只留 seed 的精确状态；默认只动 seed 内的模型")
    args = ap.parse_args()

    seed = json.loads(Path(args.seed).read_text())
    errors = check_seed(seed)
    if errors:
        sys.exit("seed.json 校验失败：\n  " + "\n  ".join(errors))
    if args.check:
        print(f"seed.json 校验通过：{len(seed['vendors'])} 个供应商、{len(seed['models'])} 个模型")
        return
    if not (args.base_url and args.token):
        ap.error("需要 --base-url 与 --token（或用 --check 只做离线校验）")

    def call(method, path, body=None):
        return api(args.base_url, args.token, args.user_id, method, path, body)

    def list_all(path):
        # rc.40 的分页接口把 page_size 截断到 100，超出部分必须翻页
        items, page = [], 1
        while True:
            data = call("GET", f"{path}?p={page}&page_size=100")
            items += data["items"] or []
            if not data["items"] or len(items) >= data["total"]:
                return items
            page += 1

    # 先探测定价接口：旧版本没有它，在任何写入发生前就报错
    try:
        snapshot = call("GET", "/api/option/model_pricing")
    except ApiError as e:
        if e.status == 404:
            sys.exit("目标实例没有 /api/option/model_pricing 接口，需要 new-api v1.0.0-rc.40 及以上")
        raise

    # 1. 供应商：按名称查重，缺失则创建（含图标）；已存在的不做修改
    vendor_ids = {v["name"]: v["id"] for v in list_all("/api/vendors/")}
    for v in seed.get("vendors", []):
        if v["name"] in vendor_ids:
            print(f"供应商已存在，跳过: {v['name']}")
            continue
        if args.dry_run:
            print(f"[dry-run] 将创建供应商: {v['name']}")
            vendor_ids[v["name"]] = -1  # 占位，仅用于 dry-run 展示模型的绑定差异
            continue
        created = call("POST", "/api/vendors/", {"name": v["name"], "icon": v.get("icon", ""), "status": 1})
        vendor_ids[v["name"]] = created["id"]
        print(f"已创建供应商: {v['name']} (id={created['id']})")
    vendor_names = {vid: name for name, vid in vendor_ids.items()}

    # 2. 模型元信息：缺失则创建；已存在则逐字段对照 seed，有差异才更新
    def same(field, a, b):
        if field == "endpoints":  # UI 保存可能改掉空格，按 JSON 语义比较
            try:
                return json.loads(a or "null") == json.loads(b or "null")
            except ValueError:
                pass
        return a == b

    def show(field, value):
        if field == "vendor_id":
            return vendor_names.get(value, "无") if value else "无"
        return json.dumps(value, ensure_ascii=False)

    existing_models = {m["model_name"]: m for m in list_all("/api/models/")}
    for m in seed["models"]:
        name = m["model_name"]
        want = {f: m.get(f, d) for f, d in META_DEFAULTS.items()}
        want["vendor_id"] = vendor_ids.get(m.get("vendor"), 0)
        exist = existing_models.get(name)
        if not exist:
            if args.dry_run:
                print(f"[dry-run] 将创建模型: {name} (vendor={m.get('vendor')})")
                continue
            # sync_official=0：seed 才是元信息的权威，关掉「同步官方元数据」以免被上游覆盖
            call("POST", "/api/models/", {"model_name": name, **want, "sync_official": 0})
            print(f"已创建模型: {name}")
            continue
        have = {f: exist.get(f, d) for f, d in META_DEFAULTS.items()}
        have["vendor_id"] = exist.get("vendor_id", 0)
        changed = [f for f in want if not same(f, have[f], want[f])]
        if not changed:
            print(f"模型元信息一致，跳过: {name}")
            continue
        if args.dry_run:
            detail = "；".join(f"{f}: {show(f, have[f])} → {show(f, want[f])}" for f in changed)
            print(f"[dry-run] 将更新模型元信息: {name}（{detail}）")
            continue
        # PUT 会整行覆盖这些列，sync_official 必须原样带回
        call("PUT", "/api/models/", {"id": exist["id"], "model_name": name,
                                     "sync_official": exist.get("sync_official", 0), **want})
        print(f"已更新模型元信息: {name}（{', '.join(changed)}）")

    # 3. 定价：按模型对照 seed，生成变更集后一次提交
    desired = {}
    for key, entries in seed["pricing"].items():
        for name, value in entries.items():
            desired.setdefault(name, {})[key] = value
    live = {e["model_name"]: e for e in snapshot["entries"]}

    def pricing_diff(old, new):
        parts = []
        for key in sorted(set(old) | set(new)):
            a, b = old.get(key), new.get(key)
            if a == b:
                continue
            short = key.removeprefix("billing_setting.")
            if key.startswith("billing_setting."):
                parts.append(f"{short} {'新增' if a is None else '删除' if b is None else '变更'}")
            else:
                parts.append(f"{short} {'无' if a is None else a}→{'无' if b is None else b}")
        return "，".join(parts)

    changes = []
    for name, target in desired.items():
        entry = live.get(name)
        configured = entry["configured"] if entry else {}
        if configured == target:
            print(f"定价一致，跳过: {name}")
            continue
        changes.append({"model_name": name, "pricing": target,
                        "expected_version": entry["version"] if entry else snapshot["empty_version"]})
        print(f"{'[dry-run] ' if args.dry_run else ''}将写入定价: {name}（{pricing_diff(configured, target)}）")
    if args.reset_pricing:
        stale = sorted(n for n, e in live.items() if n not in desired and e["configured"])
        for name in stale:
            changes.append({"model_name": name, "pricing": {}, "expected_version": live[name]["version"]})
        if stale:
            shown = ", ".join(stale[:20]) + (f" …等 {len(stale)} 个" if len(stale) > 20 else "")
            print(f"{'[dry-run] ' if args.dry_run else ''}将清除 seed 之外的模型定价: {shown}")
        else:
            print("seed 之外没有已配置的模型定价，无需清除")

    if not changes:
        print("定价无变化。")
    elif args.dry_run:
        # 预览接口无写入副作用：让后端按真实校验规则把每份草稿过一遍（表达式能否编译等）
        for c in changes:
            if c["pricing"]:
                call("POST", "/api/option/model_pricing/preview", {"model_name": c["model_name"], "pricing": c["pricing"]})
        print(f"[dry-run] 共 {len(changes)} 个模型的定价待提交，后端预校验通过")
    else:
        call("PATCH", "/api/option/model_pricing", {"changes": changes})
        print(f"已提交 {len(changes)} 个模型的定价变更")

    print("完成。" + ("（dry-run，未做任何修改）" if args.dry_run else ""))


if __name__ == "__main__":
    try:
        main()
    except ApiError as e:
        sys.exit(str(e))
