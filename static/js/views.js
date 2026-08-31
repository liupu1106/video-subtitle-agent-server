/* ==========================================================================
   views.js —— 渲染与视图切换
   只负责「把数据画出来」和「该显示哪个视图」，不直接发业务请求。
   ========================================================================== */

/* 安全解析 markdown：marked 异常时降级为纯文本，避免单条内容渲染失败导致整页打不开 */
function safeParse(md){
  try{ return window.marked ? marked.parse(md) : esc(md); }
  catch(e){ return esc(md); }
}

/* ---------------- 单视频结果 ---------------- */
function render(res){
  try{
    // 标记「当前正在展示的结果」：一键导出、结果页空状态判断都依赖它
    currentResult = res;
    const lang = res.subtitle_lang || "-";
    const dur = res.duration ? Math.round(res.duration/60)+" 分钟" : "-";
    const mSrc = res.method === "subtitle"
      ? '<span class="badge sub">字幕提取</span>'
      : '<span class="badge asr">语音转写</span>';
    const mAI = res.used_llm
      ? '<span class="badge llm">AI 梳理</span>'
      : '<span class="badge rule">规则降级</span>';
    $("meta").innerHTML =
      `<span class="tag"><b>标题：</b>${esc(res.title||"-")}</span>`+
      `<span class="tag"><b>来源：</b>${esc(res.platform||"-")}</span>`+
      mSrc + mAI +
      `<span class="tag"><b>语言：</b>${esc(lang)}</span>`+
      `<span class="tag"><b>时长：</b>${dur}</span>`;

    // 1) 梳理结果
    currentMd = String(res.structured || "");
    $("md").innerHTML = (window.marked ? marked.parse(currentMd) : esc(currentMd));
    // 重置「理解视图」状态：避免上一个视频的图残留到新视频
    currentViz = null;
    (function(){
      const vb = $("summaryViz"); if(vb){ vb.innerHTML = ""; vb.style.display = "none"; }
      const gv = $("genVizBtn"); if(gv){ gv.disabled = false; gv.textContent = "📊 生成理解视图"; }
      const hv = $("hideVizBtn"); if(hv){ hv.style.display = "none"; hv.textContent = "🙈 隐藏视图"; }
      const vh = $("vizHint"); if(vh){ vh.style.display = ""; }
      hideVizExports();
    })();

    // 2) 原始字幕（分段、带序号，易读）
    const segs = (res.raw_segments && res.raw_segments.length)
      ? res.raw_segments
      : (res.raw_transcript||"").split(/\n{2,}/).map(s=>s.trim()).filter(Boolean);
    currentRawText = segs.join("\n\n");
    if(segs.length){
      $("raw").innerHTML = segs.map((s,i)=>
        `<div class="rawseg"><div class="segn">${i+1}</div><div class="segc">${esc(s)}</div></div>`).join("");
    }else{
      $("raw").innerHTML = '<div class="rawseg"><div class="segn">—</div><div class="segc">（无原始字幕）</div></div>';
    }

    // 3) 中英文对照
    currentBilingual = res.bilingual || null;
    if(currentBilingual && currentBilingual.length){
      renderBilingual(currentBilingual, res.bilingual_lang || "en");
    }else{
      $("bilingView").innerHTML = '<div class="raw">（无中英文对照；需填写 API Key 经 AI 翻译后生成）</div>';
    }

    // 4) 技术提取
    renderTech(res.tech);
    showPane("raw");
    // 绑定「理解视图」按钮（每次渲染用 .onclick 重新赋值，幂等不重复绑定）
    const gv = $("genVizBtn");
    if(gv) gv.onclick = generateSummaryViz;
    const hv = $("hideVizBtn");
    if(hv) hv.onclick = ()=>{
      const b = $("summaryViz"); if(!b) return;
      const show = (b.style.display === "none");
      b.style.display = show ? "block" : "none";
      hv.textContent = show ? "🙈 隐藏视图" : "👁 显示视图";
      // 若展开但没有内容（之前隐藏时可能被清空），用缓存的 spec 重渲染
      if(show && currentViz && !b.querySelector(".vizsvg") && !b.querySelector(".vizerr") && !b.querySelector(".vizloading"))
        renderSummaryViz(currentViz);
    };
    const vp = $("vizPngBtn"); if(vp) vp.onclick = ()=>downloadViz("png");
    const vs = $("vizSvgBtn"); if(vs) vs.onclick = ()=>downloadViz("svg");
    const vm = $("vizMmdBtn"); if(vm) vm.onclick = ()=>downloadViz("mmd");
  }catch(e){
    // 任一处渲染异常都不应让整页变空白：原样展示报错并保留批量列表入口
    showErr("结果渲染失败：" + (e && e.message ? e.message : e) + "（结果数据可能不完整，建议重新解析该视频）");
    return;
  }
  // 内容构建成功后才切换显示，避免出现「点了查看整页消失」的空白
  $("status").style.display = "none";
  $("batchList").style.display = "none";
  // 关键：从「整批查看」页再点进去看单个视频时，必须把整批页收起来，
  // 否则两个视图叠加显示，返回后整批页仍盖在列表上，看起来像「回不去」。
  $("batchView").style.display = "none";
  $("result").style.display = "block";
  $("backBatch").style.display = fromBatch ? "block" : "none";
  const bb2 = $("backBatch2"); if(bb2) bb2.style.display = fromBatch ? "block" : "none";
  showPage("page-result");
}

