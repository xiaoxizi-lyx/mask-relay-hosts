#!/usr/bin/env python3
"""
生成各国家/地区主流运营商的 ECS 候选 /24 子网。

数据源：
  - APNIC Labs AS 用户人口估算（aspop）：每个国家/地区按用户数排名的 ASN
  - RIPE Atlas 探针存档：各国在线探针所在 ASN，作为 aspop 失真时的补充
  - ipverse/as-ip-blocks：每个 ASN 当前 BGP 宣告的前缀 + 分类（isp/hosting/...）
  - IP API：核对挑出的 /24 确实位于该国家/地区

流程：每个国家/地区取用户最多的 isp 类 ASN（累计覆盖 aspop_cover_pct% 或最多 aspop_max_asns 个），
再补充 Atlas 探针最多的 isp 类 ASN；每个 ASN 从不同前缀里确定性地挑 /24，用 IP API 核对国家，
直到每个 ASN 有 subnets_per_asn 个合格子网。上一次已核对过且仍有效的子网直接沿用，减少 API 调用。

环境变量：IP_KEY、IP_API_BASE（如 https://example.b-cdn.net/）
仅依赖 Python 3.8+ 标准库。
"""

import argparse
import bz2
import csv
import datetime as dt
import hashlib
import io
import ipaddress
import json
import os
import re
import sys
import tarfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASPOP_URL = "https://stats.labs.apnic.net/cgi-bin/aspopjson"
ATLAS_URL = "https://ftp.ripe.net/ripe/atlas/probes/archive/meta-latest"
IPVERSE_URL = "https://github.com/ipverse/as-ip-blocks/releases/latest/download/as-ip-blocks.tar.gz"
UA = "mask-relay-hosts/seeds (+https://github.com/)"
CC_RE = re.compile(r"^[A-Z]{2}$")
TODAY = dt.datetime.now(dt.timezone.utc).date()


def log(msg):
    print(msg, flush=True)


def fetch(url, timeout=120, tries=4):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:
            last = e
            time.sleep(3 * (i + 1))
    raise RuntimeError("下载失败 %s: %s" % (url, last))


# ------------------------------------------------------------ 数据源 ----

def load_aspop():
    rows = json.loads(fetch(ASPOP_URL))
    by_cc = defaultdict(list)
    for r in rows:
        cc = r.get("cc", "")
        if CC_RE.match(cc) and str(r.get("asn", "")).startswith("AS"):
            by_cc[cc].append({"asn": int(r["asn"][2:]), "descr": r.get("descr", ""),
                              "pct": r.get("country-percent") or 0, "users": r.get("users") or 0})
    for cc in by_cc:
        by_cc[cc].sort(key=lambda x: -x["users"])
    log("aspop: %d 个国家/地区, %d 行" % (len(by_cc), len(rows)))
    return by_cc


def load_atlas():
    objs = json.loads(bz2.decompress(fetch(ATLAS_URL)))
    objs = objs["objects"] if isinstance(objs, dict) else objs
    probes = defaultdict(Counter)          # cc -> Counter(asn)
    prefixes = defaultdict(Counter)        # (cc, asn) -> Counter(prefix_v4)
    n = 0
    for o in objs:
        if o.get("status") != 1 or o.get("is_anchor"):
            continue
        cc, asn = o.get("country_code"), o.get("asn_v4")
        if not cc or not asn or not CC_RE.match(cc):
            continue
        n += 1
        probes[cc][asn] += 1
        if o.get("prefix_v4"):
            prefixes[(cc, asn)][o["prefix_v4"]] += 1
    log("atlas: %d 个在线探针, %d 个国家/地区" % (n, len(probes)))
    return probes, prefixes


