const header = document.querySelector('.header');
const navigation = header?.querySelector('nav');
if (navigation) {
  navigation.id='main-navigation';
  const toggle=document.createElement('button');
  toggle.type='button';toggle.className='mobile-menu-toggle';
  toggle.setAttribute('aria-controls','main-navigation');
  toggle.innerHTML='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" aria-hidden="true"><path d="M4 7h16M4 12h16M4 17h16"/></svg>';
  function menu(open) {
    header.dataset.menuOpen=String(open);
    toggle.setAttribute('aria-expanded',String(open));
    toggle.setAttribute('aria-label',open?'关闭导航':'打开导航');
  }
  menu(false);
  header.querySelector('.header-tools').append(toggle);
  const source=document.createElement('a');
  source.href='https://github.com/HG-ha/Argusai';source.textContent='GitHub ↗';source.className='mobile-github';
  navigation.append(source);
  toggle.addEventListener('click',()=>menu(header.dataset.menuOpen!=='true'));
  navigation.addEventListener('click',()=>menu(false));
  document.addEventListener('keydown',event=>{if(event.key==='Escape'&&header.dataset.menuOpen==='true'){menu(false);toggle.focus();}});
  document.addEventListener('click',event=>{if(!header.contains(event.target))menu(false);});
  matchMedia('(min-width: 761px)').addEventListener('change',()=>menu(false));
}
const previewLinks = [...document.querySelectorAll('[data-shot-link]')];
if (previewLinks.length) {
  const dialog = document.createElement('dialog');
  dialog.className = 'image-preview';
  dialog.setAttribute('aria-labelledby', 'preview-title');
  dialog.innerHTML = '<div class="preview-toolbar"><h2 id="preview-title"></h2><span class="preview-count" aria-live="polite"></span><button type="button" data-previous aria-label="上一张图片" title="上一张（←）"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m15 5-7 7 7 7"/></svg></button><button type="button" data-next aria-label="下一张图片" title="下一张（→）"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m9 5 7 7-7 7"/></svg></button><button type="button" data-close aria-label="关闭图片预览" title="关闭（Esc）"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m6 6 12 12M18 6 6 18"/></svg></button></div><img class="preview-image" alt=""><p class="preview-status" role="status"></p>';
  document.body.append(dialog);
  const picture = dialog.querySelector('img');
  const stage=document.createElement('div');
  stage.className='preview-stage';picture.replaceWith(stage);stage.append(picture);
  const zoom=document.createElement('button');
  zoom.type='button';zoom.setAttribute('aria-label','放大图片');zoom.title='放大图片';zoom.setAttribute('aria-pressed','false');
  zoom.innerHTML='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" aria-hidden="true"><circle cx="10" cy="10" r="6"/><path d="m15 15 5 5M7 10h6m-3-3v6"/></svg>';
  dialog.querySelector('[data-close]').before(zoom);
  function resetZoom() { stage.classList.remove('is-zoomed');zoom.setAttribute('aria-pressed','false');zoom.setAttribute('aria-label','放大图片');zoom.title='放大图片';stage.scrollTop=0;stage.scrollLeft=0; }
  zoom.addEventListener('click',()=>{
    const enlarged=stage.classList.toggle('is-zoomed');
    zoom.setAttribute('aria-pressed',String(enlarged));
    zoom.setAttribute('aria-label',enlarged?'适应屏幕':'放大图片');
    zoom.title=enlarged?'适应屏幕':'放大图片';
  });
  const title = dialog.querySelector('h2');
  const count = dialog.querySelector('.preview-count');
  const status = dialog.querySelector('.preview-status');
  let current = 0, opener = null, previousOverflow = '';
  function show(index) {
    resetZoom();
    current = (index + previewLinks.length) % previewLinks.length;
    const link = previewLinks[current];
    title.textContent = link.dataset.previewTitle || link.getAttribute('aria-label') || '应用界面';
    count.textContent = (current + 1) + ' / ' + previewLinks.length;
    picture.alt = title.textContent;
    status.textContent = '图片加载中…';
    picture.src = link.href;
  }
  picture.addEventListener('load', () => { status.textContent = previewLinks[current]?.dataset.shotLink === 'cli' ? '真实终端会话渲染 · 左右方向键切换 · Esc 关闭' : '真实应用截图 · 左右方向键切换 · Esc 关闭'; });
  picture.addEventListener('error', () => { status.textContent = '图片加载失败，请关闭后重试。'; });
  previewLinks.forEach((link,index) => link.addEventListener('click',event => {
    if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    opener = link;
    previousOverflow = document.body.style.overflow;
    show(index);
    dialog.showModal();
    document.body.style.overflow = 'hidden';
    dialog.querySelector('[data-close]').focus();
  }));
  dialog.querySelector('[data-previous]').addEventListener('click',()=>show(current-1));
  dialog.querySelector('[data-next]').addEventListener('click',()=>show(current+1));
  dialog.querySelector('[data-close]').addEventListener('click',()=>dialog.close());
  dialog.addEventListener('click',event => { if(event.target===dialog) dialog.close(); });
  dialog.addEventListener('keydown',event => {
    if(event.key==='ArrowLeft' || event.key==='ArrowRight') {
      event.preventDefault();show(current+(event.key==='ArrowRight'?1:-1));
    }
  });
  dialog.addEventListener('close',()=>{ document.body.style.overflow=previousOverflow;opener?.focus(); });
  let touchStart;
  stage.addEventListener('pointerdown',event=>{if(event.pointerType==='touch')touchStart={x:event.clientX,y:event.clientY};});
  stage.addEventListener('pointerup',event=>{
    if(!touchStart||stage.classList.contains('is-zoomed')){touchStart=null;return;}
    const dx=event.clientX-touchStart.x,dy=event.clientY-touchStart.y;
    touchStart=null;
    if(Math.abs(dx)>60&&Math.abs(dy)<50)show(current+(dx<0?1:-1));
  });
  stage.addEventListener('pointercancel',()=>{touchStart=null;});
  new MutationObserver(()=>{if(dialog.open)show(current);}).observe(document.documentElement,{attributes:true,attributeFilter:['data-theme']});
}

