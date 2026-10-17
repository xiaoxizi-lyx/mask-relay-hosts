#!/usr/bin/env python3
"""
从 TUM 每周的全网 ECS 扫描结果中取 iCloud 专用代理入口节点（mask.icloud.com / mask-h2.icloud.com），
按入口所在地区与运营方分组，生成 dnsmasq / mihomo / Windows / macOS 的 hosts 与 IP 列表，
并与上次发布对比，写出中英文 release 说明。

数据：TUM relay-networks 每周扫描（https://github.com/relay-networks/relay-networks.github.io）
地理位置：IP API（环境变量 IP_API_BASE、IP_KEY），按 /24 缓存在 data/geo_cache.json
保留规则：取最近 window_scans 期非空扫描；最新一期出现的 IP 一律保留，只在更早几期出现的 IP
需要 TCP 443 仍可连通才保留——这样某次扫描恰逢 Route 53 撤下某地节点时，该组不会整组消失。

仅依赖 Python 3.8+ 标准库（解压 .zst 用 Python 3.14 的 compression.zstd 或系统 zstd 命令）。
"""

import argparse
import concurrent.futures as cf
import datetime as dt
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections import OrderedDict, defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UA = "mask-relay-hosts"
FORMATS = OrderedDict([("dnsmasq", ".conf"), ("mihomo", ".yaml"), ("windows", ".txt"), ("macos", ".txt"), ("ip-list", ".txt")])
FORMAT_LABEL = {"dnsmasq": "dnsmasq", "mihomo": "mihomo", "windows": "Windows", "macos": "macOS", "ip-list": "IP"}
CONTINENTS = OrderedDict([("AS", ("亚洲", "Asia")), ("EU", ("欧洲", "Europe")), ("NA", ("北美", "North America")),
                          ("SA", ("南美", "South America")), ("OC", ("大洋洲", "Oceania")), ("AF", ("非洲", "Africa")),
                          ("AN", ("南极洲", "Antarctica"))])
SCAN_RE = re.compile(r"^ecs-scan-(h2-)?(\d{4}-\d{2}-\d{2})-(\d{4})\.ips\.zst$")
IPV4_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")
GEO_FIELDS = ("continent_code", "country_code", "country", "country_en", "region", "region_en",
              "city", "city_en", "asn", "isp")
TODAY = dt.datetime.now(dt.timezone.utc).date()


def log(*args):
    print(*args, flush=True)


def ipkey(ip):
    return tuple(int(x) for x in ip.split("."))


def netkey(net):
    return ipkey(net.split("/")[0])


def net24(ip):
    return str(ipaddress.ip_network(ip + "/24", strict=False))


def flag(cc):
    return "".join(chr(0x1F1E6 + ord(c) - 65) for c in cc) if re.match(r"^[A-Z]{2}$", cc or "") else ""


def fetch(url, headers=None, timeout=60, tries=4, label=None):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=dict({"User-Agent": UA}, **(headers or {})))
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            last = "HTTP %d" % e.code
            if e.code in (401, 403, 404):
                break
        except Exception as e:
            last = "%s: %s" % (type(e).__name__, e)
        time.sleep(2 * (i + 1))
    raise RuntimeError("请求失败 %s：%s" % (label or url, last))


def gh_headers():
    h = {"Accept": "application/vnd.github+json"}
    if os.environ.get("GITHUB_TOKEN"):
        h["Authorization"] = "Bearer " + os.environ["GITHUB_TOKEN"]
    return h


def unzstd(blob):
    try:
        from compression import zstd  # Python 3.14+
        return zstd.decompress(blob)
    except ImportError:
        return subprocess.run(["zstd", "-dcq"], input=blob, capture_output=True, check=True).stdout


# ------------------------------------------------------------- TUM 数据 ----

