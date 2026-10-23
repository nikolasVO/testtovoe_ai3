"use strict";
const $ = id => document.getElementById(id);
let selected = "fallback", timer = null, generation = 0;
const time = value => new Date(value).toLocaleTimeString("ru-RU", {hour12: false});
function node(tag, text, className) { const e = document.createElement(tag); if (text !== undefined) e.textContent = text; if(className) e.className=className; return e; }
function setScenario(value) { selected=value; document.querySelectorAll(".scenario").forEach(b=>b.classList.toggle("selected",b.dataset.value===value)); }
document.querySelectorAll(".scenario").forEach(b=>b.addEventListener("click",()=>{setScenario(b.dataset.value);$("extra").value="";}));
$("extra").addEventListener("change",e=>{if(e.target.value) setScenario(e.target.value);});
async function api(path, options={}) {
  const response = await fetch(path, {...options, headers:{"Content-Type":"application/json", "X-Admin-Key":$("admin-key").value, ...(options.headers||{})}});
  let body; try { body=await response.json(); } catch { throw new Error(`HTTP ${response.status}: invalid response`); }
  if(!response.ok) throw new Error(typeof body.detail==="string" ? body.detail : `HTTP ${response.status}`);
  return body;
}
function render(data) {
  const {task, account, counts, run} = data;
  $("balance").textContent=account.balance; $("ledger-sum").textContent=account.ledger_sum;
  $("user").textContent=`User ${account.user_id}`; $("invariant").textContent=account.consistent?"Инвариант подтверждён":"ОШИБКА: баланс ≠ журнал";
  $("invariant").classList.toggle("ok",account.consistent);
  $("credit-count").textContent=counts.payment; $("charge-count").textContent=counts.charge; $("refund-count").textContent=counts.refund;
  const status=task?.status||"completed";
  $("task-status").textContent=status.toUpperCase(); $("task-status").className=`state ${status}`;
  $("task-id").textContent=task?`${task.task_id}  /  ${task.model_slug}`:`Run ${run.id} / 20 concurrent payment webhooks`;
  $("flow").replaceChildren();
  const attempts=task?.attempts||[];
  attempts.forEach((a,i)=>{
    if(i) $("flow").append(node("span","→","flow-arrow"));
    const card=node("div",undefined,`provider-node ${a.status}`), title=node("div",undefined,"provider-title");
    title.append(node("span",a.provider==="alpha"?"α":"β","provider-icon"),node("strong",a.provider));
    card.append(title,node("p",`${a.status}${a.error_type?` / ${a.error_type}`:""}`)); $("flow").append(card);
  });
  if(!task) $("flow").append(node("div",`20 запросов · ${counts.payment} начисление · ${account.balance} токенов`,"flow-empty ok"));
  $("result").hidden=!(task?.result_url || (run.scenario==="late" && task?.status==="failed"));
  $("result").textContent=task?.result_url ? `RESULT  ${task.result_url}` : (data.finished?"Поздний completed проигнорирован. Результат не выдан, возврат сохранён.":"Токены возвращены. Ожидаем поздний completed, чтобы проверить его отклонение…");
  const events=task?.events||[]; $("events").replaceChildren(); $("event-count").textContent=`${events.length} events`;
  for(const event of events) {
    const row=node("div",undefined,"event-row"), detail=node("div");
    detail.append(node("strong",event.kind));
    const parts=Object.entries(event.details||{}).filter(([,v])=>v!==null).map(([k,v])=>`${k}: ${v}`);
    if(parts.length) detail.append(node("p",parts.join(" · ")));
    row.append(node("span",undefined,"event-dot"),detail,node("time",time(event.created_at))); $("events").append(row);
  }
  if(!events.length) $("events").append(node("div",task?"Ожидаем события…":"Результат платёжной проверки — в журнале токенов.","empty"));
  $("ledger").replaceChildren();
  for(const entry of account.ledger) {
    const row=node("tr"); row.append(node("td",entry.kind),node("td",`${entry.delta>0?"+":""}${entry.delta}`,entry.delta>0?"positive":"negative"),node("td",time(entry.created_at))); $("ledger").append(row);
  }
}
$("run").addEventListener("click",async()=>{
  const current=++generation; clearTimeout(timer); $("run").disabled=true; $("error").hidden=true;
  $("task-status").className="state active"; $("task-status").textContent="STARTING";
  try {
    const run=await api("/demo/runs",{method:"POST",body:JSON.stringify({scenario:selected})});
    const stopAt=Date.now()+(run.provider_timeout*2+45)*1000;
    const poll=async()=>{
      if(current!==generation) return;
      try {
        const result=await api(`/demo/runs/${run.run_id}`); if(current!==generation)return; render(result);
        if(result.finished) {$("run").disabled=false;return;}
        if(Date.now()>stopAt) throw new Error("Проверка ещё не завершена. Состояние сохранено: откройте GET /tasks/{id} и логи compose.");
        timer=setTimeout(poll,700);
      } catch(error) {$("error").textContent=error.message;$("error").hidden=false;$("run").disabled=false;}
    };
    await poll();
  } catch(error) {$("error").textContent=error.message;$("error").hidden=false;$("run").disabled=false;}
});
fetch("/health/ready").then(r=>{if(!r.ok)throw new Error();$("health").textContent="Database connected";$("health-dot").classList.add("online");}).catch(()=>{$("health").textContent="API недоступен";});
