/* 港池疏浚验收计量网页前端 (原生 JS, 无构建依赖)
 *
 * 格网刷新策略
 * ------------
 * - 页面打开时一次性全量加载格网 (/api/grid: 范围 + 全部读数 + 版本, 同一快照);
 *   此后每次"刷新格网"都走 /api/grid/changes 增量, 只拉版本号大于
 *   "上次成功显示的版本" 的格变更, 未改动的读数不再传输。
 * - 同一期同一格只带回最新值; 空水深 (depth_cm=null) 同样覆盖旧显示。
 * - 增量读数与其版本由后端同一个只读快照返回; 被整批退回的批次不写变更
 *   日志, 因此不会出现在增量里。
 * - 刷新是原子的: 所有分页先缓冲, 全部成功且仍是最新请求才一次性落地;
 *   任一步失败都保留原图与原版本, 下次重试仍从原版本继续补齐。
 * - 每个刷新带递增序号: 迟到的响应若已不是最新请求, 直接丢弃, 不会盖过
 *   较新的格网。
 * - 已显示版本只存于本页内存; 服务重启后原页面继续点刷新, 版本号仍在,
 *   从该版本增量即可, 无需重载全图。
 */
"use strict";

const $ = (id) => document.getElementById(id);
const canvas = $("map");
const ctx = canvas.getContext("2d");

const PAGE_LIMIT = 20000;

let view = { minX: 0, minY: 0, maxX: 0, maxY: 0, cell: 20 };
let viewEmpty = true;        // 视图是否尚无范围 (全量加载时无数据)
let cellData = new Map();    // "x,y" -> {before, after}
let rect = null;             // 当前框选 {x0,y0,x1,y1} (格坐标, 闭区间)
let drag = null;             // 拖选状态

// 已成功显示的版本; null 表示还没做过全量加载
let shownVersion = null;
// 请求序号: 每次刷新 +1, 迟到且已过时的响应据此丢弃
let requestSeq = 0;
let lastAppliedSeq = 0;

async function api(path, opts) {
  const resp = await fetch(path, opts);
  const body = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(body.detail || `HTTP ${resp.status}`);
  return body;
}

function setMsg(id, text, cls) {
  const el = $(id);
  if (!el) return;
  el.textContent = text;
  el.className = "msg " + (cls || "mut");
}

function setVersionLabel(v) {
  const el = $("grid-ver");
  if (el) el.textContent = v === null ? "—" : "v" + v;
}

/* 将范围并进视图; 视图为空时直接采用范围。 */
function mergeView(extent) {
  const any = [extent.before, extent.after].filter(Boolean);
  if (!any.length) return;
  const minX = Math.min(...any.map((e) => e.x0));
  const maxX = Math.max(...any.map((e) => e.x1));
  const minY = Math.min(...any.map((e) => e.y0));
  const maxY = Math.max(...any.map((e) => e.y1));
  if (viewEmpty) {
    view = { minX, minY, maxX, maxY, cell: 0 };
    viewEmpty = false;
    return;
  }
  if (minX < view.minX || minY < view.minY || maxX > view.maxX || maxY > view.maxY) {
    view = {
      minX: Math.min(minX, view.minX), minY: Math.min(minY, view.minY),
      maxX: Math.max(maxX, view.maxX), maxY: Math.max(maxY, view.maxY), cell: 0,
    };
  }
}

function cellsFrom(rows) {
  const map = new Map();
  for (const c of rows) {
    const key = `${c.x},${c.y}`;
    if (!map.has(key)) map.set(key, {});
    map.get(key)[c.phase] = c.depth_cm; // null = 空水深
  }
  return map;
}

/* ---------------- 全量加载 (页面初次打开) ---------------- */

async function fullSync(seq) {
  const data = await api("/api/grid");   // 一份快照: 范围 + 读数 + 版本
  if (seq < lastAppliedSeq) return { discarded: true };
  cellData = cellsFrom(data.cells);
  const any = [data.extent.before, data.extent.after].filter(Boolean);
  viewEmpty = !any.length;
  if (viewEmpty) {
    view = { minX: 0, minY: 0, maxX: 0, maxY: 0, cell: 20 };
  } else {
    mergeView(data.extent);
  }
  shownVersion = data.data_version;
  lastAppliedSeq = seq;
  setVersionLabel(shownVersion);
  draw();
  return { version: shownVersion };
}

/* ---------------- 增量同步 ---------------- */