def tum_window(cfg):
    url = "https://api.github.com/repos/%s/contents/%s?ref=%s" % (cfg["tum_repo"], cfg["tum_path"], cfg["tum_branch"])
    scans = defaultdict(dict)
    for item in json.loads(fetch(url, gh_headers(), label="TUM 文件列表")):
        m = SCAN_RE.match(item.get("name", ""))
        if m:
            scans[m.group(2)]["h2" if m.group(1) else "quic"] = item["download_url"]
    window = []
    for date in sorted(scans, reverse=True):
        sets = {}
        for kind in ("quic", "h2"):
            sets[kind] = set()
            if kind in scans[date]:
                for tok in unzstd(fetch(scans[date][kind], label="TUM %s %s" % (date, kind))).decode("ascii", "ignore").split():
                    if IPV4_RE.match(tok):
                        try:
                            sets[kind].add(str(ipaddress.IPv4Address(tok)))
                        except ValueError:
                            pass
        if not sets["quic"] and not sets["h2"]:
            log("TUM %s 为空扫描，跳过" % date)
            continue
        window.append({"date": date, "quic": sets["quic"], "h2": sets["h2"]})
        log("TUM %s：mask.icloud.com %d 个 IP，mask-h2.icloud.com %d 个 IP" % (date, len(sets["quic"]), len(sets["h2"])))
        if len(window) >= cfg["window_scans"]:
            break
    if not window:
        raise SystemExit("TUM 没有可用的扫描数据")
    return window


# ------------------------------------------------------------- 连通性 ----

def tcp_check(ips, c):
    def one(ip):
        for _ in range(c["attempts"]):
            try:
                with socket.create_connection((ip, c["port"]), timeout=c["timeout"]):
                    return ip, True
            except OSError:
                time.sleep(0.3)
        return ip, False
    with cf.ThreadPoolExecutor(c["concurrency"]) as ex:
        return dict(ex.map(one, sorted(ips, key=ipkey)))


# ------------------------------------------------------------- 地理位置 ----

class Geo:
    def __init__(self, path, ttl_days):
        self.path, self.ttl = path, ttl_days
        self.base, self.key = os.environ.get("IP_API_BASE", "").strip(), os.environ.get("IP_KEY", "").strip()
        self.cache = json.load(open(path, encoding="utf-8")) if os.path.exists(path) else {}
        self.calls = 0

    def fresh(self, net):
        c = self.cache.get(net)
        return bool(c) and (TODAY - dt.date.fromisoformat(c["date"])).days <= self.ttl

    def lookup(self, ip):
        url = "%s?%s" % (self.base, urllib.parse.urlencode({"key": self.key, "ip": ip}))
        for i in range(3):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=20) as r:
                    geo = json.loads(r.read()).get("geo")
                if geo:
                    return geo
            except urllib.error.HTTPError as e:
                if e.code in (401, 403):
                    raise SystemExit("IP API 拒绝访问（HTTP %d），请检查 IP_KEY" % e.code)
            except Exception:
                pass
            time.sleep(1 + i)
        return None

    def resolve(self, samples):
        """samples: {net: 用来查询的 IP}；返回 {net: 地理信息}。"""
        todo = [n for n in samples if not self.fresh(n)]
        if todo and not (self.base and self.key):
            missing = [n for n in todo if n not in self.cache]
            if missing:
                raise SystemExit("缺少 IP_API_BASE / IP_KEY，且 %d 个 /24 没有缓存（如 %s）" % (len(missing), missing[0]))
            todo = []
        with cf.ThreadPoolExecutor(8) as ex:
            for net, geo in zip(todo, ex.map(lambda n: self.lookup(samples[n]), todo)):
                self.calls += 1
                if geo:
                    self.cache[net] = dict({k: geo.get(k, "") for k in GEO_FIELDS}, date=TODAY.isoformat())
                elif net in self.cache:
                    log("  ! %s 查询失败，沿用过期缓存" % net)
                else:
                    raise SystemExit("无法定位 %s（IP API 查询失败且无缓存）" % net)
        return {n: self.cache[n] for n in samples}

    def save(self):
        items = sorted(self.cache.items(), key=lambda kv: netkey(kv[0]))
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{\n" + ",\n".join("%s: %s" % (json.dumps(k), json.dumps(v, ensure_ascii=False, sort_keys=True))
                                       for k, v in items) + "\n}\n")


# ------------------------------------------------------------- 分组 ----

def interleave(lists):
    out, i = [], 0
    while any(i < len(x) for x in lists):
        out.extend(x[i] for x in lists if i < len(x))
        i += 1
    return out