const flows = {
  web: {prompt:'Web 应用上线前审计', steps:[['圈定目标','设置目标域名、测试身份与授权范围。'],['观察与验证','结合扫描、浏览器和代理请求，逐项复核。'],['整理交付','关联请求、响应与发现，形成报告并跟进复测。']]},
  reverse: {prompt:'JavaScript 请求签名分析',steps:[['导入样本','把代码或 HAR 放进逆向工作台，建立分析任务。'],['定位逻辑','结合静态分析、浏览器观察与工具，追踪调用过程。'],['验证结论','用测试输入核对结果，保留复现代码和分析记录。']]},
  continuous: {prompt:'已授权资产的定期复查',steps:[['记录基线','确认资产范围、关注项和当前结果。'],['配置计划','设置触发周期、固定会话和通知渠道。'],['跟进变化','应用运行时按计划检查，结合已有记录继续调查。']]},
};
const tabs = [...document.querySelectorAll('[data-flow]')];
function selectFlow(tab) {
  const flow = flows[tab.dataset.flow];
  tabs.forEach(item => { item.setAttribute('aria-selected', String(item === tab)); item.tabIndex = item === tab ? 0 : -1; });
  document.querySelector('#flow-panel').setAttribute('aria-labelledby', tab.id);
  document.querySelector('[data-prompt]').textContent = flow.prompt;
  const list = document.querySelector('[data-steps]');
  list.replaceChildren(...flow.steps.map(([title, description]) => { const row = document.createElement('li'); const heading = document.createElement('b'); const text = document.createElement('span'); heading.textContent = title; text.textContent = description; row.append(heading, text); return row; }));
}
tabs.forEach((tab, index) => {
  tab.addEventListener('click', () => selectFlow(tab));
  tab.addEventListener('keydown', event => {
    let next;
    if (['ArrowDown', 'ArrowRight'].includes(event.key)) next = (index + 1) % tabs.length;
    if (['ArrowUp', 'ArrowLeft'].includes(event.key)) next = (index - 1 + tabs.length) % tabs.length;
    if (event.key === 'Home') next = 0;
    if (event.key === 'End') next = tabs.length - 1;
    if (next === undefined) return;
    event.preventDefault(); selectFlow(tabs[next]); tabs[next].focus();
  });
});