async function incrementalSync(seq) {
  const since = shownVersion;
  const buffered = [];
  let cursor = null;
  let version = since;
  for (;;) {
    const q = new URLSearchParams({ since: String(since), limit: String(PAGE_LIMIT) });
    if (cursor) {
      q.set("cursor_version", String(cursor.version));
      q.set("cursor_phase", cursor.phase);
      q.set("cursor_x", String(cursor.x));
      q.set("cursor_y", String(cursor.y));
    }
    const page = await api("/api/grid/changes?" + q);
    if (seq < lastAppliedSeq) return { discarded: true }; // 迟到响应
    if (page.full_reload) return { fullReload: true };
    buffered.push(...page.changes);
    version = page.data_version;
    if (!page.next_cursor) break;
    cursor = page.next_cursor;
  }

  // 有更新请求已落地 -> 本次结果作废, 不得覆盖较新的格网
  if (seq < lastAppliedSeq) return { discarded: true };

  // 页面已带更新的版本 (例如刷新途中有一次上传触发的刷新先落地):
  // 本次响应已过时, 丢弃; 调用方会再刷一次补齐到最新。
  if (version < shownVersion) return { stale: true, version: shownVersion };

  // 先在本地算好新范围 (新补测可能带来原范围之外的格), 再一次性落地;
  // 期间的任何失败或更晚请求到达都让本次刷新整体作废, 不改动内存格网。
  let extent = null;
  if (version !== since) {
    try {
      extent = await api("/api/extent");
    } catch (e) {
      extent = null; // 范围取不到不影响读数正确性, 视图范围保守不动
    }
    if (seq < lastAppliedSeq) return { discarded: true };
    if (version < shownVersion) return { stale: true, version: shownVersion };
  }

  // 原子落地: 到这里确认本次仍是最新请求, 才开始改动格网与版本
  for (const c of buffered) {
    const key = `${c.x},${c.y}`;
    if (!cellData.has(key)) cellData.set(key, {});
    cellData.get(key)[c.phase] = c.depth_cm; // 同格只留最新值, null 照样覆盖
  }
  if (extent) mergeView(extent);
  shownVersion = version;
  lastAppliedSeq = seq;
  setVersionLabel(shownVersion);
  draw();
  return { version, applied: buffered.length };
}

/* ---------------- 统一刷新入口 ---------------- */

let fullLoaded = false;

async function refresh(opts) {
  const forceFull = !!(opts && opts.forceFull);
  const seq = ++requestSeq;
  const btn = $("btn-refresh");
  if (btn) btn.disabled = true;
  try {
    let res;
    if (forceFull || !fullLoaded || shownVersion === null) {
      res = await fullSync(seq);
      if (!res.discarded) fullLoaded = true;
    } else {
      res = await incrementalSync(seq);
      if (res.discarded) return;
      if (res.stale) {
        // 刷新途中有一次更新的刷新先落地; 再刷一次补齐到最新
        res = await incrementalSync(seq);
        if (res.discarded) return;
      }
      if (res.fullReload) {
        // 库被重置 / 版本倒退: 客户端缓存不可信, 整图重载
        res = await fullSync(seq);
      }
      if (res.stale) {
        // 第二次仍被更新的刷新抢在前: 交给那次刷新, 本次不再落地
        return;
      }
    }
    if (res.discarded || seq < lastAppliedSeq) return;
    const extra = (res && res.applied) ? `, 增量 ${res.applied} 格` : "";
    setMsg("refresh-msg", `格网已同步到数据版本 v${shownVersion}${extra}`, "ok");
    await loadBatches();
  } catch (e) {
    // 刷新失败: 原图与原版本保持不变 (增量是缓冲后原子落地的)
    setMsg(
      "refresh-msg",
      `刷新失败, 保留原图与版本 v${shownVersion ?? "未加载"}: ${e.message}`,
      "err"
    );
  } finally {
    if (btn) btn.disabled = false;
  }
}

/* ---------------- 上传 ---------------- */
document.querySelectorAll(".up button").forEach((btn) => {
  btn.addEventListener("click", async () => {
    const phase = btn.dataset.phase;
    const input = $("file-" + phase);
    if (!input.files.length) {
      setMsg("up-msg", "请先选择 " + (phase === "before" ? "前测" : "后测") + " CSV 文件", "err");
      return;
    }
    const fd = new FormData();
    fd.append("file", input.files[0]);
    btn.disabled = true;
    try {
      const r = await api(`/api/surveys/${phase}`, { method: "POST", body: fd });
      setMsg(
        "up-msg",
        `${phase === "before" ? "前测" : "后测"}已接收: 收 ${r.rows_received} 行, `
        + `写入 ${r.rows_applied} 格, 旧测次跳过 ${r.rows_skipped_stale} 格, `
        + `重算块 ${r.blocks_touched.length} 个, 数据版本 v${r.data_version}`,
        "ok"
      );
      // 上传成功后按最新版本增量补齐 (只传本批改动过的格)
      await refresh();
    } catch (e) {
      setMsg("up-msg", "上传被退回: " + e.message, "err");
    } finally {
      btn.disabled = false;
    }
  });
});