/* 中英文对照：逐段「配对卡片」布局（原文/译文左右成组，序号徽标对齐，hover 高亮），
   便于逐句对照阅读；同时兼容 bilingual 为字符串列表 / 字符串 / 键名不同（text/translation 等）的非常规结构。 */
function renderBilingual(list, lang){
  const origLbl = lang === "en" ? "英文（原文）" : "中文（原文）";
  const transLbl = lang === "en" ? "中文（译文）" : "英文（译文）";
  const items = (Array.isArray(list) ? list : (typeof list === "string" ? list.split("\n").map(s=>s.trim()).filter(Boolean) : [])).map((it,i)=>{
    if(typeof it === "string") return {o: it.trim(), t: "", n: i+1};
    return {o:(it.orig||it.text||it.source||"").trim(), t:(it.trans||it.translation||it.target||"").trim(), n: i+1};
  }).filter(x=>x.o || x.t);
  if(!items.length){
    $("bilingView").innerHTML = '<div class="raw">（无中英文对照内容）</div>';
    return;
  }
  const pairs = items.map(x=>
    `<div class="biling-pair">`
    + `<div class="bil-card bil-orig"><span class="biln">${x.n}</span><p>${esc(x.o) || '<span class="muted">（无原文）</span>'}</p></div>`
    + `<div class="bil-card bil-trans"><span class="biln">${x.n}</span><p>${esc(x.t) || '<span class="muted">（无译文）</span>'}</p></div>`
    + `</div>`).join("");
  $("bilingView").innerHTML =
    `<div class="biling-head"><span class="bilhl">${origLbl}</span><span class="bilhl">${transLbl}</span></div>` + pairs;
}

function domClassOf(str){
  return /不支持/.test(str) ? "bad-no" : (/支持/.test(str) ? "bad-yes" : "bad-unk");
}

