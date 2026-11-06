'use strict';
const image=document.getElementById('dragon'),label=document.getElementById('label'),menu=document.getElementById('menu'),error=document.getElementById('error');
const allowed=new Set(['idle','thinking','working','wave','complete','sleep','alert','drag','drag-left','drag-right','drag-up','drag-down','release']);
let current='idle',timer=null,transition=null,visible=true,drag=null,moving=false,pendingMove=[0,0],polling=false;
const labels={en:{idle:'Ready',thinking:'Thinking',working:'Working',wave:'Waiting for you',complete:'Complete',sleep:'Resting',alert:'Needs attention'},zh:{idle:'待命',thinking:'思考中',working:'执行中',wave:'等待你的确认',complete:'已完成',sleep:'休息中',alert:'需要查看'}};
function api(){return window.pywebview?.api}
function change(state){if(!allowed.has(state)||state===current)return;clearTimeout(transition);current=state;image.src=(state==='drag'?'drag-start':state)+'.webp';if(state==='drag')transition=setTimeout(()=>{if(current==='drag')image.src='drag.webp'},450);if(state==='release')transition=setTimeout(()=>{if(current==='release'){current='';clearTimeout(timer);poll()}},450)}
async function poll(){if(!visible||polling)return;polling=true;try{const s=await api().status();change(s.animation||s.state);image.style.transform=s.mirrored?'scaleX(-1)':'none';const zh=String(s.locale).startsWith('zh');label.textContent=s.task||(zh?labels.zh:labels.en)[s.state]||'ArgusAI';document.getElementById('open').textContent=zh?'打开 Agent 对话':'Open Agent';document.getElementById('hide').textContent=zh?'隐藏桌宠':'Hide companion'}catch(_){/* Main window may be shutting down. */}finally{polling=false;if(visible)timer=setTimeout(poll,750)}}
window.petVisibility=(value)=>{visible=!!value;clearTimeout(timer);timer=null;if(visible){current='';change('idle');poll()}else{image.src='poster.png'}};
image.onerror=()=>{error.hidden=false;error.textContent='ArgusAI · Animation unavailable'};
image.onload=()=>{error.hidden=true};
window.addEventListener('pywebviewready',()=>poll(),{once:true});
window.addEventListener('beforeunload',()=>{visible=false;clearTimeout(timer)});
async function flushMove(){if(moving||(!pendingMove[0]&&!pendingMove[1])||!api())return;moving=true;const [x,y]=pendingMove;pendingMove=[0,0];try{await api().move(Math.round(x),Math.round(y))}finally{moving=false;if(pendingMove[0]||pendingMove[1])flushMove()}}
image.addEventListener('pointerdown',e=>{if(e.button!==0)return;menu.hidden=true;image.style.transform='none';drag={x:e.screenX,y:e.screenY,lastX:e.screenX,lastY:e.screenY,moved:false};image.setPointerCapture(e.pointerId)});
image.addEventListener('pointermove',e=>{if(!drag)return;const dx=e.screenX-drag.lastX,dy=e.screenY-drag.lastY;drag.lastX=e.screenX;drag.lastY=e.screenY;if(Math.hypot(e.screenX-drag.x,e.screenY-drag.y)>3)drag.moved=true;if(drag.moved){if(Math.abs(dx)+Math.abs(dy)>=3)change('drag-'+(Math.abs(dx)>=Math.abs(dy)?(dx>0?'right':'left'):(dy>0?'down':'up')));pendingMove[0]+=dx;pendingMove[1]+=dy;flushMove().catch(()=>{})}});
image.addEventListener('pointerup',async e=>{if(!drag)return;const moved=drag.moved||Math.hypot(e.screenX-drag.x,e.screenY-drag.y)>3;drag=null;if(image.hasPointerCapture(e.pointerId))image.releasePointerCapture(e.pointerId);if(moved){while(moving)await new Promise(r=>setTimeout(r,20));await flushMove();await api()?.finish_drag()}else{change('wave');await api()?.interact()}});
image.addEventListener('pointercancel',()=>{drag=null;api()?.finish_drag()});
document.addEventListener('contextmenu',e=>{e.preventDefault();menu.hidden=true;api()?.menu()});
document.getElementById('open').onclick=async()=>{menu.hidden=true;await api()?.open_assistant()};
document.getElementById('hide').onclick=async()=>{menu.hidden=true;await api()?.hide()};
