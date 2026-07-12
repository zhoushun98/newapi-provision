#!/usr/bin/env python3
"""new-api 一键配置脚本：按 seed.json 灌入 模型元信息 → 计费配置。

幂等设计，可反复执行：
  - 模型：按名称查重，已存在则跳过；若已存在但供应商绑定与 seed 不符
    （如首跑时供应商还没建，模型以未绑定状态创建），会自动补绑定
  - 计费选项（ModelRatio 等）默认「合并」：只增改 seed 中的条目，
    不覆盖目标系统已有的其他模型条目
  - 加 --reset-pricing 则为「清空重置」：定价类选项整体替换为 seed 的精确状态，
    seed 之外的旧条目（如系统出厂默认的一大堆过时模型倍率）全部清掉，
    并将 options_reset_extra 列出的键（图片/音频倍率等）清为空表
  - 供应商：按名称查重，缺失则连同图标一起创建；已存在的不做修改

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

    # 1. 供应商：按名称查重，缺失则创建（含图标）；已存在的不做修改
    existing = call("GET", "/api/vendors/?p=1&page_size=100")["items"]
    vendor_ids = {v["name"]: v["id"] for v in existing}
    for v in seed.get("vendors", []):
        if v["name"] in vendor_ids:
            print(f"供应商已存在，跳过: {v['name']}")
            continue
        if args.dry_run:
            print(f"[dry-run] 将创建供应商: {v['name']}")
            continue
        created = call("POST", "/api/vendors/", {"name": v["name"], "icon": v.get("icon", ""), "status": 1})
        vendor_ids[v["name"]] = created["id"]
        print(f"已创建供应商: {v['name']} (id={created['id']})")

    # 2. 模型元信息：按名称查重，缺失则创建；已存在但供应商绑定不符则补绑定
    page = call("GET", "/api/models/search?keyword=&p=1&page_size=200")
    existing_models = {m["model_name"]: m for m in page["items"]}
    for m in seed["models"]:
        want_vendor = vendor_ids.get(m.get("vendor", ""), 0)
        exist = existing_models.get(m["model_name"])
        if exist:
            if want_vendor and exist.get("vendor_id") != want_vendor:
                if args.dry_run:
                    print(f"[dry-run] 将补绑定供应商: {m['model_name']} → {m['vendor']}")
                    continue
                full = call("GET", f"/api/models/{exist['id']}")
                full["vendor_id"] = want_vendor
                for k in ("bound_channels", "enable_groups", "quota_types", "created_time", "updated_time"):
                    full.pop(k, None)
                call("PUT", "/api/models/", full)
                print(f"已补绑定供应商: {m['model_name']} → {m['vendor']}")
            else:
                print(f"模型已存在，跳过: {m['model_name']}")
            continue
        body = {
            "model_name": m["model_name"],
            "vendor_id": want_vendor,
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
