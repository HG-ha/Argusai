'use strict';
const $=id=>document.getElementById(id);
let sid='',run='',offset=0,polling=false,sending=false,stopped=false,live=null,lastRefresh=0,loadToken=0;
const drafts=new Map();let viewMode='chat',tickTimer=null;
function queueTick(delay){clearTimeout(tickTimer);if(!stopped)tickTimer=setTimeout(tick,delay)}
const api=()=>window.pywebview.api;
function status(text,error=false){$('status').textContent=text;$('status').className=error?'error':'';layout()}
// Escape model output before adding a small, local Markdown subset. Never accept HTML.
function escapeMarkdown(text){return String(text).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function inlineMarkdown(text){
 const saved=[];const stash=html=>'\u0000'+(saved.push(html)-1)+'\u0000';
 let value=String(text).replace(/\u0000/g,'').replace(/`([^`\n]+)`/g,(_,code)=>stash('<code>'+escapeMarkdown(code)+'</code>'));
 value=value.replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+|mailto:[^\s)]+)\)/gi,(_,label,url)=>stash('<a href="'+escapeMarkdown(url)+'" target="_blank" rel="noopener noreferrer">'+escapeMarkdown(label)+'</a>'));
 value=escapeMarkdown(value).replace(/\*\*([^*\n]+)\*\*/g,'<strong>$1</strong>').replace(/__([^_\n]+)__/g,'<strong>$1</strong>').replace(/\*([^*\n]+)\*/g,'<em>$1</em>').replace(/~~([^~\n]+)~~/g,'<del>$1</del>');
 return value.replace(/\u0000(\d+)\u0000/g,(_,i)=>saved[Number(i)]||'');
}
function renderMarkdown(text){
 const lines=String(text||'').replace(/\r\n?/g,'\n').split('\n');let html='',paragraph=[],list='',fence='',code=[];
 const flush=()=>{if(paragraph.length){html+='<p>'+paragraph.map(inlineMarkdown).join('<br>')+'</p>';paragraph=[]}if(list){html+='</'+list+'>';list=''}};
 for(const line of lines){
  const marker=line.match(/^\s*(`{3,}|~{3,})(.*)$/);
  if(fence){if(marker&&marker[1][0]===fence[0]&&marker[1].length>=fence.length){html+='<pre><code>'+escapeMarkdown(code.join('\n'))+'</code></pre>';fence='';code=[]}else code.push(line);continue}
  if(marker){flush();fence=marker[1];continue}
  if(!line.trim()){flush();continue}
  const heading=line.match(/^#{1,6}\s+(.+)$/);if(heading){flush();html+='<h4>'+inlineMarkdown(heading[1])+'</h4>';continue}
  const item=line.match(/^\s*(?:([-+*])|\d+[.)])\s+(.+)$/);
  if(item){const kind=item[1]?'ul':'ol';if(paragraph.length||list&&list!==kind)flush();if(!list){list=kind;html+='<'+kind+'>'}html+='<li>'+inlineMarkdown(item[2])+'</li>';continue}
  if(/^>\s?/.test(line)){flush();html+='<blockquote>'+inlineMarkdown(line.replace(/^>\s?/,''))+'</blockquote>';continue}
  if(list)flush();paragraph.push(line);
 }
 flush();if(fence)html+='<pre><code>'+escapeMarkdown(code.join('\n'))+'</code></pre>';return html;
}
function renderReply(element,text){element.dataset.source=text;element.innerHTML=renderMarkdown(text)}
function message(role,text){if(role==='user')return null;$('messages').replaceChildren();const e=document.createElement('div');e.className='message '+role;renderReply(e,text);$('messages').append(e);layout();return e}
function layout(){
 if(viewMode==='toolbar'){api().chat_layout(44,132).catch(()=>{});return;}
 if(viewMode==='menu'){api().chat_layout(200,196).catch(()=>{});return;}
 const context=$('context');context.style.maxHeight='210px';
 const height=Math.ceil(document.body.getBoundingClientRect().height);
 api().chat_layout(Math.min(420,Math.max(52,height)),280).catch(()=>{});
}
function buttons(){const busy=!!run||sending;$('send').disabled=busy;$('input').disabled=sending;$('session').disabled=sending;$('new').disabled=sending}
async function load(target=''){
 if(sid)drafts.set(sid,$('input').value);
 const token=++loadToken;
 const r=await api().chat_snapshot(target);if(token!==loadToken)return;if(!r.ok)throw Error(r.error);
 viewMode=r.mode||'chat';$('pet-menu').hidden=viewMode!=='menu';$('chat-shell').hidden=viewMode!=='chat';$('pet-actions').hidden=viewMode!=='toolbar';document.body.classList.toggle('toolbar-view',viewMode==='toolbar');document.body.classList.toggle('menu-view',viewMode==='menu');
 const zh=String(r.locale||'zh').startsWith('zh');for(const [id,z,en] of [['menu-chat','聊天','Chat'],['menu-workbench','打开工作台','Open workbench'],['menu-snap','贴边吸附','Snap to edges'],['menu-recover','桌宠归位','Bring back'],['menu-hide','隐藏桌宠','Hide companion']])$(id).textContent=zh?z:en;
 $('menu-snap').setAttribute('aria-checked',String(!!r.snap));
 sid=r.session_id;run=r.active_run||'';offset=0;live=null;
 $('input').value=drafts.get(sid)||'';
 $('snap').checked=!!r.snap;
 renderSessions(r.sessions);
 $('messages').replaceChildren();
 $('title').textContent='ArgusAI';
 renderTasks(r.sessions);$('approval').hidden=true;status(run?'Agent 正在处理…':'');buttons();lastRefresh=Date.now();layout();
}
function renderTasks(sessions){$('tasks').replaceChildren();for(const s of sessions.filter(s=>s.running||s.pending)){const b=document.createElement('button');b.type='button';b.className='task';b.textContent=(s.pending?'◉ 待确认 · ':'● 进行中 · ')+s.name;b.onclick=()=>load(s.id).catch(e=>status(e.message,true));$('tasks').append(b)}}
async function tick(){
 if(stopped||polling)return;if(viewMode!=='chat'){queueTick(1000);return;}polling=true;
 try{
  if(run){const expected=run,selected=sid;const r=await api().chat_events(selected,expected,offset);if(run!==expected||sid!==selected)return;if(!r.ok)throw Error(r.error);offset=r.next;$('approval').hidden=!r.pending;
   let failed=false;for(const e of r.events){if(e.kind==='text'){if(!live){$('empty')?.remove();live=message('assistant','')}renderReply(live,(live.dataset.source||'')+e.text);$('messages').scrollTop=$('messages').scrollHeight}else{status(e.text,e.kind==='error');if(e.kind==='error')failed=true}}
   if(r.pending)status('等待你的确认，请在工作台处理');
   if(r.done){run='';if(!failed)status(r.pending?'等待你的确认，请在工作台处理':'');buttons()}layout();
  }else if(Date.now()-lastRefresh>5000){const r=await api().chat_snapshot(sid);if(r.ok){renderTasks(r.sessions);if(r.active_run)await load(sid)}lastRefresh=Date.now()}
 }catch(e){status(e.message,true);run='';buttons()}finally{polling=false;queueTick(400)}
}
function closeSessions(){ $('session-menu').hidden=true;$('session').setAttribute('aria-expanded','false');layout() }
function renderSessions(sessions){
 const rows=[...sessions];if(sid&&!rows.some(s=>s.id===sid))rows.unshift({id:sid,name:'新对话'});
 $('session-name').textContent=rows.find(s=>s.id===sid)?.name||'选择对话';$('session-menu').replaceChildren();
 for(const row of rows){const button=document.createElement('button');button.type='button';button.className='session-option';button.setAttribute('role','option');button.setAttribute('aria-selected',String(row.id===sid));button.textContent=row.name;button.onclick=()=>{closeSessions();load(row.id).catch(e=>status(e.message,true))};$('session-menu').append(button)}
 $('session-menu').hidden=true;$('session').setAttribute('aria-expanded','false');
}
$('session').onclick=()=>{const menu=$('session-menu');menu.hidden=!menu.hidden;$('session').setAttribute('aria-expanded',String(!menu.hidden));layout();if(!menu.hidden)(menu.querySelector('[aria-selected=true]')||menu.firstElementChild)?.focus()};
$('session-menu').onkeydown=e=>{const rows=[...$('session-menu').children],index=rows.indexOf(document.activeElement);if(e.key==='Escape'){closeSessions();$('session').focus();e.stopPropagation()}else if(e.key==='ArrowDown'||e.key==='ArrowUp'){e.preventDefault();rows[(index+(e.key==='ArrowDown'?1:-1)+rows.length)%rows.length]?.focus()}};
document.addEventListener('click',e=>{if(!e.target.closest('#session-picker')&&!$('session-menu').hidden)closeSessions()});
$('recover').onclick=()=>api().reset_position().catch(e=>status(e.message,true));
$('new').onclick=async()=>{try{const r=await api().chat_new();if(!r.ok)throw Error(r.error);await load(r.session_id)}catch(e){status(e.message,true)}};
$('workbench').onclick=$('approval').onclick=()=>api().open_assistant();
$('close').onclick=()=>api().chat_hide();
$('tasks-toggle').onclick=()=>{$('context').hidden=!$('context').hidden;if($('context').hidden)closeSessions();layout()};
$('snap').onchange=async()=>{const value=$('snap').checked;try{const r=await api().snap_enabled(value);if(!r?.ok)throw Error(r?.error||'保存失败')}catch(e){$('snap').checked=!value;status(e.message,true)}};
$('input').oninput=()=>{$('input').style.height='34px';$('input').style.height=Math.min(70,$('input').scrollHeight)+'px';layout()};
$('input').onkeydown=e=>{if(e.key==='Escape'){api().chat_hide();return;}if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing){e.preventDefault();document.querySelector('form').requestSubmit()}};
document.querySelector('form').onsubmit=async e=>{e.preventDefault();if(sending||run)return;const text=$('input').value.trim();if(!text)return;sending=true;buttons();try{if(!sid){const created=await api().chat_new();sid=created.session_id}const r=await api().chat_send(sid,text);if(!r.ok)throw Error(r.error||'发送失败');$('input').value='';$('input').style.height='34px';$('empty')?.remove();message('user',text);run=r.run_id;offset=0;live=null;status('Agent 正在处理…')}catch(error){status(error.message,true)}finally{sending=false;buttons();$('input').focus()}};
window.addEventListener('pywebviewready',async()=>{try{await load();tick()}catch(e){status(e.message,true)}},{once:true});
window.addEventListener('beforeunload',()=>{stopped=true});
window.petChatVisibility=value=>{clearTimeout(tickTimer);stopped=!value;if(value){load(sid).then(tick).catch(e=>status(e.message,true))}};

$('menu-chat').onclick=()=>api().chat_open();
$('menu-workbench').onclick=async()=>{await api().chat_hide();await api().open_assistant()};
$('menu-snap').onclick=async()=>{const value=$('menu-snap').getAttribute('aria-checked')!=='true';const r=await api().snap_enabled(value);if(r?.ok){$('menu-snap').setAttribute('aria-checked',String(value));$('snap').checked=value}};
$('menu-recover').onclick=()=>api().reset_position();
$('menu-hide').onclick=async()=>{await api().chat_hide();await api().hide()};
document.addEventListener('keydown',e=>{if(e.key==='Escape'&&viewMode==='menu')api().chat_hide()});

$('action-chat').onclick=()=>api().chat_open();
$('action-workbench').onclick=$('menu-workbench').onclick;
$('action-more').onclick=()=>api().menu();
window.addEventListener('blur',()=>{if(viewMode==='toolbar'||viewMode==='menu')api().dismiss_actions()});