def load_ipverse(wanted):
    data = fetch(IPVERSE_URL, timeout=300)
    out = {}
    pat = re.compile(r"^(?:\./)?as/(\d+)/aggregated\.json$")
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
        for m in tf:
            mm = pat.match(m.name)
            if mm and int(mm.group(1)) in wanted:
                d = json.load(tf.extractfile(m))
                md = d.get("metadata") or {}
                out[int(mm.group(1))] = {
                    "name": ("%s - %s" % (md.get("handle", ""), md.get("description", ""))).strip(" -"),
                    "cc": md.get("countryCode"), "category": md.get("category"),
                    "ipv4": (d.get("prefixes") or {}).get("ipv4") or []}
    log("ipverse: 需要 %d 个 ASN，读到 %d 个" % (len(wanted), len(out)))
    return out


# ------------------------------------------------------------- IP API ----

class IPApi:
    def __init__(self, base, key, cap):
        self.base, self.key, self.cap = base, key, cap
        self.calls, self.lock, self.cache = 0, threading.Lock(), {}

    def lookup(self, ip):
        with self.lock:
            if ip in self.cache:
                return self.cache[ip]
            if self.calls >= self.cap:
                return None
            self.calls += 1
        url = "%s?%s" % (self.base, urllib.parse.urlencode({"key": self.key, "ip": ip}))
        geo, last = None, None
        for i in range(3):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=20) as r:
                    geo = json.loads(r.read()).get("geo")
                break
            except urllib.error.HTTPError as e:
                if e.code in (401, 403):
                    raise SystemExit("IP API 拒绝访问（HTTP %d），请检查 IP_KEY" % e.code)
                last = e
            except Exception as e:
                last = e
            time.sleep(1 + i)
        if geo is None and last is not None:
            log("  ! IP API 查询 %s 失败: %s" % (ip, last))
        with self.lock:
            self.cache[ip] = geo
        return geo


def geo_brief(g):
    return {k: g.get(k, "") for k in ("country_code", "country", "country_en", "region", "city", "asn", "isp")}


# ------------------------------------------------------------- 选种子 ----

def pick_24(asn, net):
    """在前缀内确定性地挑一个 /24（同一前缀每次结果相同，避免无谓变动）。"""
    if net.prefixlen >= 24:
        return ipaddress.ip_network((net.network_address, 24), strict=False)
    n = 1 << (24 - net.prefixlen)
    idx = int(hashlib.sha256(("%d|%s" % (asn, net)).encode()).hexdigest(), 16) % n
    return ipaddress.ip_network((int(net.network_address) + idx * 256, 24))


def candidate_nets(asn, ipv4, probe_prefixes):
    nets = []
    for p in ipv4:
        try:
            net = ipaddress.ip_network(p)
        except ValueError:
            continue
        if net.prefixlen <= 24 and net.is_global:
            nets.append(net)
    probed = Counter()
    for pfx, cnt in probe_prefixes.items():
        try:
            pnet = ipaddress.ip_network(pfx)
        except ValueError:
            continue
        for net in nets:
            if pnet.version == 4 and (pnet.subnet_of(net) or net.subnet_of(pnet)):
                probed[net] += cnt
    # 有 Atlas 探针的前缀优先（真实家庭宽带所在），其余按前缀从大到小
    nets.sort(key=lambda n: (-probed[n], n.prefixlen, int(n.network_address)))
    return nets


