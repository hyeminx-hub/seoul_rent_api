import argparse
import csv
import gzip
import html
import json
import math
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Iterable

CONVERSION_RATE = 0.05


def _missing(value: object) -> bool:
    return value is None or not str(value).strip()


def _quantile(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower, upper = math.floor(position), math.ceil(position)
    if lower == upper:
        return round(ordered[lower], 2)
    return round(ordered[lower] * (upper - position) + ordered[upper] * (position - lower), 2)


def _eligible_contract(row: dict[str, str]):
    if row.get("RENT_SE", "").strip() not in {"전세", "월세"}:
        return None
    try:
        date = datetime.strptime(row.get("CTRT_DAY", ""), "%Y%m%d")
        area = float(row.get("RENT_AREA", ""))
        deposit = float(row.get("GRFE", ""))
        monthly_rent_raw = row.get("RTFE", "").strip()
        if row.get("RENT_SE", "").strip() == "월세" and not monthly_rent_raw:
            return None
        monthly_rent = float(monthly_rent_raw) if row.get("RENT_SE", "").strip() == "월세" else 0.0
    except (ValueError, TypeError):
        return None
    if not 2021 <= date.year <= 2025 or not math.isfinite(area) or area <= 0:
        return None
    if not math.isfinite(deposit) or deposit < 0 or not math.isfinite(monthly_rent) or monthly_rent < 0:
        return None
    jeonse_equivalent = deposit + monthly_rent * 12 / CONVERSION_RATE
    return date, area, jeonse_equivalent


def summarize_monthly(rows: Iterable[dict[str, str]]) -> list[dict[str, object]]:
    groups: dict[tuple[str, str, str], list[float]] = defaultdict(list)
    for row in rows:
        contract = _eligible_contract(row)
        if contract is None:
            continue
        date, area, deposit = contract
        district, usage = row.get("CGG_NM", "").strip(), row.get("BLDG_USG", "").strip()
        if not district or not usage:
            continue
        groups[(district, usage, date.strftime("%Y-%m"))].append(deposit / area)
    return [
        {"district": key[0], "building_use": key[1], "month": key[2], "count": len(values),
         "minimum": round(min(values), 2), "median": _quantile(values, .5), "maximum": round(max(values), 2)}
        for key, values in sorted(groups.items())
    ]


def summarize_annual_medians(rows: Iterable[dict[str, str]]) -> list[dict[str, object]]:
    groups: dict[tuple[int, str, str], list[float]] = defaultdict(list)
    for row in rows:
        contract = _eligible_contract(row)
        if contract is None:
            continue
        date, area, deposit = contract
        district, usage = row.get("CGG_NM", "").strip(), row.get("BLDG_USG", "").strip()
        if not district or not usage:
            continue
        price = deposit / area
        if math.isfinite(price):
            groups[(date.year, district, usage)].append(price)
    return [
        {"year": key[0], "district": key[1], "building_use": key[2], "count": len(values),
         "minimum": round(min(values), 2), "median": _quantile(values, .5), "maximum": round(max(values), 2)}
        for key, values in sorted(groups.items())
    ]


def summarize_missing(rows: Iterable[dict[str, str]]) -> dict[str, object]:
    total = 0
    missing_counts = {"MNO": 0, "SNO": 0}
    both_missing = 0
    reasons: dict[tuple[str, str, str], int] = defaultdict(int)
    for row in rows:
        if _eligible_contract(row) is None:
            continue
        total += 1
        missing_mno, missing_sno = _missing(row.get("MNO")), _missing(row.get("SNO"))
        missing_counts["MNO"] += missing_mno
        missing_counts["SNO"] += missing_sno
        both_missing += missing_mno and missing_sno
        if missing_mno or missing_sno:
            key = (row.get("LOTNO_SE_NM", "").strip() or "(결측)",
                   row.get("BLDG_USG", "").strip() or "(결측)",
                   "MNO·SNO 모두" if missing_mno and missing_sno else "일부 지번")
            reasons[key] += 1
    return {
        "rows": total,
        "MNO": {"missing": missing_counts["MNO"], "rate": missing_counts["MNO"] / total if total else 0},
        "SNO": {"missing": missing_counts["SNO"], "rate": missing_counts["SNO"] / total if total else 0},
        "both_missing": both_missing,
        "reasons": [{"lotno_se_nm": k[0], "building_use": k[1], "missing": k[2], "count": n}
                    for k, n in sorted(reasons.items(), key=lambda item: (-item[1], item[0]))],
    }


def read_rows(path: Path):
    with gzip.open(path, "rt", encoding="utf-8", newline="") as stream:
        yield from csv.DictReader(stream)


def build_map_svg(geojson: dict[str, object], width: int = 360, height: int = 520) -> str:
    features = geojson.get("features", [])
    coords: list[tuple[float, float]] = []

    def rings_for(geometry: dict[str, object]):
        if geometry.get("type") == "Polygon":
            return geometry.get("coordinates", [])
        if geometry.get("type") == "MultiPolygon":
            return [ring for polygon in geometry.get("coordinates", []) for ring in polygon]
        return []

    for feature in features:
        for ring in rings_for(feature.get("geometry", {})):
            coords.extend((float(point[0]), float(point[1])) for point in ring)
    if not coords:
        return '<svg id="districtMap" role="img" aria-label="서울 자치구 지도"></svg>'

    # A local equirectangular projection keeps the city's north-south proportions.
    lat0 = sum(lat for _, lat in coords) / len(coords)
    cos_lat = math.cos(math.radians(lat0))
    projected = [(lon * cos_lat, lat) for lon, lat in coords]
    min_x, max_x = min(x for x, _ in projected), max(x for x, _ in projected)
    min_y, max_y = min(y for _, y in projected), max(y for _, y in projected)
    pad = 12
    scale = min((width - pad * 2) / (max_x - min_x), (height - pad * 2) / (max_y - min_y))
    drawn_w, drawn_h = (max_x - min_x) * scale, (max_y - min_y) * scale
    offset_x, offset_y = (width - drawn_w) / 2, (height - drawn_h) / 2

    def xy(point):
        px, py = float(point[0]) * cos_lat, float(point[1])
        return offset_x + (px - min_x) * scale, height - offset_y - (py - min_y) * scale

    elements = []
    for idx, feature in enumerate(features):
        props = feature.get("properties", {})
        name = str(props.get("name") or props.get("SIG_KOR_NM") or "")
        code = str(props.get("SIG_CD") or feature.get("id") or idx)
        ring_sets = rings_for(feature.get("geometry", {}))
        path_parts = []
        outer_points = []
        for ring_idx, ring in enumerate(ring_sets):
            if not ring:
                continue
            points = [xy(point) for point in ring]
            if ring_idx == 0:
                outer_points.extend(points)
            path_parts.append("M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in points) + " Z")
        if not path_parts:
            continue
        cx = sum(x for x, _ in outer_points) / len(outer_points)
        cy = sum(y for _, y in outer_points) / len(outer_points)
        safe_name = html.escape(name, quote=True)
        elements.append(
            f'<path id="gu-{html.escape(code, quote=True)}" class="district-shape" data-district="{safe_name}" '
            f'd="{" ".join(path_parts)}"><title>{safe_name}</title></path>'
            f'<text class="district-label" data-label-for="{safe_name}" x="{cx:.1f}" y="{cy:.1f}">{safe_name}</text>'
        )
    return (f'<svg id="districtMap" viewBox="0 0 {width} {height}" role="img" '
            f'aria-label="서울 자치구별 전세환산 보증금 중앙값 지도">'
            '<title>서울시 자치구 경계 · 출처: 서울 열린데이터광장, 2025년 기준</title>'
            + "".join(elements) + "</svg>")


def build_html(monthly: list[dict[str, object]], annual: list[dict[str, object]], missing: dict[str, object], input_path: Path, map_svg: str = "") -> str:
    payload = json.dumps(monthly, ensure_ascii=False, separators=(",", ":"))
    annual_payload = json.dumps(annual, ensure_ascii=False, separators=(",", ":"))
    missing_rows = "".join(
        f"<tr><td>{html.escape(str(row['lotno_se_nm']))}</td><td>{html.escape(str(row['building_use']))}</td>"
        f"<td>{html.escape(str(row['missing']))}</td><td>{row['count']:,}</td></tr>"
        for row in missing["reasons"][:30]
    )
    pct = lambda field: f"{missing[field]['rate']:.1%}"
    return f'''<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>서울 전세 ㎡당 보증금 분석</title><style>
:root{{--ink:#1d2939;--muted:#667085;--line:#e4e7ec;--blue:#2563eb}}*{{box-sizing:border-box}}body{{font-family:system-ui,-apple-system,sans-serif;margin:0;background:#f4f6fa;color:var(--ink)}}main{{max-width:1180px;margin:auto;padding:30px 20px}}section{{background:#fff;border:1px solid var(--line);border-radius:14px;padding:20px;margin:16px 0}}h1{{margin:0 0 5px}}h2{{font-size:20px;margin:0 0 12px}}.muted{{color:var(--muted)}}.controls{{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin:2px 0 10px}}select{{padding:8px 10px;margin:4px;border:1px solid #d0d5dd;border-radius:8px;background:#fff}}.chart-wrap{{position:relative}}canvas{{display:block;width:100%;height:380px}}#barChart{{height:510px}}#districtTrend{{height:600px}}.chart-tip{{position:absolute;display:none;z-index:2;background:#fff;border:1px solid #cbd5e1;border-radius:9px;padding:9px 11px;box-shadow:0 5px 18px #10182826;font-size:13px;pointer-events:none;white-space:nowrap}}.bar-map-grid{{display:grid;grid-template-columns:minmax(0,1fr) 300px;gap:18px;align-items:start}}.map-panel{{border:1px solid #eaecf0;border-radius:12px;padding:12px;background:#fcfcfd}}.map-panel h3{{margin:0;font-size:15px}}#districtMap{{display:block;width:100%;height:auto;max-height:490px}}.district-shape{{fill:#f8f8f8;stroke:#fff;stroke-width:1.15;vector-effect:non-scaling-stroke;cursor:pointer}}.district-shape:hover{{stroke:#1d2939;stroke-width:2}}.district-label{{font:9px system-ui,sans-serif;fill:#344054;text-anchor:middle;dominant-baseline:middle;pointer-events:none}}.map-legend{{height:10px;border-radius:6px;background:linear-gradient(90deg,#fff,#f00);border:1px solid #eaecf0;margin:7px 0 3px}}.map-legend-labels{{display:flex;justify-content:space-between;font-size:11px;color:var(--muted)}}#mapTip{{min-height:20px;margin-top:7px;font-size:12px;color:#344054}}.metric-boxes{{display:flex;gap:8px;flex-wrap:wrap;margin:12px 0 2px}}.metric-box{{background:#f8fafc;border:1px solid #e2e8f0;border-radius:9px;padding:9px 12px;min-width:138px}}.metric-box small{{display:block;color:var(--muted);font-size:11px;margin-bottom:2px}}.metric-box strong{{font-size:14px;font-variant-numeric:tabular-nums}}.chart-note{{background:#f8fafc;border:1px solid #e4e7ec;border-left:5px solid #d7191c;border-radius:10px;padding:12px 15px;margin-top:14px;line-height:1.5;color:#344054}}.chart-note strong{{display:block;margin-bottom:2px;color:#b2182b}}.district-colors{{position:relative}}.year-legend{{display:flex;gap:14px;flex-wrap:wrap;align-items:center;color:#475467;font-size:13px;margin:6px 0 2px}}.year-key{{display:inline-flex;align-items:center;gap:6px}}.year-swatch{{width:11px;height:11px;border-radius:50%;display:inline-block}}.scroll{{overflow:auto}}table{{width:100%;border-collapse:collapse;font-size:14px}}th,td{{text-align:left;padding:9px;border-bottom:1px solid #eaecf0}}.cards{{display:flex;gap:12px;flex-wrap:wrap}}.card{{background:#f8fafc;padding:14px 18px;border-radius:10px;min-width:150px}}@media(max-width:760px){{main{{padding:16px 10px}}section{{padding:14px}}canvas{{height:330px}}#barChart{{height:600px}}#districtTrend{{height:650px}}.bar-map-grid{{grid-template-columns:1fr}}#districtMap{{max-height:390px;max-width:340px;margin:auto}}.metric-box{{min-width:calc(50% - 5px);flex:1}}}}
</style></head>
<body><main><h1>서울 자치구·건물용도별 전세환산 보증금</h1><div class="muted">2021–2025 전세·월세 계약 · 월세는 연 5.0% 전환율로 전세보증금 환산 · 가격 단위: 만원/㎡</div>
<section><h2>월별 전세환산 보증금 추이와 가격 범위</h2><div class="controls"><label>자치구 <select id="district"></select></label><label>건물용도 <select id="usage"></select></label></div><div class="chart-wrap"><canvas id="chart" aria-label="월별 전세환산 보증금 추이, 단위 만원/제곱미터"></canvas><div id="monthTip" class="chart-tip"></div></div><div id="metricBoxes" class="metric-boxes" aria-live="polite"><div class="metric-box"><small>월</small><strong>점에 마우스를 올리세요</strong></div></div><p id="summary" class="muted"></p><div class="chart-note"><strong>그래프 읽기 · 단위: 만원/㎡</strong>선과 점은 월별 중앙값이고, 음영은 실제 최솟값~최댓값입니다. 점에 마우스를 올리면 연·월, 중앙값, 최솟값~최댓값과 거래 건수를 각각 확인할 수 있습니다. 세로 실선은 가리키는 월을 나타내며 가로축은 연도만 표시합니다.</div></section>
<section><h2>자치구별 전세환산 보증금 중앙값</h2><div class="controls"><label>연도 <select id="barYear"></select></label><label>건물용도 <select id="barUsage"></select></label><span class="muted">단위: 만원/㎡</span></div><div class="bar-map-grid"><div class="bar-view"><div class="chart-wrap"><canvas id="barChart" aria-label="자치구별 전세환산 보증금 중앙값 막대그래프, 단위 만원/제곱미터"></canvas><div id="barTip" class="chart-tip"></div></div></div><aside class="map-panel"><h3>자치구 지도 · 단위: 만원/㎡</h3>{map_svg}<div id="mapLegend" class="map-legend" aria-label="낮은 값은 연한 빨강, 높은 값은 진한 빨강"></div><div class="map-legend-labels"><span id="mapLow">낮음</span><span id="mapHigh">높음</span></div><div id="mapTip" aria-live="polite">자치구에 마우스를 올리면 중앙값을 확인할 수 있습니다.</div><small class="muted">경계: <a href="https://data.seoul.go.kr/opendata/seoulStay/grid_viewer.html" target="_blank" rel="noopener">서울 열린데이터광장</a> 자치구 경계, 2025년 6월 기준</small></aside></div><div class="chart-note"><strong>그래프 읽기 · 단위: 만원/㎡</strong>막대와 지도는 같은 연도·건물용도 선택을 공유합니다. 막대는 중앙값 내림차순이며, 지도는 낮은 값이 연하고 높은 값이 진한 빨강입니다. 막대/자치구에 마우스를 올리면 해당 값과 최소·최대 범위를 확인할 수 있습니다.</div></section>
<section><h2>자치구별 연도별 전세환산 보증금 중앙값 추이</h2><div class="controls"><label>정렬 기준 연도 <select id="sortYear"></select></label><label>건물용도 <select id="trendUsage"></select></label><span class="muted">자치구: 정렬 연도 중앙값 내림차순 · 가로축 단위: 만원/㎡</span></div><div class="year-legend" id="yearLegend" aria-label="색상별 연도 범례"></div><div class="chart-wrap district-colors"><canvas id="districtTrend" aria-label="자치구별 연도별 전세환산 보증금 중앙값 연결 그래프, 가로축 단위 만원/제곱미터"></canvas><div id="trendTip" class="chart-tip"></div></div><div class="chart-note"><strong>그래프 읽기 · 가로축 단위: 만원/㎡</strong>자치구는 정렬 기준 연도의 중앙값이 높은 순서로 표시합니다. 점 색은 모든 자치구에서 같은 연도 규칙을 사용하고 범례로 구분하며, 연도 순서대로 얇은 선으로 연결합니다. 점에 마우스를 올리면 연도와 가격을 볼 수 있습니다.</div></section>
<section><h2>지번 필드 결측 현황</h2><div class="cards"><div class="card">분석 대상 전세환산 계약<br><b>{missing['rows']:,}</b></div><div class="card">본번(MNO) 결측<br><b>{missing['MNO']['missing']:,} ({pct('MNO')})</b></div><div class="card">부번(SNO) 결측<br><b>{missing['SNO']['missing']:,} ({pct('SNO')})</b></div><div class="card">둘 다 결측<br><b>{missing['both_missing']:,}</b></div></div><p>본번·부번 결측 행도 가격 집계에서 제외하지 않았습니다. 결측은 모두 동시 결측이며 대부분 단독다가구에서 나타났습니다. 지번 구분명도 비어 있어 원천자료의 공개 범위/기록 방식에 따른 패턴일 가능성이 있으나, 구체적인 원인은 이 자료만으로 확정할 수 없습니다.</p><div class="scroll"><table><thead><tr><th>지번 구분</th><th>건물용도</th><th>결측 형태</th><th>건수</th></tr></thead><tbody>{missing_rows}</tbody></table></div></section>
<section><h2>산출 기준</h2><p>전세·월세 계약을 포함했습니다. API 금액 단위(만원) 기준으로 전세환산 보증금은 보증금 + (월세 × 12 ÷ 0.05)로 계산하고, 임대면적(㎡)으로 나눴습니다. 따라서 월세 환산에는 연 5.0% 전환율을 적용했습니다. 유효 계약일·자치구·건물용도·양수 면적·숫자형 보증금이 있는 행을 사용했으며, 본번·부번 결측 여부는 필터 조건에 포함하지 않았습니다. 중복제거 파일을 입력으로 사용했습니다.</p><p class="muted">입력: {html.escape(input_path.name)}</p></section></main>
<script>
const MONTHLY={payload};const ANNUAL={annual_payload};const number=value=>Number(value).toLocaleString('ko-KR',{{maximumFractionDigits:2}});const fill=(select,values)=>select.replaceChildren(...values.map(value=>new Option(value,value)));const bwrColor=t=>{{t=Math.max(0,Math.min(1,t));const a=[0,0,255],b=[255,255,255],c=[255,0,0],v=t<.5?a.map((x,i)=>Math.round(x+(b[i]-x)*t*2)):b.map((x,i)=>Math.round(x+(c[i]-x)*(t-.5)*2));return `rgb(${{v.join(',')}})`}};
const district=document.querySelector('#district'),usage=document.querySelector('#usage'),monthCanvas=document.querySelector('#chart'),monthTip=document.querySelector('#monthTip'),metricBoxes=document.querySelector('#metricBoxes');let monthRows=[],hoverMonth=null;
const monthIndex=value=>{{const [year,month]=value.split('-').map(Number);return year*12+month-1}};
function usagesForDistrict(){{fill(usage,[...new Set(MONTHLY.filter(row=>row.district===district.value).map(row=>row.building_use))].sort());drawMonthly()}}
function monthlyGeometry(){{const width=monthCanvas.clientWidth,height=monthCanvas.clientHeight,pad={{left:66,right:18,top:18,bottom:42}},start=monthIndex(monthRows[0].month),end=monthIndex(monthRows[monthRows.length-1].month),low=Math.min(...monthRows.map(row=>row.minimum)),high=Math.max(...monthRows.map(row=>row.maximum)),range=high-low||1;return {{width,height,pad,start,end,low,high,x:value=>pad.left+(end===start?.5:(monthIndex(value)-start)/(end-start))*(width-pad.left-pad.right),y:value=>pad.top+(high-value)/range*(height-pad.top-pad.bottom)}}}}
function drawMonthly(){{monthRows=MONTHLY.filter(row=>row.district===district.value&&row.building_use===usage.value).sort((a,b)=>a.month.localeCompare(b.month));const ctx=monthCanvas.getContext('2d'),width=monthCanvas.clientWidth,height=monthCanvas.clientHeight,ratio=devicePixelRatio||1;monthCanvas.width=width*ratio;monthCanvas.height=height*ratio;ctx.setTransform(ratio,0,0,ratio,0,0);ctx.clearRect(0,0,width,height);if(!monthRows.length){{metricBoxes.replaceChildren();return}}if(hoverMonth&&!monthRows.some(row=>row.month===hoverMonth))hoverMonth=null;const g=monthlyGeometry(),{{pad,start,end,low,high,x,y}}=g;ctx.font='12px system-ui';ctx.strokeStyle='#eaecf0';ctx.fillStyle='#667085';ctx.lineWidth=1;
for(let year=Math.ceil(start/12);year<=Math.floor(end/12);year++){{const tick=`${{year}}-01`,tx=x(tick);ctx.beginPath();ctx.moveTo(tx,pad.top);ctx.lineTo(tx,height-pad.bottom);ctx.stroke();ctx.fillText(String(year),tx-13,height-14)}}for(let i=0;i<5;i++){{const value=low+(high-low)*i/4,ty=y(value);ctx.beginPath();ctx.moveTo(pad.left,ty);ctx.lineTo(width-pad.right,ty);ctx.stroke();ctx.fillText(number(value),4,ty+4)}}
ctx.fillStyle='rgba(0,0,255,.10)';ctx.beginPath();monthRows.forEach((row,i)=>i?ctx.lineTo(x(row.month),y(row.maximum)):ctx.moveTo(x(row.month),y(row.maximum)));for(let i=monthRows.length-1;i>=0;i--)ctx.lineTo(x(monthRows[i].month),y(monthRows[i].minimum));ctx.closePath();ctx.fill();ctx.strokeStyle=bwrColor(.9);ctx.lineWidth=1;ctx.beginPath();monthRows.forEach((row,i)=>i?ctx.lineTo(x(row.month),y(row.median)):ctx.moveTo(x(row.month),y(row.median)));ctx.stroke();monthRows.forEach(row=>{{ctx.beginPath();ctx.arc(x(row.month),y(row.median),3,0,Math.PI*2);ctx.fillStyle=bwrColor(.9);ctx.fill()}});if(hoverMonth){{const row=monthRows.find(item=>item.month===hoverMonth),tx=x(row.month);ctx.strokeStyle='#667085';ctx.lineWidth=1;ctx.beginPath();ctx.moveTo(tx,pad.top);ctx.lineTo(tx,height-pad.bottom);ctx.stroke();ctx.beginPath();ctx.arc(tx,y(row.median),3,0,Math.PI*2);ctx.fillStyle=bwrColor(.9);ctx.fill();monthTip.style.display='block';monthTip.style.left=Math.min(width-190,Math.max(pad.left,tx+10))+'px';monthTip.style.top=Math.max(0,y(row.median)-18)+'px';monthTip.textContent=`${{row.month}} · ${{number(row.median)}}만원/㎡`;metricBoxes.innerHTML=`<div class="metric-box"><small>연·월</small><strong>${{row.month}}</strong></div><div class="metric-box"><small>중앙값</small><strong>${{number(row.median)}}만원/㎡</strong></div><div class="metric-box"><small>최솟값~최댓값</small><strong>${{number(row.minimum)}}~${{number(row.maximum)}}만원/㎡</strong></div><div class="metric-box"><small>거래 건수</small><strong>${{row.count.toLocaleString()}}건</strong></div>`}}else{{monthTip.style.display='none';metricBoxes.innerHTML='<div class="metric-box"><small>월별 정보</small><strong>점에 마우스를 올리세요</strong></div>'}}document.querySelector('#summary').textContent=`월별 집계 ${{monthRows.length}}개 · 거래 ${{monthRows.reduce((sum,row)=>sum+row.count,0).toLocaleString()}}건 · 가격 단위 만원/㎡`}}
monthCanvas.addEventListener('mousemove',event=>{{if(!monthRows.length)return;const rect=monthCanvas.getBoundingClientRect(),xPos=event.clientX-rect.left,g=monthlyGeometry();let best=null,distance=Infinity;for(const row of monthRows){{const d=Math.abs(g.x(row.month)-xPos);if(d<distance){{distance=d;best=row}}}}hoverMonth=distance<Math.max(16,monthCanvas.clientWidth/monthRows.length*.55)?best.month:null;drawMonthly()}});monthCanvas.addEventListener('mouseleave',()=>{{hoverMonth=null;drawMonthly()}});district.addEventListener('change',usagesForDistrict);usage.addEventListener('change',()=>{{hoverMonth=null;drawMonthly()}});
const barYear=document.querySelector('#barYear'),barUsage=document.querySelector('#barUsage'),barCanvas=document.querySelector('#barChart'),barTip=document.querySelector('#barTip'),years=[...new Set(ANNUAL.map(row=>row.year))].sort();fill(barYear,years.slice().reverse());
function updateBarUsages(){{fill(barUsage,[...new Set(ANNUAL.filter(row=>row.year===Number(barYear.value)).map(row=>row.building_use))].sort());drawBars()}}
function barRows(){{return ANNUAL.filter(row=>row.year===Number(barYear.value)&&row.building_use===barUsage.value).sort((a,b)=>b.median-a.median)}}
function drawDistrictMap(){{const rows=barRows(),byName=new Map(rows.map(row=>[row.district,row])),shapes=[...document.querySelectorAll('#districtMap .district-shape')],low=rows.length?Math.min(...rows.map(row=>row.median)):0,high=rows.length?Math.max(...rows.map(row=>row.median)):1,span=high-low||1;document.querySelector('#mapLow').textContent=rows.length?`${{number(low)}}만원/㎡`:'자료 없음';document.querySelector('#mapHigh').textContent=rows.length?`${{number(high)}}만원/㎡`:'자료 없음';for(const shape of shapes){{const row=byName.get(shape.dataset.district),label=document.querySelector(`[data-label-for="${{shape.dataset.district}}"]`);shape.style.fill=row?bwrColor(.5+.5*(row.median-low)/span):'#e5e7eb';shape.style.opacity=row?'1':'.55';shape.dataset.tip=row?`${{row.district}} · 중앙값 ${{number(row.median)}}만원/㎡ · 최소 ${{number(row.minimum)}} · 최대 ${{number(row.maximum)}}만원/㎡`:shape.dataset.district+' · 해당 조건 자료 없음';if(label)label.style.fill=row&&row.median>low+span*.62?'#fff':'#344054'}}document.querySelector('#mapTip').textContent=`${{barYear.value}}년 · ${{barUsage.value||'건물용도'}} · 단위 만원/㎡`}}
function drawBars(){{const rows=barRows(),height=barCanvas.clientHeight,ctx=barCanvas.getContext('2d'),width=barCanvas.clientWidth,ratio=devicePixelRatio||1;barCanvas.width=width*ratio;barCanvas.height=height*ratio;ctx.setTransform(ratio,0,0,ratio,0,0);ctx.clearRect(0,0,width,height);drawDistrictMap();if(!rows.length)return;const pad={{left:92,right:84,top:20,bottom:38}},max=Math.max(...rows.map(row=>row.median))*1.08||1,plot=width-pad.left-pad.right,rh=(height-pad.top-pad.bottom)/rows.length,lo=Math.min(...rows.map(row=>row.median)),hi=Math.max(...rows.map(row=>row.median)),span=hi-lo||1;ctx.font='12px system-ui';ctx.strokeStyle='#eaecf0';ctx.fillStyle='#667085';ctx.textAlign='center';for(let i=0;i<=4;i++){{const value=max*i/4,tx=pad.left+plot*i/4;ctx.beginPath();ctx.moveTo(tx,pad.top);ctx.lineTo(tx,height-pad.bottom);ctx.stroke();ctx.fillText(number(value),tx,height-12)}}ctx.textAlign='right';rows.forEach((row,i)=>{{const cy=pad.top+rh*(i+.5),barWidth=row.median/max*plot;ctx.fillStyle='#475467';ctx.fillText(row.district,pad.left-10,cy+4);ctx.fillStyle=bwrColor(.5+.5*(row.median-lo)/span);ctx.fillRect(pad.left,cy-rh*.28,Math.max(1,barWidth),rh*.56);ctx.textAlign='left';ctx.fillStyle='#1d2939';ctx.fillText(number(row.median),pad.left+barWidth+6,cy+4);ctx.textAlign='right'}});ctx.textAlign='left';barCanvas._layout={{rows,pad,plot,rh,height}}}}
barCanvas.addEventListener('mousemove',event=>{{const l=barCanvas._layout;if(!l)return;const rect=barCanvas.getBoundingClientRect(),y=event.clientY-rect.top-l.pad.top,index=Math.floor(y/l.rh),row=l.rows[index];if(!row){{barTip.style.display='none';return}}barTip.style.display='block';barTip.style.left=Math.min(barCanvas.clientWidth-230,Math.max(8,event.clientX-rect.left+12))+'px';barTip.style.top=Math.max(0,event.clientY-rect.top-24)+'px';barTip.textContent=`${{row.district}} · 중앙값 ${{number(row.median)}}만원/㎡ · 최솟값 ${{number(row.minimum)}} · 최댓값 ${{number(row.maximum)}}만원/㎡`}});barCanvas.addEventListener('mouseleave',()=>barTip.style.display='none');document.querySelectorAll('#districtMap .district-shape').forEach(shape=>{{shape.addEventListener('mousemove',()=>document.querySelector('#mapTip').textContent=shape.dataset.tip);shape.addEventListener('mouseleave',()=>document.querySelector('#mapTip').textContent=`${{barYear.value}}년 · ${{barUsage.value||'건물용도'}} · 단위 만원/㎡`)}});barYear.addEventListener('change',updateBarUsages);barUsage.addEventListener('change',drawBars);
const trendUsage=document.querySelector('#trendUsage'),sortYear=document.querySelector('#sortYear'),trendCanvas=document.querySelector('#districtTrend'),trendTip=document.querySelector('#trendTip'),yearLegend=document.querySelector('#yearLegend');fill(trendUsage,[...new Set(ANNUAL.map(row=>row.building_use))].sort());fill(sortYear,years.slice().reverse());
const yearColor=year=>bwrColor(years.length<2?.5:years.indexOf(year)/(years.length-1));
yearLegend.innerHTML=years.map(year=>`<span class="year-key"><i class="year-swatch" style="background:${{yearColor(year)}}"></i>${{year}}년</span>`).join('');
function drawDistrictTrend(){{const records=ANNUAL.filter(row=>row.building_use===trendUsage.value),byDistrict=new Map();for(const row of records){{if(!byDistrict.has(row.district))byDistrict.set(row.district,new Map());byDistrict.get(row.district).set(row.year,row)}}const sy=Number(sortYear.value),ordered=[...byDistrict.entries()].sort((a,b)=>(b[1].get(sy)?.median??-Infinity)-(a[1].get(sy)?.median??-Infinity)||a[0].localeCompare(b[0],'ko'));const width=trendCanvas.clientWidth,height=trendCanvas.clientHeight,ratio=devicePixelRatio||1;trendCanvas.width=width*ratio;trendCanvas.height=height*ratio;const ctx=trendCanvas.getContext('2d');ctx.setTransform(ratio,0,0,ratio,0,0);ctx.clearRect(0,0,width,height);if(!ordered.length)return;const pad={{left:86,right:24,top:14,bottom:36}},plot=width-pad.left-pad.right,rh=(height-pad.top-pad.bottom)/ordered.length,max=Math.max(...records.map(row=>row.median))*1.08||1,x=value=>pad.left+value/max*plot,y=index=>pad.top+rh*(index+.5);ctx.font='12px system-ui';ctx.strokeStyle='#eaecf0';ctx.fillStyle='#667085';ctx.textAlign='center';for(let i=0;i<=5;i++){{const value=max*i/5,tx=pad.left+plot*i/5;ctx.beginPath();ctx.moveTo(tx,pad.top);ctx.lineTo(tx,height-pad.bottom);ctx.stroke();ctx.fillText(number(value),tx,height-10)}}ctx.textAlign='right';const points=[];ordered.forEach(([name,yearMap],index)=>{{ctx.fillStyle='#344054';ctx.fillText(name,pad.left-10,y(index)+4);const rowPoints=years.map(yr=>yearMap.get(yr)).filter(Boolean);ctx.strokeStyle='#98a2b3';ctx.lineWidth=1;ctx.beginPath();rowPoints.forEach((row,i)=>i?ctx.lineTo(x(row.median),y(index)):ctx.moveTo(x(row.median),y(index)));ctx.stroke();for(const row of rowPoints){{const px=x(row.median),py=y(index);ctx.beginPath();ctx.arc(px,py,3,0,Math.PI*2);ctx.fillStyle=yearColor(row.year);ctx.fill();ctx.strokeStyle='#475467';ctx.lineWidth=1;ctx.stroke();points.push({{x:px,y:py,row,district:name}})}}}});ctx.textAlign='left';trendCanvas._points=points;trendCanvas._layout={{pad,plot,max,height}}}}
trendCanvas.addEventListener('mousemove',event=>{{const points=trendCanvas._points||[],rect=trendCanvas.getBoundingClientRect(),px=event.clientX-rect.left,py=event.clientY-rect.top;let found=null,distance=100;for(const point of points){{const d=(point.x-px)**2+(point.y-py)**2;if(d<distance){{distance=d;found=point}}}}if(!found){{trendTip.style.display='none';return}}trendTip.style.display='block';trendTip.style.left=Math.min(trendCanvas.clientWidth-190,found.x+9)+'px';trendTip.style.top=Math.max(0,found.y-20)+'px';trendTip.textContent=`${{found.district}} · ${{found.row.year}}년 · ${{number(found.row.median)}}만원/㎡`}});trendCanvas.addEventListener('mouseleave',()=>trendTip.style.display='none');trendUsage.addEventListener('change',drawDistrictTrend);sortYear.addEventListener('change',drawDistrictTrend);
function drawAll(){{drawMonthly();drawBars();drawDistrictTrend()}}fill(district,[...new Set(MONTHLY.map(row=>row.district))].sort());if(district.options.length)usagesForDistrict();if(years.length){{updateBarUsages();drawDistrictTrend()}}window.addEventListener('resize',drawAll);
</script></body></html>'''

def main() -> int:
    parser = argparse.ArgumentParser(description="Create a Seoul jeonse price-per-area HTML report.")
    parser.add_argument("--input", type=Path, default=Path("data/processed/seoul_rent_deduplicated.csv.gz"))
    parser.add_argument("--output", type=Path, default=Path("reports/seoul_jeonse_per_sqm.html"))
    parser.add_argument("--csv-output", type=Path, default=Path("reports/seoul_jeonse_per_sqm_monthly.csv"))
    args = parser.parse_args()
    if not args.input.exists():
        print(f"Input not found: {args.input}", file=sys.stderr)
        return 1
    print(f"Reading processed data: {args.input}", flush=True)
    boundary_path = Path("data/seoul_district_boundaries_2025.json")
    if not boundary_path.exists():
        print(f"Map boundary file not found: {boundary_path}", file=sys.stderr)
        return 1
    map_svg = build_map_svg(json.loads(boundary_path.read_text(encoding="utf-8")))
    monthly = summarize_monthly(read_rows(args.input))
    annual = summarize_annual_medians(read_rows(args.input))
    missing = summarize_missing(read_rows(args.input))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(build_html(monthly, annual, missing, args.input, map_svg), encoding="utf-8")
    args.csv_output.parent.mkdir(parents=True, exist_ok=True)
    with args.csv_output.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=("district", "building_use", "month", "count", "minimum", "median", "maximum"))
        writer.writeheader()
        writer.writerows(monthly)
    print(f"Analyzed contracts: {missing['rows']:,}")
    print(f"MNO missing: {missing['MNO']['missing']:,} ({missing['MNO']['rate']:.1%}); SNO missing: {missing['SNO']['missing']:,} ({missing['SNO']['rate']:.1%})")
    print(f"MNO and SNO both missing: {missing['both_missing']:,}")
    print(f"Monthly groups: {len(monthly):,}; annual median groups: {len(annual):,}; report: {args.output}; aggregate: {args.csv_output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
