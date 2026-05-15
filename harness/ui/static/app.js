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
  const zh={OUTPUT_INVALID:"产物格式无效",FAILED:"执行失败",TIMEOUT:"超时",COMPLETED:"完成",RUNNING:"运行中",PENDING:"等待",CREATED:"已创建",OPEN:"熔断",DEGRADED:"降级",HEALTHY:"健康"};
  const en={OUTPUT_INVALID:"Output Contract Invalid",FAILED:"Failed",TIMEOUT:"Timeout",COMPLETED:"Completed",RUNNING:"Running",PENDING:"Pending",CREATED:"Created",OPEN:"Open",DEGRADED:"Degraded",HEALTHY:"Healthy"};
  return (uiLanguage==="en"?en[s]:zh[s])||s;
}
function statusHelp(st){
  const s=String(st||"PENDING");
  const zh={
    OUTPUT_INVALID:"Agent 没有产出符合角色合同的必需文件或 return_code，不代表测试结论失败。测试结论请看 bug_report.md 和 tester_result.json。",
    FAILED:"执行失败表示 Agent 进程、阶段编排或门禁失败；业务测试失败请看 tester_result.json，patch gate 失败请看 patch_validated/objective_gate。"
  };
  const en={
    OUTPUT_INVALID:"The agent did not produce the required role-contract files or return_code. This is not the test verdict; check bug_report.md and tester_result.json.",
    FAILED:"Failed means an agent process, orchestration phase, or gate failed. Business test failures are in tester_result.json; patch gate failures are in patch_validated/objective_gate."
  };
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
    if(latestData){const evts=latestData.events||[];if(!evts.some(e=>Number(e.id||0)===Number(p.id||0))){latestData.events=[...evts,p].slice(-300);renderLog(latestData)}}
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
  const backendHealth=backendHealthSummary(data.backend_health||{});
  document.getElementById("headerTask").textContent=short(task.user_prompt,50);
  // Summary bar
  document.getElementById("summary").innerHTML=`
    <div class="sum-item"><span class="sum-label">${uiLanguage==="en"?"Task":"任务"}</span><span class="sum-val">${esc(task.task_id.slice(0,8))}</span></div>
    <div class="sum-sep"></div>
    <div class="sum-item">${pill(task.status)}</div>
    <div class="sum-sep"></div>
    <div class="sum-item"><span class="sum-label">${uiLanguage==="en"?"Turns":"对话轮次"}</span><span class="sum-val">${esc((data.workflow_runs||[]).length||1)}</span></div>
    <div class="sum-sep"></div>
    <div class="sum-item"><span class="sum-label">${uiLanguage==="en"?"Workflow":"工作流"}</span><span class="sum-val">${esc(task.workflow_type||"-")}</span></div>
    <div class="sum-sep"></div>
    <div class="sum-item"><span class="sum-label">${uiLanguage==="en"?"Phase":"阶段"}</span><span class="sum-val">${esc(labelPhase(task.current_phase||"-"))}</span></div>
    <div class="sum-sep"></div>
    <div class="sum-item"><span class="sum-label">${uiLanguage==="en"?"Active":"活跃"}</span><span class="sum-val">${running.length}</span></div>
    <div class="sum-sep"></div>
    ${backendHealth}
    <button class="btn" style="margin-right:10px;padding:3px 8px;font-size:11px;flex-shrink:0" onclick="openConfig()">${uiLanguage==="en"?"Config":"配置"}</button>
    <div class="sum-prompt">${esc(task.user_prompt)}</div>`;
  renderPipeline(data.workflow_timeline||data.phases||[],task.current_phase,data.workflow_loop_edges||[],data.workflow_runs||[]);
  renderRoleBar(data.roles||{},runs);
  renderDetail(data);
  renderLog(data);
}

function backendHealthSummary(health){
  const items=Object.values(health||{}).filter(h=>h&&h.state&&h.state!=="healthy");
  if(!items.length)return "";
  return items.map(h=>`<div class="sum-item" title="${esc(h.message||"")}"><span class="sum-label">${esc(h.backend)}</span>${pill(String(h.state||"").toUpperCase())}</div><div class="sum-sep"></div>`).join("");
}

