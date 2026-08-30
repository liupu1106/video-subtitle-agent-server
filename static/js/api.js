/* ==========================================================================
   api.js —— 后端 HTTP 接口封装（唯一与服务端对话的地方）
   所有函数统一返回 Promise，失败时抛出带中文信息的 Error。
   ========================================================================== */

/* 提交解析任务。payload: {url, api_key, model, bili_cookie, batch, selected}
   返回服务端 JSON：批量为 {batch_id,...}，单条为 {job_id,...} */
async function apiProcess(payload){
  const r = await fetch("/api/process", {
    method: "POST",
    headers: {"Content-Type":"application/json"},
    body: JSON.stringify(payload)
  });
  if(!r.ok){
    const e = await r.json().catch(()=>({detail:r.statusText}));
    throw new Error(e.detail || "提交失败");
  }
  return r.json();
}

/* 查询任务状态（单条或批量均走此接口） */
async function apiFetchJob(id, signal){
  const r = await fetch("/api/job/" + id, signal ? {signal} : undefined);
  return r.json().catch(()=>({}));
}

/* 探测链接是否可批量（番剧整季 / 多P 选集） */
async function apiResolve(url){
  const r = await fetch("/api/resolve?url=" + encodeURIComponent(url));
  return r.json().catch(()=>({kind:"unknown", is_multi:false, count:0, items:[]}));
}

/* 一键导出全部：优先用 job_id（请求体极小）让服务端取结果打包成 zip；
   仅在 job_id 不可用时（实例重启导致内存结果丢失）回退到 POST 完整 result。 */
async function apiExportAll(result, jobId){
  const body = jobId ? {job_id: jobId} : {result: result};
  let r;
  try {
    r = await fetch("/api/export-all", {
      method: "POST",
      headers: {"Content-Type":"application/json"},
      body: JSON.stringify(body)
    });
  } catch (e) {
    // 网络层失败（服务未响应 / 代理中断）：原生的 "Failed to fetch" / Safari "Load failed"
    // 对用户无信息量，统一转成可读提示。这是此前「一键导出失败：Load failed」的主因之一。
    throw new Error("导出请求失败：服务无响应，请确认服务正在运行或稍后重试");
  }
  if(!r.ok){
    const e = await r.json().catch(()=>({error:r.statusText}));
    // 服务端返回字段可能是 detail 或 error；优先展示真实错误信息而非笼统的 "HTTP 500"
    throw new Error(e.detail || e.error || ("HTTP " + r.status));
  }
  return r;
}

/* 回退到服务端导出单分区文件。返回 true 表示已触发下载 */
async function apiServerExport(jobId, sec, f){
  const r = await fetch("/api/job/" + jobId + "/export?section=" + sec + "&format=" + f);
  if(!r.ok) return false;
  const blob = await r.blob();
  if(blob.size === 0) return false;
  const disp = r.headers.get("Content-Disposition") || "";
  let fname = (disp.match(/filename\*=UTF-8''([^;]+)/) || [])[1];
  if(!fname) fname = (disp.match(/filename="?([^";]+)"?/) || [])[1] || ("subtitle_" + sec + "." + f);
  downloadBlob(blob, decodeURIComponent(fname));
  return true;
}