def pick_for_hosts(ips, nets, cap):
    """超过上限时，在城市之间、再在同城的 /24 之间轮流挑选，尽量分散；结果与输入顺序无关。"""
    if len(ips) <= cap:
        return sorted(ips, key=ipkey)
    by_city = defaultdict(lambda: defaultdict(list))
    for ip in sorted(ips, key=ipkey):
        n = net24(ip)
        by_city[nets[n]["city_en"] or "?"][n].append(ip)
    seqs = [interleave([by_city[c][n] for n in sorted(by_city[c], key=netkey)]) for c in sorted(by_city)]
    return sorted(interleave(seqs)[:cap], key=ipkey)


def operator_of(asn, cfg):
    return cfg["operators"].get(asn) or (asn or "Unknown")


def build_groups(kept, nets, cfg):
    regions = defaultdict(lambda: defaultdict(list))
    for ip in kept:
        g = nets[net24(ip)]
        regions[g["country_code"] or "ZZ"][g["operator"]].append(ip)
    cont = lambda cc: next((nets[n]["continent_code"] for n in nets if nets[n]["country_code"] == cc), "")
    order = sorted(regions, key=lambda cc: (list(CONTINENTS).index(cont(cc)) if cont(cc) in CONTINENTS else 99, cc))
    groups = OrderedDict()
    for cc in order:
        ops = regions[cc]
        allips = sorted((ip for v in ops.values() for ip in v), key=ipkey)
        groups[cc] = {"region": cc, "operator": None, "operators": sorted(ops), "ips": allips}
        if len(ops) > 1:
            for op in sorted(ops):
                groups["%s-%s" % (cc, op)] = {"region": cc, "operator": op, "operators": [op],
                                              "ips": sorted(ops[op], key=ipkey)}
    for g in groups.values():
        g["hosts_ips"] = pick_for_hosts(g["ips"], nets, cfg["max_ips_per_hosts_file"])
        g["nets"] = sorted({net24(ip) for ip in g["ips"]}, key=netkey)
        g["continent"] = cont(g["region"])
    return groups


def region_names(cc, nets):
    g = next((nets[n] for n in sorted(nets) if nets[n]["country_code"] == cc), {})
    return g.get("country") or cc, g.get("country_en") or cc


def city_names(group, nets, cfg):
    seen = OrderedDict()
    for n in group["nets"]:
        g = nets[n]
        en = g.get("city_en") or "?"
        zh = cfg.get("city_zh_overrides", {}).get(en) or g.get("city") or en
        seen[en] = zh
    return seen


# ------------------------------------------------------------- 输出文件 ----

def describe(name, group, nets, cfg):
    zh, en = region_names(group["region"], nets)
    ops = group["operator"] or "+".join(group["operators"])
    return "%s · %s %s · %s · %d/%d IP" % (name, zh, en, ops, len(group["hosts_ips"]), len(group["ips"]))


def ascii_describe(name, group, nets):
    _, en = region_names(group["region"], nets)
    ops = group["operator"] or "+".join(group["operators"])
    return "%s (%s, %s) %d/%d IPs" % (name, en, ops, len(group["hosts_ips"]), len(group["ips"]))


def write_group_files(out_root, name, group, nets, cfg):
    d1, d2 = cfg["domains"]
    ips, url = group["hosts_ips"], cfg["repo_url"]
    head = describe(name, group, nets, cfg)
    files = {}
    files["dnsmasq"] = "\n".join(
        ["# mask-relay-hosts · %s" % head,
         "# %s" % url,
         "# local= 让这两个域名只用下面的地址应答，AAAA/HTTPS 返回空、不转发上游",
         "# local= makes dnsmasq answer these names only from the records below (AAAA/HTTPS: empty, never forwarded)",
         "local=/%s/" % d1, "local=/%s/" % d2]
        + ["host-record=%s,%s,%s" % (d1, d2, ip) for ip in ips]) + "\n"
    files["mihomo"] = "\n".join(
        ["# mask-relay-hosts · %s" % head,
         "# %s" % url,
         "# 合并到配置的 hosts: 段；dns.use-hosts 需为 true（默认即是）",
         "# Merge into the hosts: section of your config; dns.use-hosts must be true (default)",
         "hosts:",
         "  %s: [%s]" % (d1, ", ".join(ips)),
         "  %s: [%s]" % (d2, ", ".join(ips))]) + "\n"
    a = ascii_describe(name, group, nets)
    body = ["# BEGIN mask-relay-hosts %s" % a, "# %s" % url] + ["%s %s %s" % (ip, d1, d2) for ip in ips] + \
           ["# END mask-relay-hosts %s" % name]
    files["windows"] = "\r\n".join(body) + "\r\n"
    files["macos"] = "\n".join(body) + "\n"
    files["ip-list"] = "\n".join(group["ips"]) + "\n"
    for fmt, ext in FORMATS.items():
        path = os.path.join(out_root, "hosts", fmt, name + ext)
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(files[fmt])


