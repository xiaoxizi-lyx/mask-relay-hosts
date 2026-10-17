#!/usr/bin/env python3
"""
把工作区中指定路径的改动，通过 GraphQL createCommitOnBranch 提交到当前分支。

这样提交由 GitHub 代为签名（显示 Verified），runner 上不需要放任何私钥。
需要环境变量 GITHUB_TOKEN（contents: write），以及 Actions 自带的 GITHUB_REPOSITORY / GITHUB_REF_NAME。
仅依赖 Python 标准库。
"""

import argparse
import base64
import json
import os
import subprocess
import sys
import urllib.request

MUTATION = """
mutation($input: CreateCommitOnBranchInput!) {
  createCommitOnBranch(input: $input) { commit { oid url } }
}
"""


def git(*args):
    return subprocess.run(["git"] + list(args), check=True, capture_output=True).stdout


def changed_files(paths):
    out = git("status", "--porcelain=v1", "-z", "--untracked-files=all", "--", *paths).split(b"\0")
    additions, deletions, i = [], [], 0
    while i < len(out):
        entry = out[i].decode()
        i += 1
        if not entry:
            continue
        xy, path = entry[:2], entry[3:]
        if "R" in xy or "C" in xy:          # 重命名/复制：下一个字段是原路径
            if "R" in xy:
                deletions.append(out[i].decode())
            i += 1
        if os.path.exists(path):
            additions.append(path)
        else:
            deletions.append(path)
    return additions, deletions


def set_output(key, value):
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
            f.write("%s=%s\n" % (key, value))


def main():
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("--paths", nargs="+", required=True, help="只提交这些路径下的改动")
    ap.add_argument("--message", required=True, help="提交标题")
    ap.add_argument("--body", default="", help="提交正文")
    args = ap.parse_args()

    token = os.environ.get("GITHUB_TOKEN")
    repo, branch = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_REF_NAME")
    if not (token and repo and branch):
        sys.exit("缺少 GITHUB_TOKEN / GITHUB_REPOSITORY / GITHUB_REF_NAME")

    additions, deletions = changed_files(args.paths)
    if not additions and not deletions:
        print("没有改动，跳过提交")
        set_output("sha", git("rev-parse", "HEAD").decode().strip())
        return
    changes = {
        "additions": [{"path": p, "contents": base64.b64encode(open(p, "rb").read()).decode()} for p in additions],
        "deletions": [{"path": p} for p in deletions],
    }
    payload = {"query": MUTATION, "variables": {"input": {
        "branch": {"repositoryNameWithOwner": repo, "branchName": branch},
        "message": {"headline": args.message, "body": args.body},
        "expectedHeadOid": git("rev-parse", "HEAD").decode().strip(),
        "fileChanges": changes,
    }}}
    req = urllib.request.Request("https://api.github.com/graphql", data=json.dumps(payload).encode(),
                                 headers={"Authorization": "bearer " + token, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        resp = json.loads(r.read())
    if resp.get("errors"):
        sys.exit("提交失败：%s" % json.dumps(resp["errors"], ensure_ascii=False))
    commit = resp["data"]["createCommitOnBranch"]["commit"]
    print("已提交 %d 个新增/修改、%d 个删除：%s" % (len(additions), len(deletions), commit["url"]))
    set_output("sha", commit["oid"])
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write("\n提交：%s\n" % commit["url"])


if __name__ == "__main__":
    main()