def choose_asns(cc, aspop, atlas_probes, meta, cfg):
    picks = {}   # asn -> {source, users_pct, atlas_probes}
    excluded = set(cfg["exclude_asns"].get(cc, []))
    if cc not in cfg["aspop_unreliable"]:
        for cats in (cfg["asn_categories"], cfg["fallback_categories"]):
            cum = 0
            for r in aspop.get(cc, [])[:cfg["aspop_scan_rows"]]:
                m = meta.get(r["asn"])
                if r["asn"] in excluded or not m or not m["ipv4"] or m["category"] not in cats:
                    continue
                if picks and r["pct"] < cfg["aspop_min_pct"]:
                    break
                picks[r["asn"]] = {"source": ["aspop"], "users_pct": r["pct"], "as_name": m["name"] or r["descr"]}
                cum += r["pct"]
                if cum >= cfg["aspop_cover_pct"] or len(picks) >= cfg["aspop_max_asns"]:
                    break
            if picks:
                break
    # Atlas：aspop 失真的地区作为主数据源；其他地区只补充在 aspop 中也有一定用户量的 ASN
    unreliable = cc in cfg["aspop_unreliable"] or not aspop.get(cc)
    atlas_max = cfg["aspop_max_asns"] if unreliable else cfg["atlas_max_asns"]
    added = 0
    for asn, cnt in atlas_probes.get(cc, Counter()).most_common():
        if added >= atlas_max or cnt < cfg["atlas_min_probes"]:
            break
        m = meta.get(asn)
        if asn in excluded or not m or not m["ipv4"] or m["category"] not in cfg["asn_categories"]:
            continue
        if asn in picks:
            picks[asn]["source"].append("atlas")
            continue
        aspop_row = next((r for r in aspop.get(cc, []) if r["asn"] == asn), None)
        if not unreliable and (aspop_row is None or aspop_row["pct"] < cfg["atlas_min_aspop_pct"]):
            continue
        picks[asn] = {"source": ["atlas"], "users_pct": aspop_row["pct"] if aspop_row else None,
                      "as_name": m["name"] or (aspop_row or {}).get("descr", "")}
        added += 1
    for asn, p in picks.items():
        p["atlas_probes"] = atlas_probes.get(cc, Counter()).get(asn, 0)
    return picks


def seeds_for_asn(cc, asn, pick, meta, atlas_prefixes, prev, api, cfg):
    m = meta[asn]
    nets = candidate_nets(asn, m["ipv4"], atlas_prefixes.get((cc, asn), {}))
    base = {"asn": asn, "as_name": pick["as_name"], "category": m["category"],
            "users_pct": pick["users_pct"], "atlas_probes": pick["atlas_probes"],
            "source": "+".join(pick["source"])}
    out, used_prefixes = [], set()
    # 1) 沿用上次已核对、仍在当前宣告前缀内、未过期的子网
    for s in prev.get((cc, asn), []):
        if len(out) >= cfg["subnets_per_asn"]:
            break
        sub = ipaddress.ip_network(s["subnet"])
        owner = next((n for n in nets if sub.subnet_of(n)), None)
        age = (TODAY - dt.date.fromisoformat(s["verified"])).days
        if owner and owner not in used_prefixes and age <= cfg["reverify_days"] and s["geo"]["country_code"] == cc:
            out.append(dict(base, subnet=s["subnet"], prefix=str(owner), geo=s["geo"], verified=s["verified"]))
            used_prefixes.add(owner)
    # 2) 不够则从不同前缀挑新 /24 并核对
    lookups = 0
    for net in nets:
        if len(out) >= cfg["subnets_per_asn"] or lookups >= cfg["max_lookups_per_asn"]:
            break
        if net in used_prefixes:
            continue
        sub = pick_24(asn, net)
        lookups += 1
        g = api.lookup(str(sub.network_address + 1))
        if not g:
            continue
        if g.get("country_code") == cc:
            out.append(dict(base, subnet=str(sub), prefix=str(net), geo=geo_brief(g), verified=TODAY.isoformat()))
            used_prefixes.add(net)
    return out


