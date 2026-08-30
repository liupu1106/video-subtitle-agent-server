/* ==========================================================================
   app.js —— 业务流程（提交 / 轮询 / 链接探测）与事件绑定、初始化
   最后加载：此时 core/api/export/views/nav 均已就绪，DOM 也已存在。
   ========================================================================== */

/* ---------------- 提交解析 ---------------- */
async function postProcess(){
  const url = $("url").value.trim();
  if(!/^https?:\/\//.test(url)){ showErr("请填写合法的 http(s) 链接"); return; }
  const apiKey = $("apikey").value.trim();
  const cookie = $("cookie").value.trim();
  const batch = $("batch").checked;
  let selected = [];
  if(batch){
    selected = [...document.querySelectorAll("#sellist input:checked")].map(c=>parseInt(c.getAttribute("data-index"),10));
    if(selected.length === 0){ showErr("请至少勾选一个分集再提交批量解析"); return; }
  }
  try{ localStorage.setItem("vsb_apikey", apiKey); localStorage.setItem("vsb_cookie", cookie); }catch(e){}

  $("go").disabled = true;
  $("err").style.display = "none";
  $("result").style.display = "none";
  $("batchList").style.display = "none";
  $("status").style.display = "block";
  $("stage").textContent = "提交任务中…";
  $("bar").style.width = "3%";
  $("log").innerHTML = "";
  setJobsRunning(true);

  let job;
  try{
    const res = await apiProcess({
      url, api_key: apiKey,
      asr_model: $("asrModel").value,
      llm_model: $("llmModel").value,
      fallback_model: $("fallbackModel").value,
      bili_cookie: cookie, batch, selected
    });
    job = res.batch_id || res.job_id;
    if(!job) throw new Error("提交失败：未返回任务标识");
  }catch(e){
    showErr(e.message); reset(); setJobsRunning(false); return;
  }

  poll(job);
  // 必须在 poll 之后：此时 currentJobId 已就绪，showPage 才能正确收起「暂无任务」空提示
  showPage("page-jobs");
}

/* ---------------- 轮询任务状态 ---------------- */
function poll(job){
  currentJobId = job; currentBatchId = job;
  timer = setInterval(async ()=>{
    try{
      const d = await apiFetchJob(job);
      if(d.kind === "batch"){
        // 始终用最新数据覆盖 lastBatchData：即便当前正在查看分集/整批详情、被下方守卫拦截不重渲染，
        // 返回时也能直接拿到最新进度（避免「进度条不动 / 没有查看按钮」）。
        lastBatchData = d;
        // 把各已完成子任务的结果写入前端缓存：整批查看 / 批量导出都直接读缓存，
        // 即使云托管实例重启或多副本轮询也能稳定拿到结果（与单视频缓存一致）。
        (d.children||[]).forEach(ch=>{ if(ch.status==="done" && ch.result) cachePut(ch.id, ch.result); });
        $("stage").textContent = d.stage || "";
        $("bar").style.width = (d.progress||0)+"%";
        $("log").innerHTML = (d.log||[]).map(l=>"<div>"+esc(l)+"</div>").join("");
        $("log").scrollTop = $("log").scrollHeight;
        if(d.status==="done"){ clearInterval(timer); reset(); setJobsRunning(false); }
        // 正在查看某个分集 / 整批详情时，不要覆盖当前正在展示的页面（否则点查看后会马上被轮询刷掉）
        if(viewingDetail) return;
        $("result").style.display = "none";
        renderBatch(d);
        return;
      }
      $("stage").textContent = d.stage || "";
      $("bar").style.width = (d.progress||0)+"%";
      $("log").innerHTML = (d.log||[]).map(l=>"<div>"+esc(l)+"</div>").join("");
      $("log").scrollTop = $("log").scrollHeight;
      if(d.status==="done"){ clearInterval(timer); cachePut(job, d.result); render(d.result); reset(); setJobsRunning(false); }
      else if(d.status==="error"){ clearInterval(timer); showErrMsg(d.error||"处理失败"); reset(); setJobsRunning(false); }
    }catch(e){ /* 忽略瞬时错误继续轮询 */ }
  }, 1500);
}

/* ---------------- 链接探测（自动识别可批量来源）---------------- */
function checkResolve(){
  const u = $("url").value.trim();
  const box = $("batch"), hint = $("batchHint");
  if(!/^https?:\/\//.test(u)){
    box.disabled = true; box.checked = false;
    hint.textContent = "粘贴番剧整季或视频选集（多P）链接后，此处自动可用，将逐集处理并可在下方逐集导出";
    $("selPanel").style.display = "none";
    return;
  }
  hint.textContent = "正在探测链接…";
  apiResolve(u).then(d=>{
    if(d.is_multi){
      box.disabled = false;
      box.checked = true;
      const what = d.kind==="bangumi" ? "整季" : "选集";
      hint.textContent = `检测到${what}共 ${d.count} 个分集，已自动勾选整集批量解析（可取消）`;
      buildSellist(d.items||[], d.kind);
    }else{
      box.disabled = true; box.checked = false;
      $("selPanel").style.display = "none";
      hint.textContent = d.kind==="video"
        ? "该视频为单P，无需批量；直接提交即可处理单集"
        : "该链接不是番剧整季或视频选集，批量不可用（单视频直接提交即可）";
    }
  }).catch(()=>{
    box.disabled = true; box.checked = false;
    $("selPanel").style.display = "none";
    hint.textContent = "链接探测失败，批量不可用；可忽略此项直接提交单视频";
  });
}

/* ==========================================================================
   事件绑定
   ========================================================================== */
$("go").onclick = postProcess;
document.querySelectorAll(".tab").forEach(t=>{ t.onclick = ()=> showPane(t.getAttribute("data-tab")); });

/* 返回按钮（顶部 sticky + 底部各一个） */
$("backBatch").onclick = backToBatchList;
{ const b = $("backBatch2"); if(b) b.onclick = backToBatchList; }
$("backBatchView").onclick = backFromBatchView;
{ const b = $("backBatchView2"); if(b) b.onclick = backFromBatchView; }

/* 批量勾选 */
$("url").addEventListener("input", ()=>{ clearTimeout(resolveTimer); resolveTimer = setTimeout(checkResolve, 600); });
$("selAll").onclick = ()=>{ document.querySelectorAll("#sellist input").forEach(c=>c.checked=true); updateSelCount(); };
$("selInv").onclick = ()=>{ document.querySelectorAll("#sellist input").forEach(c=>c.checked=!c.checked); updateSelCount(); };
// 单个复选框变化时实时刷新「已选 x/X」（事件委托，覆盖动态生成的复选框）
$("sellist").addEventListener("change", updateSelCount);

/* 整视频一键导出 */
$("exportAll").onclick = ()=>{ if(currentResult) exportAll(currentResult, currentJobId); else showErr("结果尚未加载"); };

/* 分区导出：优先用浏览器本地缓存的结果在本地生成文件，缓存缺失时回退服务端导出 */
document.querySelectorAll("#result .exptoolbar button[data-f]").forEach(b=>{
  b.onclick = async ()=>{
    if(!currentJobId) return;
    const sec = b.getAttribute("data-sec"), f = b.getAttribute("data-f");
    const res = cacheGet(currentJobId);
    if(res){
      try{ await clientExport(res, sec, f); }
      catch(e){ showErr("导出失败：" + (e && e.message ? e.message : e)); }
      return;
    }
    try{
      if(await serverExport(currentJobId, sec, f)) return;
    }catch(e){}
    showErr("该任务结果已不可用（云托管实例重启导致内存状态丢失）。建议：①在云托管「配置」中将最小实例数设为 1；②重新提交解析后即时导出；③或保持本页面不刷新直接导出（本页已缓存结果）。");
  };
});

/* 分区复制 */
document.querySelectorAll("#result .exptoolbar button.cp").forEach(b=>{
  b.onclick = ()=>{
    const sec = b.getAttribute("data-sec");
    let txt = "";
    if(sec==="raw") txt = currentRawText;
    else if(sec==="summary") txt = currentMd;
    else if(sec==="tech") txt = techText;
    else if(sec==="bilingual"){
      const lines = ["原文\t译文"];
      for(const it of (currentBilingual||[])) lines.push((it.orig||"").trim()+"\t"+(it.trans||"").trim());
      txt = lines.join("\n");
    }
    if(!txt){ return; }
    navigator.clipboard.writeText(txt).then(()=>{
      const t = b.textContent; b.textContent = "已复制 ✓"; setTimeout(()=>b.textContent=t, 1500);
    });
  };
});

/* ---------------- 初始化：拉取可用模型填充下拉框 ---------------- */
async function fillModelSelects(){
  // 给单个 select 追加选项（保留首个「自动/不指定」占位）
  function fill(sel, items){
    const first = sel.options[0];
    sel.innerHTML = "";
    sel.appendChild(first);
    (items||[]).forEach(m=>{
      const o = document.createElement("option");
      o.value = m; o.textContent = m;
      sel.appendChild(o);
    });
  }
  try{
    const d = await fetch("/api/models").then(r=>r.json());
    fill($("asrModel"), d.asr);
    fill($("llmModel"), d.llm);
    // 替代模型：跨用途并集（语音 + 文本），让用户指定一个统一备用
    const fb = Array.from(new Set((d.asr||[]).concat(d.llm||[])));
    fill($("fallbackModel"), fb);
  }catch(e){ /* 接口不可达时下拉框保留占位，仍可正常提交 */ }
}

try{
  const k = localStorage.getItem("vsb_apikey"); if(k) $("apikey").value = k;
  const c = localStorage.getItem("vsb_cookie"); if(c) $("cookie").value = c;
}catch(e){}
fillModelSelects();
// 统一走一次 showPage，让各页「空状态 / 内容块」的显隐保持一致
showPage("page-new");
