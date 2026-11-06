// Companion-only adapter for the pinned pywebview bridge. Static closures keep
// the local pages compatible with WebKit without enabling CSP unsafe-eval.
(()=>{
 const allowed=new Set(['status','interact','menu','chat_snapshot','chat_open','dismiss_actions','chat_hide','chat_layout','chat_new','chat_send','chat_events','open_assistant','move','save_position','hide','finish_drag','snap_enabled','reset_position']);
 let current=window.pywebview;
 function install(bridge){
  if(!bridge)return bridge;
  bridge._createApi=function(functions){
   for(const entry of functions){
    const name=entry.func;if(!allowed.has(name))continue;
    bridge._returnValuesCallbacks[name]={};
    bridge.api[name]=function(...args){
     const id=String(Math.random()).slice(2);
     const promise=new Promise((resolve,reject)=>bridge._checkValue(name,resolve,reject,id));
     bridge._jsApiCallback(name,args,id);return promise;
    };
   }
  };return bridge;
 }
 Object.defineProperty(window,'pywebview',{configurable:true,get:()=>current,set:value=>{current=install(value)}});
 if(current)install(current);
})();
