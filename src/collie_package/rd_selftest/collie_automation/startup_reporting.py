# pyright: reportGeneralTypeIssues=false, reportArgumentType=false, reportOperatorIssue=false, reportCallIssue=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUnknownVariableType=false

import html
import os
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from .startup_runner import LaunchResidencyRecord

from .. import state


_OFFLINE_CHART_JS = r"""(function(){
'use strict';
function _getCanvas(target){
  if(!target) return null;
  if(target instanceof HTMLCanvasElement) return target;
  if(target.canvas instanceof HTMLCanvasElement) return target.canvas;
  return null;
}
function _setupCanvas(canvas){
  var rect = canvas.getBoundingClientRect ? canvas.getBoundingClientRect() : {width:canvas.width,height:canvas.height};
  var dpr = window.devicePixelRatio || 1;
  var w = Math.max(1, Math.floor(rect.width || canvas.width || 600));
  var h = Math.max(1, Math.floor(rect.height || canvas.height || 240));
  if(canvas.width !== w*dpr || canvas.height !== h*dpr){
    canvas.width = w*dpr; canvas.height = h*dpr;
    canvas.style.width = w + 'px'; canvas.style.height = h + 'px';
  }
  var ctx = canvas.getContext('2d');
  ctx.setTransform(dpr,0,0,dpr,0,0);
  return {ctx:ctx,w:w,h:h};
}
function _num(x){ x = +x; return isFinite(x) ? x : 0; }
function _max(arr){
  var m = 0;
  for(var i=0;i<(arr||[]).length;i++){ var v = _num(arr[i]); if(v > m) m = v; }
  return m;
}
function _axes(ctx,w,h,pad){
  ctx.strokeStyle = 'rgba(17,24,39,0.25)';
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(pad,pad);
  ctx.lineTo(pad,h-pad);
  ctx.lineTo(w-pad,h-pad);
  ctx.stroke();
}
function _renderBar(ctx,w,h,pad,labels,datasets){
  var values = (datasets[0] && datasets[0].data) ? datasets[0].data : [];
  var maxV = _max(values) || 1;
  var n = Math.max(1, values.length);
  var chartW = w - pad*2;
  var chartH = h - pad*2;
  var gap = 6;
  var barW = Math.max(2, (chartW - gap*(n-1)) / n);
  ctx.font = '11px sans-serif';
  ctx.textAlign = 'center';
  ctx.textBaseline = 'bottom';
  for(var i=0;i<n;i++){
    var v = _num(values[i]);
    var bh = chartH * (v / maxV);
    var x = pad + i*(barW+gap);
    var y = (h - pad) - bh;
    ctx.fillStyle = 'rgba(78,115,223,0.35)';
    ctx.fillRect(x,y,barW,bh);
    ctx.fillStyle = 'rgba(78,115,223,0.95)';
    ctx.fillText(String(v), x + barW/2, y - 2);
    var label = (labels && labels[i] != null) ? String(labels[i]) : '';
    if(label){
      ctx.fillStyle = 'rgba(17,24,39,0.7)';
      ctx.textBaseline = 'top';
      ctx.fillText(label, x + barW/2, h - pad + 6);
      ctx.textBaseline = 'bottom';
    }
  }
}
function _renderLine(ctx,w,h,pad,labels,datasets){
  var values = (datasets[0] && datasets[0].data) ? datasets[0].data : [];
  var maxV = _max(values) || 1;
  var n = Math.max(1, values.length);
  var chartW = w - pad*2;
  var chartH = h - pad*2;
  var step = (n <= 1) ? 0 : (chartW / (n-1));
  ctx.strokeStyle = 'rgba(78,115,223,0.95)';
  ctx.lineWidth = 2;
  ctx.beginPath();
  for(var i=0;i<n;i++){
    var v = _num(values[i]);
    var x = pad + step*i;
    var y = (h - pad) - (chartH * (v / maxV));
    if(i===0) ctx.moveTo(x,y); else ctx.lineTo(x,y);
  }
  ctx.stroke();
  ctx.fillStyle = 'rgba(78,115,223,0.95)';
  for(var j=0;j<n;j++){
    var vv = _num(values[j]);
    var xx = pad + step*j;
    var yy = (h - pad) - (chartH * (vv / maxV));
    ctx.beginPath(); ctx.arc(xx,yy,3,0,Math.PI*2); ctx.fill();
  }
}

function Chart(target, config){
  this.config = config || {};
  this.canvas = _getCanvas(target) || target;
  if(!this.canvas) return;
  this.render();
}
Chart.register = function(){ };
Chart.prototype.render = function(){
  var cfg = this.config || {};
  var type = String(cfg.type || 'bar');
  var data = cfg.data || {};
  var labels = data.labels || [];
  var datasets = data.datasets || [];
  if(!datasets || !datasets.length) return;
  var s = _setupCanvas(this.canvas);
  var ctx = s.ctx, w = s.w, h = s.h;
  ctx.clearRect(0,0,w,h);
  var pad = 32;
  _axes(ctx,w,h,pad);
  if(type === 'line') _renderLine(ctx,w,h,pad,labels,datasets);
  else _renderBar(ctx,w,h,pad,labels,datasets);
};

window.Chart = window.Chart || Chart;
window.ChartDataLabels = window.ChartDataLabels || {};
})();
"""