function renderPipeline(phases,curPhase,loopEdges,workflowRuns){
  const timeline=buildTimeline(phases,curPhase);
  const root=document.getElementById("pipeline");
  if(!timeline.length){root.innerHTML=`<div class="empty-msg">${esc(t("noPhases"))}</div>`;return}
  if((workflowRuns||[]).length>1){
    root.innerHTML=`<div class="pipeline-runs">${workflowRuns.map(run=>renderWorkflowRun(run,curPhase,loopEdges)).join("")}</div>`;
    scrollCurrentPipelineNode(root);
    return;
  }
  root.innerHTML=renderTimelineRow(timeline,curPhase,loopEdges);
  scrollCurrentPipelineNode(root);
}

function renderWorkflowRun(run,curPhase,loopEdges){
  const title=(uiLanguage==="en"?"Turn ":"对话 ")+(Number(run.turn_index||0)+1);
  const prompt=short(run.prompt||"",90);
  return `<div class="run-lane">
    <div class="run-head">
      <div class="run-title"><span>${esc(title)}</span>${pill(run.status||"RUNNING")}</div>
      <div class="run-meta">${esc(run.workflow_type||"-")} · ${esc(run.phase_count||0)} ${uiLanguage==="en"?"phases":"个阶段"}</div>
      <div class="run-prompt">${esc(prompt)}</div>
    </div>
    <div class="run-flow">${renderTimelineRow(run.phases||[],curPhase,loopEdges)}</div>
  </div>`;
}