/* ---------------- 绘制 ---------------- */
function colorFor(d) {
  if (d.before === undefined || d.after === undefined) return "#e8edf2";
  if (d.before === null || d.after === null) return "#94a3b8";
  const diff = d.after - d.before; // 正: 挖深; 负: 回淤
  if (diff === 0) return "#f1f5f9";
  const mag = Math.min(Math.abs(diff) / 50, 1);
  if (diff > 0) {
    const g = Math.round(185 - mag * 130), b = Math.round(120 - mag * 90);
    return `rgb(185,${g},${b})`;
  }
  const g = Math.round(78 + (1 - mag) * 120), r = Math.round(29 + (1 - mag) * 150);
  return `rgb(${r},${g},216)`;
}

function draw() {
  const W = canvas.width, H = canvas.height;
  ctx.clearRect(0, 0, W, H);
  const nx = view.maxX - view.minX + 1 || 1;
  const ny = view.maxY - view.minY + 1 || 1;
  const cell = Math.max(4, Math.floor(Math.min((W - 40) / nx, (H - 40) / ny)));
  view.cell = cell;
  const ox = 20, oy = 20;
  const px = (x) => ox + (x - view.minX) * cell;
  const py = (y) => oy + (y - view.minY) * cell;

  // 格底色
  for (const [key, d] of cellData) {
    const [x, y] = key.split(",").map(Number);
    ctx.fillStyle = colorFor(d);
    ctx.fillRect(px(x), py(y), cell, cell);
  }

  // 块边界 (粗线)
  ctx.strokeStyle = "#475569";
  ctx.lineWidth = 1.5;
  const bs = Number($("bsize").textContent) || 16;
  for (let bx = Math.floor(view.minX / bs); bx <= Math.floor(view.maxX / bs); bx++) {
    const x = bx * bs;
    if (x < view.minX || x > view.maxX) continue;
    ctx.beginPath(); ctx.moveTo(px(x), oy); ctx.lineTo(px(x), oy + ny * cell); ctx.stroke();
  }
  for (let by = Math.floor(view.minY / bs); by <= Math.floor(view.maxY / bs); by++) {
    const y = by * bs;
    if (y < view.minY || y > view.maxY) continue;
    ctx.beginPath(); ctx.moveTo(ox, py(y)); ctx.lineTo(ox + nx * cell, py(y)); ctx.stroke();
  }

  // 细格线
  ctx.strokeStyle = "rgba(100,116,139,.25)";
  ctx.lineWidth = 1;
  ctx.strokeRect(ox, oy, nx * cell, ny * cell);

  // 框选区
  if (rect) {
    const r = normalize(rect);
    ctx.fillStyle = "rgba(253,230,138,.45)";
    ctx.strokeStyle = "#b45309";
    ctx.lineWidth = 2;
    const rw = (r.x1 - r.x0 + 1) * cell, rh = (r.y1 - r.y0 + 1) * cell;
    ctx.fillRect(px(r.x0), py(r.y0), rw, rh);
    ctx.strokeRect(px(r.x0), py(r.y0), rw, rh);
  }
}

function normalize(r) {
  return {
    x0: Math.min(r.x0, r.x1), x1: Math.max(r.x0, r.x1),
    y0: Math.min(r.y0, r.y1), y1: Math.max(r.y0, r.y1),
  };
}

/* ---------------- 鼠标沿格线框选 ---------------- */
function eventCell(ev) {
  const box = canvas.getBoundingClientRect();
  const sx = canvas.width / box.width, sy = canvas.height / box.height;
  const cx = (ev.clientX - box.left) * sx - 20;
  const cy = (ev.clientY - box.top) * sy - 20;
  const x = view.minX + Math.floor(cx / view.cell);
  const y = view.minY + Math.floor(cy / view.cell);
  return { x, y };
}