def _write_offline_chart_js(output_dir: str) -> str:
    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, 'chart.min.js')
    if os.path.isfile(path):
        return path
    with open(path, 'w', encoding='utf-8') as f:
        f.write(_OFFLINE_CHART_JS)
    return path


def analyze_results(
    round1: Dict[str, Optional[int]], round2: Dict[str, Optional[int]]
) -> List[Tuple[str, int, int, str]]:
    """对两轮 PID 结果进行冷/热启动判定。"""
    results: List[Tuple[str, int, int, str]] = []
    for pkg in round1:
        pid1 = round1[pkg]
        pid2 = round2[pkg]

        if pid1 is None or pid2 is None:
            continue

        is_cold = pid1 != pid2

        results.append((pkg, pid1, pid2, "冷启动" if is_cold else "热启动"))

    return sorted(results, key=lambda x: x[0])


def _shorten(text: str, width: int = 40) -> str:
    return text if len(text) <= width else f"{text[: width - 3]}..."


def _format_prev_stat(detail: Dict[str, object]) -> str:
    checked = detail.get("checked", []) or []
    alive = detail.get("alive", []) or []
    rate = detail.get("rate")
    if not checked:
        return "-"
    rate_str = f"{len(alive)}/{len(checked)}"
    if rate is not None:
        rate_str += f"({rate*100:.1f}%)"
    if alive:
        rate_str += f" {','.join(alive)}"
    return rate_str


def _color_rate(text: str, rate: Optional[float]) -> str:
    if rate is not None and rate < 1.0:
        return f"\033[91m{text}\033[0m"
    return text


