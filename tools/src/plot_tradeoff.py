#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
import re
import struct
import zlib
from pathlib import Path


def fallback_png(path: Path, points: list[dict[str, object]]) -> None:
    try:
        from PIL import Image, ImageDraw, ImageFont
        memories = list(dict.fromkeys(str(point["memory_condition"]) for point in points)) or ["no-data"]
        panel_width, height = 620, 680; width = panel_width * len(memories)
        canvas = Image.new("RGB", (width, height), "white"); draw = ImageDraw.Draw(canvas); font = ImageFont.load_default()
        y_values = [float(point[key]) for point in points for key in ("first_query_improvement_p25", "first_query_improvement_p75")]
        ymin, ymax = (min(y_values), max(y_values)) if y_values else (0.0, 1.0)
        padding = max(.5, (ymax-ymin)*.08); ymin -= padding; ymax += padding
        colors = [(31,119,180),(255,127,14),(44,160,44),(214,39,40)]
        for panel, memory in enumerate(memories):
            subset = [point for point in points if str(point["memory_condition"]) == memory]
            left, right, top, bottom = panel*panel_width+70, (panel+1)*panel_width-30, 55, 535
            x_values = [float(point[key]) for point in subset for key in ("prefetch_p25_us", "prefetch_p75_us") if float(point[key]) > 0]
            xmin, xmax = (min(x_values), max(x_values)) if x_values else (1.0, 10.0)
            if xmin == xmax: xmin, xmax = xmin/1.2, xmax*1.2
            lxmin, lxmax = math.log10(xmin), math.log10(xmax)
            xp = lambda value: left + int((math.log10(max(float(value), xmin))-lxmin)/(lxmax-lxmin)*(right-left))
            yp = lambda value: bottom-int((float(value)-ymin)/(ymax-ymin)*(bottom-top))
            draw.line((left,bottom,right,bottom),fill=(70,70,70),width=1); draw.line((left,top,left,bottom),fill=(70,70,70),width=1)
            for tick in range(5):
                y=ymin+(ymax-ymin)*tick/4; py=yp(y); draw.line((left,py,right,py),fill=(225,225,225)); draw.text((left-48,py-6),f"{y:.1f}",fill=(30,30,30),font=font)
                x=10**(lxmin+(lxmax-lxmin)*tick/4); px=xp(x); draw.line((px,top,px,bottom),fill=(235,235,235)); draw.text((px-18,bottom+8),f"{x:.0f}",fill=(30,30,30),font=font)
            draw.text((left+(right-left)//2-55,20),f"Trade-off: {memory}",fill=(0,0,0),font=font)
            draw.text((left+(right-left)//2-70,bottom+28),"Prefetch elapsed (us, log)",fill=(0,0,0),font=font)
            draw.text((left,top-20),"First-query improvement (%)",fill=(0,0,0),font=font)
            color=colors[panel%len(colors)]; labels=[]
            for index, point in enumerate(subset):
                x,y=xp(point["prefetch_median_us"]),yp(point["first_query_improvement_median"]); x0,x1=xp(point["prefetch_p25_us"]),xp(point["prefetch_p75_us"]); y0,y1=yp(point["first_query_improvement_p25"]),yp(point["first_query_improvement_p75"])
                draw.line((x0,y,x1,y),fill=color,width=2); draw.line((x0,y-3,x0,y+3),fill=color,width=2); draw.line((x1,y-3,x1,y+3),fill=color,width=2)
                draw.line((x,y0,x,y1),fill=color,width=2); draw.line((x-3,y0,x+3,y0),fill=color,width=2); draw.line((x-3,y1,x+3,y1),fill=color,width=2)
                marker=index%4
                if marker==0: draw.ellipse((x-5,y-5,x+5,y+5),fill=color)
                elif marker==1: draw.rectangle((x-5,y-5,x+5,y+5),fill=color)
                elif marker==2: draw.polygon([(x,y-6),(x-6,y+5),(x+6,y+5)],fill=color)
                else: draw.polygon([(x,y-6),(x-6,y),(x,y+6),(x+6,y)],fill=color)
                strategy=str(point["strategy_key"]); strategy=re.sub(r"residency_topk_.+_i(\d+)_l(\d+)",r"resident-i\1l\2",strategy).replace("offset_topk_interior_n","offset-n").replace("range_interior","range")
                labels.append(f"{point['workload_type']}:{point['layout']}:{strategy}")
            for index,label in enumerate(labels):
                draw.text((left,bottom+55+index*14),label,fill=color,font=font)
        canvas.save(path,format="PNG")
        return
    except ImportError:
        pass
    width, height, margin = 900, 600, 70
    pixels = bytearray([255] * width * height * 3)
    def pixel(x: int, y: int, color=(20, 70, 150)) -> None:
        if 0 <= x < width and 0 <= y < height:
            at = (y * width + x) * 3; pixels[at:at+3] = bytes(color)
    def line(x0: int, y0: int, x1: int, y1: int, color=(80, 80, 80)) -> None:
        dx, sx, dy, sy, err = abs(x1-x0), (1 if x0 < x1 else -1), -abs(y1-y0), (1 if y0 < y1 else -1), abs(x1-x0)-abs(y1-y0)
        while True:
            pixel(x0, y0, color)
            if x0 == x1 and y0 == y1: break
            e2 = 2 * err
            if e2 >= dy: err += dy; x0 += sx
            if e2 <= dx: err += dx; y0 += sy
    line(margin, height-margin, width-margin, height-margin); line(margin, margin, margin, height-margin)
    if points:
        xs = [float(p["prefetch_p75_us"]) for p in points] + [float(p["prefetch_p25_us"]) for p in points]
        ys = [float(p["first_query_improvement_p75"]) for p in points] + [float(p["first_query_improvement_p25"]) for p in points] + [0.0]
        xmin, xmax, ymin, ymax = min(xs), max(xs), min(ys), max(ys)
        if xmin == xmax: xmin, xmax = xmin-1, xmax+1
        if ymin == ymax: ymin, ymax = ymin-1, ymax+1
        xp = lambda value: margin + int((value-xmin)/(xmax-xmin)*(width-2*margin))
        yp = lambda value: height-margin-int((value-ymin)/(ymax-ymin)*(height-2*margin))
        line(margin, yp(0), width-margin, yp(0), (180, 180, 180))
        for point in points:
            x, y = xp(float(point["prefetch_median_us"])), yp(float(point["first_query_improvement_median"]))
            x0, x1 = xp(float(point["prefetch_p25_us"])), xp(float(point["prefetch_p75_us"]))
            y0, y1 = yp(float(point["first_query_improvement_p25"])), yp(float(point["first_query_improvement_p75"]))
            line(x0, y, x1, y); line(x, y0, x, y1)
            for dx in range(-4, 5):
                for dy in range(-4, 5):
                    if dx*dx + dy*dy <= 16: pixel(x+dx, y+dy)
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)
    scanlines = b"".join(b"\0" + bytes(pixels[y*width*3:(y+1)*width*3]) for y in range(height))
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(scanlines, 9)) + chunk(b"IEND", b""))


def render_plot(path: Path, backend: str, points: list[dict[str, object]]) -> None:
    try:
        import matplotlib.pyplot as plt
    except ImportError:
        fallback_png(path, points)
        return
    memories = list(dict.fromkeys(str(point["memory_condition"]) for point in points))
    shapes = list(dict.fromkeys((str(point["layout"]), str(point["strategy_key"])) for point in points))
    colors = {name: plt.get_cmap("tab10")(index % 10) for index, name in enumerate(memories)}
    markers = ["o", "s", "^", "D", "v", "P", "X", "<", ">", "*"]
    shape_markers = {shape: markers[index % len(markers)] for index, shape in enumerate(shapes)}
    if not memories: memories = ["no-data"]
    figure, axes = plt.subplots(1, len(memories), figsize=(max(9, 6 * len(memories)), 6), squeeze=False, sharey=True)
    y_bounds = [float(point[key]) for point in points for key in ("first_query_improvement_p25", "first_query_improvement_p75")]
    if y_bounds:
        ymin, ymax = min(y_bounds), max(y_bounds); padding = max(0.5, (ymax - ymin) * .08)
        y_limits = (ymin - padding, ymax + padding)
    else: y_limits = None
    def short_strategy(value: object) -> str:
        text = str(value).replace("offset_topk_interior_n", "offset-n").replace("range_interior", "range")
        match = re.fullmatch(r"residency_topk_.+_i(\d+)_l(\d+)", text)
        return f"resident-i{match.group(1)}l{match.group(2)}" if match else text
    for index, memory in enumerate(memories):
        axis = axes[0][index]; memory_points = [point for point in points if str(point["memory_condition"]) == memory]
        for point in memory_points:
            x, y = float(point["prefetch_median_us"]), float(point["first_query_improvement_median"])
            shape = (str(point["layout"]), str(point["strategy_key"]))
            label = f"{point['workload_type']}:{point['layout']}:{short_strategy(point['strategy_key'])}"
            axis.errorbar(x, y, xerr=[[x-float(point["prefetch_p25_us"])], [float(point["prefetch_p75_us"])-x]], yerr=[[y-float(point["first_query_improvement_p25"])], [float(point["first_query_improvement_p75"])-y]], linestyle="none", marker=shape_markers[shape], color=colors[memory], capsize=3, alpha=.85, label=label)
        if memory_points and all(float(point["prefetch_p25_us"]) > 0 for point in memory_points): axis.set_xscale("log")
        if y_limits:
            axis.set_ylim(*y_limits)
            if y_limits[0] <= 0 <= y_limits[1]: axis.axhline(0, color="grey", linewidth=.8)
        axis.set_title(f"{backend} / {memory}")
        axis.set_xlabel("Prefetch elapsed (us, log scale)")
        axis.grid(alpha=.25, which="both")
    axes[0][0].set_ylabel("First-query improvement (%)")
    if points:
        handles, labels = axes[0][0].get_legend_handles_labels()
        figure.legend(handles, labels, loc="lower center", ncol=min(3, max(1, len(labels))), fontsize=7)
    figure.tight_layout(rect=(0, .12 if points else 0, 1, 1))
    figure.savefig(path, dpi=160)
    plt.close(figure)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--experiment-dir", required=True, type=Path)
    args = parser.parse_args()
    with (args.experiment_dir / "config.json").open(encoding="utf-8") as handle:
        backends = json.load(handle)["prefetch"]["backends"]
    comparison_files = sorted((args.experiment_dir / "results").glob("*/*/memory_conditions/*/backend_comparison.csv"))
    if not comparison_files:
        parser.error("no backend_comparison.csv files found")
    rows = []
    for path in comparison_files:
        workload_type, layout, _, memory_condition, _ = path.relative_to(args.experiment_dir / "results").parts
        with path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                row.update({"_workload_type": workload_type, "_layout": layout, "_memory_condition": memory_condition})
                rows.append(row)
    grouped: dict[tuple[str, ...], dict[str, dict[str, str]]] = {}
    keys = ("_workload_type", "_layout", "_memory_condition", "backend", "strategy_key")
    for row in rows:
        if row["strategy_key"] != "baseline":
            grouped.setdefault(tuple(row[k] for k in keys), {})[row["metric"]] = row
    points = []
    for identity, metrics in grouped.items():
        x, y = metrics.get("prefetch_elapsed_us"), metrics.get("first_query_improvement_percent")
        if not x or not y: continue
        point = {"workload_type": identity[0], "layout": identity[1], "memory_condition": identity[2], "backend": identity[3], "strategy_key": identity[4], "prefetch_median_us": x["median"], "prefetch_p25_us": x["p25"], "prefetch_p75_us": x["p75"], "first_query_improvement_median": y["median"], "first_query_improvement_p25": y["p25"], "first_query_improvement_p75": y["p75"]}; points.append(point)
    output_dir = args.experiment_dir / "plots"
    output_dir.mkdir(parents=True, exist_ok=True)
    fields = ["workload_type", "layout", "memory_condition", "strategy_key", "backend", "prefetch_median_us", "prefetch_p25_us", "prefetch_p75_us", "first_query_improvement_median", "first_query_improvement_p25", "first_query_improvement_p75"]
    with (output_dir / "tradeoff_points.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields); writer.writeheader(); writer.writerows(points)
    for backend in backends:
        render_plot(output_dir / f"tradeoff_{backend}.png", backend, [point for point in points if point["backend"] == backend])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
