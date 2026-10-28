from __future__ import annotations

import html
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from cortex.schema import (
    GraphSpace,
    GraphState,
    NodeMergeConflict,
    SchemaDefinition,
    validate_graph_state,
)

_CORE_NODE_LIMIT = 460
_PLACEHOLDER_RE = re.compile(r"__CORTEX_(TITLE|DATA)__")


def render_graph_html(
    graph: GraphSpace,
    schema: SchemaDefinition,
    *,
    title: str,
    node_conflicts: tuple[NodeMergeConflict, ...] = (),
) -> str:
    """Render one validated GraphSpace as a self-contained interactive HTML page."""

    # Legacy argument is imported once; rendering uses the same inline nodes.
    payload = {"graph": graph.model_dump(mode="python")}
    if node_conflicts:
        payload["node_conflicts"] = [
            c.model_dump(mode="python") for c in node_conflicts
        ]
    state = GraphState.model_validate(payload)
    validated_graph = state.graph
    validated_schema = SchemaDefinition.model_validate(schema.model_dump(mode="python"))
    validate_graph_state(state, validated_schema)
    payload = {
        "meta": {
            "nodes": len(validated_graph.nodes),
            "edges": len(validated_graph.edges),
            "conflicts": len(state.conflict_nodes),
            "core_node_limit": _CORE_NODE_LIMIT,
            "node_type_labels": {
                node_type.id: node_type.name
                for node_type in validated_schema.node_types
            },
            "edge_type_labels": {
                edge_type.id: edge_type.name
                for edge_type in validated_schema.edge_types
            },
        },
        "graph": validated_graph.model_dump(mode="json"),
    }
    embedded_data = _safe_script_json(payload)
    replacements = {
        "TITLE": html.escape(title, quote=True),
        "DATA": embedded_data,
    }
    return _PLACEHOLDER_RE.sub(
        lambda match: replacements[match.group(1)], _HTML_TEMPLATE
    )