def generate_residency_only_report(
    package_count: int,
    rounds: int,
    alive_history: List[int],
    residency_records: Optional[List[LaunchResidencyRecord]] = None,
    residency_summary: Optional[Dict[int, Dict[str, object]]] = None,
    oomadj_summary: Optional[str] = None,
    kill_summary: Optional[str] = None,
    ftrace_summary: Optional[str] = None,
    start_time: Optional[datetime] = None,
    end_time: Optional[datetime] = None,
):
    """生成仅包含驻留信息的终端报告（不做冷/热启动判定）。"""
    print("\n驻留测试报告:")
    print("-" * 65)
    print(f"总轮次: {rounds} | 覆盖应用: {package_count} 个")
    if start_time:
        print(f"测试开始时间: {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    if end_time:
        print(f"测试结束时间: {end_time.strftime('%Y-%m-%d %H:%M:%S')}")
    if start_time and end_time:
        print(f"测试耗时: {end_time - start_time}")

    if residency_records:
        print("\n驻留明细（启动前存活的前序应用驻留率）:")
        header = (
            f"{'轮'.ljust(3)}{'序'.ljust(3)}{'应用名称'.ljust(25)}"
            f"{'启动前存活'.ljust(12)}{'存活列表'.ljust(42)}"
            f"{'前1'.ljust(16)}{'前2'.ljust(16)}{'前3'.ljust(16)}{'前4'.ljust(16)}{'前5'.ljust(16)}"
        )
        print(header)
        print("-" * len(header))
        for record in residency_records:
            alive_list = ", ".join(record.alive_before) if record.alive_before else "-"
            alive_list = _shorten(alive_list, 40)
            row = (
                f"{str(record.round_no).ljust(3)}"
                f"{str(record.order_in_round).ljust(3)}"
                f"{record.package.ljust(25)}"
                f"{str(len(record.alive_before)).ljust(12)}"
                f"{alive_list.ljust(42)}"
                f"{_format_prev_stat(record.prev_alive_stats.get(1, {})).ljust(16)}"
                f"{_format_prev_stat(record.prev_alive_stats.get(2, {})).ljust(16)}"
                f"{_format_prev_stat(record.prev_alive_stats.get(3, {})).ljust(16)}"
                f"{_format_prev_stat(record.prev_alive_stats.get(4, {})).ljust(16)}"
                f"{_format_prev_stat(record.prev_alive_stats.get(5, {})).ljust(16)}"
            )
            print(row)

    if residency_summary:
        print("\n前序驻留率均值（全部启动过程）:")
        for n in range(1, 6):
            item = residency_summary.get(n, {})
            total = item.get("total") or 0
            alive = item.get("alive") or 0
            rate = item.get("rate")
            rate_str = f"{rate*100:.1f}%" if rate is not None else "N/A"
            rate_str = _color_rate(rate_str, rate)
            print(f"  前{n}: {rate_str} （{alive}/{total}）")

    if oomadj_summary:
        print("\n驻留(OOMAdj)解析概要:")
        print(oomadj_summary)

    if kill_summary:
        print("\n查杀解析概要:")
        print(kill_summary)

    if ftrace_summary:
        print("\nftrace Global Stats:")
        print(ftrace_summary)


def generate_report(
    results,
    package_count: int,
    background: float,
    residency_records: Optional[List[LaunchResidencyRecord]] = None,
    residency_summary: Optional[Dict[int, Dict[str, object]]] = None,
    oomadj_summary: Optional[str] = None,
    kill_summary: Optional[str] = None,
    ftrace_summary: Optional[str] = None,
    start_time: Optional[datetime] = None,
    end_time: Optional[datetime] = None,
):
    """生成终端可读的报告。"""
    print("\n冷启动分析报告:")
    print("-" * 65)
    print(f"{'应用名称'.ljust(25)} | 第1轮PID | 第2轮PID | 状态")
    print("-" * 65)

    cold_count = 0
    for item in results:
        color = "\033[92m" if item[3] == "冷启动" else "\033[0m"
        print(
            f"{color}{item[0].ljust(25)} | {str(item[1]).ljust(8)} | {str(item[2]).ljust(8)} | {item[3]}\033[0m"
        )
        if item[3] == "冷启动":
            cold_count += 1

    package_count = package_count or 1
    background = background if background else 0
    print("-" * 65)
    print(f"总计: {len(results)} 个应用 (有效数据)")
    print("真实冷启动")
    if len(results):
        print(f"    冷启动率: {cold_count/len(results)*100:.1f}% ({cold_count} 个)")
        print(
            f"    热启动率: {(len(results)-cold_count)/len(results)*100:.1f}% ({len(results)-cold_count} 个)"
        )
    else:
        print("    冷启动率: 0.0% (无有效数据)")
        print("    热启动率: 0.0% (无有效数据)")
    print("客观冷启动")
    print(f"    平均后台驻留: {background:.1f} 个")
    if background:
        print(f"    驻留利用率: {(len(results)-cold_count)/background*100:.1f}% ")
    else:
        print("    驻留利用率: 0.0% (缺少后台驻留数据)")
    if start_time:
        print(f"测试开始时间: {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    if end_time:
        print(f"测试结束时间: {end_time.strftime('%Y-%m-%d %H:%M:%S')}")
    if start_time and end_time:
        duration = end_time - start_time
        print(f"测试耗时: {duration}")

    if residency_records:
        print("\n驻留明细（启动前存活的前序应用驻留率）:")
        header = (
            f"{'轮'.ljust(3)}{'序'.ljust(3)}{'应用名称'.ljust(25)}"
            f"{'启动前存活'.ljust(12)}{'存活列表'.ljust(42)}"
            f"{'前1'.ljust(16)}{'前2'.ljust(16)}{'前3'.ljust(16)}{'前4'.ljust(16)}{'前5'.ljust(16)}"
        )
        print(header)
        print("-" * len(header))
        for record in residency_records:
            alive_list = ", ".join(record.alive_before) if record.alive_before else "-"
            alive_list = _shorten(alive_list, 40)
            row = (
                f"{str(record.round_no).ljust(3)}"
                f"{str(record.order_in_round).ljust(3)}"
                f"{record.package.ljust(25)}"
                f"{str(len(record.alive_before)).ljust(12)}"
                f"{alive_list.ljust(42)}"
                f"{_format_prev_stat(record.prev_alive_stats.get(1, {})).ljust(16)}"
                f"{_format_prev_stat(record.prev_alive_stats.get(2, {})).ljust(16)}"
                f"{_format_prev_stat(record.prev_alive_stats.get(3, {})).ljust(16)}"
                f"{_format_prev_stat(record.prev_alive_stats.get(4, {})).ljust(16)}"
                f"{_format_prev_stat(record.prev_alive_stats.get(5, {})).ljust(16)}"
            )
            print(row)

    if residency_summary:
        print("\n前序驻留率均值（全部启动过程）:")
        for n in range(1, 6):
            item = residency_summary.get(n, {})
            total = item.get("total") or 0
            alive = item.get("alive") or 0
            rate = item.get("rate")
            rate_str = f"{rate*100:.1f}%" if rate is not None else "N/A"
            rate_str = _color_rate(rate_str, rate)
            print(f"  前{n}: {rate_str} （{alive}/{total}）")

    if oomadj_summary:
        print("\n驻留(OOMAdj)解析概要:")
        print(oomadj_summary)

    if kill_summary:
        print("\n查杀解析概要:")
        print(kill_summary)

    if ftrace_summary:
        print("\nftrace Global Stats:")
        print(ftrace_summary)


def generate_html_report(
    results: List[Tuple[str, int, int, str]],
    package_count: int,
    background: float,
    alive_history: List[int],
    residency_records: Optional[List[LaunchResidencyRecord]] = None,
    residency_summary: Optional[Dict[int, Dict[str, object]]] = None,
    oomadj_summary: Optional[str] = None,
    kill_summary: Optional[str] = None,
    ftrace_summary: Optional[str] = None,
    memcat_html: Optional[str] = None,
    start_time: Optional[datetime] = None,
    end_time: Optional[datetime] = None,
):
    """生成包含可视化报告和折线图的HTML文件。"""
    _write_offline_chart_js(state.FILE_DIR or os.getcwd())
    cold_count = sum(1 for item in results if item[3] == "冷启动")

    n = package_count
    background = background if n > 0 else 0

    table_rows = []
    for item in results:
        status_class = "class='cold'" if item[3] == "冷启动" else ""
        escaped = [
            html.escape(str(item[0])),
            html.escape(str(item[1])),
            html.escape(str(item[2])),
            html.escape(str(item[3])),
        ]
        table_rows.append(
            f"""
            <tr {status_class}>
                <td>{escaped[0]}</td>
                <td>{escaped[1]}</td>
                <td>{escaped[2]}</td>
                <td>{escaped[3]}</td>
            </tr>
        """
        )

    total_apps = len(results)
    cold_rate = cold_count / total_apps * 100 if total_apps else 0
    utilization = (total_apps - cold_count) / background * 100 if background else 0

    chart_labels = list(range(1, len(alive_history) + 1)) if alive_history else []
    chart_data = alive_history if alive_history else []

    residency_rows: List[str] = []
    if residency_records:
        for record in residency_records:
            alive_list = ", ".join(html.escape(name) for name in record.alive_before) or "-"
            def _cell(n: int) -> str:
                detail = record.prev_alive_stats.get(n, {}) if record.prev_alive_stats else {}
                checked = detail.get("checked", []) or []
                alive = detail.get("alive", []) or []
                rate = detail.get("rate")
                if not checked:
                    return "-"
                cell = f"{len(alive)}/{len(checked)}"
                if rate is not None:
                    cell += f" ({rate*100:.1f}%)"
                if alive:
                    cell += "<br><small>" + ", ".join(html.escape(a) for a in alive) + "</small>"
                return cell

            residency_rows.append(
                f"""
                <tr>
                    <td>{record.round_no}</td>
                    <td>{record.order_in_round}</td>
                    <td>{html.escape(record.package)}</td>
                    <td>{len(record.alive_before)}</td>
                    <td>{alive_list}</td>
                    <td>{_cell(1)}</td>
                    <td>{_cell(2)}</td>
                    <td>{_cell(3)}</td>
                    <td>{_cell(4)}</td>
                    <td>{_cell(5)}</td>
                </tr>
                """
            )

    summary_rows: List[str] = []
    if residency_summary:
        for n in range(1, 6):
            item = residency_summary.get(n, {})
            total = item.get("total") or 0
            alive = item.get("alive") or 0
            rate = item.get("rate")
            rate_str = f"{rate*100:.1f}%" if rate is not None else "N/A"
            rate_class = "low-rate" if rate is not None and rate < 1.0 else ""
            summary_rows.append(
                f"<tr class='{rate_class}'><td>前{n}</td><td>{alive}</td><td>{total}</td><td>{rate_str}</td></tr>"
            )

    residency_section = ""
    if residency_rows:
        residency_section = f"""
        <div class="residency-section">
            <h2>驻留明细</h2>
            <table class="residency-table">
                <thead>
                    <tr>
                        <th>轮次</th>
                        <th>序号</th>
                        <th>应用</th>
                        <th>启动前存活数</th>
                        <th>存活列表</th>
                        <th>前1</th>
                        <th>前2</th>
                        <th>前3</th>
                        <th>前4</th>
                        <th>前5</th>
                    </tr>
                </thead>
                <tbody>
                    {''.join(residency_rows)}
                </tbody>
            </table>
        </div>
        """

    summary_section = ""
    if summary_rows:
        summary_section = f"""
        <div class="residency-section">
            <h2>前序驻留率汇总</h2>
            <table class="residency-table">
                <thead>
                    <tr><th>窗口</th><th>存活</th><th>样本</th><th>驻留率</th></tr>
                </thead>
                <tbody>
                    {''.join(summary_rows)}
                </tbody>
            </table>
        </div>
        """

    oomadj_section = ""
    if oomadj_summary:
        oomadj_section = f"""
        <div class="residency-section card">
            <h2>驻留(OOMAdj)解析概要</h2>
            <pre>{html.escape(oomadj_summary)}</pre>
        </div>
        """

    kill_section = ""
    if kill_summary:
        kill_section = f"""
        <div class="residency-section card">
            <h2>查杀解析概要</h2>
            <pre>{html.escape(kill_summary)}</pre>
        </div>
        """

    ftrace_section = ""
    if ftrace_summary:
        ftrace_section = f"""
        <div class="residency-section card">
            <h2>ftrace Global Stats</h2>
            <pre>{html.escape(ftrace_summary)}</pre>
        </div>
        """

    memcat_section = ""
    if memcat_html:
        safe_path = html.escape(memcat_html)
        memcat_section = f"""
        <div class="residency-section card memcat-card">
            <div class="memcat-header">
                <h2>Memcat 内存视图</h2>
                <button class="btn" id="memcatToggle">全屏/退出全屏</button>
            </div>
            <div class="iframe-wrapper">
                <iframe src="{safe_path}" title="memcat-report" loading="lazy"></iframe>
                <div class="iframe-note">如果 iframe 未加载，可直接打开文件: {safe_path}</div>
            </div>
        </div>
        <script>
            const memcatCard = document.querySelector('.memcat-card');
            const memcatToggle = document.getElementById('memcatToggle');
            if (memcatCard && memcatToggle) {{
                memcatToggle.addEventListener('click', () => {{
                    memcatCard.classList.toggle('fullscreen');
                }});
            }}
        </script>
        """

    start_str = start_time.strftime("%Y-%m-%d %H:%M:%S") if start_time else "-"
    end_str = end_time.strftime("%Y-%m-%d %H:%M:%S") if end_time else "-"
    duration_str = (
        str(end_time - start_time) if start_time and end_time else "-"
    )

    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <title>冷启动分析报告</title>
        <script src="chart.min.js"></script>
        <style>
            body {{
                font-family: "Helvetica Neue", Arial, sans-serif;
                margin: 0;
                padding: 0;
                background: linear-gradient(135deg, #f5f7fa 0%, #e8ecf3 100%);
                color: #1f2933;
            }}
            .page {{
                max-width: 1100px;
                margin: 0 auto;
                padding: 32px 24px 64px;
            }}
            h1, h2 {{
                text-align: center;
                letter-spacing: 0.02em;
            }}
            .card {{
                background: #ffffff;
                border: 1px solid #e5e7eb;
                border-radius: 12px;
                padding: 20px;
                margin: 20px auto;
                box-shadow: 0 10px 30px rgba(15, 23, 42, 0.08);
            }}
            table {{ 
                border-collapse: collapse; 
                width: 100%; 
                margin: 12px 0;
                box-shadow: 0 1px 4px rgba(0,0,0,0.08);
            }}
            th, td {{
                border: 1px solid #e5e7eb;
                padding: 12px;
                text-align: center;
            }}
            th {{ 
                background: #f3f4f6; 
                font-weight: 600; 
                color: #111827;
            }}
            tr:nth-child(even) td {{ background: #fbfdff; }}
            tr.cold {{ color: #1c7c54; font-weight: 600; }}
            .stats-grid {{
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
                gap: 12px;
                text-align: center;
            }}
            .stat-pill {{
                background: #f8fafc;
                border: 1px solid #e5e7eb;
                border-radius: 10px;
                padding: 12px;
                font-weight: 600;
            }}
            .chart-container {{ 
                max-width: 900px;
                margin: 32px auto;
            }}
            .residency-table td, .residency-table th {{
                font-size: 12px;
            }}
            .residency-section {{
                margin-top: 32px;
            }}
            .iframe-wrapper {{
                width: 100%;
                min-height: 520px;
            }}
            .iframe-wrapper iframe {{
                width: 100%;
                min-height: 500px;
                border: 1px solid #e5e7eb;
                border-radius: 12px;
                background: #fff;
            }}
            .iframe-note {{
                margin-top: 6px;
                font-size: 12px;
                color: #6b7280;
            }}
            .memcat-card.fullscreen {{
                position: fixed;
                inset: 12px;
                z-index: 9999;
                background: #ffffff;
                margin: 0;
                padding: 12px;
                width: auto;
                height: auto;
                overflow: auto;
            }}
            .memcat-card.fullscreen .iframe-wrapper iframe {{
                min-height: calc(100vh - 120px);
            }}
            .memcat-header {{
                display: flex;
                align-items: center;
                justify-content: space-between;
                gap: 12px;
                flex-wrap: wrap;
            }}
            .btn {{
                background: #4e73df;
                color: #fff;
                border: none;
                border-radius: 8px;
                padding: 8px 12px;
                cursor: pointer;
                font-weight: 600;
                box-shadow: 0 4px 12px rgba(78,115,223,0.2);
            }}
            .btn:hover {{
                background: #3b5fc7;
            }}
            .low-rate td:last-child {{
                color: #d9534f;
                font-weight: 700;
            }}
            pre {{
                white-space: pre-wrap;
                word-break: break-all;
                background: #f8fafc;
                border: 1px solid #e5e7eb;
                border-radius: 8px;
                padding: 12px;
                overflow-x: auto;
            }}
        </style>
    </head>
    <body>
        <div class="page">
            <h1>冷启动分析报告</h1>
            
            <div class="card">
                <h2>总体结果</h2>
                <table>
                    <thead>
                        <tr>
                            <th>应用名称</th>
                            <th>第1轮PID</th>
                            <th>第2轮PID</th>
                            <th>状态</th>
                        </tr>
                    </thead>
                    <tbody>
                        {"".join(table_rows)}
                    </tbody>
                </table>
            </div>

            <div class="card">
                <h2>关键指标</h2>
                <div class="stats-grid">
                    <div class="stat-pill">总计应用：{total_apps} 个</div>
                    <div class="stat-pill">冷启动率：{cold_rate:.1f}% ({cold_count} 个)</div>
                    <div class="stat-pill">热启动率：{100 - cold_rate:.1f}% ({total_apps - cold_count} 个)</div>
                    <div class="stat-pill">平均后台驻留：{background:.1f} 个</div>
                    <div class="stat-pill">驻留利用率：{utilization:.1f}%</div>
                    <div class="stat-pill">测试开始：{start_str}</div>
                    <div class="stat-pill">测试结束：{end_str}</div>
                    <div class="stat-pill">测试耗时：{duration_str}</div>
                </div>
            </div>

            <div class="card chart-container">
                <canvas id="residenceChart"></canvas>
            </div>

        <script>
            // 折线图配置
            const ctx = document.getElementById('residenceChart');
            new Chart(ctx, {{
                type: 'line',
                data: {{
                    labels: {chart_labels},
                    datasets: [{{
                        label: '后台进程数量变化',
                        data: {chart_data},
                        borderColor: '#4e73df',
                        backgroundColor: 'rgba(78, 115, 223, 0.05)',
                        borderWidth: 2,
                        pointRadius: 3,
                        tension: 0.1
                    }}]
                }},
                options: {{
                    responsive: true,
                    plugins: {{
                        title: {{
                            display: true,
                            text: '后台进程驻留趋势'
                        }}
                    }},
                    scales: {{
                        y: {{
                            title: {{ display: true, text: '进程数量' }},
                            beginAtZero: true,
                            grace: 5
                        }},
                        x: {{
                            title: {{ display: true, text: '检测轮次' }}
                        }}
                    }}
                }}
            }});
        </script>
            {residency_section}
            {summary_section}
            {oomadj_section}
            {kill_section}
            {memcat_section}
            {ftrace_section}
        </div>
    </body>
    </html>
    """
    now = datetime.now()
    _ = now.strftime("%d_%H_%M")  # 保留时间计算兼容现有调用
    output_file = os.path.join(state.FILE_DIR, "冷启动分析报告.html")
    with open(output_file, "w", encoding="utf-8") as f:
        f.write(html_content)


def generate_residency_only_html_report(
    package_count: int,
    rounds: int,
    alive_history: List[int],
    residency_records: Optional[List[LaunchResidencyRecord]] = None,
    residency_summary: Optional[Dict[int, Dict[str, object]]] = None,
    oomadj_summary: Optional[str] = None,
    kill_summary: Optional[str] = None,
    ftrace_summary: Optional[str] = None,
    memcat_html: Optional[str] = None,
    start_time: Optional[datetime] = None,
    end_time: Optional[datetime] = None,
):
    """生成仅包含驻留信息的 HTML 报告。"""
    _write_offline_chart_js(state.FILE_DIR or os.getcwd())
    chart_labels = list(range(1, len(alive_history) + 1)) if alive_history else []
    chart_data = alive_history if alive_history else []

    residency_rows: List[str] = []
    if residency_records:
        for record in residency_records:
            alive_list = ", ".join(html.escape(name) for name in record.alive_before) or "-"

            def _cell(n: int) -> str:
                detail = record.prev_alive_stats.get(n, {}) if record.prev_alive_stats else {}
                checked = detail.get("checked", []) or []
                alive = detail.get("alive", []) or []
                rate = detail.get("rate")
                if not checked:
                    return "-"
                cell = f"{len(alive)}/{len(checked)}"
                if rate is not None:
                    cell += f" ({rate*100:.1f}%)"
                if alive:
                    cell += "<br><small>" + ", ".join(html.escape(a) for a in alive) + "</small>"
                return cell

            residency_rows.append(
                f"""
                <tr>
                    <td>{record.round_no}</td>
                    <td>{record.order_in_round}</td>
                    <td>{html.escape(record.package)}</td>
                    <td>{len(record.alive_before)}</td>
                    <td>{alive_list}</td>
                    <td>{_cell(1)}</td>
                    <td>{_cell(2)}</td>
                    <td>{_cell(3)}</td>
                    <td>{_cell(4)}</td>
                    <td>{_cell(5)}</td>
                </tr>
                """
            )

    summary_rows: List[str] = []
    if residency_summary:
        for n in range(1, 6):
            item = residency_summary.get(n, {})
            total = item.get("total") or 0
            alive = item.get("alive") or 0
            rate = item.get("rate")
            rate_str = f"{rate*100:.1f}%" if rate is not None else "N/A"
            rate_class = "low-rate" if rate is not None and rate < 1.0 else ""
            summary_rows.append(
                f"<tr class='{rate_class}'><td>前{n}</td><td>{alive}</td><td>{total}</td><td>{rate_str}</td></tr>"
            )

    residency_section = ""
    if residency_rows:
        residency_section = f"""
        <div class="residency-section">
            <h2>驻留明细</h2>
            <table class="residency-table">
                <thead>
                    <tr>
                        <th>轮次</th>
                        <th>序号</th>
                        <th>应用</th>
                        <th>启动前存活数</th>
                        <th>存活列表</th>
                        <th>前1</th>
                        <th>前2</th>
                        <th>前3</th>
                        <th>前4</th>
                        <th>前5</th>
                    </tr>
                </thead>
                <tbody>
                    {''.join(residency_rows)}
                </tbody>
            </table>
        </div>
        """

    summary_section = ""
    if summary_rows:
        summary_section = f"""
        <div class="residency-section">
            <h2>前序驻留率汇总</h2>
            <table class="residency-table">
                <thead>
                    <tr><th>窗口</th><th>存活</th><th>样本</th><th>驻留率</th></tr>
                </thead>
                <tbody>
                    {''.join(summary_rows)}
                </tbody>
            </table>
        </div>
        """

    oomadj_section = ""
    if oomadj_summary:
        oomadj_section = f"""
        <div class="residency-section card">
            <h2>驻留(OOMAdj)解析概要</h2>
            <pre>{html.escape(oomadj_summary)}</pre>
        </div>
        """

    kill_section = ""
    if kill_summary:
        kill_section = f"""
        <div class="residency-section card">
            <h2>查杀解析概要</h2>
            <pre>{html.escape(kill_summary)}</pre>
        </div>
        """

    ftrace_section = ""
    if ftrace_summary:
        ftrace_section = f"""
        <div class="residency-section card">
            <h2>ftrace Global Stats</h2>
            <pre>{html.escape(ftrace_summary)}</pre>
        </div>
        """

    memcat_section = ""
    if memcat_html:
        safe_path = html.escape(memcat_html)
        memcat_section = f"""
        <div class="residency-section card memcat-card">
            <div class="memcat-header">
                <h2>Memcat 内存视图</h2>
                <button class="btn" id="memcatToggle">全屏/退出全屏</button>
            </div>
            <div class="iframe-wrapper">
                <iframe src="{safe_path}" title="memcat-report" loading="lazy"></iframe>
                <div class="iframe-note">如果 iframe 未加载，可直接打开文件: {safe_path}</div>
            </div>
        </div>
        <script>
            const memcatCard = document.querySelector('.memcat-card');
            const memcatToggle = document.getElementById('memcatToggle');
            if (memcatCard && memcatToggle) {{
                memcatToggle.addEventListener('click', () => {{
                    memcatCard.classList.toggle('fullscreen');
                }});
            }}
        </script>
        """

    start_str = start_time.strftime("%Y-%m-%d %H:%M:%S") if start_time else "-"
    end_str = end_time.strftime("%Y-%m-%d %H:%M:%S") if end_time else "-"
    duration_str = str(end_time - start_time) if start_time and end_time else "-"

    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <title>驻留测试报告</title>
        <script src="chart.min.js"></script>
        <style>
            body {{
                font-family: "Helvetica Neue", Arial, sans-serif;
                margin: 0;
                padding: 0;
                background: linear-gradient(135deg, #f5f7fa 0%, #e8ecf3 100%);
                color: #1f2933;
            }}
            .page {{
                max-width: 1100px;
                margin: 0 auto;
                padding: 32px 24px 64px;
            }}
            h1, h2 {{
                text-align: center;
                letter-spacing: 0.02em;
            }}
            .card {{
                background: #ffffff;
                border: 1px solid #e5e7eb;
                border-radius: 12px;
                padding: 20px;
                margin: 20px auto;
                box-shadow: 0 10px 30px rgba(15, 23, 42, 0.08);
            }}
            .stats-grid {{
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
                gap: 12px;
                text-align: center;
            }}
            .stat-pill {{
                background: #f8fafc;
                border: 1px solid #e5e7eb;
                border-radius: 10px;
                padding: 12px;
                font-weight: 600;
            }}
            table {{ 
                border-collapse: collapse; 
                width: 100%; 
                margin: 12px 0;
                box-shadow: 0 1px 4px rgba(0,0,0,0.08);
            }}
            th, td {{
                border: 1px solid #e5e7eb;
                padding: 12px;
                text-align: center;
            }}
            th {{ 
                background: #f3f4f6; 
                font-weight: 600; 
                color: #111827;
            }}
            tr:nth-child(even) td {{ background: #fbfdff; }}
            .chart-container {{ 
                max-width: 900px;
                margin: 32px auto;
            }}
            .residency-table td, .residency-table th {{
                font-size: 12px;
            }}
            .residency-section {{
                margin-top: 32px;
            }}
            .iframe-wrapper {{
                width: 100%;
                min-height: 520px;
            }}
            .iframe-wrapper iframe {{
                width: 100%;
                min-height: 500px;
                border: 1px solid #e5e7eb;
                border-radius: 12px;
                background: #fff;
            }}
            .iframe-note {{
                margin-top: 6px;
                font-size: 12px;
                color: #6b7280;
            }}
            .memcat-card.fullscreen {{
                position: fixed;
                inset: 12px;
                z-index: 9999;
                background: #ffffff;
                margin: 0;
                padding: 12px;
                width: auto;
                height: auto;
                overflow: auto;
            }}
            .memcat-card.fullscreen .iframe-wrapper iframe {{
                min-height: calc(100vh - 120px);
            }}
            .memcat-header {{
                display: flex;
                align-items: center;
                justify-content: space-between;
                gap: 12px;
                flex-wrap: wrap;
            }}
            .btn {{
                background: #4e73df;
                color: #fff;
                border: none;
                border-radius: 8px;
                padding: 8px 12px;
                cursor: pointer;
                font-weight: 600;
                box-shadow: 0 4px 12px rgba(78,115,223,0.2);
            }}
            .btn:hover {{
                background: #3b5fc7;
            }}
            .low-rate td:last-child {{
                color: #d9534f;
                font-weight: 700;
            }}
            pre {{
                white-space: pre-wrap;
                word-break: break-all;
                background: #f8fafc;
                border: 1px solid #e5e7eb;
                border-radius: 8px;
                padding: 12px;
                overflow-x: auto;
            }}
        </style>
    </head>
    <body>
        <div class="page">
            <h1>驻留测试报告</h1>
            <div class="card">
                <h2>关键指标</h2>
                <div class="stats-grid">
                    <div class="stat-pill">覆盖应用：{package_count} 个</div>
                    <div class="stat-pill">执行轮次：{rounds} 轮</div>
                    <div class="stat-pill">测试开始：{start_str}</div>
                    <div class="stat-pill">测试结束：{end_str}</div>
                    <div class="stat-pill">测试耗时：{duration_str}</div>
                </div>
            </div>

            <div class="card chart-container">
                <canvas id="residenceChart"></canvas>
            </div>

            {residency_section}
            {summary_section}
            {oomadj_section}
            {kill_section}
            {memcat_section}
            {ftrace_section}
        </div>
        <script>
            const ctx = document.getElementById('residenceChart');
            new Chart(ctx, {{
                type: 'line',
                data: {{
                    labels: {chart_labels},
                    datasets: [{{
                        label: '后台进程数量变化',
                        data: {chart_data},
                        borderColor: '#4e73df',
                        backgroundColor: 'rgba(78, 115, 223, 0.05)',
                        borderWidth: 2,
                        pointRadius: 3,
                        tension: 0.1
                    }}]
                }},
                options: {{
                    responsive: true,
                    plugins: {{
                        title: {{
                            display: true,
                            text: '后台进程驻留趋势'
                        }}
                    }},
                    scales: {{
                        y: {{
                            title: {{ display: true, text: '进程数量' }},
                            beginAtZero: true,
                            grace: 5
                        }},
                        x: {{
                            title: {{ display: true, text: '检测轮次' }}
                        }}
                    }}
                }}
            }});
        </script>
    </body>
    </html>
    """
    output_file = os.path.join(state.FILE_DIR, "驻留测试报告.html")
    with open(output_file, "w", encoding="utf-8") as f:
        f.write(html_content)
