"""校验：compare 响应 JSON 结构 + sparse 行 versions 省略 + 冲突计数。"""
import json, sys, urllib.request


def get_compare():
    body = json.dumps({"direction": "absorb", "source_branch": "A_r2", "target_branch": "B_r2"}).encode()
    req = urllib.request.Request("http://127.0.0.1:8000/api/merge/branch/compare", data=body,
                                 headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=180).read())


def main():
    d = get_compare()
    g = d["groups"]["big_data"]
    s = g["sheets"]["BigData"]
    rows = s["rows"]
    sparse = [r for r in rows if len(r["cells"]) == 1]
    conflict = [r for r in rows if any(c.get("conflict") for c in r["cells"])]
    print("groups:", list(d["groups"]))
    print("big_data rows:", len(rows), "sparse(1cell):", len(sparse), "conflict_rows:", len(conflict))
    print("sparse cell versions:", sparse[0]["cells"][0].get("versions"))
    print("conflict row cells versions present:", all("versions" in c and c["versions"] for c in conflict[0]["cells"]))
    total = sum(1 for r in rows for c in r["cells"] if c.get("conflict"))
    print("total conflicts:", total)
    ok = len(sparse) > 90000 and sparse[0]["cells"][0].get("versions") == {} and total == 4
    print("结论:", "响应结构正确、sparse versions 省略、4 处冲突完整 ✓" if ok else "校验失败 ❌")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