function buildTechHtml(tech){
  if(!tech) return '<div class="raw">未生成技术提取（需填写 API Key 并经 AI 梳理）。</div>';
  const feats = Array.isArray(tech.features) ? tech.features : [];
  const techs = Array.isArray(tech.technologies) ? tech.technologies : [];
  const isNew = techs.length && typeof techs[0] === "object";
  let h = "";
  if(isNew){
    const copy = ["【视频提及的技术】"];
    techs.forEach((t,i)=>{
      const name = t.name || ("技术"+(i+1));
      const fn = (t.function||"").trim();
      const dom = (t.domestic||"").trim();
      const notes = (t.notes||"").trim();
      const dc = domClassOf(dom);
      let card = `<div class="techsec"><h3>${i+1}. ${esc(name)}</h3>`;
      if(fn)  card += `<div class="techline"><span class="tlbl">主要作用</span><span>${esc(fn)}</span></div>`;
      if(dom) card += `<div class="techline"><span class="tlbl">国内使用</span><span class="dombox ${dc}" style="display:inline-block;padding:6px 12px">${esc(dom)}</span></div>`;
      if(notes) card += `<div class="techline"><span class="tlbl">特别说明</span><span class="note">${esc(notes)}</span></div>`;
      card += `</div>`;
      h += card;
      copy.push(`${i+1}. ${name}`);
      if(fn) copy.push(`   主要作用：${fn}`);
      if(dom) copy.push(`   国内使用：${dom}`);
      if(notes) copy.push(`   特别说明：${notes}`);
    });
    copy.push("\n【主要功能】");
    copy.push(feats.length ? feats.map((x,i)=>`${i+1}. ${x}`).join("\n") : "(无)");
    techText = copy.join("\n");
  }else{
    h += '<div class="techsec"><h3>🔧 视频提及的技术</h3>';
    h += techs.length ? "<ul>"+techs.map(x=>`<li>${esc(x)}</li>`).join("")+"</ul>"
                      : '<p class="muted">未提取到明确技术。</p>';
    h += "</div>";
    techText = "【视频提及的技术】\n" + (techs.length ? techs.map((x,i)=>`${i+1}. ${x}`).join("\n") : "(无)");
  }
  h += '<div class="techsec"><h3>⚙️ 视频整体主要功能</h3>';
  h += feats.length ? "<ul>"+feats.map(x=>`<li>${esc(x)}</li>`).join("")+"</ul>"
                    : '<p class="muted">未提取到明确功能。</p>';
  h += "</div>";
  return h;
}

function renderTech(tech){ $("tech").innerHTML = buildTechHtml(tech); }

/* ---------------- 理解视图（根据梳理内容自动选图型，Mermaid 渲染） ---------------- */
let currentViz = null;   // 当前视频已生成的可视化 spec，切 tab / 隐藏后再显示时复用

/* 动态加载 Mermaid（仅一次）；CDN 不可达时回调 err，前端降级展示原始定义 */
function ensureMermaid(cb){
  if(window.__vizMermaid){ cb(window.__vizMermaid, null); return; }
  if(window.__vizMermaidLoading){ setTimeout(()=>ensureMermaid(cb), 300); return; }
  window.__vizMermaidLoading = true;
  const s = document.createElement("script");
  s.src = "https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js";
  s.onload = ()=>{
    try{
      window.__vizMermaid = window.mermaid;
      window.__vizMermaid.initialize({ startOnLoad:false, securityLevel:"loose" });
    }catch(e){}
    window.__vizMermaidLoading = false;
    cb(window.__vizMermaid || null, null);
  };
  s.onerror = ()=>{ window.__vizMermaidLoading = false; cb(null, new Error("Mermaid 脚本加载失败（需联网）")); };
  document.head.appendChild(s);
}

/* 点击「生成理解视图」：把当前梳理内容发给后端，按情境自动选图 */
function generateSummaryViz(){
  const btn = $("genVizBtn"), box = $("summaryViz"), hint = $("vizHint");
  const md = String((currentResult && currentResult.structured) || "").trim();
  if(!md){ showErr("当前没有可可视化的梳理内容（该视频可能未用 AI 梳理，或内容为空）"); return; }
  let apiKey = "";
  try{ apiKey = localStorage.getItem("vsb_apikey") || ""; }catch(e){}
  // 不在此拦截：若 localStorage 为空但服务端配了 SERVER_API_KEY，后端仍可用；
  // 缺 Key 时由后端返回 400 并提示，避免「已配 Key 却误报未填」。
  btn.disabled = true; btn.textContent = "⏳ 生成中…";
  hint.style.display = "none";
  hideVizExports();
  box.style.display = "block";
  box.innerHTML = '<div class="vizloading">🧠 正在根据内容生成最适合的理解视图…</div>';
  const payload = { text: md, title: (currentResult && currentResult.title) || "", api_key: apiKey };
  fetch("/api/visualize", { method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify(payload) })
    .then(r => r.json().then(d => ({ ok:r.ok, d })))
    .then(({ ok, d })=>{
      if(!ok) throw new Error(d.detail || "生成失败");
      currentViz = d;
      renderSummaryViz(d);
      btn.disabled = false; btn.textContent = "🔄 重新生成视图";
      $("hideVizBtn").style.display = "";
      showVizExports();
    })
    .catch(err=>{
      btn.disabled = false; btn.textContent = "📊 生成理解视图";
      box.style.display = "block";
      box.innerHTML = '<div class="vizerr">⚠️ 生成失败：' + esc(err.message || String(err)) + '</div>';
    });
}