def manual_seeds(cc, items, prev_by_subnet, api, cfg, aspop, meta):
    out = []
    for it in items:
        sub = str(ipaddress.ip_network(it["subnet"], strict=False))
        old = prev_by_subnet.get(sub)
        if old and (TODAY - dt.date.fromisoformat(old["verified"])).days <= cfg["reverify_days"]:
            g, ver = old["geo"], old["verified"]
        else:
            raw = api.lookup(str(ipaddress.ip_network(sub).network_address + 1))
            g, ver = (geo_brief(raw) if raw else None), TODAY.isoformat()
        if not g or g.get("country_code") != cc:
            log("  ! 人工子网 %s 不在 %s（IP API: %s），已跳过" % (sub, cc, g and g.get("country_code")))
            continue
        asn = int(str(g["asn"])[2:]) if str(g.get("asn", "")).startswith("AS") else None
        row = next((r for r in aspop.get(cc, []) if r["asn"] == asn), None)
        m = meta.get(asn) or {}
        out.append({"asn": asn, "as_name": m.get("name") or g.get("isp", ""), "category": m.get("category"),
                    "users_pct": row["pct"] if row else None,
                    "atlas_probes": 0, "source": "manual", "note": it.get("note", ""),
                    "subnet": sub, "prefix": None, "geo": g, "verified": ver})
    return out


# ------------------------------------------------------------- 输出 ----

