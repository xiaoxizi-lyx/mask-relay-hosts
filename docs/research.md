# 研究记录：iCloud 专用代理入口节点的调度规律与 2026 年 10 月香港节点事件

中文 | [English summary](#english-summary)

本文整理了建立本仓库之前的全部调查：用 ECS（EDNS Client Subnet）模拟各地用户向权威 DNS 查询 `mask.icloud.com` / `mask-h2.icloud.com`，统计入口节点分布与漂移规律；分析 TUM 的全网扫描数据；记录 2026-10-13 起香港 Apple 入口节点被撤出、两天后恢复的全过程，以及用户在此期间的实测。

文中区分三类内容：**测量**（可由附录数据复核）、**用户实测**（标注「用户实测」）、**推测**（标注「推测」，未经证实）。时间均为 UTC，括号内为北京时间。

## 目录

- [摘要](#摘要)
- [1. 背景：入口节点与解析链](#1-背景入口节点与解析链)
  - [1.1 相关项目](#11-相关项目)
- [2. 方法与踩过的坑](#2-方法与踩过的坑)
- [3. 各地区的调度规律（2026-10-14，香港节点撤出期间）](#3-各地区的调度规律2026-10-14香港节点撤出期间)
- [4. 入口节点全景（TUM 数据）](#4-入口节点全景tum-数据)
- [5. 香港入口节点事件](#5-香港入口节点事件)
- [6. 推测与解读](#6-推测与解读)
- [7. 实用结论：本仓库为何这样设计](#7-实用结论本仓库为何这样设计)
- [8. 局限](#8-局限)
- [9. 数据附录](#9-数据附录)

## 摘要

- **解析链**：`mask.icloud.com`、`mask-h2.icloud.com` 在 Apple 的 NS 上是静态 CNAME，指向 `mask.apple-dns.net`；后者由 AWS Route 53 托管，**按 ECS 的 /24 调度**。同一时刻两个域名得到的入口节点池完全相同。
- **规模**：入口节点约 1,400 个 IPv4，只集中在 112 个 /24、32 个国家/地区、67 个城市（TUM 2026-10-13），由 Apple（AS714）与 Akamai（AS36183）运营。近四年 Akamai 的 /24 从约 100 个降到约 61 个，Apple 从约 36 个增到约 51 个。
- **四种调度模式**（10-14 测量）：本地固定（台、日、韩、新、澳、英、德、法、荷、南非、美等）；固定漂移到邻近或远处（印尼→新加坡，阿联酋→维也纳 Akamai）；区域内随机（俄罗斯→欧洲 9 城，墨西哥→美国 12 城）；跨洲随机（中港澳→亚太加美西的 12 个节点）。
- **香港节点事件**：TUM 2026-10-13 那期扫描中，`mask.icloud.com` 的结果里还有香港 Apple 的 37 个 IP，随后扫描的 `mask-h2.icloud.com` 结果里一个都没有。两份结果的差异恰好只有这 37 个 IP，而此前四年香港 Apple 在两份结果之间最多只差过 1 个 IP。最合理的解释是撤出发生在 10-13 的两次扫描之间。10-14 全天，中港澳 100% 被分到亚太和美西的随机节点，全球 232 个地区的种子都解析不到香港。但这些服务器的 TCP 443 一直开放，**用户实测强制解析到香港入口仍可正常使用**。10-15 11:00（19:00）前恢复，中港澳 44 个子网 100% 固定到香港 Apple，10-17 复查依旧如此。
- **香港 Akamai 节点**（`172.224.177.0/24`）不受这次撤出影响，但只分配给极少数网络，主要是香港宽频企业方案（AS9381）等企业网和机房网络。**用户实测：强制使用它可以连通，但又慢又卡；香港 Apple 则一切正常。**
- **实用结论**：用 hosts 把这两个域名固定到想要的入口节点，可以免疫 Route 53 的漂移和临时撤出。但必须同时让 AAAA 返回空，否则 IPv6 会绕过固定。本仓库据此生成各软件的配置。

## 1. 背景：入口节点与解析链

iCloud 专用代理采用两跳架构。设备先连到入口节点（ingress），再经出口节点（egress）访问网站。Apple 公开了出口地址表（`egress-ip-ranges.csv`），但不公开入口地址。设备用系统 DNS 解析 `mask.icloud.com`（QUIC）或 `mask-h2.icloud.com`（TCP/TLS 回退，见 [APNIC 博客](https://blog.apnic.net/2023/01/25/an-investigation-into-apples-new-relay-network/)），再从应答中随机选一个地址连接（见 [AsiaCCS 2023 论文](https://dl.acm.org/doi/10.1145/3579856.3595793)；首次解析为明文 DNS，见 [GFW Report](https://gfw.report/blog/private_relay_censorship/en/)）。

我们实测的解析链如下：

| 步骤 | 权威服务器 | 结果 |
|---|---|---|
| `mask.icloud.com` | `a–d.ns.apple.com` | CNAME → `mask.apple-dns.net`（TTL 43200，不随 ECS 变化） |
| `mask-h2.icloud.com` | `a–d.ns.apple.com` | CNAME → `mask.apple-dns.net`（TTL 900，不随 ECS 变化） |
| `mask.apple-dns.net` | Route 53（`ns-*.awsdns-*`） | A / AAAA，按 ECS 调度，返回 scope `/24`；每次最多 8 个地址 |

- 目前两个域名最终查询的是同一个名字，所以入口节点池相同；我们 10-15、10-17 的测量里两个域名的结果也完全一致。历史上并非一直如此：TUM 数据中，香港 Akamai 段 `172.224.25.0/24` 在 2022-06 至 2023-08 期间一直只出现在 `mask.icloud.com` 的结果里，当时两个域名的解析链可能不同。
- **有 AAAA 记录**，例如香港 ECS 得到 `2403:300:1366:…`，美国得到 `2620:149:a43:…`。**没有 HTTPS（类型 65）记录**，不存在通过 IP 提示绕过 A 记录的问题。

### 1.1 相关项目

调查时在 GitHub 上没有找到按地区提供入口节点 hosts 的项目。相关的有：

- **出口地址表**：如 [hroost/icloud-private-relay-iplist](https://github.com/hroost/icloud-private-relay-iplist)、[nextdns/icloud-private-relay-mmdb](https://github.com/nextdns/icloud-private-relay-mmdb)，每天把 Apple 的 `egress-ip-ranges.csv` 转成各种格式。处理的是出口，与入口无关。
- **入口扫描**：[TUM relay-networks](https://relay-networks.github.io/) 每周做全网 ECS 遍历并公开结果，是本仓库的数据来源；[mertrasna/icpr-external-observation](https://github.com/mertrasna/icpr-external-observation) 提供了可复现的 ECS 扫描器，但不发布结果。
- **不带 ECS 的解析结果**：[dibdot/DoH-IP-blocklists](https://github.com/dibdot/DoH-IP-blocklists) 每小时解析一批 DoH 域名，其中包括这两个域名，但只能拿到 runner 所在地（美国）的入口节点。
- **分流规则**：如 [NSRingo/PrivateRelay](https://github.com/NSRingo/PrivateRelay)、[ninjai/apple](https://github.com/ninjai/apple) 提供 Surge/Clash 规则，用于在不支持的地区开启专用代理，不涉及入口节点的选择。

## 2. 方法与踩过的坑

### 2.1 公共 DNS 加 ECS：会混入缓存结果

最初用 `dns.google/resolve?edns_client_subnet=…` 查询，21 个地区大多返回 scope `/0` 的应答。这是 Google 缓存里的通用结果，各地查到的都是同一批洛杉矶节点（17.248.171.x 等）。只保留 scope 为 `/24` 的应答才可信，而每个子网要重试 6–30 次才能拿到一次。结论：**判断地区差异时，不能用会缓存的公共解析器**。

### 2.2 直接问权威 NS

改为自己构造带 ECS 的查询：先问 Apple 的 NS 拿 CNAME，再直接问 Route 53。所有应答的 scope 都是 `/24`，没有缓存干扰。后文所有结论都基于这种方法。几个基线：

| ECS | 结果 |
|---|---|
| 不带 ECS | 按查询来源 IP 调度（从美国西岸的服务器查询，得到美西节点） |
| `10.0.0.0/24`（私网） | 被忽略，同样按来源 IP 调度 |
| `0.0.0.0/0`（不含位置） | 固定返回挪威奥斯陆的 Akamai 节点 |

### 2.3 选哪些子网当 ECS

为了覆盖全球，我们生成过一份「各国主流运营商」的 /24 列表，共 232 个国家/地区、1,375 个 /24。做法是：按 [APNIC Labs 用户人口估算](https://stats.labs.apnic.net/aspop)选用户最多的 ASN，用 [RIPE Atlas](https://atlas.ripe.net/) 探针分布补充，从 [ipverse](https://github.com/ipverse/as-ip-blocks) 的 BGP 前缀中挑 /24，再逐个核对地理位置。这个过程中遇到的问题：

- **aspop 在俄罗斯失真**：aspop 靠 Google 广告投放估算用户数，Google 2022 年起在俄罗斯停投广告，排名被克里米亚、顿巴斯、阿布哈兹的网络主导，Rostelecom 只排第 7。改用 RIPE Atlas 后才得到 Rostelecom、Beeline、MGTS 这些主流运营商。
- **ipverse 的分类不总准**：尼日利亚三大运营商 MTN、Airtel、Glo 被标成 `hosting`。
- **Starlink 在也门占 87%（aspop），但它的前缀都定位在美国**，导致一开始选不到当地运营商。
- RIPE Atlas 在中国大陆排前的是阿里云和腾讯云的探针，需按分类过滤。

后来改为直接使用 TUM 的数据（见 §4），这份列表只用于 §5 的全球反向扫描。

### 2.4 地理位置与核实

入口 IP 的地理位置来自第三方 IP 数据库，按 /24 查询：同一 /24 内各 IP 的结果一致。两处需要说明：

- IP 库把一个 Akamai 段（`172.224.111.0/24`）标为「茨城」。它的坐标和所属地区都在**大阪府茨木市（Ibaraki）**，不是茨城县。DNS 上它总和东京的 Akamai 段一起返回。
- 中港澳随机池里占比最高的 `17.248.153.0/24` 标为孟买。它与香港的 `17.248.152.0/24` 相邻，我们怀疑定位有误，于是从美国西岸测 TCP 443 延迟：`17.248.153.x` 为 234 ms，与孟买的 Akamai 节点（235 ms）一致；香港节点为 143–148 ms。**确认它确实在孟买。**

## 3. 各地区的调度规律（2026-10-14，香港节点撤出期间）

**测量**：10-14 17:28（10-15 01:28），21 个地区，每个地区一个主流运营商 /24，两个域名各查 8 次；中、港、俄、墨各查 64 次。随后 17:38–17:48，对漂移地区换 2–3 家运营商、分 4 轮共 128 次复测，并加入澳门。

| 模式 | 地区 | 落点 |
|---|---|---|
| 本地固定 | 台湾、日本、韩国、新加坡、澳大利亚、英国、德国、法国、荷兰、南非、美国 | 本地 Apple 节点（台湾新北、大阪、首尔、新加坡、悉尼、斯劳、法兰克福、克利希、霍夫多普、米德兰、迈阿密） |
| 本地固定（Akamai） | 印度、土耳其、加拿大、巴西 | 本地 Akamai 节点（孟买、伊斯坦布尔、多伦多、圣保罗） |
| 固定漂移·邻近 | 印度尼西亚（2 家运营商） | 100% 新加坡 Apple（约 900 km） |
| 固定漂移·远处 | 阿联酋（Etisalat、du） | 100% 维也纳 Akamai（约 4,250 km） |
| 区域内随机 | 俄罗斯（3 家运营商） | 欧洲 9 个城市随机：马德里、法兰克福、斯劳、克利希、曼彻斯特、柏林、基斯塔、霍夫多普、维堡 |
| 区域内随机 | 墨西哥（3 家运营商） | 美国 12 个城市随机：阿什本、达拉斯、锡考克斯、恩格尔伍德、亚特兰大等 |
| 跨洲随机 | 中国大陆（3 家）、香港（2 家）、澳门（2 家） | **同一个 12 节点池**：Apple 孟买、东京、大阪、首尔、台湾新北、新加坡、洛杉矶、圣何塞、西雅图；Akamai 大阪、茨木、东京 |

- **中港澳的随机池**：7 个子网共 896 次查询，没有一次分到香港。孟买占比最高（18%），而距离中港澳都不远的台湾新北只占 2.9%，所以**选择与距离无关**。
- **同一模式内，换运营商结果相同**：俄罗斯 3 家运营商落在同一组欧洲城市；墨西哥 3 家落在同一组美国城市；阿联酋、印尼各 2 家，结果完全一致。说明这几类是按国家/地区调度的。
- 澳门、中国大陆在 TUM 四年多的记录里**从未有过入口节点**。

## 4. 入口节点全景（TUM 数据）

TUM（慕尼黑工业大学）自 2022 年起每周一对全网前缀做 ECS 遍历，周四左右在 [GitHub](https://github.com/relay-networks/relay-networks.github.io/tree/main/ingress-ecs-scan-data) 发布。截至 2026-10-13 共 450 个文件、194 个非空扫描日期，历史上出现过 208 个不同的 /24。**2025-10-14 至 2026-06-09 的扫描全部为空，中断约 8 个月**，2026-06-16 恢复。

- **最新一期（2026-10-13）**：两个域名合并 1,387 个 IP、112 个 /24，分布在 32 个国家/地区；其中 Apple 51 个 /24，Akamai 61 个 /24。
- **长期趋势**：/24 总数一直在 112–137 之间。Akamai 从约 100 个（2022）降到约 61 个，Apple 从约 36 个增到约 51 个。历史上非 Apple、非 Akamai 的只出现过 Cloudflare 的 `104.30.128.0/24`、`104.30.129.0/24`，最新一期没有。
- **与我们的测量交叉验证**：10-14 我们用 ECS 观测到的 818 个 IP、59 个 /24，全部出现在 TUM 最近 4 周的数据中。10-15 和 10-17 拿到的 37 个香港 Apple IP，也与 TUM 记录完全相同。

大中华区入口节点的历史（TUM）：

| /24 | 地区 | 运营方 | 首次出现 | 最后出现 | 10-13 QUIC / h2 |
|---|---|---|---|---|---|
| `17.248.152.0/24` | 香港 | Apple | 2022-01-14 | 2026-10-13 | 有 / **无** |
| `17.248.163.0/24` | 香港 | Apple | 2023-12-19 | 2026-10-13 | 有 / **无** |
| `17.248.216.0/24` | 香港 | Apple | 2026-10-06 | 2026-10-13 | 有 / **无** |
| `172.224.177.0/24` | 香港（将军澳） | Akamai | 2023-04-25 | 2026-10-13 | 有 / 有 |
| `172.224.25.0/24` | 香港 | Akamai（AS20940） | 2022-02-05 | 2023-08-01 | — |
| `17.250.122.0/24` | 台湾（新北） | Apple | 2022-01-14 | 2026-10-13 | 有 / 有 |
| `172.224.27.0/24` | 台湾（台北） | Akamai | 2022-01-14 | 2025-09-23 | — |

## 5. 香港入口节点事件

### 5.1 时间线

| 时间（UTC） | 观测 | 来源 |
|---|---|---|
| 09-29、10-06 | 香港 Apple 3 个 /24 在两个域名的扫描中都在；10-06 新增 `17.248.216.0/24` | TUM |
| 10-13 06:05 起的扫描 | `mask.icloud.com` 的结果中有香港 Apple 37 个 IP，随后扫描的 `mask-h2.icloud.com` 结果中为 0；两份结果的差异**恰好只有这 37 个 IP**。此前四年中，香港 Apple 在两份结果之间最多只差过 1 个 IP（香港 Akamai 段倒常有只出现在一份里的情况）。香港 Akamai 这次两份都在 | TUM（10-16 发布） |
| 10-13 至 10-14 | 「前几天还能解析到香港的入口节点，大约从 10-14 开始，无论 ECS 是不是香港 IP，都不再解析到香港 Apple 入口」 | 用户实测 |
| 10-14 17:04 | 经 Google DNS 加 ECS：中国大陆 → 孟买、西雅图；香港 → 孟买、新加坡 | 测量 |
| 10-14 17:28–17:48 | 直问权威 NS：中港澳 7 个子网 896 次查询，0% 分到香港，落在 12 节点随机池（§3） | 测量 |
| 10-14 23:14 | 全球 232 个地区、1,375 个种子共 1,720 次查询：**没有任何地区分到香港**；4 个香港 /24 一个都没出现 | 测量 |
| 10-14 约 23:15 | 香港 4 个 /24 的入口 IP **TCP 443 全部可连通**（从美国西岸测 RTT 143–148 ms） | 测量 |
| 撤出期间 | 入口 IP 的 443 仍开放；**用 hosts 强制解析到香港入口节点，专用代理依然能正常使用** | 用户实测 |
| 10-15 10:56（18:56）时 | 用户发现香港节点的 DNS 解析已重新上线 | 用户实测 |
| 10-15 11:00–11:07 | 中港澳 44 个子网（港 16、澳 8、大陆 20），3 轮共 8,448 次：**100% 香港 Apple**，每次 8 个 IP，共 37 个，与 TUM 相同；未出现香港 Akamai | 测量 |
| 10-17 20:14–20:22 | 方法相同、结果相同：100% 香港 Apple | 测量 |

### 5.2 恢复后，香港节点还服务哪些地区

用全球 1,375 个种子、每个查 4 次（10-15 11:01 与 10-17 20:14 两次），统计会分到香港节点的比例：

| 比例 | 地区 |
|---|---|
| 100% | 中国大陆、香港、澳门、菲律宾、老挝 |
| 按子网固定 | 越南 50%、泰国 30%、缅甸 20%、东帝汶 17%（两次完全相同） |
| 随机，少量 | 乌兹别克斯坦、吉尔吉斯斯坦、孟加拉、尼泊尔、塔吉克斯坦、阿富汗、哈萨克斯坦、巴基斯坦、不丹、印度、土库曼斯坦，0–25% 不等，两次之间有波动 |

- **东南亚是按子网固定分配的**：越南、泰国、缅甸、东帝汶共 32 个子网，两次扫描结果完全相同，每个子网要么 4 次全分到香港，要么一次都不分到。没分到香港的，固定落在新加坡（缅甸为金奈）。同一运营商的不同网段也可能不同，例如越南 Viettel 的 `171.237.150.0/24` 分到香港，`115.79.7.0/24` 分到新加坡。
- **中亚、南亚是随机的**：在孟买、日本、韩国、新加坡、台湾、美西之间随机，恢复后香港也成了其中一个选项。这个池子就是撤出期间中港澳被分到的那个池子，多了香港。
- **对照**：撤出期间（10-14 23:14），同一批种子没有任何地区分到香港。

### 5.3 香港 Akamai 节点（`172.224.177.0/24`）

- TUM 每周都能看到它，香港 Apple 被撤出的 10-13 也在。但我们的种子从未分到过它。
- 我们把香港、澳门、台湾全部已分配地址段各取一个 /24 扫描，共 4,530 个，只用 DNS 查询。香港 3,369 个 /24 中：80.9% 分到香港 Apple，18.7% 落在其他地区（推测多为在香港注册、实际在海外使用的地址），**只有 13 个（0.4%）分到香港 Akamai**。复测 104 次，全部固定，用到的 9 个 IP 与 TUM 相同。
- 这 13 个 /24 的归属：香港宽频企业方案 HKBN Enterprise Solutions（AS9381，机房类）7 个，Stacks 2 个，以及香港宽频家宽（AS9269）、香港互联网交换中心 HKIX、星岛电子报、Alllan 各 1 个。
- 澳门 97% 分到香港 Apple；台湾 98.5% 分到本地新北节点。
- **用户实测：强制解析到香港 Akamai 也能正常连通，但速度又慢又卡；强制解析到香港 Apple 则一切正常。**

## 6. 推测与解读

以下均为**推测**，供后续验证：

1. **撤出是在 DNS 调度层面摘除，而不是下线服务器**。撤出期间服务器在线、443 开放、强制解析可用，恢复后 IP 与之前完全相同。更像维护、容量调整或调度策略的临时变更。
2. **Route 53 的配置大致是「按地区或网段指定节点池」**：
   - 大中华区池在撤出时是亚太加美西的 12 个节点、按权重随机（孟买权重最高），恢复后中港澳固定到香港；
   - 俄罗斯、墨西哥对应各自大洲的随机池；
   - 东南亚按 /24 固定分配，可能使用了基于网段的路由（如 Route 53 的 IP-based routing）或基于延迟的路由；
   - `0.0.0.0/0` 固定落在奥斯陆，像是没匹配上任何规则时的默认记录。
3. **香港 Akamai 节点面向少数企业网和机房网络**。普通家宽用户强制使用时慢、卡，可能是这个节点的容量或其与家宽网络之间的互联不适合大量普通用户。目前只有用户的单次体验，原因未验证。
4. **撤出的起止时间**：开始于 TUM 10-13 的 QUIC 扫描之后、h2 扫描之前（两次扫描的具体时刻未公开），最晚不晚于 10-14 17:04（我们最早的观测）；恢复于 10-14 23:14 之后、10-15 10:56 之前。

## 7. 实用结论：本仓库为何这样设计

- **用 hosts 固定入口节点**就能免疫 Route 53 的漂移和临时撤出：撤出期间服务器仍可用，只是 DNS 不再给出。中港澳用户优先选 `HK-Apple`；香港 Akamai 慢，不建议普通用户使用。
- **必须同时让 AAAA 返回空**。我们用 dnsmasq 2.92 实测过：只写 `address=` 或 `host-record=` 时，A 记录被固定了，但 AAAA 查询会转发上游，拿回 Route 53 当下给的 IPv6 入口，固定就失效了。`local=` 加 `host-record=` 才能让 AAAA 和 HTTPS 返回空、不转发。mihomo 的 hosts 只要命中，AAAA 就返回空，而且先于 fake-ip 处理（源码确认）。
- **每个文件最多 24 个 IP**：37 个 IP 的应答在不带 EDNS 的 UDP 下已经会被截断（需改用 TCP），24 个正好装进 512 字节。
- **保留规则**：TUM 若恰好在撤出期间扫描（如 10-13 的 h2 扫描），只用最新一期会让 `HK-Apple` 整组消失。所以取最近 4 期，较早出现的 IP 只要 TCP 443 仍可连通就保留。

## 8. 局限

- 地理位置来自第三方 IP 数据库，城市级别可能有误（见 §2.4）。
- TUM 只扫描 IPv4，IPv6 入口不在本研究和本仓库的范围内。
- 我们的测量都是短时间快照（每次几分钟），种子子网也只是抽样；比例只反映当时的情况。
- TUM 可能长时间中断（2025-10 至 2026-06 中断约 8 个月）；本仓库会停在最后一期数据。
- Route 53 的配置随时可能变化，本文的规律只代表 2026 年 10 月中旬。

## 9. 数据附录

[`docs/data/`](data/) 下的 CSV，均由当时保存的原始结果统计得出：

| 文件 | 内容 |
|---|---|
| [`2026-10-14_authoritative_21regions.csv`](data/2026-10-14_authoritative_21regions.csv) | 10-14 17:28，21 个地区直查权威 NS 的落点分布 |
| [`2026-10-14_drift_retest.csv`](data/2026-10-14_drift_retest.csv) | 10-14 17:38–17:48，7 个漂移地区、17 个子网、各 128 次的落点分布 |
| [`cn_hk_mo_hk_share.csv`](data/cn_hk_mo_hk_share.csv) | 中港澳各子网在 10-14、10-15、10-17 三次测量中分到香港节点的比例 |
| [`hk_nodes_by_ecs_country.csv`](data/hk_nodes_by_ecs_country.csv) | 恢复后全球扫描中，各国家/地区分到香港节点的比例（10-15、10-17） |
| [`sea_hk_mapping_per_subnet.csv`](data/sea_hk_mapping_per_subnet.csv) | 越南、泰国、缅甸、东帝汶逐子网的分配结果 |
| [`hk_akamai_served_networks.csv`](data/hk_akamai_served_networks.csv) | 会分到香港 Akamai 节点的 13 个 /24 及其 ASN |
| [`tum_greater_china_history.csv`](data/tum_greater_china_history.csv) | TUM 中大中华区入口 /24 的出现历史 |

致谢：入口 IP 的长期数据来自 Patrick Sattler、Juliane Aulbach、Johannes Zirngibl、Georg Carle，*Towards a Tectonic Traffic Shift? Investigating Apple's New Relay Network*，IMC 2022（[relay-networks.github.io](https://relay-networks.github.io/)）。

---

## English summary

This document records the investigation behind this repository (all times UTC).

- **Resolution chain.** `mask.icloud.com` and `mask-h2.icloud.com` are static CNAMEs on Apple's nameservers to `mask.apple-dns.net`, which AWS Route 53 steers per ECS `/24`; both names therefore share one pool. AAAA records exist; there are no HTTPS records. Public resolvers such as Google DNS return cached scope-`/0` answers, so we queried the authoritative servers directly with ECS.
- **Footprint.** About 1,400 IPv4 ingress addresses in 112 /24s across 32 countries/regions and 67 cities (TUM, 2026-10-13), run by Apple (AS714) and Akamai (AS36183). Since 2022 Akamai's share has fallen from ~100 to ~61 /24s while Apple's grew from ~36 to ~51 (TUM data; TUM's scans were empty from 2025-10-14 to 2026-06-09).
- **Steering patterns (2026-10-14).** Local and fixed for most large markets; fixed drift (Indonesia → Singapore, UAE → Akamai Vienna); random within a continent (Russia → 9 European cities, Mexico → 12 US cities); random across continents for mainland China, Hong Kong and Macau (one 12-node pool spanning Mumbai, Japan, Korea, Taiwan, Singapore and the US West Coast; Mumbai weighted highest, nearest Taiwan lowest).
- **The Hong Kong event.** In TUM's 2026-10-13 scan, the `mask.icloud.com` pass still contained the 37 Hong Kong Apple IPs, while the later `mask-h2.icloud.com` pass contained none, and those 37 IPs were the only difference between the two (in four years, Hong Kong Apple had never differed by more than one IP between the passes). The most likely explanation is that the withdrawal happened between the two passes. Throughout 10-14, mainland China, Hong Kong and Macau got 0% Hong Kong nodes, and no ECS from 232 regions resolved to Hong Kong. Yet TCP 443 on every Hong Kong ingress IP stayed open, and **the user confirmed that pinning the names to Hong Kong ingress IPs kept Private Relay working**. By 10-15 11:00 resolution was restored: 44 subnets in CN/HK/MO resolved 100% to the same 37 Hong Kong Apple IPs (8,448 queries), unchanged on 10-17. After restoration, Hong Kong also serves the Philippines and Laos fully, specific /24s in Vietnam, Thailand, Myanmar and Timor-Leste (deterministic per subnet), and appears as one option in the Central/South Asian random pool.
- **Hong Kong Akamai** (`172.224.177.0/24`) was unaffected by the withdrawal but is assigned to only 13 of 3,369 sampled Hong Kong /24s, mostly enterprise and data-center networks such as HKBN Enterprise Solutions (AS9381). **User test: pinning to it works but is slow and laggy, while Hong Kong Apple works perfectly.**
- **Practical takeaway.** Pinning the two names via hosts makes clients immune to Route 53 drift and temporary withdrawals, provided AAAA is suppressed as well. Our tests with dnsmasq 2.92 show that plain `address=`/`host-record=` still forwards AAAA upstream; `local=` + `host-record=` does not. The files in this repository are built accordingly.
