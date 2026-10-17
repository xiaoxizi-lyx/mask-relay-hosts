# mask-relay-hosts

中文 | [English](#english)

把 iCloud 专用代理（Private Relay）的入口域名 `mask.icloud.com`、`mask-h2.icloud.com` **固定解析到指定地区的入口节点**，不再受 Route 53 调度漂移或故障影响。

Apple 用 Route 53 按用户所在网络分配入口节点，有时会把整个地区的用户调到别处。例如 2026-10-13 香港的 Apple 入口节点曾被临时撤出调度：港澳和中国大陆用户被随机分到孟买、日本、韩国、美西等地；两天后才恢复。期间香港的服务器一直在线，只是解析不到。用本仓库的 hosts，把这两个域名直接指向你想用的入口节点即可。

## 快速开始

1. 在 **[分组索引](hosts/README.md)** 中选一个分组，例如 `HK-Apple`（香港 · Apple 自营）。
2. 取你所用软件对应的文件：

| 软件 | 文件 | 用法 |
|---|---|---|
| dnsmasq（OpenWrt、梅林等路由器） | `hosts/dnsmasq/<分组>.conf` | 见下文，**推荐** |
| mihomo / Clash Meta | `hosts/mihomo/<分组>.yaml` | 合并进配置的 `hosts:` |
| Windows | `hosts/windows/<分组>.txt` | 追加到 `C:\Windows\System32\drivers\etc\hosts` |
| macOS / Linux | `hosts/macos/<分组>.txt` | 追加到 `/etc/hosts` |
| 其他 | `hosts/ip-list/<分组>.txt` | 该组全部 IP，一行一个 |

每次更新都会发布 [release](../../releases)，附全部文件的 zip，并用中英文列出各地区的入口节点、相对上次增减的 IP。

### dnsmasq（推荐，在路由器上对全家设备生效）

把文件内容追加到 dnsmasq 配置后重启 dnsmasq：

- OpenWrt：`/etc/dnsmasq.conf`，然后 `/etc/init.d/dnsmasq restart`
- 梅林固件：`/jffs/configs/dnsmasq.conf.add`，然后 `service restart_dnsmasq`
- Debian / Ubuntu：保存为 `/etc/dnsmasq.d/mask-relay.conf`，然后 `systemctl restart dnsmasq`

文件用 `local=` 加 `host-record=` 写成：这两个域名只按文件里的地址回答，**AAAA（IPv6）与 HTTPS 记录返回空、不会转发到上游**，设备只能使用固定的 IPv4 入口。若只写 `address=` 或 `host-record=`，AAAA 查询仍会被转发，支持 IPv6 的设备会拿到 Route 53 当前给出的 IPv6 入口，固定就失效了。

验证（应只返回文件中的 IP，AAAA 无结果）：

```bash
nslookup mask.icloud.com 192.168.1.1
nslookup -type=AAAA mask.icloud.com 192.168.1.1
```

一次只用一个分组；同时载入多个分组会合并它们的地址。

### mihomo / Clash Meta

把文件中 `hosts:` 下的两行合并到你配置的 `hosts:` 段。`dns.use-hosts` 需为 `true`（默认即是）。mihomo 先查 hosts 再处理 fake-ip，所以 fake-ip 模式下这两个域名返回的也是固定的真实 IP；hosts 里只有 IPv4 时，它们的 AAAA 查询返回空。设备需使用 mihomo 提供的 DNS。

### Windows / macOS / Linux

以管理员身份把文件内容追加到系统 hosts，之后刷新缓存：Windows 运行 `ipconfig /flushdns`；macOS 运行 `sudo dscacheutil -flushcache; sudo killall -HUP mDNSResponder`。系统 hosts 无法让 AAAA 查询返回空，在有 IPv6 的网络中设备仍可能使用 Route 53 给出的 IPv6 入口，因此更推荐在路由器上用 dnsmasq。

## 注意事项

- **只有 IPv4**：数据来源只扫描 IPv4，请按上文让这两个域名的 AAAA 返回空。
- **每个 hosts 文件最多 24 个 IP**，正好装进一个不带 EDNS 的 512 字节 DNS 响应，在城市和 /24 之间平均挑选；`ip-list` 是该组全部 IP。专用代理会从返回的地址中随机选一个连接。
- **分组规则**：每个地区一个总组（如 `HK`、`TW`）；同一地区同时有 Apple 和 Akamai 运营的入口节点时，再细分出 `HK-Apple`、`HK-Akamai` 这样的组。
- **设备需使用你配置的 DNS**：设备上若装了 DoH/DoT 描述文件或使用其他 DNS，路由器上的配置不会生效。
- **固定的只是入口节点**；出口节点及其地区仍由 Apple 分配。
- 若某组的入口节点被 Apple 永久下线，在下一次更新前该组可能暂时不可用，请关注 release。

## 数据与更新

- **入口 IP**：[TUM](https://relay-networks.github.io/) 每周对全网前缀做 ECS 扫描（每周一扫描，通常周四发布）。本仓库合并 `mask.icloud.com` 与 `mask-h2.icloud.com` 两份结果：两个域名都 CNAME 到同一个 `mask.apple-dns.net`，入口节点相同。
- **保留规则**：取最近 4 期非空扫描。最新一期出现的 IP 一律保留；只在更早几期出现的 IP，需 TCP 443 仍可连通才保留。这样即使某次扫描恰逢 Route 53 临时撤下某地节点，该组也不会整组消失；真正下线的 IP 会被剔除。
- **地区**：每个 /24 用第三方 IP 数据库定位一次，结果缓存 30 天（`data/geo_cache.json`）。
- **自动更新**：[workflow](.github/workflows/update-hosts.yml) 每天检查 TUM 是否发布了新扫描；有新扫描才重新生成（连通性检测也随之每周一次，避免偶发抖动造成无意义的发布），入口 IP 有变化时提交（由 GitHub 签名）并发布 release。手动运行时可勾选 force 强制重建。需要仓库 Secret `IP_KEY`、`IP_API_BASE`。

| 路径 | 说明 |
|---|---|
| `hosts/README.md` | 分组索引（地区、运营方、城市、IP 数、各格式文件链接） |
| `hosts/<格式>/<分组>.*` | 各软件的 hosts 文件 |
| `data/ingress.json` | 当前全部入口 IP 及其 /24、地区、运营方、分组（供程序读取） |
| `data/geo_cache.json` | /24 地理位置缓存 |
| `scripts/build.py` | 生成脚本（仅标准库） |
| `config/build.json` | 参数：保留窗口、每文件 IP 上限、缓存天数等 |

入口 IP 数据来自 Patrick Sattler 等人的研究：*Towards a Tectonic Traffic Shift? Investigating Apple's New Relay Network*，IMC 2022（[relay-networks.github.io](https://relay-networks.github.io/)）。

---

## English

Pin the iCloud Private Relay ingress hostnames `mask.icloud.com` and `mask-h2.icloud.com` to **ingress nodes in a region of your choice**, so you are no longer affected by Route 53 steering drift or outages.

Route 53 assigns ingress nodes by client network and sometimes moves a whole region elsewhere. On 2026-10-13, for example, Apple's Hong Kong ingress nodes were temporarily taken out of rotation, and clients in Hong Kong, Macau and mainland China were spread across Mumbai, Japan, Korea and the US West Coast for two days, even though the Hong Kong servers stayed online.

**Quick start**: pick a group in the **[group index](hosts/README.md)** (e.g. `HK-Apple` = Hong Kong, operated by Apple) and use the file for your software:

| Software | File | How |
|---|---|---|
| dnsmasq (OpenWrt, Asuswrt-Merlin, …) — recommended | `hosts/dnsmasq/<group>.conf` | append to the dnsmasq config (OpenWrt `/etc/dnsmasq.conf`, Merlin `/jffs/configs/dnsmasq.conf.add`, Debian `/etc/dnsmasq.d/`) and restart |
| mihomo / Clash Meta | `hosts/mihomo/<group>.yaml` | merge into `hosts:`; keep `dns.use-hosts: true` (default) |
| Windows | `hosts/windows/<group>.txt` | append to `C:\Windows\System32\drivers\etc\hosts`, then `ipconfig /flushdns` |
| macOS / Linux | `hosts/macos/<group>.txt` | append to `/etc/hosts`, then flush the DNS cache |
| Anything else | `hosts/ip-list/<group>.txt` | all IPs of the group, one per line |

Notes:

- **IPv4 only.** The dnsmasq files use `local=` + `host-record=`, so AAAA and HTTPS queries for these names get an empty answer and are never forwarded; mihomo behaves the same when hosts holds only IPv4. With plain `address=`/`host-record=`, AAAA would still be forwarded and IPv6-capable devices would receive whatever IPv6 ingress Route 53 hands out. System hosts files cannot suppress AAAA, so prefer the router.
- Each hosts file holds **at most 24 IPs** (fits a 512-byte non-EDNS DNS answer), spread across cities and /24s; `ip-list` has every IP. Private Relay picks one of the returned addresses at random.
- **Groups**: one group per region (`HK`, `TW`, …); regions served by both Apple and Akamai also get per-operator groups (`HK-Apple`, `HK-Akamai`).
- Devices must use the DNS you configure (no DoH/DoT profiles). Only the ingress is pinned; the egress is still chosen by Apple.
- **Data**: ingress IPs come from [TUM's weekly ECS scans](https://relay-networks.github.io/) (scanned Mondays, usually published Thursdays). The last 4 non-empty scans are used: IPs in the latest scan are always kept; IPs seen only in older scans are kept while TCP 443 is still reachable, so a group does not vanish just because a scan happened during a temporary Route 53 withdrawal. Each /24 is geolocated once and cached for 30 days.
- A daily [workflow](.github/workflows/update-hosts.yml) checks whether TUM has published a new scan and only then rebuilds (so the reachability check also runs weekly); when the ingress set changes it commits (signed by GitHub) and publishes a [release](../../releases) with a zip and bilingual notes listing every region and the IPs added/removed. Run it manually with `force` to rebuild anyway.

Data source: Sattler et al., *Towards a Tectonic Traffic Shift? Investigating Apple's New Relay Network*, IMC 2022 ([relay-networks.github.io](https://relay-networks.github.io/)).