def write_outputs(out_dir, regions, sources):
    os.makedirs(out_dir, exist_ok=True)
    jpath = os.path.join(out_dir, "ecs_seeds.json")
    old = None
    if os.path.exists(jpath):
        with open(jpath, encoding="utf-8") as f:
            old = json.load(f)
    if old and old.get("regions") == regions:
        log("种子与上次完全相同，不改动输出文件")
        return False
    doc = {"generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "sources": sources,
           "stats": {"regions": len(regions), "seeds": sum(len(r["seeds"]) for r in regions.values()),
                     "asns": len({(cc, s["asn"]) for cc, r in regions.items() for s in r["seeds"]})},
           "regions": regions}
    with open(jpath, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1, sort_keys=False)
        f.write("\n")
    with open(os.path.join(out_dir, "ecs_seeds.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["cc", "region", "subnet", "asn", "as_name", "users_pct", "atlas_probes", "source",
                    "prefix", "geo_city", "geo_asn", "verified"])
        for cc, r in regions.items():
            for s in r["seeds"]:
                w.writerow([cc, r["name"], s["subnet"], s["asn"], s["as_name"], s["users_pct"], s["atlas_probes"],
                            s["source"], s["prefix"], s["geo"].get("city", ""), s["geo"].get("asn", ""), s["verified"]])
    with open(os.path.join(out_dir, "SUMMARY.md"), "w", encoding="utf-8") as f:
        f.write("# ECS 候选子网汇总\n\n生成时间：%s（UTC）\n\n" % doc["generated_at"])
        f.write("共 %(regions)d 个国家/地区、%(asns)d 个运营商 ASN、%(seeds)d 个 /24。"
                "「覆盖用户」为已选 ASN 在 APNIC aspop 中的用户占比之和（估算值）。\n\n" % doc["stats"])
        f.write("| 代码 | 国家/地区 | 子网数 | 覆盖用户 | 运营商（aspop 用户占比） |\n|---|---|---|---|---|\n")
        for cc, r in regions.items():
            asns = []
            for s in r["seeds"]:
                label = "AS%s %s%s" % (s["asn"], s["as_name"].split(" - ")[-1][:28],
                                       "（%s%%）" % s["users_pct"] if s["users_pct"] is not None else "")
                if label not in asns:
                    asns.append(label)
            f.write("| %s | %s | %d | %s | %s |\n" % (cc, r["name"], len(r["seeds"]),
                                                      "aspop 失真" if r["users_pct_covered"] is None
                                                      else "%d%%" % r["users_pct_covered"], "<br>".join(asns)))
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("--config", default=os.path.join(ROOT, "config", "seeds.json"))
    ap.add_argument("--out-dir", default=os.path.join(ROOT, "seeds"))
    ap.add_argument("--only", help="只处理这些国家/地区代码，逗号分隔（调试用）")
    args = ap.parse_args()

    base, key = os.environ.get("IP_API_BASE", "").strip(), os.environ.get("IP_KEY", "").strip()
    if not base or not key:
        raise SystemExit("缺少环境变量 IP_API_BASE 或 IP_KEY")
    with open(args.config, encoding="utf-8") as f:
        cfg = json.load(f)

    prev_doc = {}
    prev_path = os.path.join(args.out_dir, "ecs_seeds.json")
    if os.path.exists(prev_path):
        with open(prev_path, encoding="utf-8") as f:
            prev_doc = json.load(f)
    prev, prev_by_subnet = defaultdict(list), {}
    for cc, r in (prev_doc.get("regions") or {}).items():
        for s in r["seeds"]:
            prev[(cc, s["asn"])].append(s)
            prev_by_subnet[s["subnet"]] = s

    aspop = load_aspop()
    atlas_probes, atlas_prefixes = load_atlas()
    ccs = sorted(set(aspop) | set(atlas_probes) | set(cfg["manual"]))
    if args.only:
        only = {c.strip().upper() for c in args.only.split(",")}
        ccs = [c for c in ccs if c in only]
    wanted = set()
    for cc in ccs:
        wanted.update(r["asn"] for r in aspop.get(cc, [])[:cfg["aspop_scan_rows"]])
        wanted.update(a for a, n in atlas_probes.get(cc, Counter()).items() if n >= cfg["atlas_min_probes"])
    meta = load_ipverse(wanted)

    api = IPApi(base, key, cfg["max_lookups_total"])
    plan = [(cc, asn, p) for cc in ccs for asn, p in choose_asns(cc, aspop, atlas_probes, meta, cfg).items()]
    log("计划：%d 个国家/地区，%d 个 (地区, ASN) 组合" % (len(ccs), len(plan)))

    with ThreadPoolExecutor(8) as ex:
        results = list(ex.map(lambda t: seeds_for_asn(t[0], t[1], t[2], meta, atlas_prefixes, prev, api, cfg), plan))

    regions = {}
    per_cc = defaultdict(list)
    for (cc, asn, p), seeds in zip(plan, results):
        per_cc[cc].extend(seeds)
    for cc in ccs:
        per_cc[cc].extend(manual_seeds(cc, cfg["manual"].get(cc, []), prev_by_subnet, api, cfg, aspop, meta))
    missing = []
    for cc in ccs:
        seeds = per_cc[cc]
        if not seeds:
            missing.append(cc)
            continue
        seeds.sort(key=lambda s: (s["source"] == "manual", -(s["users_pct"] or 0), -s["atlas_probes"],
                                  s["asn"] or 0, ipaddress.ip_network(s["subnet"])))
        g = seeds[0]["geo"]
        best = {}
        for s in seeds:
            best[s["asn"]] = max(best.get(s["asn"], 0), s["users_pct"] or 0)
        covered = sum(best.values())
        regions[cc] = {"name": g.get("country") or g.get("country_en") or cc, "name_en": g.get("country_en", ""),
                       "users_pct_covered": None if cc in cfg["aspop_unreliable"] else covered,
                       "seeds": seeds}

    sources = {"aspop": ASPOP_URL, "atlas": ATLAS_URL, "ipverse": IPVERSE_URL,
               "aspop_unreliable": sorted(cfg["aspop_unreliable"])}
    changed = write_outputs(args.out_dir, regions, sources)

    n_seeds = sum(len(r["seeds"]) for r in regions.values())
    reused = sum(1 for r in regions.values() for s in r["seeds"] if s["subnet"] in prev_by_subnet)
    summary = ("国家/地区 %d 个（无合格子网 %d 个：%s）；子网 %d 个（沿用上次 %d，新增 %d）；本次 IP API 调用 %d 次；输出%s" %
               (len(regions), len(missing), ",".join(missing) or "无", n_seeds, reused, n_seeds - reused,
                api.calls, "已更新" if changed else "无变化"))
    log(summary)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write("### ECS 种子生成\n\n%s\n" % summary)


if __name__ == "__main__":
    main()
