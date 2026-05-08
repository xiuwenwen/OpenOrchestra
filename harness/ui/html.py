from __future__ import annotations


def render_html() -> str:
    return r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>OpenOrchestra</title>
  <style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
    :root{--bg:#000000;--surface:#09090b;--elevated:#121214;--overlay:#1e1e24;--border:rgba(255,255,255,0.1);--border-muted:rgba(255,255,255,0.05);--text:#ededed;--muted:#a1a1aa;--subtle:#71717a;--accent:#6366f1;--accent-soft:rgba(99,102,241,.15);--good:#10b981;--good-soft:rgba(16,185,129,.15);--bad:#ef4444;--bad-soft:rgba(239,68,68,.15);--warn:#f59e0b;--warn-soft:rgba(245,158,11,.15);--info:#8b5cf6;--info-soft:rgba(139,92,246,.15);--radius:10px;--font:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif;--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
    *{box-sizing:border-box;margin:0}
    body{background:var(--bg);color:var(--text);font:14px/1.5 var(--font);overflow-x:hidden}
    ::selection{background:var(--accent);color:#fff}
    ::-webkit-scrollbar{width:6px;height:6px}
    ::-webkit-scrollbar-track{background:transparent}
    ::-webkit-scrollbar-thumb{background:var(--border);border-radius:3px}

    /* === HEADER === */
    .header{height:52px;display:flex;align-items:center;justify-content:space-between;padding:0 20px;border-bottom:1px solid var(--border);background:rgba(9,9,11,0.7);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);position:sticky;top:0;z-index:10}
    .header-left{display:flex;align-items:center;gap:14px;min-width:0}
    .logo{font-size:15px;font-weight:700;white-space:nowrap;background:linear-gradient(135deg,var(--accent),var(--info));-webkit-background-clip:text;-webkit-text-fill-color:transparent}
    .header-task{font-size:13px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:400px}
    .header-right{display:flex;align-items:center;gap:10px;flex-shrink:0}
    .seg{display:inline-flex;border:1px solid var(--border);border-radius:6px;overflow:hidden}
    .seg button{border:0;background:transparent;color:var(--muted);padding:4px 10px;font-size:12px;cursor:pointer;font-weight:600}
    .seg button.on{background:var(--accent);color:#fff}
    .seg button:hover:not(.on){background:var(--overlay)}
    #heartbeat{font-size:11px;color:var(--subtle);font-family:var(--mono)}

    /* === LAYOUT === */
    .shell{display:grid;grid-template-columns:260px 1fr;height:calc(100vh - 52px)}
    .sidebar{border-right:1px solid var(--border);background:var(--surface);overflow-y:auto;padding:12px}
    .sidebar h2{font-size:12px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin-bottom:10px;font-weight:700}
    .content{overflow-y:auto;display:flex;flex-direction:column;gap:0}

    /* === TASK LIST === */
    .tsk{width:100%;text-align:left;background:transparent;border:1px solid transparent;border-radius:8px;padding:8px 10px;cursor:pointer;color:var(--text);margin-bottom:4px;transition:all .15s}
    .tsk:hover{background:var(--elevated);border-color:var(--border)}
    .tsk.act{background:var(--accent-soft);border-color:var(--accent)}
    .tsk-top{display:flex;justify-content:space-between;align-items:center;gap:6px}
    .tsk-id{font-family:var(--mono);font-size:12px;font-weight:600}
    .tsk-prompt{font-size:12px;color:var(--muted);margin-top:3px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}

    /* === STATUS PILLS === */
    .pill{display:inline-flex;align-items:center;gap:4px;padding:2px 8px;border-radius:99px;font-size:11px;font-weight:700;letter-spacing:.02em}
    .pill::before{content:'';width:6px;height:6px;border-radius:50%;flex-shrink:0}
    .pill.RUNNING{background:var(--accent-soft);color:var(--accent)}.pill.RUNNING::before{background:var(--accent);animation:blink 1.5s infinite}
    .pill.COMPLETED{background:var(--good-soft);color:var(--good)}.pill.COMPLETED::before{background:var(--good)}
    .pill.FAILED,.pill.TIMEOUT{background:var(--bad-soft);color:var(--bad)}.pill.FAILED::before,.pill.TIMEOUT::before{background:var(--bad)}
    .pill.OUTPUT_INVALID{background:var(--warn-soft);color:var(--warn)}.pill.OUTPUT_INVALID::before{background:var(--warn)}
    .pill.PENDING,.pill.CREATED{background:var(--overlay);color:var(--muted)}.pill.PENDING::before,.pill.CREATED::before{background:var(--subtle)}
    .pill.INFO{background:var(--overlay);color:var(--muted)}.pill.INFO::before{display:none}

    /* === SUMMARY BAR === */
    .summary{display:flex;align-items:center;gap:16px;padding:14px 24px;border-bottom:1px solid var(--border);background:var(--surface);flex-wrap:wrap}
    .sum-item{display:flex;align-items:center;gap:6px;font-size:13px}
    .sum-label{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.06em}
    .sum-val{font-weight:600;font-family:var(--mono);font-size:13px}
    .sum-sep{width:1px;height:20px;background:var(--border)}
    .sum-prompt{color:var(--muted);font-size:13px;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}

    /* === PIPELINE === */
    .pipe-wrap{padding:28px 24px 20px;border-bottom:1px solid var(--border);overflow-x:auto}
    .pipe{display:flex;align-items:flex-start;gap:0;min-width:min-content}
    .pipe-node{display:flex;flex-direction:column;align-items:center;width:120px;min-width:120px;cursor:pointer;position:relative}
    .pipe-node:hover .dot{transform:scale(1.15)}
    .pipe-node.sel .dot{box-shadow:0 0 0 3px var(--accent-soft)}
    .dot{width:36px;height:36px;border-radius:50%;border:3px solid var(--border);background:var(--surface);display:flex;align-items:center;justify-content:center;transition:all .2s;position:relative;z-index:1;font-size:14px}
    .pipe-node.done .dot{border-color:var(--good);background:var(--good-soft);color:var(--good)}
    .pipe-node.run .dot{border-color:var(--accent);background:var(--accent-soft);color:var(--accent);animation:glow 2s ease-in-out infinite}
    .pipe-node.fail .dot{border-color:var(--bad);background:var(--bad-soft);color:var(--bad)}
    .pipe-node.loop .dot{border-color:var(--info);background:var(--info-soft);color:var(--info)}
    .pipe-label{margin-top:8px;font-size:11px;font-weight:600;text-align:center;color:var(--muted);line-height:1.2;max-width:110px;word-wrap:break-word}
    .pipe-node.run .pipe-label{color:var(--accent)}
    .pipe-node.done .pipe-label{color:var(--good)}
    .pipe-node.fail .pipe-label{color:var(--bad)}
    .pipe-round{font-size:10px;color:var(--subtle);margin-top:2px;font-family:var(--mono)}
    .loop-tag{font-size:9px;background:var(--info-soft);color:var(--info);padding:1px 5px;border-radius:99px;font-weight:700;margin-top:3px}
    .pipe-line{flex:1;height:3px;background:var(--border);margin-top:17px;min-width:16px;position:relative;border-radius:2px}
    .pipe-line.done{background:var(--good);box-shadow:0 0 6px rgba(63,185,80,.4)}
    .pipe-line.active{background:linear-gradient(90deg,var(--good),var(--accent));box-shadow:0 0 8px rgba(47,129,247,.3)}
    .empty-msg{padding:24px;color:var(--muted);font-size:13px;text-align:center}

    /* === ROLE BAR === */
    .role-bar{display:flex;gap:8px;padding:14px 24px;border-bottom:1px solid var(--border);overflow-x:auto;background:var(--bg)}
    .role-chip{display:flex;align-items:center;gap:7px;padding:6px 14px;border-radius:8px;border:1px solid var(--border);background:var(--surface);cursor:pointer;white-space:nowrap;font-size:12px;font-weight:600;transition:all .15s}
    .role-chip:hover{border-color:var(--accent);background:var(--elevated)}
    .role-chip.active{border-color:var(--accent);background:var(--accent-soft)}
    .role-chip .rc-dot{width:8px;height:8px;border-radius:50%;background:var(--subtle);flex-shrink:0}
    .role-chip.RUNNING .rc-dot{background:var(--accent);animation:blink 1.5s infinite}
    .role-chip.COMPLETED .rc-dot{background:var(--good)}
    .role-chip.FAILED .rc-dot{background:var(--bad)}
    .role-chip.OUTPUT_INVALID .rc-dot{background:var(--warn)}
    .role-chip.TIMEOUT .rc-dot{background:var(--bad)}
    .role-chip .rc-count{font-size:10px;color:var(--muted);font-family:var(--mono)}

    /* === DETAIL PANEL === */
    .detail{display:grid;grid-template-columns:340px 1fr;border-bottom:1px solid var(--border);max-height:0;overflow:hidden;transition:max-height .3s ease}
    .detail.open{max-height:70vh;overflow:visible}
    .detail-left{border-right:1px solid var(--border);padding:16px;overflow-y:auto;max-height:70vh;background:var(--surface)}
    .detail-right{padding:16px;overflow-y:auto;max-height:70vh;display:flex;flex-direction:column;gap:10px}
    .detail-title{font-size:13px;font-weight:700;margin-bottom:10px;display:flex;align-items:center;justify-content:space-between}
    .detail-close{background:transparent;border:1px solid var(--border);color:var(--muted);border-radius:6px;padding:3px 8px;font-size:11px;cursor:pointer}
    .detail-close:hover{color:var(--text);border-color:var(--accent)}

    /* Agent cards in detail */
    .ag-card{border:1px solid var(--border);border-radius:8px;padding:10px;margin-bottom:8px;background:var(--elevated);transition:border-color .15s}
    .ag-card:hover{border-color:var(--accent)}
    .ag-head{display:flex;justify-content:space-between;align-items:center;gap:8px}
    .ag-name{font-weight:600;font-size:13px}
    .ag-meta{font-size:11px;color:var(--muted);margin-top:3px}
    .ag-files{display:flex;flex-wrap:wrap;gap:4px;margin-top:8px}

    /* File buttons */
    .fbtn{font-size:11px;padding:3px 8px;border-radius:5px;border:1px solid var(--border);background:var(--surface);color:var(--muted);cursor:pointer;font-family:var(--mono);transition:all .12s;white-space:nowrap}
    .fbtn:hover{border-color:var(--accent);color:var(--accent);background:var(--accent-soft)}
    .fbtn.pri{border-color:var(--accent);color:var(--accent)}
    .fbtn:disabled{opacity:.4;cursor:not-allowed}

    /* Viewer */
    .viewer-head{display:flex;justify-content:space-between;align-items:center;gap:10px;margin-bottom:6px}
    .viewer-path{font-size:12px;color:var(--muted);font-family:var(--mono);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
    .viewer-note{font-size:11px;color:var(--subtle)}
    pre{white-space:pre-wrap;word-break:break-word;padding:14px;border:1px solid var(--border);border-radius:8px;background:#010409;color:#c9d1d9;max-height:50vh;overflow:auto;font-size:12px;line-height:1.55;font-family:var(--mono);flex:1;min-height:120px;tab-size:2}

    /* === LOG BAR === */
    .logbar{border-top:1px solid var(--border);background:var(--surface);margin-top:auto}
    .logbar-head{display:flex;align-items:center;justify-content:space-between;padding:8px 24px;cursor:pointer;user-select:none}
    .logbar-head:hover{background:var(--elevated)}
    .logbar-title{font-size:12px;font-weight:600;color:var(--muted);display:flex;align-items:center;gap:8px}
    .log-badge{font-size:10px;background:var(--accent-soft);color:var(--accent);padding:1px 6px;border-radius:99px;font-weight:700;font-family:var(--mono)}
    .logbar-preview{font-size:11px;color:var(--subtle);flex:1;text-align:right;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;margin-left:16px}
    .log-body{max-height:0;overflow:hidden;transition:max-height .3s ease}
    .log-body.open{max-height:300px;overflow-y:auto}
    .log-item{display:flex;align-items:flex-start;gap:10px;padding:6px 24px;font-size:12px;border-top:1px solid var(--border-muted)}
    .log-item:hover{background:var(--elevated)}
    .log-time{color:var(--subtle);font-family:var(--mono);font-size:11px;flex-shrink:0;width:70px}
    .log-type{font-weight:600;width:110px;flex-shrink:0}
    .log-msg{color:var(--muted);flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}

    /* === ROUND TABS === */
    .rtabs{display:flex;gap:4px;flex-wrap:wrap;margin-bottom:10px}
    .rtab{font-size:11px;padding:3px 8px;border-radius:5px;border:1px solid var(--border);background:transparent;color:var(--muted);cursor:pointer;font-family:var(--mono)}
    .rtab:hover{border-color:var(--accent)}
    .rtab.on{border-color:var(--accent);background:var(--accent-soft);color:var(--accent);font-weight:700}

    /* === ANIMATIONS === */
    @keyframes glow{0%,100%{box-shadow:0 0 0 0 rgba(47,129,247,.4)}50%{box-shadow:0 0 0 10px rgba(47,129,247,0)}}
    @keyframes blink{0%,100%{opacity:1}50%{opacity:.3}}

    /* === RESPONSIVE === */
    @media(max-width:900px){
      .shell{grid-template-columns:1fr}
      .sidebar{display:none}
      .detail{grid-template-columns:1fr;max-height:none}
      .detail.open{max-height:none}
      .detail-left,.detail-right{max-height:none}
      .pipe-node{width:90px;min-width:90px}
    }

    /* === MODAL === */
    .modal{display:none;position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,0.5);z-index:9999;align-items:center;justify-content:center;backdrop-filter:blur(4px)}
    .modal.open{display:flex}
    .modal-content{background:var(--surface);border:1px solid var(--border);border-radius:12px;width:500px;max-width:90%;box-shadow:0 8px 32px rgba(0,0,0,0.5);display:flex;flex-direction:column}
    .modal-header{padding:16px 20px;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:center}
    .modal-header h3{margin:0;font-size:16px;color:var(--text)}
    .modal-close{background:transparent;border:none;color:var(--muted);cursor:pointer;font-size:18px}
    .modal-close:hover{color:var(--text)}
    .modal-body{padding:20px;max-height:60vh;overflow-y:auto;display:flex;flex-direction:column;gap:12px}
    .modal-footer{padding:16px 20px;border-top:1px solid var(--border);display:flex;justify-content:flex-end;gap:10px;align-items:center}
    .btn{padding:6px 14px;border-radius:6px;border:1px solid var(--border);background:var(--elevated);color:var(--text);cursor:pointer;font-size:13px;font-weight:600}
    .btn:hover:not(:disabled){background:var(--border)}
    .btn.primary{background:var(--accent);color:#fff;border-color:var(--accent)}
    .btn.primary:hover:not(:disabled){background:#4f46e5}
    .btn:disabled{opacity:0.5;cursor:not-allowed}
    .cfg-row{display:grid;grid-template-columns:100px 1fr 1fr;gap:10px;align-items:center}
    .cfg-label{font-size:13px;font-weight:600;color:var(--text)}
    .cfg-input{background:#010409;border:1px solid var(--border);color:var(--text);padding:6px 8px;border-radius:4px;font-size:13px;width:100%}
  </style>
</head>
<body>
  <div class="header">
    <div class="header-left">
      <span class="logo">OpenOrchestra</span>
      <span class="header-task" id="headerTask"></span>
    </div>
    <div class="header-right">
      <button class="btn" style="padding:4px 10px;font-size:12px;margin-right:15px;background:transparent;border-color:var(--border-muted)" onclick="openConfig()"><span data-i18n="settings">设置</span></button>
      <div class="seg"><button id="langZh" class="on" onclick="setLang('zh')">中</button><button id="langEn" onclick="setLang('en')">EN</button></div>
      <span id="heartbeat"></span>
    </div>
  </div>
  <div class="shell">
    <div class="sidebar">
      <h2 data-i18n="taskHistory">任务历史</h2>
      <div id="tasks"></div>
    </div>
    <div class="content">
      <div class="summary" id="summary"></div>
      <div class="pipe-wrap"><div class="pipe" id="pipeline"></div></div>
      <div class="role-bar" id="roleBar"></div>
      <div class="detail" id="detail">
        <div class="detail-left" id="detailLeft"></div>
        <div class="detail-right" id="detailRight">
          <div class="viewer-head">
            <span class="viewer-path" id="viewerPath" data-i18n="selectFile">点击文件按钮查看内容</span>
            <button class="detail-close" onclick="clearViewer()" data-i18n="clear">清空</button>
          </div>
          <div class="viewer-note" id="translationNote"></div>
          <pre id="fileText"></pre>
        </div>
      </div>
      <div class="logbar" id="logbar">
        <div class="logbar-head" onclick="toggleLog()">
          <span class="logbar-title"><span data-i18n="activityLog">活动日志</span><span class="log-badge" id="logBadge">0</span></span>
          <span class="logbar-preview" id="logPreview"></span>
        </div>
        <div class="log-body" id="logBody"></div>
      </div>
    </div>
  </div>

  <div id="configModal" class="modal">
    <div class="modal-content">
      <div class="modal-header">
        <h3 id="configTitle">任务配置 / Task Config</h3>
        <button class="modal-close" onclick="closeConfig()">✕</button>
      </div>
      <div class="modal-body" id="configBody"></div>
      <div class="modal-footer">
        <span id="configStatus" style="font-size:12px;color:var(--warn);margin-right:auto;"></span>
        <button class="btn" onclick="closeConfig()">取消 / Cancel</button>
        <button class="btn primary" id="configSaveBtn" onclick="saveConfig()">保存 / Save</button>
      </div>
    </div>
  </div>

<script>
// JS Part 1: State, API, i18n, core rendering
let currentTask=new URLSearchParams(location.search).get("task"),latestData=null,uiLanguage=localStorage.getItem("harness-ui-lang")||"zh";
let selectedPhaseIdx=-1,selectedRole=null,selectedRoundKey=null,currentFile=null,translationSeq=0,lastScrolledKey=null;
let eventSource=null,eventSourceTask=null,lastEventId=0,refreshTimer=null,logOpen=false,fileRefreshTimer=null;
const translationCache=new Map();
const rl={planner:"规划者",executor:"执行者",tester:"测试者",reviewer:"审阅者",judge:"裁决者",communicator:"交付者",orchestrator:"编排器"};
const rlEn={planner:"Planner",executor:"Executor",tester:"Tester",reviewer:"Reviewer",judge:"Judge",communicator:"Communicator",orchestrator:"Orchestrator"};
const pl={PLANNING_DRAFT:"规划草案",PLANNING_PEER_REVIEW:"规划互审",PLANNING_REVISION:"规划修订",PLAN_REVIEW:"方案审阅",PLAN_JUDGEMENT:"计划裁决",EXECUTION:"执行实现",PATCH_MERGE:"合并补丁",TESTING:"测试",TEST_JUDGEMENT:"测试裁决",FIXING:"修复",REVIEWING:"审阅",REVIEW_JUDGEMENT:"审阅裁决",REVIEW_FIXING:"审阅修复",REGRESSION_TESTING:"回归测试",FINAL_JUDGEMENT:"最终裁决",DELIVERY:"交付",MISC_RESPONSE:"直接回答",COMPLETED:"完成"};
const plEn={PLANNING_DRAFT:"Planning",PLANNING_PEER_REVIEW:"Peer Review",PLANNING_REVISION:"Revision",PLAN_REVIEW:"Plan Review",PLAN_JUDGEMENT:"Plan Judge",EXECUTION:"Execution",PATCH_MERGE:"Patch Merge",TESTING:"Testing",TEST_JUDGEMENT:"Test Judge",FIXING:"Fixing",REVIEWING:"Review",REVIEW_JUDGEMENT:"Review Judge",REVIEW_FIXING:"Review Fix",REGRESSION_TESTING:"Regression",FINAL_JUDGEMENT:"Final Judge",DELIVERY:"Delivery",MISC_RESPONSE:"Response",COMPLETED:"Done"};
const i18n={
  zh:{taskHistory:"任务历史",activityLog:"活动日志",selectFile:"点击文件按钮查看内容",clear:"清空",noTasks:"暂无任务",noPhases:"任务启动后显示流程",noRole:"选择角色查看详情",translating:"翻译中…",translatedByModel:"已翻译(模型)",translatedFallback:"已翻译(词表)",original:"原文"},
  en:{taskHistory:"Task History",activityLog:"Activity Log",selectFile:"Click a file button to view",clear:"Clear",noTasks:"No tasks yet",noPhases:"Pipeline appears after task starts",noRole:"Select a role to view details",translating:"Translating…",translatedByModel:"Translated (model)",translatedFallback:"Translated (glossary)",original:"Original"}
};
const phaseIcons={PLANNING_DRAFT:"📋",PLANNING_PEER_REVIEW:"👁",PLANNING_REVISION:"✏️",PLAN_REVIEW:"🔍",PLAN_JUDGEMENT:"⚖️",EXECUTION:"⚡",PATCH_MERGE:"🔀",TESTING:"🧪",TEST_JUDGEMENT:"⚖️",FIXING:"🔧",REVIEWING:"🔍",REVIEW_JUDGEMENT:"⚖️",REVIEW_FIXING:"🔧",REGRESSION_TESTING:"🧪",FINAL_JUDGEMENT:"⚖️",DELIVERY:"📦",MISC_RESPONSE:"💬",COMPLETED:"✅"};
const roleOrder=["orchestrator","planner","executor","tester","reviewer","judge","communicator"];
const dateFmt=new Intl.DateTimeFormat(navigator.language||"zh-CN",{hour:"2-digit",minute:"2-digit",second:"2-digit"});

function t(k){return(i18n[uiLanguage]&&i18n[uiLanguage][k])||i18n.zh[k]||k}
function roleLabel(r){return uiLanguage==="en"?(rlEn[r]||r):(rl[r]||r)}
function labelPhase(p){return(uiLanguage==="en"?plEn[p]:pl[p])||p||"-"}
function esc(s){return String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]))}
function short(s,n=60){s=String(s??"").replace(/\s+/g," ");return s.length>n?s.slice(0,n-1)+"…":s}
function fmtBytes(b){if(b==null)return"";if(b<1024)return b+" B";if(b<1048576)return Math.round(b/1024)+" KB";return(b/1048576).toFixed(1)+" MB"}
function statusLabel(st){
  const s=String(st||"PENDING");
  const zh={OUTPUT_INVALID:"产物格式无效",FAILED:"执行失败",TIMEOUT:"超时",COMPLETED:"完成",RUNNING:"运行中",PENDING:"等待",CREATED:"已创建"};
  const en={OUTPUT_INVALID:"Output Contract Invalid",FAILED:"Failed",TIMEOUT:"Timeout",COMPLETED:"Completed",RUNNING:"Running",PENDING:"Pending",CREATED:"Created"};
  return (uiLanguage==="en"?en[s]:zh[s])||s;
}
function statusHelp(st){
  const s=String(st||"PENDING");
  const zh={OUTPUT_INVALID:"Agent 没有产出符合角色合同的必需文件或 return_code，不代表测试结论失败。测试结论请看 build_result_code、test_result_code、bug_result_code 或 test_gate。"};
  const en={OUTPUT_INVALID:"The agent did not produce the required role-contract files or return_code. This is not the test verdict; check build_result_code, test_result_code, bug_result_code, or test_gate for test results."};
  return (uiLanguage==="en"?en[s]:zh[s])||s;
}
function pill(st){let s=st||"PENDING";return `<span class="pill ${esc(s)}" title="${esc(statusHelp(s))}">${esc(statusLabel(s))}</span>`}
async function apiErrorMessage(r){
  try{
    const p=await r.json();
    return p?.error?.message||p?.error?.code||JSON.stringify(p);
  }catch(_){
    return await r.text();
  }
}
async function getJson(u){const r=await fetch(u);if(!r.ok)throw new Error(await apiErrorMessage(r));return r.json()}
async function postJson(u,p){const r=await fetch(u,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(p)});if(!r.ok)throw new Error(await apiErrorMessage(r));return r.json()}

function setLang(l){
  uiLanguage=l==="en"?"en":"zh";localStorage.setItem("harness-ui-lang",uiLanguage);
  document.documentElement.lang=uiLanguage==="en"?"en":"zh-CN";
  document.getElementById("langZh").classList.toggle("on",uiLanguage==="zh");
  document.getElementById("langEn").classList.toggle("on",uiLanguage==="en");
  document.querySelectorAll("[data-i18n]").forEach(el=>{el.textContent=t(el.dataset.i18n)});
  if(latestData)renderSnapshot(latestData);renderFileText();
}

function connectSSE(taskId){
  if(!window.EventSource)return;if(eventSource&&eventSourceTask===taskId)return;
  if(eventSource)eventSource.close();eventSourceTask=taskId;
  eventSource=new EventSource(`/api/events?task=${encodeURIComponent(taskId)}&last_id=${encodeURIComponent(lastEventId)}`);
  eventSource.addEventListener("progress",ev=>{
    const p=JSON.parse(ev.data);lastEventId=Math.max(lastEventId,Number(p.id||0));
    if(p.task_id!==currentTask)return;
    if(latestData){const evts=latestData.events||[];if(!evts.some(e=>Number(e.id||0)===Number(p.id||0))){latestData.events=[...evts,p].slice(-300);renderLog(latestData.events)}}
    scheduleRefresh(100);
  });
  eventSource.onerror=()=>scheduleRefresh(1000);
}
function scheduleRefresh(d=150){if(refreshTimer)return;refreshTimer=setTimeout(()=>{refreshTimer=null;refresh()},d)}

async function refresh(){
  try{
    const tl=await getJson("/api/tasks");
    const lt=(tl.tasks||[]).find(t=>t.task_id===tl.latest_task_id);
    if(tl.latest_task_id&&(!currentTask||(lt&&lt.status==="RUNNING"&&currentTask!==tl.latest_task_id))){
      if(currentTask!==tl.latest_task_id)lastEventId=0;currentTask=tl.latest_task_id;
      history.replaceState(null,"","?task="+encodeURIComponent(currentTask));
    }
    if(!currentTask&&tl.tasks.length)currentTask=tl.tasks[0].task_id;
    renderTasks(tl.tasks);
    if(currentTask){connectSSE(currentTask);latestData=await getJson("/api/tasks/"+encodeURIComponent(currentTask));
      lastEventId=Math.max(lastEventId,(latestData.events||[]).reduce((m,e)=>Math.max(m,Number(e.id||0)),0));
      renderSnapshot(latestData);}
    document.getElementById("heartbeat").textContent=dateFmt.format(new Date());
  }catch(e){document.getElementById("heartbeat").textContent="Error"}
}

function renderTasks(tasks){
  const root=document.getElementById("tasks");
  if(!tasks.length){root.innerHTML=`<div class="empty-msg">${esc(t("noTasks"))}</div>`;return}
  root.innerHTML=tasks.map((tk,i)=>`<button class="tsk ${tk.task_id===currentTask?"act":""}" onclick="selectTask('${esc(tk.task_id)}')">
    <div class="tsk-top"><span class="tsk-id">${esc(tk.task_id.slice(0,8))}</span>${pill(tk.status)}</div>
    <div class="tsk-prompt">${esc(short(tk.user_prompt,50))}</div></button>`).join("");
}
function selectTask(id){currentTask=id;lastEventId=0;selectedPhaseIdx=-1;selectedRole=null;selectedRoundKey=null;
  history.replaceState(null,"","?task="+encodeURIComponent(id));refresh()}
// JS Part 2: Rendering functions
function renderSnapshot(data){
  const task=data.task;if(!task)return;
  const runs=data.agent_runs||[],running=runs.filter(r=>r.status==="RUNNING");
  document.getElementById("headerTask").textContent=short(task.user_prompt,50);
  // Summary bar
  document.getElementById("summary").innerHTML=`
    <div class="sum-item"><span class="sum-label">${uiLanguage==="en"?"Task":"任务"}</span><span class="sum-val">${esc(task.task_id.slice(0,8))}</span></div>
    <div class="sum-sep"></div>
    <div class="sum-item">${pill(task.status)}</div>
    <div class="sum-sep"></div>
    <div class="sum-item"><span class="sum-label">${uiLanguage==="en"?"Workflow":"工作流"}</span><span class="sum-val">${esc(task.workflow_type||"-")}</span></div>
    <div class="sum-sep"></div>
    <div class="sum-item"><span class="sum-label">${uiLanguage==="en"?"Phase":"阶段"}</span><span class="sum-val">${esc(labelPhase(task.current_phase||"-"))}</span></div>
    <div class="sum-sep"></div>
    <div class="sum-item"><span class="sum-label">${uiLanguage==="en"?"Active":"活跃"}</span><span class="sum-val">${running.length}</span></div>
    <div class="sum-sep"></div>
    <button class="btn" style="margin-right:10px;padding:3px 8px;font-size:11px;flex-shrink:0" onclick="openConfig()">${uiLanguage==="en"?"Config":"配置"}</button>
    <div class="sum-prompt">${esc(task.user_prompt)}</div>`;
  renderPipeline(data.workflow_timeline||data.phases||[],task.current_phase,data.workflow_loop_edges||[]);
  renderRoleBar(data.roles||{},runs);
  renderDetail(data);
  renderLog(data.events||[]);
}

function renderPipeline(phases,curPhase,loopEdges){
  const timeline=buildTimeline(phases,curPhase);
  const root=document.getElementById("pipeline");
  if(!timeline.length){root.innerHTML=`<div class="empty-msg">${esc(t("noPhases"))}</div>`;return}
  const loopIdx=new Set((loopEdges||[]).map(e=>Number(e.to_index)));
  let html="";
  timeline.forEach((item,i)=>{
    const st=item.status||(item.phase_type===curPhase?"RUNNING":"PENDING");
    const isCur=item.phase_type===curPhase&&st!=="COMPLETED";
    const isLoop=Boolean(item.loop_revisit)||loopIdx.has(Number(item.timeline_index??i));
    let cls=st==="COMPLETED"?"done":st==="FAILED"?"fail":isCur?"run":"";
    if(isLoop&&cls!=="run")cls+=" loop";
    const sel=i===selectedPhaseIdx?"sel":"";
    const icon=phaseIcons[item.phase_type]||"○";
    html+=`<div class="pipe-node ${cls} ${sel}" onclick="selectPipeNode(${i})" title="${esc(labelPhase(item.phase_type))}">
      <div class="dot">${st==="COMPLETED"?"✓":st==="FAILED"?"✕":icon}</div>
      <div class="pipe-label">${esc(labelPhase(item.phase_type))}</div>
      <div class="pipe-round">R${esc(item.round_id??"0")}</div>
      ${isLoop?`<div class="loop-tag">${uiLanguage==="en"?"loop":"循环"}${Number(item.phase_occurrence||1)>1?" #"+item.phase_occurrence:""}</div>`:""}
    </div>`;
    if(i<timeline.length-1){
      const nextSt=timeline[i+1].status||(timeline[i+1].phase_type===curPhase?"RUNNING":"PENDING");
      const lineCls=st==="COMPLETED"?(nextSt==="COMPLETED"?"done":"active"):"";
      html+=`<div class="pipe-line ${lineCls}"></div>`;
    }
  });
  root.innerHTML=html;
  // auto-scroll to current if changed
  const cur=root.querySelector(".pipe-node.run")||root.querySelector(".pipe-node.sel");
  const curIdx=cur?Array.from(root.children).indexOf(cur):-1;
  const scrollKey=currentTask+":"+curIdx;
  if(cur && lastScrolledKey!==scrollKey){
    cur.scrollIntoView({behavior:"smooth",inline:"center",block:"nearest"});
    lastScrolledKey=scrollKey;
  }
}

function buildTimeline(phases,curPhase){
  const wfOrder=["PLANNING_DRAFT","PLANNING_PEER_REVIEW","PLANNING_REVISION","PLAN_REVIEW","EXECUTION","PATCH_MERGE","TESTING","TEST_JUDGEMENT","FIXING","REVIEWING","REVIEW_JUDGEMENT","REVIEW_FIXING","REGRESSION_TESTING","FINAL_JUDGEMENT","DELIVERY"];
  const existing=(phases||[]).map((p,i)=>({...p,phase_type:p.phase_type||p,timeline_index:p.timeline_index??i}));
  if(existing.length)return existing;
  const ci=wfOrder.indexOf(curPhase||"");
  if(ci<0)return curPhase?[{phase_type:curPhase,status:"RUNNING",round_id:0}]:[];
  return wfOrder.slice(0,ci+1).map((p,i)=>({phase_type:p,status:p===curPhase?"RUNNING":"PENDING",round_id:0,timeline_index:i}));
}

function selectPipeNode(idx){
  if(selectedPhaseIdx===idx){selectedPhaseIdx=-1}else{selectedPhaseIdx=idx}
  selectedRole=null;selectedRoundKey=null;
  if(latestData)renderSnapshot(latestData);
}

function renderRoleBar(roles,runs){
  const items=roleOrder.filter(r=>roles[r]).map(r=>roles[r]);
  const extras=Object.values(roles).filter(r=>!roleOrder.includes(r.role));
  const all=[...items,...extras];
  document.getElementById("roleBar").innerHTML=all.map(r=>{
    const st=esc(r.status||"PENDING");
    const active=selectedRole===r.role?"active":"";
    return `<div class="role-chip ${st} ${active}" onclick="selectRoleChip('${esc(r.role)}')">
      <span class="rc-dot"></span>
      <span>${esc(roleLabel(r.role))}</span>
      <span class="rc-count" title="${uiLanguage==='en'?'Agent Runs / Generated Files':'Agent 执行次数 / 产生的文件数量'}">
        ${r.agent_count||0} ${uiLanguage==="en"?"runs":"次运行"}, ${r.artifact_count||0} ${uiLanguage==="en"?"files":"个文件"}
      </span>
    </div>`;
  }).join("");
}

function selectRoleChip(role){
  if(selectedRole===role){selectedRole=null}else{selectedRole=role;selectedPhaseIdx=-1;selectedRoundKey=null}
  if(latestData)renderSnapshot(latestData);
}

function renderDetail(data){
  const panel=document.getElementById("detail");
  const runs=data.agent_runs||[];
  const roundsByRole=data.role_rounds||{};
  // Determine what to show
  let detailRuns=[],title="",showPanel=false;
  if(selectedPhaseIdx>=0){
    const timeline=buildTimeline(data.workflow_timeline||data.phases||[],data.task?.current_phase);
    const phase=timeline[selectedPhaseIdx];
    if(phase){
      title=labelPhase(phase.phase_type)+" · R"+( phase.round_id??0);
      detailRuns=runs.filter(r=>r.phase_id===phase.phase_id||(r.phase_type===phase.phase_type&&Number(r.phase_round_id||0)===Number(phase.round_id||0)));
      showPanel=true;
    }
  }else if(selectedRole&&roundsByRole[selectedRole]){
    const rounds=roundsByRole[selectedRole];
    if(rounds.length){
      const selKey=selectedRoundKey||roundK(rounds[rounds.length-1]);
      const sel=rounds.find(r=>roundK(r)===selKey)||rounds[rounds.length-1];
      selectedRoundKey=roundK(sel);
      title=roleLabel(selectedRole);
      // Round tabs + runs
      const tabs=rounds.map(r=>{const k=roundK(r);return`<button class="rtab ${k===selectedRoundKey?"on":""}" onclick="selectRound('${esc(selectedRole)}','${esc(k)}')">${uiLanguage==="en"?"R":"轮"}${r.round_id} · ${esc(labelPhase(r.phase_type))}</button>`}).join("");
      detailRuns=sel.runs||[];
      document.getElementById("detailLeft").innerHTML=`<div class="detail-title"><span>${esc(title)}</span><button class="detail-close" onclick="closeDetail()">✕</button></div><div class="rtabs">${tabs}</div>${renderAgentCards(detailRuns)}`;
      panel.classList.add("open");return;
    }
  }else{
    // Show running agents if any
    const running=runs.filter(r=>r.status==="RUNNING");
    if(running.length){title=uiLanguage==="en"?"Active Agents":"活跃 Agent";detailRuns=running;showPanel=true}
  }
  if(showPanel&&detailRuns.length){
    document.getElementById("detailLeft").innerHTML=`<div class="detail-title"><span>${esc(title)}</span><button class="detail-close" onclick="closeDetail()">✕</button></div>${renderAgentCards(detailRuns)}`;
    panel.classList.add("open");
  }else if(selectedPhaseIdx>=0||selectedRole){
    document.getElementById("detailLeft").innerHTML=`<div class="detail-title"><span>${esc(title)}</span><button class="detail-close" onclick="closeDetail()">✕</button></div><div class="empty-msg">${esc(t("noRole"))}</div>`;
    panel.classList.add("open");
  }else{panel.classList.remove("open")}
}

function selectRound(role,key){selectedRoundKey=key;if(latestData)renderSnapshot(latestData)}
function closeDetail(){selectedPhaseIdx=-1;selectedRole=null;selectedRoundKey=null;document.getElementById("detail").classList.remove("open")}
function roundK(item){return`${item.round_id}:${item.phase_type}`}

function renderAgentCards(runs){
  if(!runs.length)return`<div class="empty-msg">${esc(t("noRole"))}</div>`;
  return runs.map(r=>{
    const arts=(r.artifacts||[]).filter(a=>a.exists);
    const deliveryTypes=["delivery.md","final_delivery.md","usage_guide.md","response.md","plan.md","decision_summary.md","review_report.md","test_report.md","bug_report.md","self_check.md","merge_report.md"];
    const priArts=arts.filter(a=>deliveryTypes.includes(a.artifact_type));
    const otherArts=arts.filter(a=>!deliveryTypes.includes(a.artifact_type));
    return`<div class="ag-card">
      <div class="ag-head"><span class="ag-name">${esc(roleLabel(r.role))} / ${esc(r.agent_id)}</span>${pill(r.status)}</div>
      <div class="ag-meta">${esc(labelPhase(r.phase_type||"-"))} · R${esc(r.phase_round_id??"-")} · try ${Number(r.retry_count)+1}</div>
      <div class="ag-files">
        ${fBtn(r.prompt_path,"prompt",false)}${fBtn(r.stdout_path,"stdout",true)}${fBtn(r.stderr_path,"stderr",false)}${fBtn(r.diagnostics_path,"diag",false)}
        ${priArts.map(a=>aBtn(a,true)).join("")}${otherArts.map(a=>aBtn(a,false)).join("")}
      </div></div>`;
  }).join("");
}

function fBtn(info,label,pri){
  if(!info||!info.exists)return`<button class="fbtn" disabled>${esc(label)}</button>`;
  return`<button class="fbtn ${pri?"pri":""}" onclick="openFile('${esc(encodeURIComponent(info.path))}','${esc(label)}')">${esc(label)}</button>`;
}
function aBtn(a,pri){
  if(!a.exists)return"";
  return`<button class="fbtn ${pri?"pri":""}" onclick="openFile('${esc(encodeURIComponent(a.path))}','${esc(short(a.artifact_type,30))}')">${esc(short(a.artifact_type,20))}</button>`;
}

// File viewer
async function openFile(ep,label){
  const data=await getJson("/api/file?path="+ep+"&max_chars=200000");
  currentFile={label,encodedPath:ep,live:isLiveLogLabel(label),...data};renderFileText();
  scheduleFileRefresh();
  document.getElementById("detail").classList.add("open");
}
function clearViewer(){currentFile=null;stopFileRefresh();document.getElementById("viewerPath").textContent=t("selectFile");document.getElementById("fileText").textContent="";document.getElementById("translationNote").textContent=""}
function isLiveLogLabel(label){return["stdout","stderr","diag"].includes(String(label||"").toLowerCase())}
function stopFileRefresh(){if(fileRefreshTimer){clearTimeout(fileRefreshTimer);fileRefreshTimer=null}}
function scheduleFileRefresh(){
  stopFileRefresh();
  if(!currentFile||!currentFile.live)return;
  fileRefreshTimer=setTimeout(refreshCurrentFile,1000);
}
async function refreshCurrentFile(){
  fileRefreshTimer=null;
  if(!currentFile||!currentFile.live||!currentFile.encodedPath)return;
  const previousPath=currentFile.path, previousLabel=currentFile.label, previousEncodedPath=currentFile.encodedPath;
  try{
    const data=await getJson("/api/file?path="+previousEncodedPath+"&max_chars=200000");
    if(!currentFile||currentFile.path!==previousPath)return;
    currentFile={label:previousLabel,encodedPath:previousEncodedPath,live:true,...data};
    renderFileText();
  }catch(e){}
  scheduleFileRefresh();
}
function renderFileText(){
  if(!currentFile){document.getElementById("viewerPath").textContent=t("selectFile");document.getElementById("translationNote").textContent="";return}
  const sfx=currentFile.truncated_from_start?(uiLanguage==="en"?" (tail)":"(尾部)"):"";
  document.getElementById("viewerPath").textContent=currentFile.label+" · "+currentFile.path+sfx;
  const src=currentFile.text||"";
  if(currentFile.live){document.getElementById("fileText").textContent=src;document.getElementById("translationNote").textContent=uiLanguage==="en"?"Live log":"实时日志";return}
  if(uiLanguage!=="zh"){document.getElementById("fileText").textContent=src;document.getElementById("translationNote").textContent=t("original");return}
  const ck=currentFile.path+":"+currentFile.size+":"+src.length;
  const cached=translationCache.get(ck);
  if(cached){document.getElementById("fileText").textContent=cached.text;document.getElementById("translationNote").textContent=cached.mode==="model"?t("translatedByModel"):t("translatedFallback");return}
  const fb=translateMd(src);document.getElementById("fileText").textContent=fb;document.getElementById("translationNote").textContent=t("translating");
  const seq=++translationSeq,path=currentFile.path;
  postJson("/api/translate",{text:src,path}).then(d=>{
    if(!currentFile||currentFile.path!==path||uiLanguage!=="zh"||seq!==translationSeq)return;
    const tr=d.text||fb,mode=d.mode||"fallback";translationCache.set(ck,{text:tr,mode});
    document.getElementById("fileText").textContent=tr;document.getElementById("translationNote").textContent=mode==="model"?t("translatedByModel"):t("translatedFallback");
  }).catch(()=>{if(!currentFile||currentFile.path!==path||uiLanguage!=="zh"||seq!==translationSeq)return;
    translationCache.set(ck,{text:fb,mode:"fallback"});document.getElementById("fileText").textContent=fb;document.getElementById("translationNote").textContent=t("translatedFallback")});
}

// Activity Log
function toggleLog(){logOpen=!logOpen;document.getElementById("logBody").classList.toggle("open",logOpen)}
function renderLog(events){
  const flow=(events||[]).filter(e=>/^(task_|phase_|agent_|patch_|test_|delivery_|judge_)/.test(String(e.event_type||""))).slice(-60);
  document.getElementById("logBadge").textContent=String(flow.length);
  if(flow.length){const last=flow[flow.length-1];document.getElementById("logPreview").textContent=`${flowLabel(last.event_type)} · ${labelPhase(last.phase||"")} · ${last.role?roleLabel(last.role):""}`}
  document.getElementById("logBody").innerHTML=flow.slice().reverse().map(e=>{
    const st=String(e.status||"");
    return`<div class="log-item"><span class="log-time">${esc(dateFmt.format(new Date(Number(e.ts||0)*1000)))}</span><span class="log-type ${esc(st)}">${esc(flowLabel(e.event_type))}</span><span class="log-msg">${esc(labelPhase(e.phase||""))} ${e.role?esc(roleLabel(e.role)):""} ${esc(e.agent_id||"")} ${esc(e.message||"")}</span></div>`;
  }).join("");
}
function flowLabel(et){
  const zh={task_created:"任务创建",task_started:"任务启动",task_completed:"任务完成",task_failed:"任务失败",phase_started:"阶段开始",phase_completed:"阶段完成",phase_skipped:"阶段跳过",agent_started:"Agent启动",agent_heartbeat:"Agent运行",agent_completed:"Agent完成",agent_failed:"Agent失败",agent_retryable_failure:"Agent重试",patch_validated:"补丁门禁",test_gate:"测试门禁",delivery_published:"交付发布",judge_decision:"裁决"};
  const en={task_created:"Task Created",task_started:"Task Started",task_completed:"Task Done",task_failed:"Task Failed",phase_started:"Phase Start",phase_completed:"Phase Done",phase_skipped:"Phase Skip",agent_started:"Agent Start",agent_heartbeat:"Agent Run",agent_completed:"Agent Done",agent_failed:"Agent Fail",agent_retryable_failure:"Agent Retry",patch_validated:"Patch Gate",test_gate:"Test Gate",delivery_published:"Delivery",judge_decision:"Judge"};
  return(uiLanguage==="en"?en[et]:zh[et])||et||"-";
}

// Translation (client-side glossary fallback)
function translateMd(text){
  if(!text)return text;let inF=false;
  return text.split("\n").map(l=>{if(/^\s*```/.test(l)){inF=!inF;return l}if(inF||preserveLine(l))return l;return transLine(l)}).join("\n");
}
function preserveLine(l){
  const t=l.trim();if(!t)return true;
  if(hasCN(t))return true;
  if(/^(diff --git|index |--- |\+\+\+ |@@ |[+-]{3,})/.test(t))return true;
  if(/^[+-]\s/.test(t)&&/[`$./\\]|^\+\s*(import|from|def|class|const|let|var|function)\b/.test(t))return true;
  if(/^(curl|python3?|pip|npm|pytest|git|docker|make|node|claude|codex|gemini|qwen|cd|mkdir|cp|mv|rm|cat|ls)\b/.test(t))return true;
  if(/^\$ /.test(t)||/^(https?:\/\/|file:\/\/)/.test(t))return true;
  if(/^(\/|~\/|\.\.\?\/)[^\s]*$/.test(t))return true;
  if(/^\s*[{[\]}],?\s*$/.test(l)||/^\s*"[^"]+"\s*:\s*("[^"]*"|\d+|true|false|null|[{[]),?\s*$/.test(l))return true;
  if(/^\s*[A-Z0-9_]+\s*=/.test(l))return true;
  return false;
}
function transLine(l){
  const ph=[];let p=l.replace(/`[^`]*`|https?:\/\/\S+|(?:\/|~\/|\.\.?\/)[^\s),;]+|[A-Za-z0-9_.-]+\.(?:md|py|js|json|yaml|txt|log|diff|html|css|sh)\b/g,tok=>{const m=`__K${ph.length}__`;ph.push(tok);return m});
  p=glossary(p);return p.replace(/__K(\d+)__/g,(_,i)=>ph[Number(i)]??"");
}
function glossary(t){
  const r=[[/\bTask\b/g,"任务"],[/\bRole\b/g,"角色"],[/\bPhase\b/g,"阶段"],[/\bRound\b/g,"轮次"],[/\bImplementation\b/gi,"实现"],[/\bTesting\b/gi,"测试"],[/\bReview\b/gi,"审阅"],[/\bcompleted\b/gi,"已完成"],[/\bsuccess\b/gi,"成功"],[/\bfailed\b/gi,"失败"],[/\bnone\b/gi,"无"]];
  let o=t;for(const[p,v]of r)o=o.replace(p,v);return o;
}
function hasCN(t){const c=(t.slice(0,2000).match(/[\u4e00-\u9fff]/g)||[]).length;const l=(t.slice(0,2000).match(/[A-Za-z]/g)||[]).length;return c>0&&c>=l*.25}

async function openConfig(){
  const d = document.getElementById("configModal");
  d.classList.add("open");
  const b = document.getElementById("configBody");
  b.innerHTML = `<div style="text-align:center;padding:20px;color:var(--muted)">加载中 / Loading...</div>`;
  const stat = document.getElementById("configStatus");
  stat.textContent = "";
  document.getElementById("configSaveBtn").disabled = false;

    try {
    const cfg = await getJson("/api/config");
    let html = "";
    const models = cfg.backend_options || ["codex","claude","gemini","qwen"];
    html += `<div class="cfg-row">
      <div class="cfg-label">${uiLanguage==="en"?"Save scope":"保存范围"}</div>
      <label style="display:flex;align-items:center;gap:8px;color:var(--muted);font-size:12px">
        <input type="checkbox" id="cfg-persist" ${cfg.persist_supported ? "" : "disabled"}>
        ${uiLanguage==="en"?"Persist to config file":"写入配置文件"}
      </label>
    </div>`;
    if(cfg.config_path){
      html += `<div style="color:var(--muted);font-size:11px;margin:0 0 10px 0">${esc(cfg.config_path)}</div>`;
    }
    roleOrder.forEach(r => {
      if(r==="orchestrator") return;
      const count = (cfg.roles && cfg.roles[r] && cfg.roles[r].count) || 1;
      const be = (cfg.agent_backend && cfg.agent_backend[r]) || "codex";
      html += `<div class="cfg-row">
        <div class="cfg-label">${roleLabel(r)}</div>
        <input class="cfg-input" type="number" id="cfg-cnt-${r}" value="${count}" min="1" max="10">
        <select class="cfg-input" id="cfg-be-${r}">
          ${models.map(m=>`<option value="${m}" ${m===be?'selected':''}>${m}</option>`).join("")}
        </select>
      </div>`;
    });
    b.innerHTML = html;
  } catch(e){
    b.innerHTML = `<div style="color:var(--bad)">加载失败 / Failed to load: ${esc(e.message)}</div>`;
  }
}
function closeConfig(){
  document.getElementById("configModal").classList.remove("open");
}
async function saveConfig(){
  const persist = !!(document.getElementById("cfg-persist") && document.getElementById("cfg-persist").checked);
  const payload = {roles:{}, agent_backend:{}, persist:persist};
  roleOrder.forEach(r => {
    if(r==="orchestrator") return;
    const cnt = document.getElementById("cfg-cnt-"+r);
    const be = document.getElementById("cfg-be-"+r);
    if(cnt && be){
      payload.roles[r] = {count: parseInt(cnt.value, 10)};
      payload.agent_backend[r] = be.value;
    }
  });
  const btn = document.getElementById("configSaveBtn");
  const stat = document.getElementById("configStatus");
  try{
    btn.disabled = true;
    stat.style.color="var(--text)"; stat.textContent = "保存中 / Saving...";
    await postJson("/api/config", payload);
    stat.style.color="var(--good)"; stat.textContent = "已保存运行配置 / Runtime config saved";
    setTimeout(closeConfig, 500);
  }catch(e){
    stat.style.color="var(--bad)"; stat.textContent = "保存失败 / Failed: " + (e.message || "Unknown error");
    btn.disabled = false;
  }
}

// Init
setLang(uiLanguage);clearViewer();refresh();setInterval(refresh,5000);
</script></body></html>
"""