def write_graph_html_from_json(
    source: str | Path,
    target: str | Path,
    schema: SchemaDefinition,
    *,
    title: str,
) -> Path:
    """Load a pure or Result-wrapped GraphSpace JSON and publish its HTML projection."""

    source_path = Path(source).expanduser().resolve()
    try:
        payload: Any = json.loads(source_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{source_path}: invalid JSON: {exc}") from exc
    except OSError as exc:
        raise ValueError(f"unable to read graph JSON {source_path}: {exc}") from exc
    if isinstance(payload, dict) and payload.get("relevant_graph") is not None:
        payload = payload["relevant_graph"]
    elif isinstance(payload, dict) and "graph" in payload:
        payload = {
            key: payload[key] for key in ("graph", "node_conflicts") if key in payload
        }
    try:
        state = GraphState.model_validate(payload)
    except ValidationError as exc:
        raise ValueError(f"{source_path}: invalid graph: {exc}") from exc
    content = render_graph_html(state.graph, schema, title=title)
    return _atomic_write_text(Path(target), content)


def _safe_script_json(payload: Any) -> str:
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return (
        serialized.replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def _atomic_write_text(target: Path, content: str) -> Path:
    resolved = target.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{resolved.name}.",
        suffix=".tmp",
        dir=resolved.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(temporary, resolved)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return resolved


# Interactive Canvas graph presentation adapted from the earlier Wiki projection.
_HTML_TEMPLATE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<title>__CORTEX_TITLE__</title>
<style>
*{box-sizing:border-box}html,body{margin:0;height:100%;overflow:hidden;background:#111217;color:#d8d8df}
body{font-family:-apple-system,BlinkMacSystemFont,"Inter","PingFang SC","Microsoft YaHei",sans-serif}.shell{display:grid;grid-template-columns:276px minmax(0,1fr) 360px;height:100vh}
.left,.right{overflow:auto;padding:14px;background:rgba(18,19,26,.97)}.left{border-right:1px solid #292c37}.right{border-left:1px solid #292c37}.stage{position:relative;overflow:hidden;background:#090a0f}canvas{width:100%;height:100%;display:block;cursor:grab}canvas.dragging{cursor:grabbing}
h1{margin:0;color:#f3f4f6;font-size:16px;line-height:1.35}h2{margin:16px 0 8px;color:#9da3b3;font-size:12px}.desc,.muted{color:#858b9b;font-size:11.5px;line-height:1.55}.desc{margin-top:7px}.stats{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:13px}.stat{padding:9px;border:1px solid #2a2d38;border-radius:7px;background:#1c1e26}.stat b{display:block;color:#f3f4f6;font-size:17px}.stat span{color:#818697;font-size:10.5px}
.controls{display:grid;gap:8px;margin-top:13px}.pair{display:grid;grid-template-columns:1fr 1fr;gap:8px}input,select,button{width:100%;border:1px solid #303442;border-radius:7px;background:#1d1f28;color:#d8d8df;padding:8px 9px;font-size:12px;outline:none}button{cursor:pointer;text-align:left}button:hover,button.active{border-color:#7f7fd5;background:#25273a}.pair button{text-align:center}option{color:#111217}.list,.legend{display:grid;gap:7px}.row strong{display:block;overflow:hidden;color:#ececf1;font-size:12px;text-overflow:ellipsis;white-space:nowrap}.row span{display:block;margin-top:4px;overflow:hidden;color:#838899;font-size:10.5px;text-overflow:ellipsis;white-space:nowrap}
.pill{display:flex;align-items:center;justify-content:space-between;padding:6px 7px;border:1px solid #282b36;border-radius:7px;color:#aeb3c2;font-size:11px}.pill span{display:flex;align-items:center;gap:7px}.dot{width:7px;height:7px;border-radius:50%}.topbar{position:absolute;z-index:2;top:12px;right:14px;left:14px;display:flex;justify-content:space-between;pointer-events:none}.badge{padding:6px 9px;border:1px solid #292d39;border-radius:7px;background:rgba(17,18,23,.82);color:#aeb3c2;font-size:11px}.tip{position:absolute;z-index:4;max-width:300px;padding:8px 10px;border:1px solid #343846;border-radius:7px;background:rgba(18,19,26,.96);color:#d8d8df;font-size:11px;line-height:1.5;opacity:0;pointer-events:none}.tip.show{opacity:1}.tip b{display:block;margin-bottom:3px;color:#fff;font-size:12px}
.node-head{display:flex;align-items:flex-start;gap:8px}.node-head i{width:9px;height:9px;margin-top:6px;border-radius:50%;flex:0 0 auto}.node-head strong{color:#f3f4f6;font-size:18px;line-height:1.3}.tags{display:flex;flex-wrap:wrap;gap:5px;margin:9px 0}.tag{padding:3px 7px;border:1px solid #2b2f3b;border-radius:7px;background:#1d1f28;color:#aeb3c2;font-size:10.5px}.tag.conflict{border-color:#a74f62;background:#3a2029;color:#ffb4c1}.tag.normal{border-color:#315e50;background:#192f29;color:#8fd6bd}.summary{color:#c9ccd6;font-size:12px;line-height:1.7}.card{display:block;margin:8px 0;padding:9px;border:1px solid #292c37;border-radius:7px;background:#1c1e26}button.card{text-align:left}.card.conflict-record{border-color:#713848;background:#2a1b22}.card b{display:block;margin-bottom:5px;color:#e5e7eb;font-size:11.5px}.card p{margin:4px 0 0;color:#adb2c0;font-size:11.5px;line-height:1.6}.card small{display:block;margin-top:6px;color:#707586;font-size:10.5px;overflow-wrap:anywhere}.empty{padding:11px;border:1px dashed #303442;border-radius:7px;color:#777d8e;font-size:11.5px}pre{margin:0;overflow:auto;color:#aeb7ca;font:10.5px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace;white-space:pre-wrap;overflow-wrap:anywhere}
@media(max-width:960px){.shell{grid-template-columns:220px minmax(0,1fr);grid-template-rows:58vh 42vh}.right{grid-column:1/-1;border-top:1px solid #292c37;border-left:0}}@media(max-width:640px){html,body{height:auto;overflow:auto}.shell{grid-template-columns:1fr;grid-template-rows:auto 55vh auto;height:auto;min-height:100vh}.left,.right{border:0}.stage{min-height:55vh}.right{grid-column:1;border-top:1px solid #292c37}}
</style>
</head>
<body>
<div class="shell">
<aside class="left"><h1>__CORTEX_TITLE__</h1><div class="desc">GraphSpace Node 与 Edge 语义网络</div><div class="stats" id="stats"></div><div class="controls"><input id="search" placeholder="搜索名称 / 类型 / 属性"/><select id="type"></select><select id="status"><option value="all">全部 Merge Status</option><option value="normal">NORMAL</option><option value="conflict">CONFLICT</option></select><div class="pair"><button id="overview">核心网络</button><button id="all">全部节点</button></div><button id="focus">查看关系子图</button><button id="reset">重置视图</button></div><h2>Nodes</h2><div class="list" id="results"></div><h2>Node 类型</h2><div class="legend" id="legend"></div><h2>Edge 关系</h2><div class="legend" id="edgeLegend"></div></aside>
<main class="stage"><div class="topbar"><div class="badge" id="graphInfo"></div><div class="badge">GraphSpace · 离线视图</div></div><div class="tip" id="tip"></div><canvas id="canvas"></canvas></main>
<aside class="right" id="detail"></aside>
</div>
<script>
const DATA=__CORTEX_DATA__;
const graph=DATA.graph,nodeConflicts=graph.nodes.filter(node=>node.merge_status==='conflict'),conflictById=new Map(nodeConflicts.map(item=>[item.id,item])),nodeLabels=DATA.meta.node_type_labels||{},edgeLabels=DATA.meta.edge_type_labels||{},coreLimit=DATA.meta.core_node_limit||460;
const fixedPalette={table:'#5f8fc4',column:'#8797aa',dimension_value:'#58aebe',metric:'#c58d45',generation_rule:'#c77888',example:'#8d78c5',knowledge_unit:'#8797aa',concept:'#58aebe',entity:'#5598bd',event:'#c77888',process:'#8d78c5',rule:'#c58d45',theme:'#7d8fd1',domain:'#5fa8a0'};
function stableHash(value){let h=2166136261;for(const c of String(value)){h^=c.charCodeAt(0);h=Math.imul(h,16777619)}return h>>>0}
function colorForType(value){if(fixedPalette[value])return fixedPalette[value];return`hsl(${stableHash(value)%360} 38% 56%)`}
function esc(value){return String(value==null?'':value).replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]))}
function short(value,size){const text=String(value==null?'':value);return text.length>size?text.slice(0,size-1)+'…':text}
function pretty(value){return JSON.stringify(value,null,2)}
function typeLabel(value){return nodeLabels[value]||value||'未分类'}function edgeLabel(value){return edgeLabels[value]||value||'关联'}
const allNodes=graph.nodes.map(node=>({...node,type:node.node_type,title:node.name,degree:0,mergeStatus:conflictById.has(node.id)||node.merge_status==='conflict'?'conflict':'normal'}));
const allEdges=graph.edges.map(edge=>({...edge,source:edge.from_node_id,target:edge.to_node_id,relation:edge.edge_type}));
const byId=new Map(allNodes.map(node=>[node.id,node])),adj=new Map(allNodes.map(node=>[node.id,[]]));
for(const edge of allEdges){if(!adj.has(edge.source)||!adj.has(edge.target))continue;adj.get(edge.source).push({id:edge.target,edge,out:true});adj.get(edge.target).push({id:edge.source,edge,out:false});byId.get(edge.source).degree++;byId.get(edge.target).degree++}
const canvas=document.getElementById('canvas'),ctx=canvas.getContext('2d'),tip=document.getElementById('tip');
let selected='',focused=false,showAll=false,query='',type='all',status='all',visible=[],visibleEdges=[],particles=[],transform={x:0,y:0,k:1},hover='',drag=null,moved=false;
function score(node){return node.degree*10+(node.sources||[]).length}
function rank(a,b){return score(b)-score(a)||String(a.id).localeCompare(String(b.id))}
function normalized(value){return String(value==null?'':value).toLowerCase().replace(/[\s:：。；;,，、/_\-（）()《》]+/g,'')}
function searchScore(node){const needle=normalized(query),name=normalized(node.name),typeName=normalized(typeLabel(node.node_type)),body=normalized(JSON.stringify([node.description,node.aliases,node.tags,node.properties,node.sources]));let relevance=0;if(name===needle)relevance=100000;else if(name.startsWith(needle))relevance=80000;else if(name.includes(needle))relevance=60000-name.indexOf(needle)*120;else if(typeName.includes(needle))relevance=40000;else if(body.includes(needle))relevance=20000-body.indexOf(needle);return relevance+Math.min(999,score(node))}
function matches(node){if(type!=='all'&&node.node_type!==type)return false;if(status!=='all'&&node.mergeStatus!==status)return false;const needle=query.trim().toLowerCase();return!needle||[node.name,node.node_type,typeLabel(node.node_type),node.mergeStatus,node.description,JSON.stringify(node.aliases),JSON.stringify(node.tags),JSON.stringify(node.properties),JSON.stringify(node.sources)].join(' ').toLowerCase().includes(needle)}
function radius(node){return Math.max(5.5,Math.min(14,6+Math.sqrt(node.degree)*1.35))}
function resetTransform(){transform={x:0,y:0,k:visible.length>coreLimit?.55:.85}}
function initialize(){const nodeCounts=new Map(),edgeCounts=new Map();for(const node of allNodes)nodeCounts.set(node.node_type,(nodeCounts.get(node.node_type)||0)+1);for(const edge of allEdges)edgeCounts.set(edge.edge_type,(edgeCounts.get(edge.edge_type)||0)+1);document.getElementById('stats').innerHTML=[['Nodes',allNodes.length],['Edges',allEdges.length],['Conflicts',nodeConflicts.length],['Node 类型',nodeCounts.size]].map(([label,value])=>`<div class="stat"><b>${value}</b><span>${label}</span></div>`).join('');const nodeRows=[...nodeCounts.entries()].sort((a,b)=>b[1]-a[1]||a[0].localeCompare(b[0]));document.getElementById('type').innerHTML='<option value="all">全部 Node 类型</option>'+nodeRows.map(([value,count])=>`<option value="${esc(value)}">${esc(typeLabel(value))} ${count}</option>`).join('');document.getElementById('legend').innerHTML=nodeRows.map(([value,count])=>`<div class="pill"><span><i class="dot" style="background:${colorForType(value)}"></i>${esc(typeLabel(value))}</span><b>${count}</b></div>`).join('')||'<div class="empty">暂无节点</div>';document.getElementById('edgeLegend').innerHTML=[...edgeCounts.entries()].sort((a,b)=>b[1]-a[1]||a[0].localeCompare(b[0])).map(([value,count])=>`<div class="pill"><span>${esc(edgeLabel(value))}</span><b>${count}</b></div>`).join('')||'<div class="empty">暂无关系</div>';resize()}
function compute(){let ids;if(focused&&selected){ids=new Set([selected]);for(const row of[...(adj.get(selected)||[])].sort((a,b)=>rank(byId.get(a.id),byId.get(b.id))).slice(0,80))ids.add(row.id)}else{const matched=allNodes.filter(matches).sort((a,b)=>query?searchScore(b)-searchScore(a):rank(a,b)),useCore=!showAll&&!query&&type==='all'&&matched.length>coreLimit;ids=new Set((useCore?matched.slice(0,coreLimit):matched).map(node=>node.id));if(selected)ids.add(selected)}visible=[...ids].map(id=>byId.get(id)).filter(Boolean).sort((a,b)=>query?searchScore(b)-searchScore(a):rank(a,b));visibleEdges=allEdges.filter(edge=>ids.has(edge.source)&&ids.has(edge.target)&&(!focused||edge.source===selected||edge.target===selected));seed();renderResults();renderDetail();updateControls()}
function seed(){const width=canvas.clientWidth,height=canvas.clientHeight,mode=focused?'focus':showAll?'all':'core',prior=new Map(particles.filter(node=>node.layoutMode===mode).map(node=>[node.id,node])),center=prior.get(selected)||{x:width/2,y:height/2};particles=visible.map((node,index)=>{const old=prior.get(node.id);if(old)return{...old,...node,r:radius(node)};const angle=(stableHash(node.id+'angle')/4294967295)*Math.PI*2,distance=(focused?62:48)*Math.sqrt(index+1)+(stableHash(node.id+'radius')/4294967295-.5)*18;return{...node,x:center.x+Math.cos(angle)*distance,y:center.y+Math.sin(angle)*distance,vx:0,vy:0,r:radius(node),layoutMode:mode}});const label=focused?'Node 关系子图':showAll?'Node 全局图':allNodes.length>coreLimit?'Node 核心网络':'Node 全局图';document.getElementById('graphInfo').textContent=`${label} · ${particles.length} Nodes · ${visibleEdges.length} Edges`}
function repel(a,b){let dx=b.x-a.x,dy=b.y-a.y,distanceSquared=dx*dx+dy*dy+1,distance=Math.sqrt(distanceSquared),minimum=a.r+b.r+22,force=Math.min(.42,1450/distanceSquared);if(distance<minimum)force+=(minimum-distance)*.013;dx/=distance;dy/=distance;a.vx-=dx*force;a.vy-=dy*force;b.vx+=dx*force;b.vy+=dy*force}
function tick(){const width=canvas.clientWidth,height=canvas.clientHeight,centerX=width/2,centerY=height/2,particleById=new Map(particles.map(node=>[node.id,node]));for(const node of particles){const pull=focused&&node.id===selected?.018:.0012;node.vx+=(centerX-node.x)*pull;node.vy+=(centerY-node.y)*pull}for(const edge of visibleEdges){const a=particleById.get(edge.source),b=particleById.get(edge.target);if(!a||!b)continue;let dx=b.x-a.x,dy=b.y-a.y,distance=Math.hypot(dx,dy)||1,target=focused?210:165,force=(distance-target)*(focused?.0018:.001);dx/=distance;dy/=distance;a.vx+=dx*force;a.vy+=dy*force;b.vx-=dx*force;b.vy-=dy*force}const cell=180,grid=new Map();for(let i=0;i<particles.length;i++){const node=particles[i],key=Math.floor(node.x/cell)+','+Math.floor(node.y/cell);if(!grid.has(key))grid.set(key,[]);grid.get(key).push(i)}for(let i=0;i<particles.length;i++){const a=particles[i],gridX=Math.floor(a.x/cell),gridY=Math.floor(a.y/cell);for(let x=-1;x<=1;x++)for(let y=-1;y<=1;y++)for(const index of(grid.get((gridX+x)+','+(gridY+y))||[]))if(index>i)repel(a,particles[index])}for(const node of particles){node.vx*=.86;node.vy*=.86;node.x+=node.vx;node.y+=node.vy}draw();requestAnimationFrame(tick)}
function drawArrow(a,b,active){const dx=b.x-a.x,dy=b.y-a.y,distance=Math.hypot(dx,dy)||1,targetX=b.x-dx/distance*(b.r+3),targetY=b.y-dy/distance*(b.r+3),size=(active?8:6)/Math.sqrt(transform.k),angle=Math.atan2(dy,dx);ctx.beginPath();ctx.moveTo(targetX,targetY);ctx.lineTo(targetX-Math.cos(angle-.55)*size,targetY-Math.sin(angle-.55)*size);ctx.lineTo(targetX-Math.cos(angle+.55)*size,targetY-Math.sin(angle+.55)*size);ctx.closePath();ctx.fillStyle=active?'rgba(199,210,254,.95)':'rgba(148,163,184,.48)';ctx.fill()}
function draw(){const particleById=new Map(particles.map(node=>[node.id,node])),small=particles.length<=40;ctx.clearRect(0,0,canvas.clientWidth,canvas.clientHeight);ctx.save();ctx.translate(transform.x,transform.y);ctx.scale(transform.k,transform.k);for(const edge of visibleEdges){const a=particleById.get(edge.source),b=particleById.get(edge.target);if(!a||!b)continue;const active=selected&&(edge.source===selected||edge.target===selected);ctx.beginPath();ctx.moveTo(a.x,a.y);ctx.lineTo(b.x,b.y);ctx.strokeStyle=active?'rgba(199,210,254,.95)':small?'rgba(167,139,250,.62)':'rgba(148,163,184,.28)';ctx.lineWidth=(active?2:small?1.25:.8)/transform.k;ctx.stroke();drawArrow(a,b,active);if(active||small){ctx.font=`600 ${9/Math.sqrt(transform.k)}px -apple-system,"PingFang SC",sans-serif`;ctx.textAlign='center';ctx.lineWidth=3/transform.k;ctx.strokeStyle='#090a0f';ctx.fillStyle='#c4b5fd';const x=(a.x+b.x)/2,y=(a.y+b.y)/2,label=short(edgeLabel(edge.edge_type),12);ctx.strokeText(label,x,y);ctx.fillText(label,x,y)}}for(const node of particles){const active=node.id===selected,over=node.id===hover;ctx.beginPath();ctx.arc(node.x,node.y,active?node.r+2:over?node.r+1:node.r,0,Math.PI*2);ctx.fillStyle=colorForType(node.node_type);ctx.globalAlpha=active?1:.9;ctx.fill();ctx.globalAlpha=1;ctx.strokeStyle=node.mergeStatus==='conflict'?'#ff6f91':active?'#fff':'rgba(255,255,255,.28)';ctx.lineWidth=(node.mergeStatus==='conflict'?2.6:active?1.5:.7)/transform.k;ctx.stroke()}const labelLimit=small?Infinity:focused?80:24,labelIds=Number.isFinite(labelLimit)?new Set([...particles].sort(rank).slice(0,labelLimit).map(node=>node.id)):null;for(const node of particles){const active=node.id===selected,over=node.id===hover;if(!active&&!over&&labelIds&&!labelIds.has(node.id))continue;const size=active?13:over?11:10;ctx.font=`${active?650:560} ${size/Math.sqrt(transform.k)}px -apple-system,"PingFang SC",sans-serif`;ctx.textAlign='center';ctx.lineWidth=3/transform.k;ctx.strokeStyle='#090a0f';ctx.fillStyle=node.mergeStatus==='conflict'?'#ff9aae':active?'#fff':'#d5d9e3';const label=short(node.name,active?34:24),y=node.y+node.r+11/transform.k;ctx.strokeText(label,node.x,y);ctx.fillText(label,node.x,y)}ctx.restore()}
function renderResults(){const rows=allNodes.filter(matches).sort((a,b)=>query?searchScore(b)-searchScore(a):rank(a,b)).slice(0,12);document.getElementById('results').innerHTML=rows.map(node=>`<button class="row ${node.id===selected?'active':''}" data-id="${esc(node.id)}"><strong>${esc(node.name)}</strong><span>${esc(typeLabel(node.node_type))} · ${node.degree} relations · ${node.mergeStatus.toUpperCase()}</span></button>`).join('')||'<div class="empty">没有匹配节点</div>';document.querySelectorAll('.row').forEach(element=>element.onclick=()=>selectNode(element.dataset.id))}
function propertyCard(title,value){if(value==null||(Array.isArray(value)&&!value.length)||(typeof value==='object'&&!Array.isArray(value)&&!Object.keys(value).length))return'';return`<h2>${esc(title)}</h2><div class="card"><pre>${esc(pretty(value))}</pre></div>`}
function conflictPoints(conflict){if(!conflict)return'';const points=(conflict.conflict_points||[]).map(point=>{const choices=(point.choices||[]).map((choice,index)=>`<div class="card"><b>Choice ${index+1}</b><p>${esc(choice.statement)}</p><small>Alternatives: ${esc((choice.alternative_ids||[]).join(', '))}</small></div>`).join('');return`<div class="card conflict-record"><b>${esc(point.point_id)} · ${esc(point.aspect)}</b><p>${esc(point.description||'')}</p>${choices}</div>`}).join('');return`<h2>Difference Points · ${(conflict.conflict_points||[]).length}</h2>${points||'<div class="empty">暂无可用 Difference Point</div>'}`}
function conflictAudit(node,conflict){if(!conflict)return'';const canonical=`<details class="card"><summary>Common Node Content · ${esc(node.id)}</summary><pre>${esc(pretty(node))}</pre></details>`,alternatives=conflict.alternatives.map((item,index)=>`<details class="card"><summary>Alternative ${index+1} · ${esc(item.overrides.name||item.alternative_id)}</summary><small>${esc(item.alternative_id)}</small><pre>${esc(pretty({overrides:item.overrides,sources:item.sources}))}</pre></details>`).join('');return`<h2>公共内容与候选差异 · ${conflict.alternatives.length}</h2>${canonical}${alternatives}`}
function renderDetail(){const node=byId.get(selected),element=document.getElementById('detail');if(!node){element.innerHTML='<div class="empty">选择一个节点查看内容</div>';return}const conflict=conflictById.get(node.id),relations=[...(adj.get(node.id)||[])].sort((a,b)=>edgeLabel(a.edge.edge_type).localeCompare(edgeLabel(b.edge.edge_type))||String(a.id).localeCompare(String(b.id))),relationCards=relations.map(row=>{const other=byId.get(row.id),edge=row.edge,direction=row.out?'→ ':'← ',details={id:edge.id,...edge.properties,...((edge.sources||[]).length?{sources:edge.sources}:{})};return`<button class="card relation" data-id="${esc(other.id)}"><b>${esc(direction+other.name)}</b><p>${esc(edgeLabel(edge.edge_type))} · ${esc(edge.edge_type)}</p><small>${esc(short(pretty(details),260))}</small></button>`}).join(''),ordinaryDetails=conflict?'':`${node.description?`<h2>描述</h2><p class="summary">${esc(node.description)}</p>`:''}${propertyCard('Aliases',node.aliases)}${propertyCard('Tags',node.tags)}${propertyCard('Properties',node.properties)}${propertyCard('Sources',node.sources)}`;element.innerHTML=`<div class="node-head"><i style="background:${colorForType(node.node_type)}"></i><strong>${esc(node.name)}</strong></div><div class="tags"><span class="tag">${esc(typeLabel(node.node_type))}</span><span class="tag ${node.mergeStatus}">${node.mergeStatus.toUpperCase()}</span><span class="tag">${node.degree} 条关系</span><span class="tag">${esc(node.id)}</span>${conflict?`<span class="tag">${conflict.alternatives.length} Alternatives</span>`:''}</div>${conflictPoints(conflict)}${ordinaryDetails}<h2>关系</h2>${relationCards||'<div class="empty">暂无关系</div>'}${conflictAudit(node,conflict)}`;element.querySelectorAll('.relation').forEach(button=>button.onclick=()=>selectNode(button.dataset.id))}
function updateControls(){document.getElementById('overview').classList.toggle('active',!focused&&!showAll);document.getElementById('all').classList.toggle('active',!focused&&showAll);document.getElementById('focus').classList.toggle('active',focused)}
function selectNode(id){if(!byId.has(id))return;selected=id;focused=true;resetTransform();compute()}
function back(clear=false){focused=false;showAll=false;selected='';if(clear){query='';type='all';status='all';document.getElementById('search').value='';document.getElementById('type').value='all';document.getElementById('status').value='all'}resetTransform();compute()}
function screenToWorld(x,y){return{x:(x-transform.x)/transform.k,y:(y-transform.y)/transform.k}}function pick(x,y){const point=screenToWorld(x,y);for(let index=particles.length-1;index>=0;index--){const node=particles[index];if(Math.hypot(point.x-node.x,point.y-node.y)<=node.r+7)return node}return null}
function resize(){const ratio=window.devicePixelRatio||1,rect=canvas.getBoundingClientRect();canvas.width=rect.width*ratio;canvas.height=rect.height*ratio;ctx.setTransform(ratio,0,0,ratio,0,0);resetTransform();compute()}
canvas.onmousedown=event=>{drag={x:event.clientX,y:event.clientY,tx:transform.x,ty:transform.y};moved=false;canvas.classList.add('dragging')};window.onmousemove=event=>{if(drag){const dx=event.clientX-drag.x,dy=event.clientY-drag.y;moved=moved||Math.hypot(dx,dy)>3;transform.x=drag.tx+dx;transform.y=drag.ty+dy}const rect=canvas.getBoundingClientRect(),node=pick(event.clientX-rect.left,event.clientY-rect.top);hover=node?.id||'';if(node){tip.classList.add('show');tip.style.left=Math.min(rect.width-310,event.clientX-rect.left+14)+'px';tip.style.top=Math.max(10,event.clientY-rect.top+12)+'px';tip.innerHTML=`<b>${esc(node.name)}</b>${esc(typeLabel(node.node_type))} · ${node.degree} relations`}else tip.classList.remove('show')};window.onmouseup=()=>{drag=null;canvas.classList.remove('dragging')};canvas.onclick=event=>{if(moved)return;const rect=canvas.getBoundingClientRect(),node=pick(event.clientX-rect.left,event.clientY-rect.top);node?selectNode(node.id):back(false)};canvas.onwheel=event=>{event.preventDefault();const rect=canvas.getBoundingClientRect(),mouseX=event.clientX-rect.left,mouseY=event.clientY-rect.top,before=screenToWorld(mouseX,mouseY);transform.k=Math.max(.3,Math.min(4,transform.k*(event.deltaY<0?1.12:.89)));transform.x=mouseX-before.x*transform.k;transform.y=mouseY-before.y*transform.k};
document.getElementById('search').oninput=event=>{query=event.target.value;focused=false;resetTransform();compute()};document.getElementById('type').onchange=event=>{type=event.target.value;selected='';focused=false;resetTransform();compute()};document.getElementById('status').onchange=event=>{status=event.target.value;selected='';focused=false;showAll=true;resetTransform();compute()};document.getElementById('overview').onclick=()=>back(false);document.getElementById('all').onclick=()=>{focused=false;showAll=true;selected='';resetTransform();compute()};document.getElementById('focus').onclick=()=>{if(selected){focused=true;resetTransform();compute()}};document.getElementById('reset').onclick=()=>back(true);window.onresize=resize;initialize();requestAnimationFrame(tick);
</script>
</body>
</html>
"""


__all__ = ["render_graph_html", "write_graph_html_from_json"]