/* 把后端返回的 spec 渲染成图；失败则降级展示原始 Mermaid 文本，绝不丢功能 */
function renderSummaryViz(d){
  const box = $("summaryViz");
  if(!box) return;
  const title = d.title || "理解视图";
  const cap = d.caption || "";
  ensureMermaid((mermaid, err)=>{
    if(err || !mermaid){
      box.innerHTML = '<div class="vizerr">⚠️ 图表库加载失败（需联网加载 Mermaid）：' + esc(err ? err.message : "") + '</div>'
        + '<pre class="vizraw">' + esc(d.mermaid || "") + '</pre>';
      return;
    }
    try{
      const id = "viz" + Date.now();
      mermaid.render(id, d.mermaid).then(({ svg })=>{
        box.innerHTML = '<div class="vizhead">📈 ' + esc(title) + ' <span class="viztype">[' + esc(d.type || "") + ']</span></div>'
          + (cap ? '<div class="vizcap">' + esc(cap) + '</div>' : '')
          + '<div class="vizsvg">' + svg + '</div>';
      }).catch(e=>{
        box.innerHTML = '<div class="vizerr">⚠️ 图表渲染失败（模型生成的 Mermaid 语法可能有误）：' + esc(e && e.message ? e.message : String(e)) + '</div>'
          + '<pre class="vizraw">' + esc(d.mermaid || "") + '</pre>';
      });
    }catch(e){
      box.innerHTML = '<div class="vizerr">⚠️ 图表渲染异常：' + esc(String(e)) + '</div><pre class="vizraw">' + esc(d.mermaid || "") + '</pre>';
    }
  });
}

/* 生成成功后显示导出按钮（PNG / SVG / Mermaid源）；隐藏视图时按钮保留 */
function showVizExports(){
  ["vizDlSep","vizPngBtn","vizSvgBtn","vizMmdBtn"].forEach(id=>{
    const el = $(id); if(el) el.style.display = "";
  });
}
function hideVizExports(){
  ["vizDlSep","vizPngBtn","vizSvgBtn","vizMmdBtn"].forEach(id=>{
    const el = $(id); if(el) el.style.display = "none";
  });
}