canvas.addEventListener("mousedown", (ev) => {
  const c = eventCell(ev);
  if (c.x < view.minX || c.x > view.maxX || c.y < view.minY || c.y > view.maxY) return;
  drag = { x0: c.x, y0: c.y };
  rect = { x0: c.x, y0: c.y, x1: c.x, y1: c.y };
  syncRectInputs();
  draw();
});
canvas.addEventListener("mousemove", (ev) => {
  if (!drag) return;
  const c = eventCell(ev);
  rect = { x0: drag.x0, y0: drag.y0, x1: c.x, y1: c.y };
  syncRectInputs();
  draw();
});
window.addEventListener("mouseup", () => { drag = null; });

function syncRectInputs() {
  if (!rect) return;
  const r = normalize(rect);
  $("r-x0").value = r.x0; $("r-y0").value = r.y0;
  $("r-x1").value = r.x1; $("r-y1").value = r.y1;
}

/* ---------------- 核量 ---------------- */
async function runSelection(cellwise) {
  const r = {
    x0: Number($("r-x0").value), y0: Number($("r-y0").value),
    x1: Number($("r-x1").value), y1: Number($("r-y1").value),
  };
  if ([r.x0, r.y0, r.x1, r.y1].some((v) => Number.isNaN(v))) {
    setMsg("sel-msg", "请在画布拖选或填写完整的整数格坐标", "err");
    return;
  }
  rect = r;
  draw();
  try {
    const q = new URLSearchParams({ ...r, cellwise: String(cellwise) });
    const res = await api("/api/selection?" + q);
    $("r-area").textContent = `${res.area_cells_m2} m²`;
    $("r-vol").textContent = `${res.net_volume_m3.toFixed(3)}`;
    $("r-blocks").textContent = res.whole_blocks;
    $("r-edge").textContent =
      `${res.edge_cells} 格 (其中可计量 ${res.edge_matched_cells})`;
    $("r-ver").textContent = "v" + res.data_version;
    $("r-mode").textContent = res.mode === "cellwise" ? "全部细格(校验)" : "整块+边缘";
    setMsg(
      "sel-msg",
      `选区 [${res.x0},${res.y0}]–[${res.x1},${res.y1}] 共 `
      + `${(res.x1-res.x0+1)*(res.y1-res.y0+1)} 格; `
      + `整块与边缘取自同一快照 v${res.data_version}`,
      "ok"
    );
  } catch (e) {
    setMsg("sel-msg", "核量失败: " + e.message, "err");
  }
}

$("btn-select").addEventListener("click", () => runSelection(false));
$("btn-cellwise").addEventListener("click", () => runSelection(true));
$("btn-clear").addEventListener("click", () => {
  rect = null; draw();
  ["r-x0", "r-y0", "r-x1", "r-y1"].forEach((id) => ($(id).value = ""));
  setMsg("sel-msg", "", "mut");
});

/* ---------------- 批次留痕 ---------------- */
async function loadBatches() {
  const data = await api("/api/batches?limit=20");
  const tb = document.querySelector("#batch-table tbody");
  tb.innerHTML = "";
  for (const b of data.batches) {
    const tr = document.createElement("tr");
    tr.innerHTML =
      `<td>${b.id}</td><td>${b.phase === "before" ? "前" : "后"}</td>`
      + `<td>${b.survey_no}</td><td>${b.row_count}</td>`
      + `<td class="tag-${b.status}">${b.status === "accepted" ? "接收" : "退回"}</td>`
      + `<td style="text-align:left">${b.reject_reason || ""}</td>`;
    tb.appendChild(tr);
  }
}

/* ---------------- 刷新按钮 ---------------- */
$("btn-refresh").addEventListener("click", () => refresh());
$("btn-reload").addEventListener("click", () => {
  // 显式全量重载: 丢弃当前格网与版本, 从 0 重新拉全图
  fullLoaded = false;
  cellData = new Map();
  viewEmpty = true;
  shownVersion = null;
  lastAppliedSeq = ++requestSeq;   // 让所有在途响应作废
  setVersionLabel(null);
  refresh({ forceFull: true });
});

/* ---------------- 启动 ---------------- */
(async function init() {
  try {
    const blk = await api("/api/blocks");
    $("bsize").textContent = blk.block_size;
    $("bsize2").textContent = blk.block_size;
  } catch (e) {
    setMsg("up-msg", "初始化失败: " + e.message, "err");
    return;
  }
  // 页面初次打开只在这里加载一次全图; 之后一律增量
  await refresh();
})();

/* 供端到端自测读取内部状态 (只读) */
window.__dredgingGrid = {
  version: () => shownVersion,
  cell: (x, y) => cellData.get(`${x},${y}`) || null,
  size: () => cellData.size,
};