const tasks = {
  traffic: {prompt:'梳理测试站点的登录与 API 请求，找出需要复核的权限边界。',steps:[['在浏览器中观察业务','在授权范围内操作页面，结合代理捕获请求与响应。'],['沿请求继续调查','检索接口、身份和参数，调用请求工具做受控复核。'],['把结论连回证据','保存相关流量、发现和复现材料，交付可复查的结果。']],output:'请求与响应 / 复核记录 / 风险与证据'},
  sandbox: {prompt:'给这些响应写一个检查脚本，运行验证，把结果整理成报告。',steps:[['在会话工作区整理输入','读取任务引用的文件，明确脚本需要处理的数据。'],['编写、执行并检查结果','按需要使用 Python、Node.js 或 Shell，读取输出后继续修改。'],['保留可交付的文件','将验证脚本、运行结果和报告保存在会话工作区。']],output:'验证脚本 / 运行输出 / 报告与文件'},
  reverse: {prompt:'分析这个应用的请求签名，定位关键逻辑，编写代码核对结果。',steps:[['建立逆向作业','导入代码、应用、二进制或流量样本，关联 ReverseJob。'],['按线索组织工具','结合静态分析、解包、反编译和动态观察，追踪关键逻辑。'],['用代码复现与验证','编写验证脚本，比较输入和输出，保留复现材料。']],output:'分析记录 / 复现代码 / 样本与证据'},
  continuity: {prompt:'持续复查这批已授权资产，有变化时接着调查，并把结果发到飞书。',steps:[['明确计划与基线','保存范围、关注项、已有发现和后续待办。'],['拆分协作与安排触发','按需组织子 Agent 或小队，用定时或事件触发后续工作。'],['接上进度并报告变化','读取检查点与记录，继续调查，保存产物并投递结果。']],output:'任务计划 / 检查点 / 变化记录与渠道通知'},
};
const taskTabs = [...document.querySelectorAll('[data-task]')];
function selectTask(tab) {
  const task = tasks[tab.dataset.task];
  taskTabs.forEach(item => { item.setAttribute('aria-selected',String(item === tab)); item.tabIndex=item===tab?0:-1; });
  document.querySelector('#task-panel').setAttribute('aria-labelledby',tab.id);
  document.querySelector('[data-task-prompt]').textContent=task.prompt;
  document.querySelector('[data-task-output]').textContent=task.output;
  document.querySelector('[data-task-steps]').replaceChildren(...task.steps.map(([title,text])=>{
    const li=document.createElement('li'),b=document.createElement('b'),p=document.createElement('p');
    b.textContent=title; p.textContent=text; li.append(b,p); return li;
  }));
}
taskTabs.forEach((tab,index)=>{
  tab.addEventListener('click',()=>selectTask(tab));
  tab.addEventListener('keydown',event=>{
    let next;
    if(['ArrowRight','ArrowDown'].includes(event.key)) next=(index+1)%taskTabs.length;
    if(['ArrowLeft','ArrowUp'].includes(event.key)) next=(index-1+taskTabs.length)%taskTabs.length;
    if(event.key==='Home') next=0;
    if(event.key==='End') next=taskTabs.length-1;
    if(next===undefined)return;
    event.preventDefault();selectTask(taskTabs[next]);taskTabs[next].focus();
  });
});