/* 一键导出视图：kind ∈ png | svg | mmd；纯前端实现，无需后端 */
function downloadViz(kind){
  const box = $("summaryViz");
  const svgEl = box ? box.querySelector(".vizsvg svg") : null;
  const safeTitle = ((currentResult && currentResult.title) || currentViz && currentViz.title || "理解视图")
    .replace(/[\\/:*?"<>|\n\r]+/g, "_").slice(0, 40);
  const typeTag = currentViz && currentViz.type ? currentViz.type : "viz";
  const base = safeTitle + "-理解视图-" + typeTag;
  if(kind === "mmd"){
    if(!currentViz || !currentViz.mermaid){ alert("尚无可下载的视图（请先生成理解视图）"); return; }
    triggerDownloadBlob(new Blob([currentViz.mermaid], {type:"text/plain;charset=utf-8"}), base + ".mmd");
    return;
  }
  // png / svg 都需要渲染出的 <svg>
  if(!svgEl){ alert("当前视图为降级文本（图表库未加载或 Mermaid 语法有误），无法导出图片；可下载「Mermaid源」后用 Mermaid Live Editor 打开。"); return; }
  const xml = new XMLSerializer().serializeToString(svgEl);
  if(kind === "svg"){
    const out = '<?xml version="1.0" encoding="UTF-8" standalone="no"?>\n' + xml;
    triggerDownloadBlob(new Blob([out], {type:"image/svg+xml;charset=utf-8"}), base + ".svg");
    return;
  }
  // png：SVG -> Blob -> Image -> Canvas(白底, 2x) -> toBlob
  const svgBlob = new Blob([xml], {type:"image/svg+xml;charset=utf-8"});
  const url = URL.createObjectURL(svgBlob);
  const img = new Image();
  img.onload = function(){
    const vb = svgEl.viewBox && svgEl.viewBox.baseVal;
    const w = vb && vb.width ? vb.width : (svgEl.width && svgEl.width.baseVal && svgEl.width.baseVal.value) || svgEl.clientWidth || 800;
    const h = vb && vb.height ? vb.height : (svgEl.height && svgEl.height.baseVal && svgEl.height.baseVal.value) || svgEl.clientHeight || 600;
    const scale = 2;
    const canvas = document.createElement("canvas");
    canvas.width = Math.round(w*scale); canvas.height = Math.round(h*scale);
    const ctx = canvas.getContext("2d");
    ctx.fillStyle = "#ffffff"; ctx.fillRect(0,0,canvas.width,canvas.height);
    ctx.scale(scale, scale);
    ctx.drawImage(img, 0, 0, w, h);
    URL.revokeObjectURL(url);
    canvas.toBlob(function(b){
      if(!b){ alert("PNG 导出失败，可改下载 SVG 或 Mermaid源"); return; }
      triggerDownloadBlob(b, base + ".png");
    }, "image/png");
  };
  img.onerror = function(){ URL.revokeObjectURL(url); alert("PNG 导出失败，可改下载 SVG 或 Mermaid源"); };
  img.src = url;
}

function triggerDownloadBlob(blob, filename){
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url; a.download = filename;
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(()=>URL.revokeObjectURL(url), 4000);
}

function showPane(tab){
  activeTab = tab;
  document.querySelectorAll(".tab").forEach(x=>x.classList.toggle("active", x.getAttribute("data-tab")===tab));
  ["raw","summary","tech","biling"].forEach(p=>{ $("pane-"+p).style.display = (p===tab)?"block":"none"; });
}

/* ---------------- 批量列表 ---------------- */
function renderBatch(d){
  lastBatchData = d;
  $("batchList").style.display = "block";
  const doneCnt = (d.children||[]).filter(c=>c.status==="done").length;
  const bar = `<div class="batchbar">
      <button id="ovSummaryBtn">📋 整批查看梳理结果</button>
      <button id="ovTechBtn">🔧 整批查看技术提取</button>
      <button class="ghost" id="ovExportAllBtn">⬇ 整批一键导出全部</button>
      <span class="ckhint">已完成 ${doneCnt} / ${d.children ? d.children.length : 0} 集；可分别查看所有视频的梳理整合与技术整合</span>
    </div>`;
  const rows = (d.children||[]).map(ch=>{
    const st = ch.status;
    const badge = st==="done" ? '<span class="badge sub">完成</span>'
               : st==="error" ? '<span class="badge asr">失败</span>'
               : '<span class="badge rule">处理中</span>';
    const reason = ch.error || "";
    const isNoKey = /DASHSCOPE_API_KEY|语音转写|转写|ASR/.test(reason);
    const failNote = (st==="error" && reason)
      ? `<span class="bfail" title="${esc(reason)}">${isNoKey ? "缺 API Key：该集无字幕需语音转写" : "失败：" + esc(reason.slice(0,48))}</span>`
      : "";
    let actions = "";
    if(st==="done"){
      actions = `<span class="viewlink" data-id="${ch.id}" data-act="view">查看</span>`
              + `<span class="viewlink" data-id="${ch.id}" data-act="exp">导出</span>`;
    }
    return `<div class="brow">
      <span class="bno">${ch.ep_no||""}</span>
      <div class="binfo">
        <span class="btitle" title="${esc(ch.title||"")}">${esc(ch.title||"")}</span>
        ${failNote}
      </div>
      <span class="bbadge">${badge}</span>
      <span class="bprog">${ch.progress||0}%</span>
      ${actions}
    </div>`;
  }).join("");
  $("batchList").innerHTML = bar + rows;
  $("ovSummaryBtn").onclick = ()=> openBatchSummary(d.children||[]);
  $("ovTechBtn").onclick = ()=> openBatchTech(d.children||[]);
  $("ovExportAllBtn").onclick = ()=> exportAllBatch(d.children||[]);
  document.querySelectorAll("#batchList .viewlink").forEach(a=>{
    a.onclick = ()=>{
      const id = a.getAttribute("data-id"), act = a.getAttribute("data-act");
      if(act==="view") openChild(id);
      else {
        const res = cacheGet(id);
        if(!res){ showErr("该分集结果已不可用（云托管实例重启导致内存状态丢失），请重新解析后即时操作"); return; }
        if(act==="exp") exportAll(res, id);
      }
    };
  });
}

/* 从服务端重新拉取当前批量任务并渲染列表（返回分集/整批详情后调用，保证拿到最新进度与已完成结果） */
async function refreshCurrentBatch(){
  if(!currentBatchId){ if(lastBatchData) renderBatch(lastBatchData); return; }
  try{
    const d = await apiFetchJob(currentBatchId);
    if(d && d.kind === "batch"){ lastBatchData = d; renderBatch(d); return; }
  }catch(e){}
  if(lastBatchData) renderBatch(lastBatchData);  // 拉取失败兜底：用最近一次缓存数据渲染
}

/* 查看某个分集：优先读本地缓存，避免依赖易失效的服务端内存状态 */
async function openChild(id){
  fromBatch = true; currentJobId = id; viewingDetail = true;
  let cached = cacheGet(id);
  if(cached){ currentResult = cached; render(cached); return; }
  try{
    const d = await apiFetchJob(id);
    if(d.status==="error"){ showErrMsg(d.error||"该分集处理失败"); return; }
    if(d.result){ cachePut(id, d.result); currentResult = d.result; render(d.result); }
    else if((cached = cacheGet(id))){ currentResult = cached; render(cached); }
    else { showErr("该分集尚未完成，暂无法查看"); }
  }catch(e){
    if((cached = cacheGet(id))){ currentResult = cached; render(cached); }
    else showErr("加载分集失败");
  }
}

/* 返回「任务进度」：result（单视频）与 batchView（整批查看）都必须收起，
   否则其中一个会残留在页面上盖住列表，看起来就像「返回按钮失灵」。 */
function backToBatchList(){
  fromBatch = false; viewingDetail = false;
  $("result").style.display = "none";
  $("batchView").style.display = "none";
  $("batchList").style.display = "block";
  $("status").style.display = "block";
  showPage("page-jobs");
  refreshCurrentBatch();
}

/* ---------------- 整批查看 ---------------- */
/* 整批查看的数据来源与单视频「查看」同源：直接读前端缓存（localStorage），
   解析完成的轮询会把每个已完成子任务的结果写入缓存，因此可即时整合，
   不再依赖易因多副本/缩容失败的同步服务端拉取。仅对缓存缺失的子任务后台补齐。 */
async function fillMissingBatch(list, onEach){
  // 并行补齐缺失子任务；每个完成后回调 onEach(ch) 触发渐进渲染。
  // 不使用 Promise.allSettled：老版本 Safari（<13.1）不支持该 API，调用即抛 TypeError，
  // 会导致整批补齐逻辑中断。改用全版本兼容的 forEach + async IIFE + .catch 兜底。
  // 超时放宽到 25s：云托管冷启动可能耗时数秒~20s，5s 会误杀导致永远补不上。
  (list||[]).forEach((ch)=>{
    (async ()=>{
      if(ch.result || cacheGet(ch.id)) return;
      const ctrl = new AbortController();
      const to = setTimeout(()=>ctrl.abort(), 25000);
      try{
        const d = await apiFetchJob(ch.id, ctrl.signal);
        if(d && d.result){ cachePut(ch.id, d.result); }
      }catch(e){} finally{ clearTimeout(to); }
      if(typeof onEach === "function") onEach(ch);
    })().catch(()=>{});
  });
}

/* 直接从缓存构建整批 HTML。mode: "summary" | "tech" */
function buildBatchHtml(list, mode){
  let html = "", avail = 0;
  for(const ch of list){
    const res = ch.result || cacheGet(ch.id);
    const head = `<h3 class="ovh">${esc(ch.ep_no||"")}. ${esc(ch.title||"")}<span class="ovtag">${esc((res&&res.platform)||"")}</span></h3>`;
    if(!res){
      html += `<div class="ovsec">${head}<div class="ovunavail">👉 该集结果暂未加载：请先点击该视频的【查看】，或重新解析后即时整批查看。</div></div>`;
      continue;
    }
    avail++;
    try{
      if(mode === "summary"){
        const sum = res.structured || res.clean_transcript || "（无梳理结果）";
        html += `<div class="ovsec">${head}`
              + `<div style="font-size:13px;color:var(--sub);margin:6px 0 4px">📋 梳理结果</div>`
              + `<div class="ovsub">${safeParse(sum)}</div></div>`;
      }else{
        html += `<div class="ovsec">${head}`
              + `<div style="font-size:13px;color:var(--sub);margin:6px 0 4px">🔧 技术提取</div>`
              + `<div class="ovtech">${buildTechHtml(res.tech)}</div></div>`;
      }
    }catch(e){
      html += `<div class="ovsec">${head}<div class="ovunavail">（该集渲染失败）</div></div>`;
    }
  }
  return { html, avail };
}

/* 统一切到「整批查看」页：收起其余所有视图，避免多个视图叠加残留 */
function showBatchView(){
  $("status").style.display = "none";
  $("batchList").style.display = "none";
  $("result").style.display = "none";
  $("batchView").style.display = "block";
  const b = $("backBatchView2"); if(b) b.style.display = "block";
  showPage("page-batch");
}

/* 填充整批查看页的导出工具条：根据当前模式给出对应格式的一键导出按钮 */
function fillBatchViewToolbar(mode){
  const tb = $("batchViewToolbar");
  if(!tb) return;
  const lbl = mode === "summary" ? "导出本批（合并·梳理）：" : "导出本批（合并·技术）：";
  const btns = mode === "summary"
    ? `<button data-bf="md">MD</button><button data-bf="txt">TXT</button><button data-bf="pdf">PDF</button>`
    : `<button data-bf="txt">TXT</button><button data-bf="xlsx">Excel</button><button data-bf="pdf">PDF</button>`;
  tb.innerHTML = `<span class="explbl">${lbl}</span>${btns}`;
  tb.querySelectorAll("button[data-bf]").forEach(b=>{
    b.onclick = ()=> exportBatchCombined(b.getAttribute("data-bf"));
  });
}

function openBatchSummary(children){
  const list = children || (lastBatchData && lastBatchData.children) || [];
  if(!list.length){ showErr("暂无可查看的整批视频"); return; }
  viewingDetail = true;
  batchViewMode = "summary";
  fillBatchViewToolbar("summary");
  const paint = ()=>{
    const r = buildBatchHtml(list, "summary");
    $("batchViewBody").innerHTML = r.html;
    $("batchViewTitle").textContent = `📋 整批查看 · 梳理结果（已加载 ${r.avail}/${list.length}）`;
  };
  paint();
  showBatchView();
  fillMissingBatch(list, paint);
}

function openBatchTech(children){
  const list = children || (lastBatchData && lastBatchData.children) || [];
  if(!list.length){ showErr("暂无可查看的整批视频"); return; }
  viewingDetail = true;
  batchViewMode = "tech";
  fillBatchViewToolbar("tech");
  const paint = ()=>{
    const r = buildBatchHtml(list, "tech");
    $("batchViewBody").innerHTML = r.html;
    $("batchViewTitle").textContent = `🔧 整批查看 · 技术提取（已加载 ${r.avail}/${list.length}）`;
  };
  paint();
  showBatchView();
  fillMissingBatch(list, paint);
}

/* 从整批查看返回：同样要把 result 一起收起，防止返回后残留遮挡 */
function backFromBatchView(){
  viewingDetail = false;
  $("batchView").style.display = "none";
  $("result").style.display = "none";
  $("batchList").style.display = "block";
  $("status").style.display = "block";
  showPage("page-jobs");
  refreshCurrentBatch();
}

/* ---------------- 批量勾选面板 ---------------- */
function buildSellist(items, kind){
  const list = $("sellist");
  list.innerHTML = items.map((it,i)=>{
    const num = kind==="video" ? (it.page||(i+1)) : (i+1);
    const dur = it.duration ? Math.round(it.duration/60)+"分" : "";
    return `<label class="selitem"><input type="checkbox" data-index="${i}" checked/>
      <span>#${num}</span><span class="seltitle" title="${esc(it.title||"")}">${esc(it.title||"")}</span>
      <span class="seldur">${dur}</span></label>`;
  }).join("");
  $("selPanel").style.display = "block";
  updateSelCount();
}

function updateSelCount(){
  const total = document.querySelectorAll("#sellist input").length;
  const n = document.querySelectorAll("#sellist input:checked").length;
  $("selInfo").textContent = `已选 ${n} / ${total}（默认全选）`;
}
