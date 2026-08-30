/* ==========================================================================
   nav.js —— 左侧导航与页面切换
   页面容器为 <section class="page" id="page-xxx">，靠 .active 控制显隐。
   ========================================================================== */

function showPage(pageId){
  document.querySelectorAll(".page").forEach(p=>{
    p.classList.toggle("active", p.id === pageId);
  });
  document.querySelectorAll(".navitem").forEach(b=>{
    b.classList.toggle("active", b.getAttribute("data-page") === pageId);
  });
  syncPageContent(pageId);
}

/* 切回某个页面时，恢复该页内容块的可见性。
   为什么必须做：内容块（#result / #batchView）的 display 由业务逻辑控制，
   「返回任务进度」会把它们设成 none；而页面切换只切 .page。
   若不在进入时恢复，用户点「返回」后再从左侧导航切回来，
   页面显示了、内容块却还是 none —— 看起来就是「内容消失了」（其实还在 DOM 里）。 */
function syncPageContent(pageId){
  if(pageId === "page-result"){
    const has = !!currentResult;
    setDisp($("result"), has ? "block" : "none");
    setDisp($("resultEmpty"), has ? "none" : "block");
  }
  if(pageId === "page-batch"){
    const body = $("batchViewBody");
    const has = !!(body && String(body.innerHTML).trim());
    setDisp($("batchView"), has ? "block" : "none");
    setDisp($("batchEmpty"), has ? "none" : "block");
  }
  if(pageId === "page-jobs"){
    // 点左侧「任务进度」即「回到任务列表」，不再处于某个分集/整批详情视图，
    // 复位标志以恢复轮询对列表的实时刷新（与「返回任务进度」按钮行为一致）。
    viewingDetail = false;
    const has = !!(currentJobId || lastBatchData);
    if(has){
      // 关键修复：切回「任务进度」时必须复原进度/日志与分集列表的可见性，
      // 并收起可能仍残留隐藏的详情视图（整批查看 / 单集结果）。
      // 否则从「整批查看」或「查看单集」点左侧「任务进度」导航回来时，
      // 这些视图处于 display:none，而 syncPageContent 又不恢复它们，
      // 任务进度页看似「内容消失」（实际 DOM 还在）。
      setDisp($("jobsEmpty"), "none");
      setDisp($("batchView"), "none");
      setDisp($("result"), "none");
      if(lastBatchData && lastBatchData.kind === "batch"){
        // 批量任务：恢复进度/日志卡片与分集列表
        setDisp($("status"), "block");
        setDisp($("batchList"), "block");
      }else{
        // 单视频：结果已切到「结果预览」页；任务进度页保留最终进度/日志卡片，
        // 避免点导航回来时整页空白。
        setDisp($("status"), "block");
        setDisp($("batchList"), "none");
      }
    }else{
      // 确实没有任何任务：只显示空提示，其余一律收起
      setDisp($("jobsEmpty"), "block");
      setDisp($("status"), "none");
      setDisp($("batchList"), "none");
      setDisp($("batchView"), "none");
      setDisp($("result"), "none");
    }
  }
}
function setDisp(el, v){ if(el) el.style.display = v; }

/* 任务进行中时，在「任务进度」菜单上显示一个提示点 */
function setJobsRunning(running){
  const dot = $("navJobsDot");
  if(dot) dot.style.display = running ? "inline-block" : "none";
}

document.querySelectorAll(".navitem").forEach(b=>{
  b.onclick = ()=> showPage(b.getAttribute("data-page"));
});
