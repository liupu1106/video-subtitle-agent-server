/* ==========================================================================
   export.js —— 导出与下载
   优先用浏览器本地缓存的结果在前端直接生成文件（不受服务端实例重启/多副本影响），
   仅在缓存缺失且服务端仍保留结果时回退到服务端导出。
   ========================================================================== */

/* ---------------- 内容序列化（与后端 _section_blocks 保持一致）---------------- */
function segTextOf(res){
  const segs = res.raw_segments || [];
  if(!segs.length) return res.clean_transcript || res.raw_transcript || "";
  return segs.join("\n\n");
}

function lightMd(s){
  s = String(s);
  s = s.replace(/^#{1,6}\s*/gm, "");
  s = s.replace(/^\s*[-*]\s*/gm, "");
  s = s.replace(/\*\*/g, "");
  return s;
}

/* 把某分区表达为统一的 blocks：
   [["title",...],["h",...],["p"/"md",...],["table",[[...]]]]
   与后端 _section_blocks 保持一致的容错：bilingual 可能是字符串列表/字符串/键名不同，tech 可能是裸列表。 */
function sectionBlocks(res, section){
  const title = res.title || "视频字幕";
  const blocks = [["title", title]];
  if(section === "raw"){
    blocks.push(["h","一、原始字幕（完整文本）"]);
    blocks.push(["p", segTextOf(res)]);
  }else if(section === "summary"){
    blocks.push(["h","二、智能梳理结果"]);
    blocks.push(["md", String(res.structured || res.clean_transcript || "（无梳理结果）")]);
  }else if(section === "tech"){
    blocks.push(["h","三、技术提取"]);
    let tech = res.tech;
    if(!tech || typeof tech !== "object" || Array.isArray(tech)) tech = { technologies: Array.isArray(tech) ? tech : [] };
    const techs = (tech.technologies && Array.isArray(tech.technologies)) ? tech.technologies : [];
    const feats = (tech.features && Array.isArray(tech.features)) ? tech.features : [];
    if(techs.length && typeof techs[0] === "object"){
      const rows = [["#","技术名称","主要作用","国内可用性","特别说明"]];
      techs.forEach((t,i)=>{
        if(!t || typeof t !== "object"){ rows.push([String(i+1), String(t||""), "", "", ""]); return; }
        // 每个字段都 String() 兜底：真实 LLM 返回里 name/function 等可能是对象/数组，
        // 直接写进 XLSX 单元格会抛异常导致导出失败。
        rows.push([String(i+1), String(t.name||""), String(t.function||""), String(t.domestic||""), String(t.notes||"")]);
      });
      blocks.push(["table", rows]);
    }else if(techs.length){
      blocks.push(["p", techs.map(x=>"- "+x).join("\n") || "（无）"]);
    }
    blocks.push(["h","主要功能"]);
    blocks.push(["p", feats.map(x=>"- "+x).join("\n") || "（无）"]);
  }else if(section === "bilingual"){
    blocks.push(["h","四、中英文对照"]);
    let bl = res.bilingual;
    if(typeof bl === "string") bl = bl.split("\n").map(s=>s.trim()).filter(Boolean);
    bl = Array.isArray(bl) ? bl : [];
    if(bl.length){
      const rows = [["原文","译文"]];
      bl.forEach(it=>{
        if(it && typeof it === "object"){
          // String() 兜底：真实 LLM 返回里 orig/trans 可能是对象/数组，直接 .trim() 会抛异常导致导出失败。
          rows.push([String(it.orig||it.text||it.source||"").trim(), String(it.trans||it.translation||it.target||"").trim()]);
        }else{
          rows.push([String(it).trim(), ""]);
        }
      });
      blocks.push(["table", rows]);
    }else{
      blocks.push(["p","（无中英文对照数据；需填写 API Key 经 AI 翻译后生成）"]);
    }
  }
  return blocks;
}

function serializeText(blocks){
  const out = [];
  for(const [typ,p] of blocks){
    if(typ==="title"){ out.push(p,""); }
    else if(typ==="h"){ out.push("■ "+p,""); }
    else if(typ==="p"||typ==="md"){ out.push(lightMd(p),""); }
    else if(typ==="table"){ for(const row of p) out.push(row.join("\t")); out.push(""); }
  }
  return out.join("\n");
}

function serializeMd(blocks){
  const out = [];
  for(const [typ,p] of blocks){
    if(typ==="title"){ out.push("# "+p,""); }
    else if(typ==="h"){ out.push("## "+p,""); }
    else if(typ==="p"||typ==="md"){ out.push(p,""); }
    else if(typ==="table"){
      out.push("| "+p[0].join(" | ")+" |");
      out.push("| "+p[0].map(()=>"---").join(" | ")+" |");
      for(const row of p.slice(1)) out.push("| "+row.join(" | ")+" |");
      out.push("");
    }
  }
  return out.join("\n");
}

function srtTime(sec){
  sec = Math.max(0, sec);
  const h = Math.floor(sec/3600), m = Math.floor((sec%3600)/60), s = sec%60;
  const ms = Math.floor((s-Math.floor(s))*1000), S = Math.floor(s);
  const p2 = x => String(x).padStart(2,"0"), p3 = x => String(x).padStart(3,"0");
  return p2(h)+":"+p2(m)+":"+p2(S)+","+p3(ms);
}

function buildSrt(res){
  let segs = res.raw_segments || [];
  if(!segs.length){ const t = res.clean_transcript || res.raw_transcript || ""; segs = t.split("\n\n").filter(x=>x.trim()); }
  if(!segs.length) return "";
  const n = segs.length, dur = res.duration || 0, span = dur ? dur/n : 5.0;
  let out = "";
  for(let i=0;i<n;i++){ out += (i+1)+"\n"+srtTime(i*span)+" --> "+srtTime((i+1)*span)+"\n"+segs[i].trim()+"\n\n"; }
  return out;
}

function blocksToAoa(blocks){
  const a = [];
  for(const [typ,p] of blocks){
    if(typ==="title") a.push([p]);
    else if(typ==="h") a.push([p]);
    else if(typ==="p"||typ==="md") a.push([lightMd(p)]);
    else if(typ==="table") for(const row of p) a.push(row);
  }
  return a;
}

function safeName(title, label, ext){
  const t = String(title||"subtitle").replace(/[\\/:*?"<>|]/g,"_").trim() || "subtitle";
  return t+"_"+label+"."+ext;
}

/* ---------------- 下载与 PDF ---------------- */
function downloadBlob(blob, filename){
  const u = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = u; a.download = filename;
  document.body.appendChild(a); a.click();
  setTimeout(()=>{ URL.revokeObjectURL(u); a.remove(); }, 1000);
}

function ensureSheetJS(){
  return new Promise((res,rej)=>{
    if(window.XLSX) return res();
    const s = document.createElement("script");
    s.src = "https://cdn.jsdelivr.net/npm/xlsx/dist/xlsx.full.min.js";
    s.onload = ()=>res();
    s.onerror = ()=>rej(new Error("Excel 组件加载失败"));
    document.head.appendChild(s);
  });
}

function blocksToPdfHtml(blocks, title){
  let h = "<h1>"+esc(title)+"</h1>";
  for(const [typ,p] of blocks.slice(1)){
    if(typ==="h") h += "<h2>"+esc(p)+"</h2>";
    else if(typ==="p"||typ==="md") h += "<pre>"+esc(p)+"</pre>";
    else if(typ==="table"){
      h += "<table><tr>"+p[0].map(c=>"<th>"+esc(c)+"</th>").join("")+"</tr>";
      for(const row of p.slice(1)) h += "<tr>"+row.map(c=>"<td>"+esc(c)+"</td>").join("")+"</tr>";
      h += "</table>";
    }
  }
  return h;
}

function exportPdf(blocks, title, label){
  printWindow(title, blocksToPdfHtml(blocks, title));
}

/* 打开一个打印窗口并写入 HTML（导出 PDF 的通用实现） */
function printWindow(title, bodyHtml){
  const w = window.open("", "_blank");
  if(!w){ showErr("浏览器拦截了打印窗口，请允许弹出后重试"); return false; }
  const css = "body{font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;padding:24px;line-height:1.8;color:#111}"
    + "h1{font-size:20px;margin:0 0 12px}h2{font-size:16px;border-left:3px solid #6c8cff;padding-left:8px;margin:14px 0 6px}"
    + "table{border-collapse:collapse;width:100%;margin:8px 0}td,th{border:1px solid #ccc;padding:6px 8px;font-size:13px;vertical-align:top;text-align:left}"
    + "th{background:#1e2340;color:#fff}pre{white-space:pre-wrap;font-family:inherit;margin:0}"
    + "hr{border:none;border-top:1px solid #eee;margin:18px 0}";
  w.document.write('<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8"><title>'
    + esc(title) + '</title><style>' + css + '</style></head><body>'
    + bodyHtml
    + '<scr' + 'ipt>window.onload=function(){setTimeout(function(){window.print();},300);};</scr' + 'ipt></body></html>');
  w.document.close();
  return true;
}

/* ---------------- 导出入口 ---------------- */
/* 回退到服务端导出（薄封装，内部走 api.js） */
async function serverExport(jobId, sec, f){
  return apiServerExport(jobId, sec, f);
}

/* 前端本地生成文件 */
async function clientExport(res, sec, f){
  const label = SECTION_LABEL[sec] || sec;
  const title = res.title || "subtitle";
  const blocks = sectionBlocks(res, sec);
  if(f==="json"){ downloadBlob(new Blob([JSON.stringify(res,null,2)],{type:"application/json;charset=utf-8"}), safeName(title,label,"json")); return; }
  if(f==="txt"){ downloadBlob(new Blob([serializeText(blocks)],{type:"text/plain;charset=utf-8"}), safeName(title,label,"txt")); return; }
  if(f==="md"){ downloadBlob(new Blob([serializeMd(blocks)],{type:"text/markdown;charset=utf-8"}), safeName(title,label,"md")); return; }
  if(f==="srt"){ downloadBlob(new Blob([buildSrt(res)],{type:"application/x-subrip;charset=utf-8"}), safeName(title,label,"srt")); return; }
  if(f==="xlsx"){
    await ensureSheetJS();
    const sheet = XLSX.utils.aoa_to_sheet(blocksToAoa(blocks));
    const wb = XLSX.utils.book_new();
    XLSX.utils.book_append_sheet(wb, sheet, (label||"sheet").slice(0,31));
    const out = XLSX.write(wb, {bookType:"xlsx", type:"array"});
    downloadBlob(new Blob([out],{type:"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}), safeName(title,label,"xlsx"));
    return;
  }
  if(f==="pdf"){ exportPdf(blocks, title, label); return; }
  throw new Error("不支持的格式："+f);
}

function zipNameFromDisp(disp, fallback){
  if(!disp) return fallback;
  let n = (disp.match(/filename\*=UTF-8''([^;]+)/) || [])[1];
  if(!n) n = (disp.match(/filename="?([^";]+)"?/) || [])[1];
  return n ? decodeURIComponent(n) : fallback;
}

/* 一键导出：打包某视频所有分区的所有格式。优先用 job_id 走服务端（请求体极小） */
async function exportAll(res, jobId){
  if(!res){ showErr("结果不可用，无法导出"); return; }
  try{
    const r = await apiExportAll(res, jobId);
    const blob = await r.blob();
    if(blob.size === 0) throw new Error("空文件");
    downloadBlob(blob, zipNameFromDisp(r.headers.get("Content-Disposition"), (res.title||"subtitle")+"_全部导出.zip"));
  }catch(e){
    showErr("一键导出失败：" + (e && e.message ? e.message : e));
  }
}

/* 整批：逐个导出每个已完成视频的 zip（依次下载） */
async function exportAllBatch(children){
  let n = 0;
  for(const ch of (children||[])){
    if(ch.status !== "done") continue;
    const res = cacheGet(ch.id);
    if(!res){ continue; }
    await exportAll(res, ch.id); n++;
    await new Promise(r=>setTimeout(r, 500));
  }
  if(n === 0) showErr("没有可导出的已完成视频（结果可能已因实例重启丢失，请重新解析）");
  else flashMsg("已导出 " + n + " 个视频的全部文件 ✓");
}

/* ---------------- 整批查看页：合并导出 ---------------- */
/* 把整批查看页当前展示的内容（所有已加载分集的 梳理结果 或 技术提取）合并成一个文件。
   与「整批一键导出全部」（逐个视频单独 zip）不同：这里是把整批整合为单份文档，便于整体阅读/分享。 */

/* 技术的纯文本表达（供 TXT 合并导出） */
function buildTechText(tech){
  if(!tech) return "（未生成技术提取）";
  const feats = Array.isArray(tech.features) ? tech.features : [];
  const techs = Array.isArray(tech.technologies) ? tech.technologies : [];
  const lines = ["【视频提及的技术】"];
  if(techs.length && typeof techs[0] === "object"){
    techs.forEach((t,i)=>{
      lines.push(`${i+1}. ${t.name || ("技术"+(i+1))}`);
      if(t.function) lines.push("   主要作用：" + String(t.function));
      if(t.domestic) lines.push("   国内使用：" + String(t.domestic));
      if(t.notes)    lines.push("   特别说明：" + String(t.notes));
    });
  }else if(techs.length){
    lines.push(techs.map((x,i)=>`${i+1}. ${x}`).join("\n"));
  }else{
    lines.push("(无)");
  }
  lines.push("");
  lines.push("【主要功能】");
  lines.push(feats.length ? feats.map((x,i)=>`${i+1}. ${x}`).join("\n") : "(无)");
  return lines.join("\n");
}

/* 收集整批已加载的结果（优先用前端缓存，避免依赖易失效的服务端内存状态） */
function collectBatchItems(){
  const d = lastBatchData;
  if(!d || !d.children || !d.children.length) return null;
  const items = [];
  for(const ch of d.children){
    const res = ch.result || cacheGet(ch.id);
    if(res) items.push({ ep: ch.ep_no || "", title: ch.title || "", res });
  }
  return items.length ? { items, title: d.title } : null;
}

/* 合并导出 PDF（整批查看页） */
function exportBatchPdf(items, mode, title){
  let body = `<h1>整批${mode==="summary"?"梳理结果":"技术提取"}（${items.length} 集）</h1>`;
  items.forEach(it=>{
    body += `<h2>${esc(it.ep ? it.ep+". " : "")}${esc(it.title)}</h2>`;
    if(mode === "summary"){
      const sum = it.res.structured || it.res.clean_transcript || "（无梳理结果）";
      body += (window.marked ? marked.parse(sum) : `<pre>${esc(sum)}</pre>`);
    }else{
      body += buildTechHtml(it.res.tech);
    }
    body += "<hr/>";
  });
  if(printWindow((mode==="summary"?"整批梳理结果":"整批技术提取"), body))
    flashMsg("已生成整批打印视图（" + items.length + " 集）✓");
}

/* 整批查看页「一键导出」入口：按当前模式（batchViewMode）与所选格式导出合并文档 */
async function exportBatchCombined(fmt){
  const collected = collectBatchItems();
  if(!collected){ showErr("暂无可导出的整批内容（结果可能已因实例重启丢失，请重新解析后即时整批查看）"); return; }
  const { items, title } = collected;
  const mode = batchViewMode || "summary";
  try{
    if(fmt === "pdf"){ exportBatchPdf(items, mode, title); return; }
    if(fmt === "xlsx"){
      // 技术模式：把所有分集的技术行合并到一张表
      await ensureSheetJS();
      const aoa = [["分集","#","技术名称","主要作用","国内可用性","特别说明"]];
      items.forEach(it=>{
        const tech = it.res.tech || {};
        const techs = Array.isArray(tech.technologies) ? tech.technologies : [];
        const feats = Array.isArray(tech.features) ? tech.features : [];
        const rowPrefix = it.title;
        if(techs.length && typeof techs[0] === "object"){
          techs.forEach((t,i)=> aoa.push([rowPrefix, String(i+1), String(t.name||""), String(t.function||""), String(t.domestic||""), String(t.notes||"")]));
        }else if(techs.length){
          techs.forEach((t,i)=> aoa.push([rowPrefix, String(i+1), String(t), "", "", ""]));
        }
        feats.forEach(f=> aoa.push([rowPrefix, "功能", String(f), "", "", ""]));
      });
      const sheet = XLSX.utils.aoa_to_sheet(aoa);
      const wb = XLSX.utils.book_new();
      XLSX.utils.book_append_sheet(wb, sheet, "整批技术");
      const out = XLSX.write(wb, { bookType:"xlsx", type:"array" });
      downloadBlob(new Blob([out], { type:"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" }),
                   safeName(title || "整批技术提取", "技术提取", "xlsx"));
      flashMsg("已导出整批技术提取（" + items.length + " 集）✓");
      return;
    }
    // 文本类（md / txt）
    const label = mode === "summary" ? "梳理结果" : "技术提取";
    const parts = items.map(it=>{
      const head = `# ${it.ep ? it.ep + ". " : ""}${it.title}`;
      if(mode === "summary"){
        const sum = it.res.structured || it.res.clean_transcript || "（无梳理结果）";
        return head + "\n\n" + (fmt === "txt" ? lightMd(sum) : sum);
      }
      return head + "\n\n" + buildTechText(it.res.tech);
    });
    const content = parts.join("\n\n---\n\n");
    const ext = fmt, mime = fmt === "md" ? "text/markdown;charset=utf-8" : "text/plain;charset=utf-8";
    downloadBlob(new Blob([content], { type: mime }), safeName(title || ("整批"+label), label, ext));
    flashMsg("已导出本批（" + items.length + " 集）✓");
  }catch(e){
    showErr("整批导出失败：" + (e && e.message ? e.message : e));
  }
}