function renderTimelineRow(timeline,curPhase,loopEdges){
  const loopIdx=new Set((loopEdges||[]).map(e=>Number(e.to_index)));
  let html="";
  timeline.forEach((item,i)=>{
    const globalIdx=Number(item.timeline_index??i);
    const st=item.status||(item.phase_type===curPhase?"RUNNING":"PENDING");
    const isCur=item.phase_type===curPhase&&st!=="COMPLETED";
    const isLoop=Boolean(item.loop_revisit)||loopIdx.has(globalIdx);
    let cls=st==="COMPLETED"?"done":st==="FAILED"?"fail":isCur?"run":"";
    if(isLoop&&cls!=="run")cls+=" loop";
    const sel=globalIdx===selectedPhaseIdx?"sel":"";
    const icon=phaseIcons[item.phase_type]||"○";
    html+=`<div class="pipe-node ${cls} ${sel}" onclick="selectPipeNode(${globalIdx})" title="${esc(labelPhase(item.phase_type))}">
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
  return html;
}

function scrollCurrentPipelineNode(root){
  const cur=root.querySelector(".pipe-node.run")||root.querySelector(".pipe-node.sel");
  const curIdx=cur?selectedPhaseIdx+":"+cur.textContent:-1;
  const scrollKey=currentTask+":"+curIdx;
  if(cur && lastScrolledKey!==scrollKey){
    cur.scrollIntoView({behavior:"smooth",inline:"center",block:"nearest"});
    lastScrolledKey=scrollKey;
  }
}

function buildTimeline(phases,curPhase){
  const wfOrder=["PLANNING_DRAFT","PLANNING_PEER_REVIEW","PLANNING_REVISION","PLAN_REVIEW","EXECUTION","PATCH_MERGE","TESTING","TEST_JUDGEMENT","FIXING","REVIEWING","REVIEW_FIXING","REGRESSION_TESTING","DELIVERY"];
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
  const deliveryTypes=["delivery.md","final_delivery.json","usage_guide.md","response.md","plan.md","decision.json","review_result.json","bug_report.md","tester_result.json","self_check.md","merged_patch_metadata.json"];
    const priArts=arts.filter(a=>deliveryTypes.includes(a.artifact_type));
    const otherArts=arts.filter(a=>!deliveryTypes.includes(a.artifact_type));
    return`<div class="ag-card">
      <div class="ag-head"><span class="ag-name">${esc(roleLabel(r.role))} / ${esc(r.agent_id)}</span>${pill(r.status)}</div>
      <div class="ag-meta">${esc(labelPhase(r.phase_type||"-"))} · R${esc(r.phase_round_id??"-")} · try ${Number(r.retry_count)+1}</div>
      <div class="ag-files">
        ${fBtn(r.live_path,"live",true)}${fBtn(r.trace_path,"trace",false)}${fBtn(r.prompt_path,"prompt",false)}${fBtn(r.command_path,"cmd",false)}${fBtn(r.stdout_path,"stdout",false)}${fBtn(r.stderr_path,"stderr",false)}${fBtn(r.diagnostics_path,"diag",false)}
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
function isLiveLogLabel(label){return["live","stdout","stderr","diag"].includes(String(label||"").toLowerCase())}
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
function renderLog(snapshot){
  const phases=(snapshot&&snapshot.workflow_timeline)||[],runs=(snapshot&&snapshot.agent_runs)||[];
  const rows=phases.map(p=>flowRow(p,runs)).filter(Boolean);
  document.getElementById("logBadge").textContent=String(rows.length);
  if(rows.length){const last=rows[rows.length-1];document.getElementById("logPreview").textContent=`${last.phase} · ${last.role} · ${last.duration}`}
  document.getElementById("logBody").innerHTML=rows.slice().reverse().map(r=>`
    <div class="log-item">
      <span class="log-time">R${esc(r.round)}</span>
      <span class="log-type ${esc(r.status)}">${esc(r.role)}</span>
      <span class="log-msg">${esc(r.phase)} · ${esc(statusLabel(r.status))} · ${esc(r.duration)}${r.agents?` · ${esc(r.agents)}`:""}</span>
    </div>`).join("");
}
function flowRow(phase,runs){
  const phaseRuns=runs.filter(r=>r.phase_id===phase.phase_id);
  return {
    round:phase.round_id??0,
    role:roleLabel(phase.role||"-"),
    phase:labelPhase(phase.phase_type||"-"),
    status:String(phase.status||"RUNNING"),
    duration:durationLabel(phase.started_at,phase.completed_at),
    agents:phaseRuns.map(r=>`${r.agent_id}:${durationLabel(r.started_at,r.completed_at)}`).join(", ")
  };
}
function durationLabel(start,end){
  if(!start)return "-";
  const s=Date.parse(start),e=end?Date.parse(end):Date.now();
  if(!Number.isFinite(s)||!Number.isFinite(e)||e<s)return "-";
  const sec=Math.floor((e-s)/1000),h=Math.floor(sec/3600),m=Math.floor((sec%3600)/60),ss=sec%60;
  if(h)return`${h}h ${String(m).padStart(2,"0")}m`;
  if(m)return`${m}m ${String(ss).padStart(2,"0")}s`;
  return`${ss}s`;
}
function failureKind(e){
  const et=String(e.event_type||""),st=String(e.status||"");
  if(et==="patch_validated"&&(st==="FAILED"||st==="fail"))return uiLanguage==="en"?"[Patch gate failed]":"[patch gate失败]";
  if(et==="test_gate"&&(st==="FAILED"||st==="fail"))return uiLanguage==="en"?"[Business tests failed]":"[业务测试失败]";
  if(et==="runtime_readiness"&&(st==="FAILED"||st==="fail"))return uiLanguage==="en"?"[Runtime readiness failed]":"[运行环境验证失败]";
  if(et==="agent_failed")return uiLanguage==="en"?"[Agent execution failed]":"[Agent执行失败]";
  if(st==="OUTPUT_INVALID")return uiLanguage==="en"?"[Output contract invalid]":"[产物合同无效]";
  return "";
}
function flowLabel(et){
  const zh={task_created:"任务创建",task_started:"任务启动",task_completed:"任务完成",task_failed:"任务失败",phase_started:"阶段开始",phase_completed:"阶段完成",phase_skipped:"阶段跳过",agent_started:"Agent启动",agent_heartbeat:"Agent运行",agent_completed:"Agent完成",agent_failed:"Agent失败",agent_retryable_failure:"Agent重试",backend_health_changed:"后端健康",backend_circuit_open:"后端熔断",patch_validated:"补丁门禁",test_gate:"测试门禁",runtime_readiness:"运行环境验证",delivery_published:"交付发布",judge_decision:"裁决"};
  const en={task_created:"Task Created",task_started:"Task Started",task_completed:"Task Done",task_failed:"Task Failed",phase_started:"Phase Start",phase_completed:"Phase Done",phase_skipped:"Phase Skip",agent_started:"Agent Start",agent_heartbeat:"Agent Run",agent_completed:"Agent Done",agent_failed:"Agent Fail",agent_retryable_failure:"Agent Retry",backend_health_changed:"Backend Health",backend_circuit_open:"Backend Open",patch_validated:"Patch Gate",test_gate:"Test Gate",runtime_readiness:"Runtime Ready",delivery_published:"Delivery",judge_decision:"Judge"};
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
