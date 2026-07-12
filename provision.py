#!/usr/bin/env python3
"""new-api 一键配置脚本：按 seed.json 灌入 模型元信息 → 计费配置。

幂等设计，可反复执行：
  - 模型：按名称查重，已存在则跳过
  - 计费选项（ModelRatio 等）默认「合并」：只增改 seed 中的条目，
    不覆盖目标系统已有的其他模型条目
  - 加 --reset-pricing 则为「清空重置」：定价类选项整体替换为 seed 的精确状态，
    seed 之外的旧条目（如系统出厂默认的一大堆过时模型倍率）全部清掉，
    并将 options_reset_extra 列出的键（图片/音频倍率等）清为空表
  - 供应商：不创建、不修改（自行在 UI 维护）；仅按名称只读查询用于模型绑定，
    查不到则该模型不绑定供应商并给出提示

用法：
  python3 provision.py --base-url http://目标机:3000 --token <管理员访问令牌> [--user-id 1] [--dry-run]

管理员访问令牌：目标系统 控制台 → 个人资料 → 生成访问令牌（access token）。
"""

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path


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
        sys.exit(f"HTTP {e.code} {method} {path}: {e.read().decode()[:300]}")
    if not data.get("success", False):
        sys.exit(f"API 失败 {method} {path}: {data.get('message')}")
    return data.get("data")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--token", required=True)
    ap.add_argument("--user-id", default="1")
    ap.add_argument("--seed", default=str(Path(__file__).parent / "seed.json"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--reset-pricing", action="store_true",
                    help="定价选项整体替换为 seed 精确状态（清掉 seed 之外的所有旧条目），默认为合并模式")
    args = ap.parse_args()

    seed = json.loads(Path(args.seed).read_text())

    def call(method, path, body=None):
        return api(args.base_url, args.token, args.user_id, method, path, body)

    # 1. 供应商：只读查询，用于把 seed 里的供应商名解析成目标系统的 id（不创建、不修改）
    existing = call("GET", "/api/vendors/?p=1&page_size=100")["items"]
    vendor_ids = {v["name"]: v["id"] for v in existing}
    missing = {m["vendor"] for m in seed["models"] if m.get("vendor") and m["vendor"] not in vendor_ids}
    for name in sorted(missing):
        print(f"提示: 目标系统没有供应商「{name}」，相关模型将不绑定供应商（请自行在 UI 创建后重跑）")

    # 2. 模型元信息：按名称查重，缺失则创建
    page = call("GET", "/api/models/search?keyword=&p=1&page_size=200")
    existing_models = {m["model_name"] for m in page["items"]}
    for m in seed["models"]:
        if m["model_name"] in existing_models:
            print(f"模型已存在，跳过: {m['model_name']}")
            continue
        body = {
            "model_name": m["model_name"],
            "vendor_id": vendor_ids.get(m["vendor"], 0),
            "icon": m.get("icon", ""),
            "endpoints": m.get("endpoints", ""),
            "tags": m.get("tags", ""),
            "description": m.get("description", ""),
            "status": m.get("status", 1),
            "name_rule": m.get("name_rule", 0),
        }
        if args.dry_run:
            print(f"[dry-run] 将创建模型: {m['model_name']} (vendor={m['vendor']})")
            continue
        call("POST", "/api/models/", body)
        print(f"已创建模型: {m['model_name']}")

    # 3. 计费选项：读取现值 → 合并或整体替换 → 有变化才写回
    current = {o["key"]: o["value"] for o in call("GET", "/api/option/")}

    def put_option(key, value_map, note):
        if args.dry_run:
            print(f"[dry-run] 将更新选项 {key}：{note}")
            return
        call("PUT", "/api/option/", {"key": key, "value": json.dumps(value_map, ensure_ascii=False)})
        print(f"已更新选项: {key}（{note}）")

    for key, entries in seed["options_merge"].items():
        live = json.loads(current.get(key) or "{}")
        if args.reset_pricing:
            if live == entries:
                print(f"选项已是目标状态，跳过: {key}")
                continue
            removed = [k for k in live if k not in entries]
            put_option(key, entries, f"重置为 {len(entries)} 项，清除旧条目 {len(removed)} 项")
        else:
            changed = {k: v for k, v in entries.items() if live.get(k) != v}
            if not changed:
                print(f"选项无变化，跳过: {key}")
                continue
            live.update(changed)
            put_option(key, live, f"新增/修改 {len(changed)} 项: {', '.join(changed)}")

    if args.reset_pricing:
        for key in seed.get("options_reset_extra", []):
            live = json.loads(current.get(key) or "{}")
            if not live:
                print(f"选项已为空，跳过: {key}")
                continue
            put_option(key, {}, f"清空（原有 {len(live)} 项）")

    print("完成。" + ("（dry-run，未做任何修改）" if args.dry_run else ""))


if __name__ == "__main__":
    main()
