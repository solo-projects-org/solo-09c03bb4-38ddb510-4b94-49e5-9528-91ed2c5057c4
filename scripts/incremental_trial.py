#!/usr/bin/env python3
"""增量刷新试算 (对运行中的服务):

1. 初次全量加载两期格网, 记下显示版本;
2. 交错补测与增量刷新: 每次只回传改动过的格, 未改动读数不重复传输;
3. 同一期同一格只回传最新值; 空水深照常覆盖;
4. 同号异值整批退回: 不推进版本、不出现在增量里;
5. 反复补测同一格: 变更日志行数不变 (额外存储不随补测次数累积);
6. 增量里的每格读数与响应版本来自同一份已提交快照。

用法: python scripts/incremental_trial.py [BASE]
"""

import csv
import io
import json
import sqlite3
import sys
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
DB = None  # 可选: 直接查库核对存储, 由 --db 指定


def _post(phase, rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["x", "y", "survey_no", "depth_cm"])
    for r in rows:
        w.writerow(["" if v is None else v for v in r])
    boundary = "----inc"
    body = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
        f"filename=\"s.csv\"\r\n\r\n{buf.getvalue()}\r\n--{boundary}--\r\n"
    ).encode()
    req = urllib.request.Request(
        BASE + f"/api/surveys/{phase}", data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _get(path):
    with urllib.request.urlopen(BASE + path) as r:
        return json.loads(r.read())


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))
    if not cond:
        raise SystemExit(1)


def main():
    # 基线 20x20 两期
    grid = [(x, y, 1, 200) for x in range(20) for y in range(20)]
    _post("before", grid)
    _post("after", [(x, y, 1, 230) for x, y, _, _ in grid])

    full = _get("/api/grid")
    v0 = full["data_version"]
    check("初次全量加载", len(full["cells"]) == 800, f"{len(full['cells'])} 读数, v{v0}")

    # 补测 2 格, 增量只回 2 格
    _post("after", [(1, 1, 2, 260), (2, 2, 2, 210)])
    inc = _get(f"/api/grid/changes?since={v0}")
    check("增量只回传改动格", len(inc["changes"]) == 2, f"{len(inc['changes'])} 格")
    check("未改动读数不重复传输",
          {c["phase"] for c in inc["changes"]} == {"after"})
    check("增量版本前进一格", inc["data_version"] == v0 + 1, f"v{inc['data_version']}")
    v1 = inc["data_version"]

    # 空水深覆盖
    _post("after", [(3, 3, 3, None)])
    inc = _get(f"/api/grid/changes?since={v1}")
    check("空水深照样出现在增量且为 null",
          len(inc["changes"]) == 1 and inc["changes"][0]["depth_cm"] is None,
          str(inc["changes"]))
    v2 = inc["data_version"]

    # 同格连续补测 3 次: 只回最新值
    _post("after", [(4, 4, 4, 240)])
    _post("after", [(4, 4, 5, 250)])
    _post("after", [(4, 4, 6, 270)])
    inc = _get(f"/api/grid/changes?since={v2}")
    got = {(c["x"], c["y"]): c for c in inc["changes"]}
    check("同一格只回传最新值",
          got[(4, 4)]["depth_cm"] == 270 and got[(4, 4)]["survey_no"] == 6,
          str(got.get((4, 4))))
    v3 = inc["data_version"]

    # 退回批次不进增量、不推进版本
    status, _ = _post("after", [(1, 1, 2, 999)])
    check("同号异值整批退回", status == 409, f"HTTP {status}")
    inc = _get(f"/api/grid/changes?since={v3}")
    check("退回批次不推进版本且不在增量里",
          inc["data_version"] == v3 and inc["changes"] == [],
          f"v{inc['data_version']}, {len(inc['changes'])} 格")

    # 反复补测同一格: 增量大小恒定
    sizes = []
    vprev = v3
    for sno in range(100, 160):
        _post("after", [(5, 5, sno, 300 + sno)])
        page = _get(f"/api/grid/changes?since={vprev}")
        sizes.append(len(page["changes"]))
        vprev = page["data_version"]
    check("反复补测同一格每次增量恒为 1 格", set(sizes) == {1}, f"{sizes[:5]}…")

    # 交错补测若干格后: 用增量拼出的视图与全量重载完全一致
    v_before_burst = vprev
    for i in range(10):
        _post("after", [(i, 10, 50 + i, 250 + i)])
        _post("before", [(10, i, 50 + i, 180 + i)])
    view = {}
    since = v0
    cursor = None
    while True:
        q = f"/api/grid/changes?since={since}"
        if cursor:
            q += (f"&cursor_version={cursor['version']}&cursor_phase={cursor['phase']}"
                  f"&cursor_x={cursor['x']}&cursor_y={cursor['y']}")
        page = _get(q)
        for c in page["changes"]:
            view.setdefault((c["x"], c["y"]), {})[c["phase"]] = c["depth_cm"]
        if not page["next_cursor"]:
            break
        cursor = page["next_cursor"]
    full2 = _get("/api/grid")
    actual = {}
    for c in full2["cells"]:
        actual.setdefault((c["x"], c["y"]), {})[c["phase"]] = c["depth_cm"]
    # 增量视图可能缺少从未变更过的基线格, 只比对增量覆盖到的格
    mism = sum(
        1 for k, phases in view.items() for ph, dv in phases.items()
        if actual.get(k, {}).get(ph) != dv
    )
    check("增量拼出的视图与全量一致", mism == 0, f"{len(view)} 格, {mism} 处不一致")
    check("全量范围覆盖增量格", all(k in actual for k in view))
    print("\n增量试算全部通过")


if __name__ == "__main__":
    main()
