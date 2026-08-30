/* ==========================================================================
   core.js —— 全局状态、工具函数、结果缓存、错误提示
   最先加载；其余模块共享这里的全局函数与变量。
   ========================================================================== */
const $ = id => document.getElementById(id);

/* ---------------- 全局状态 ---------------- */
let timer = null, currentMd = "", currentJobId = "", currentBatchId = "";
let techText = "", currentBilingual = null;
let activeTab = "raw", fromBatch = false, lastBatchData = null;
let batchViewMode = "summary";   // 整批查看当前模式：summary | tech（合并导出按钮依赖它）
let viewingDetail = false;   // 正在查看单个分集/整批详情时为 true，阻止轮询覆盖当前页
let currentRawText = "";     // 原始字幕纯文本（供复制/导出，不含序号）
let currentResult = null;
let resolveTimer = null;

/* 结果缓存：任务完成后把 result 存在浏览器，之后「查看 / 导出」优先用缓存，
   这样即使云托管实例缩容到 0 或多副本轮询导致服务端内存状态丢失，也能稳定查看与导出。
   同时写入 localStorage，刷新页面后仍可命中（避免重新拉取已丢失的服务端状态）。 */
const resultCache = {};

function cachePut(id, res){
  resultCache[id] = res;
  // 刻意不在这里设置 currentResult：批量轮询会为每一个已完成子任务调用 cachePut，
  // 若在此赋值，「单视频结果」页会显示一个用户并未主动查看过的视频。
  // currentResult 只表示「当前正在展示的结果」，由 render() 设置。
  try{ localStorage.setItem("vsb_res_"+id, JSON.stringify(res)); }catch(e){}
}

function cacheGet(id){
  if(resultCache[id]) return resultCache[id];
  try{ const s = localStorage.getItem("vsb_res_"+id); if(s) return JSON.parse(s); }catch(e){}
  return null;
}

/* ---------------- 工具 ---------------- */
/* esc 必须对数字/null/undefined 都安全：真实后端返回的 ep_no 是数字(如 1)，
   早期版本直接对非字符串调用 .replace 会抛 TypeError，导致整页渲染崩溃变空白。 */
function esc(s){
  return (s == null ? "" : String(s)).replace(/[&<>]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));
}

function reset(){ $("go").disabled = false; }

/* 分区中文名（导出文件名 / 工具条文案共用） */
const SECTION_LABEL = {raw:"原始字幕", summary:"梳理结果", tech:"技术提取", bilingual:"中英文对照"};

/* ---------------- 错误提示 ---------------- */
function showErr(m){
  $("err").style.display = "block";
  $("err").textContent = "⚠ " + m;
}

function showErrMsg(m){
  // 针对「无 API Key 无法语音转写」给出明确指引，而非笼统显示失败
  if(/DASHSCOPE_API_KEY|语音转写|转写|ASR/.test(m || "")){
    $("err").innerHTML = '⚠ ' + esc(m) +
      '<br/><span style="color:var(--sub);font-size:13px;line-height:1.6">' +
      '该视频没有可提取的字幕，需要走语音转写。请在左侧「⚙️ 模型与密钥」填入你的 DashScope Key 后重新提交；' +
      '或在云托管「环境变量」配置 DASHSCOPE_API_KEY。带字幕的视频无需 Key 也能处理。</span>';
  }else{
    $("err").textContent = "⚠ " + m;
  }
  $("err").style.display = "block";
}

/* 工具条上的临时成功提示（3 秒后还原） */
function flashMsg(m){
  const t = $("allToolsHint");
  if(!t) return;
  const old = t.textContent;
  t.textContent = m; t.style.color = "var(--ok)";
  setTimeout(()=>{ t.textContent = old; t.style.color = ""; }, 3000);
}