def write_index(out_root, groups, nets, cfg, tum):
    lines = ["# 分组索引 / Group index", "",
             "数据 Data：TUM 扫描 %s（保留窗口 window：%s）" % (tum["latest_scan"], "、".join(tum["window"])), "",
             "每个 hosts 文件最多 %d 个 IP（在城市和 /24 之间平均挑选）；`ip-list` 为该组全部 IP。" % cfg["max_ips_per_hosts_file"],
             "Each hosts file holds at most %d IPs (spread across cities and /24s); `ip-list` has every IP of the group." % cfg["max_ips_per_hosts_file"],
             "", "| 分组 Group | 地区 Region | 运营方 Operator | 城市 Cities | IP（hosts / 全部 all） | 文件 Files |",
             "|---|---|---|---|---|---|"]
    for name, g in groups.items():
        zh, en = region_names(g["region"], nets)
        cities = city_names(g, nets, cfg)
        links = " · ".join("[%s](%s/%s%s)" % (FORMAT_LABEL[f], f, name, ext) for f, ext in FORMATS.items())
        lines.append("| **%s** | %s %s %s | %s | %s | %d / %d | %s |" % (
            name, flag(g["region"]), zh, en, g["operator"] or " + ".join(g["operators"]),
            "、".join("%s %s" % (zh_c, en_c) if zh_c != en_c else en_c for en_c, zh_c in cities.items()),
            len(g["hosts_ips"]), len(g["ips"]), links))
    with open(os.path.join(out_root, "hosts", "README.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


# ------------------------------------------------------------- 差异与说明 ----

def by_region_operator(doc):
    out = defaultdict(set)
    for ip, m in (doc or {}).get("ips", {}).items():
        g = doc["nets"].get(m["net"])
        if g:
            out[(g["country_code"], g["operator"])].add(ip)
    return out


def compress(ips):
    groups = defaultdict(list)
    for ip in sorted(ips, key=ipkey):
        a, _, b = ip.rpartition(".")
        groups[a].append(int(b))
    parts = []
    for prefix in sorted(groups, key=lambda p: ipkey(p + ".0")):
        nums, spans, start = groups[prefix], [], None
        for i, x in enumerate(nums):
            start = x if start is None else start
            if i + 1 == len(nums) or nums[i + 1] != x + 1:
                spans.append(str(start) if start == x else "%d-%d" % (start, x))
                start = None
        parts.append("%s.%s" % (prefix, spans[0]) if len(nums) == 1 else "%s.{%s}" % (prefix, ",".join(spans)))
    return " ".join(parts)


def release_notes(doc, prev, groups, nets, cfg, stats, alive, tcp_trusted, tag):
    tum = doc["tum"]
    lines = []
    lines += ["## 概要 / Summary", ""]
    lines.append("- 数据 Data：TUM 扫描 **%s**（保留窗口 window：%s）" % (tum["latest_scan"], "、".join(tum["window"])))
    if stats["stale_days"] > cfg["stale_warning_days"]:
        lines.append("- ⚠️ TUM 最新扫描已是 %d 天前 / TUM's latest scan is %d days old" % (stats["stale_days"], stats["stale_days"]))
    lines.append("- 入口节点 Ingress：**%d** 个 IP，%d 个 /24，%d 个国家/地区 · %d IPs in %d /24s across %d countries/regions" % (
        stats["ips"], stats["nets"], stats["regions"], stats["ips"], stats["nets"], stats["regions"]))
    if stats["retained"]:
        lines.append("- 其中 %d 个 IP 不在最新扫描中、但 TCP 443 仍可连通，继续保留 · %d IPs are absent from the latest scan but still reachable on TCP 443, so they are kept" % (stats["retained"], stats["retained"]))
    if stats["dropped"]:
        lines.append("- 剔除 %d 个 IP：只在更早的扫描中出现且已不可连通 · Dropped %d IPs seen only in older scans and no longer reachable" % (stats["dropped"], stats["dropped"]))
    if not tcp_trusted:
        lines.append("- ⚠️ 本次 TCP 连通性检测成功率过低，未据此剔除任何 IP · TCP check success rate too low this run; nothing was dropped based on it")
    base = (prev or {}).get("last_release", {}).get("tag")
    lines += ["", "## 当前入口节点所在地区 / Current ingress regions", "",
              "| 地区 Region | 运营方 Operator | 城市 Cities | /24 | IP | TCP 443 可达 Reachable | 分组 Groups |", "|---|---|---|---|---|---|---|"]
    cur = by_region_operator(doc)
    reach = lambda ips: "%d/%d" % (sum(1 for x in ips if alive.get(x)), len(ips)) if alive else "-"
    for cc in [g["region"] for g in groups.values() if g["operator"] is None]:
        zh, en = region_names(cc, nets)
        ops = sorted(op for (c, op) in cur if c == cc)
        for i, op in enumerate(ops):
            ips = cur[(cc, op)]
            sub = {"nets": sorted({net24(x) for x in ips}, key=netkey)}
            cities = city_names(sub, nets, cfg)
            lines.append("| %s | %s | %s | %d | %d | %s | `%s` |" % (
                "%s %s %s" % (flag(cc), zh, en) if i == 0 else "", op,
                "、".join("%s %s" % (z, e) if z != e else e for e, z in cities.items()),
                len(sub["nets"]), len(ips), reach(ips), cc if len(ops) == 1 else "%s-%s" % (cc, op)))
        if len(ops) > 1:
            g = groups[cc]
            lines.append("| | 合计 Total | | %d | %d | %s | `%s` |" % (len(g["nets"]), len(g["ips"]), reach(g["ips"]), cc))
    lines += ["", "## 本次变化 / Changes" + (" since %s" % base if base else ""), ""]
    if prev is None:
        lines.append("首次发布 / Initial release.")
    else:
        old = by_region_operator(prev)
        keys = sorted(set(old) | set(cur))
        added = [(k, cur[k] - old.get(k, set())) for k in keys if cur.get(k, set()) - old.get(k, set())]
        removed = [(k, old[k] - cur.get(k, set())) for k in keys if old.get(k, set()) - cur.get(k, set())]
        pg, ng = set((prev.get("groups") or {}).keys()), set(groups.keys())
        if ng - pg:
            lines.append("- 新增分组 New groups：%s" % ", ".join("`%s`" % x for x in sorted(ng - pg)))
        if pg - ng:
            lines.append("- 移除分组 Removed groups：%s" % ", ".join("`%s`" % x for x in sorted(pg - ng)))
        for title, items in (("新增 / Added", added), ("减少 / Removed", removed)):
            lines += ["", "### %s（%d）" % (title, sum(len(v) for _, v in items)), ""]
            if not items:
                lines.append("无 / None")
            for (cc, op), ips in items:
                zh, en = region_names(cc, nets if cc in {nets[n]["country_code"] for n in nets} else prev["nets"])
                lines.append("- %s %s %s · %s：%d 个 IP · `%s`" % (flag(cc), zh, en, op, len(ips), compress(ips)))
    lines += ["", "## IP 明细 / IP details", "", "<details><summary>展开 / Expand</summary>", "",
              "| 地区 Region | 运营方 Operator | 城市 City | /24 | IP |", "|---|---|---|---|---|"]
    for (cc, op) in sorted(cur, key=lambda k: ([g["region"] for g in groups.values() if g["operator"] is None].index(k[0]), k[1])):
        for n in sorted({net24(x) for x in cur[(cc, op)]}, key=netkey):
            g = nets[n]
            ips = [x for x in cur[(cc, op)] if net24(x) == n]
            zh = cfg.get("city_zh_overrides", {}).get(g["city_en"]) or g["city"]
            city = "%s %s" % (zh, g["city_en"]) if zh and zh != g["city_en"] else g["city_en"]
            lines.append("| %s | %s | %s | %s | `%s` |" % (cc, op, city, n, compress(ips)))
    lines += ["", "</details>", "",
              "## 使用 / Usage", "",
              "各软件的用法见 [README](%s#readme)；分组与文件索引见 [hosts/README.md](%s/blob/%s/hosts/README.md)。" % (cfg["repo_url"], cfg["repo_url"], tag),
              "See the [README](%s#readme) for usage, and [hosts/README.md](%s/blob/%s/hosts/README.md) for the group index." % (cfg["repo_url"], cfg["repo_url"], tag),
              "", "---", "入口 IP 数据来自 TUM 的每周 ECS 扫描（Sattler et al., *Towards a Tectonic Traffic Shift? Investigating Apple's New Relay Network*, IMC 2022, https://relay-networks.github.io/）；地理位置来自第三方 IP 数据库。",
              "Ingress IPs come from TUM's weekly ECS scans (Sattler et al., IMC 2022, https://relay-networks.github.io/); geolocation is from a third-party IP database."]
    return "\n".join(lines) + "\n"


def next_tag():
    base = TODAY.strftime("%Y.%m.%d")
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not repo:
        return base
    for i in range(1, 20):
        tag = base if i == 1 else "%s.%d" % (base, i)
        try:
            fetch("https://api.github.com/repos/%s/releases/tags/%s" % (repo, tag), gh_headers(), tries=1, label="release 查询")
        except RuntimeError as e:
            if "HTTP 404" in str(e):
                return tag
            raise
    raise SystemExit("今天的 release 编号已用完")


# ------------------------------------------------------------- 主流程 ----

def main():
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("--root", default=ROOT, help="输出根目录（默认仓库根目录）")
    ap.add_argument("--build-dir", default=None, help="release 说明与 zip 的输出目录（默认 <root>/build）")
    ap.add_argument("--skip-tcp", action="store_true", help="跳过 TCP 连通性检测（调试用；此时不剔除任何 IP）")
    ap.add_argument("--force", action="store_true", help="TUM 没有新扫描时也重新生成")
    args = ap.parse_args()
    cfg = json.load(open(os.path.join(ROOT, "config", "build.json"), encoding="utf-8"))
    build_dir = args.build_dir or os.path.join(args.root, "build")
    data_path = os.path.join(args.root, "data", "ingress.json")
    prev = json.load(open(data_path, encoding="utf-8")) if os.path.exists(data_path) else None

    window = tum_window(cfg)
    latest = window[0]
    if prev and prev["tum"]["latest_scan"] == latest["date"] and not args.force:
        # 没有新扫描就不重新探测、不改动任何文件，避免连通性的偶发抖动造成无意义的发布
        log("TUM 最新扫描仍是 %s，与上次相同，跳过（如需重建请加 --force）" % latest["date"])
        if os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
                f.write("release=false\n")
        return
    meta = {}
    for scan in window:
        for ip in scan["quic"] | scan["h2"]:
            m = meta.setdefault(ip, {"last_seen": scan["date"], "seen_scans": 0})
            m["seen_scans"] += 1
    for ip, m in meta.items():
        m["in_latest"] = ip in latest["quic"] or ip in latest["h2"]
        m["net"] = net24(ip)

    if args.skip_tcp:
        alive, tcp_trusted = {}, False
    else:
        t0 = time.time()
        alive = tcp_check(meta, cfg["tcp_check"])
        ratio = sum(alive.values()) / len(alive)
        tcp_trusted = ratio >= cfg["tcp_check"]["min_alive_ratio"]
        log("TCP 443：%d/%d 可连通（%.0f%%），用时 %.0fs" % (sum(alive.values()), len(alive), 100 * ratio, time.time() - t0))
    kept = sorted((ip for ip, m in meta.items() if m["in_latest"] or not tcp_trusted or alive.get(ip)), key=ipkey)
    dropped = sorted(set(meta) - set(kept), key=ipkey)

    geo = Geo(os.path.join(args.root, "data", "geo_cache.json"), cfg["geo_ttl_days"])
    samples = {}
    for ip in kept:
        samples.setdefault(net24(ip), ip)
    nets = geo.resolve(samples)
    nets = {n: dict(g, operator=operator_of(g.get("asn"), cfg)) for n, g in nets.items()}
    nets = {n: {k: v for k, v in g.items() if k != "date"} for n, g in nets.items()}
    log("地理位置：%d 个 /24，本次 IP API 调用 %d 次" % (len(nets), geo.calls))

    groups = build_groups(kept, nets, cfg)
    doc = OrderedDict([
        ("tum", {"latest_scan": latest["date"], "window": [s["date"] for s in window],
                 "source": "https://github.com/%s/tree/%s/%s" % (cfg["tum_repo"], cfg["tum_branch"], cfg["tum_path"])}),
        ("last_release", (prev or {}).get("last_release", {})),
        ("nets", OrderedDict((n, nets[n]) for n in sorted(nets, key=netkey))),
        ("groups", OrderedDict((k, {"region": g["region"], "operator": g["operator"], "ips": g["ips"],
                                    "hosts_ips": g["hosts_ips"]}) for k, g in groups.items())),
        ("ips", OrderedDict((ip, {"net": meta[ip]["net"], "last_seen": meta[ip]["last_seen"],
                                  "seen_scans": meta[ip]["seen_scans"], "in_latest": meta[ip]["in_latest"]}) for ip in kept)),
    ])

    changed = prev is None or by_region_operator(prev) != by_region_operator(doc) or \
        set((prev.get("groups") or {})) != set(groups)
    tag = next_tag() if changed else None
    if changed:
        doc["last_release"] = {"tag": tag, "date": TODAY.isoformat(), "tum_scan": latest["date"]}

    # 重新生成 hosts/ 下的全部文件（已不存在的分组随之删除）
    hosts_dir = os.path.join(args.root, "hosts")
    if os.path.isdir(hosts_dir):
        shutil.rmtree(hosts_dir)
    for fmt in FORMATS:
        os.makedirs(os.path.join(hosts_dir, fmt), exist_ok=True)
    for name, g in groups.items():
        write_group_files(args.root, name, g, nets, cfg)
    write_index(args.root, groups, nets, cfg, doc["tum"])
    os.makedirs(os.path.dirname(data_path), exist_ok=True)
    with open(data_path, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
        f.write("\n")
    geo.save()

    stats = {"ips": len(kept), "nets": len(nets), "regions": len([g for g in groups.values() if g["operator"] is None]),
             "retained": sum(1 for ip in kept if not meta[ip]["in_latest"]), "dropped": len(dropped),
             "stale_days": (TODAY - dt.date.fromisoformat(latest["date"])).days}
    summary = ("TUM %s · %d 个 IP / %d 个 /24 / %d 个地区 · 分组 %d 个 · 保留 %d、剔除 %d · IP API %d 次 · %s" % (
        latest["date"], stats["ips"], stats["nets"], stats["regions"], len(groups), stats["retained"], stats["dropped"],
        geo.calls, "有变化，将发布 %s" % tag if changed else "入口 IP 无变化，不发布"))
    log(summary)
    os.makedirs(build_dir, exist_ok=True)
    out = {"release": "true" if changed else "false"}
    if changed:
        notes = os.path.join(build_dir, "release-notes.md")
        with open(notes, "w", encoding="utf-8") as f:
            f.write(release_notes(doc, prev, groups, nets, cfg, stats, alive, tcp_trusted, tag))
        zpath = os.path.join(build_dir, "mask-relay-hosts-%s.zip" % tag)
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
            for base, _, files in os.walk(hosts_dir):
                for fn in sorted(files):
                    full = os.path.join(base, fn)
                    z.write(full, os.path.relpath(full, args.root))
        out.update(tag=tag, title="%s · TUM %s" % (tag, latest["date"]), notes=notes, zip=zpath)
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
            for k, v in out.items():
                f.write("%s=%s\n" % (k, v))
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write("### mask-relay-hosts\n\n%s\n" % summary)


if __name__ == "__main__":
    main()
