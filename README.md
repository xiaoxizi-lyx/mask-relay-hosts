# mask-relay-hosts

研究 iCloud 专用代理（Private Relay）入口节点 `mask.icloud.com` / `mask-h2.icloud.com` 在各国家/地区的解析情况。

目前包含第一部分：**生成各国家/地区主流运营商的 ECS 候选子网**，供后续直接向权威 NS 发 ECS 查询时使用。

## 目录

| 路径 | 说明 |
|---|---|
| `seeds/ecs_seeds.json` | 候选子网（完整字段，供程序读取） |
| `seeds/ecs_seeds.csv` | 同上，扁平表格 |
| `seeds/SUMMARY.md` | 各国家/地区的子网数、运营商与覆盖用户比例 |
| `seeds/geo_cache.json` | IP API 查询缓存（含失败结果），90 天后重新查询 |
| `config/seeds.json` | 生成参数与人工覆盖 |
| `scripts/gen_seeds.py` | 生成脚本（仅标准库） |
| `scripts/gh_signed_commit.py` | 在 Actions 中通过 GraphQL 提交，由 GitHub 签名 |
| `.github/workflows/ecs-seeds.yml` | 每周一自动运行，也可手动触发 |

## 生成方法

1. **选运营商**：按 [APNIC Labs AS 用户人口估算](https://stats.labs.apnic.net/aspop) 从用户最多的 ASN 依次尝试，
   只有核对通过、真正拿到子网的 ASN 才计入覆盖率，直到累计覆盖 70% 用户或选够 5 个。
   ASN 分类取自 ipverse：`isp` 类直接参与；其他分类只有在 aspop 中用户占比 ≥3% 时才参与
   （ipverse 的分类并不总准，例如尼日利亚三大运营商被标成 `hosting`）。
   Cloudflare、Akamai、Fastly、Apple、Google、Amazon、Microsoft 等代理出口/云厂商 ASN 一律排除（`global_exclude_asns`）。
   再用 [RIPE Atlas](https://atlas.ripe.net/) 在线探针最多的 `isp` 类 ASN 补充（需在 aspop 中也有 ≥1% 用户）。
   aspop 依赖 Google 广告估算，在 Google 停投广告的地区会失真（目前标记了 RU），这些地区改以 Atlas 为主。
2. **挑子网**：从 [ipverse/as-ip-blocks](https://github.com/ipverse/as-ip-blocks) 取该 ASN 当前宣告的前缀，优先有 Atlas 探针的前缀，
   每个前缀内用哈希确定性地挑一个 /24（同一前缀每次结果相同），每个 ASN 取 2 个来自不同前缀的 /24。
3. **核对**：用 IP API 查询 `x.x.x.1`，国家/地区一致才收录；不一致就换下一个前缀。
4. **沿用与缓存**：上次已核对、仍在当前宣告前缀内、且未超过 90 天的子网直接沿用；IP API 的查询结果（包括国家不符的）
   缓存在 `seeds/geo_cache.json`，90 天内不重复查询。结果与上次相同时不改动文件、不提交。

`config/seeds.json` 中的 `manual` 可人工指定子网（同样会核对），`exclude_asns` 可排除特定 ASN。

## 运行

本地：

```bash
IP_KEY=... IP_API_BASE=https://xxx/ python3 scripts/gen_seeds.py            # 全部国家/地区
IP_KEY=... IP_API_BASE=https://xxx/ python3 scripts/gen_seeds.py --only JP,MO --out-dir /tmp/seeds
```

Actions 需要两个仓库 Secret：`IP_KEY`、`IP_API_BASE`。

## 数据来源与许可

- APNIC Labs AS Customer Population（aspop）
- RIPE Atlas 探针元数据（RIPE NCC）
- ipverse/as-ip-blocks（CC0 1.0）

生成结果中的用户比例均为 APNIC 的估算值；子网只是用于 ECS 查询的代表性网段，不代表其中任何具体用户。
