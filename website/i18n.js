(() => {
  const pairs = `
在终端里，接着工作。|Keep working in the terminal.
多轮 Agent 对话、会话与小队、待办和运行监控，都可以在命令行中使用。|Use multi-turn Agent conversations, sessions, teams, todos and runtime monitoring from the command line.
命令行使用手册 ↗|CLI user guide ↗
命令行工作台|CLI workspace
放大查看命令行工作台|Enlarge the CLI workspace
ArgusAI 真实终端会话，左侧待办与会话、中央命令和右侧运行监控|An actual ArgusAI terminal session: todos and session details on the left, commands in the center and runtime monitoring on the right
真实终端会话渲染 · 左右侧栏已展开 · 本地演示文件与状态|Actual terminal session rendering · Both sidebars expanded · Local demo files and session status
真实终端会话渲染 · 左右方向键切换 · Esc 关闭|Actual terminal session rendering · Arrow keys to browse · Esc to close
首页|Home
打开导航|Open navigation
关闭导航|Close navigation
切换主题|Switch color theme
放大图片|Zoom image
适应屏幕|Fit to screen
执行|Execution
控制|Control
连续性|Continuity
部署|Deployment
工作台|Workspace
使用场景|Use cases
更新日志|Changelog
更新|Update
恢复 QQ 接入，支持官方扫码登录、Markdown 卡片与文件回复。|Restored QQ integration with official QR sign-in, Markdown cards and file replies.
完善渠道流式与分段回复，修复多条消息漏发和多余回执。|Improved channel streaming and multi-message replies; fixed missing messages and redundant delivery receipts.
支持 QQ、飞书合并转发，完善多渠道转发与附件留存，供 Agent 按需读取。|Added QQ and Feishu merged forwards and improved forwarded content and attachment retention across channels for on-demand Agent access.
修复桌面 Agent 重启后误打开渠道会话的问题。|Fixed the desktop Agent reopening a channel conversation after restart.
主导航|Main navigation
网站主题|Color theme
浅色|Light
深色|Dark
跟随系统|System
开源安全工作台|Open-source security workspace
开源安全工作台 · ArgusAI|Agent security workspace · ArgusAI
工作台 · ArgusAI|Workspace · ArgusAI
使用场景 · ArgusAI|Use cases · ArgusAI
更新日志 · ArgusAI|Changelog · ArgusAI
页面未找到 · ArgusAI|Page not found · ArgusAI
使用文档 ↗|Documentation ↗
反馈问题 ↗|Report an issue ↗
仅用于自有系统或明确授权的安全评估。|For security assessments of systems you own or are explicitly authorized to test.
让 Agent|Let your Agent
接手安全工作。|take on security work.
ArgusAI 是开源的 Agent 安全工作台。|ArgusAI is an open-source Agent security workspace.
从浏览器和流量，到代码、样本与验证，|From browsers and traffic to code, samples and verification,
把工具交给 Agent，把过程留在你面前。|give your Agent the tools and keep its work in view.
下载 ArgusAI|Download ArgusAI
看看 Agent 怎么工作|See the Agent at work
从一句任务开始。|Start with a task.
对话、工具、文件和执行记录，在同一个工作区。|Conversation, tools, files and execution history in one workspace.
它能调动的，|Your Agent can use
是整个工作台。|the entire workspace.
Agent 能读取当前任务状态，操作浏览器、分析代理请求、建立逆向作业，按需要调用工具和技能。你可以随时打开功能页，看它查到了什么、正在做什么。|The Agent can inspect task state, operate a browser, analyze proxy requests and create reverse-engineering jobs. Open the relevant workspace at any time to inspect findings and progress.
展开 Agent 的工作过程 ↗|Explore how the Agent works ↗
浏览器与流量|Browser & traffic
打开页面、观察交互，捕获请求，沿着真实流量分析接口与权限。|Open pages, observe interactions and capture requests to investigate APIs and access boundaries.
代码与沙盒|Code & sandbox
编写并运行脚本，检查输出，把报告和中间产物留在会话工作区。|Write and run scripts, inspect output and keep reports and intermediate files in the session workspace.
样本与逆向|Samples & reverse engineering
从 JS、WASM 到应用与二进制样本，组织分析工具，复现并验证结论。|Analyze JS, WASM, applications and binaries; build reproductions and verify findings.
协作与续跑|Collaboration & continuity
拆分子任务、组织小队、保存检查点，由计划和事件接续后面的工作。|Delegate subtasks, coordinate teams and save checkpoints. Schedules and events can resume work.
离开电脑，|Away from your computer?
也能把任务交给它。|Send your Agent a task.
在 QQ、飞书、Telegram、Slack 等渠道发送任务与文件，接收回复和产物。ArgusAI 在你的电脑或服务器上继续运行。|Send tasks and files through QQ, Feishu, Telegram, Slack and other channels. ArgusAI keeps working on your computer or server and sends back replies and artifacts.
了解通讯接入 ↗|Explore messaging channels ↗
模型、工具和数据，按你的环境来。|Your environment. Your models, tools and data.
支持多端点路由和本地大模型。准备好模型与工具依赖后，可以部署在内网或离线环境，处理敏感业务。底层运行时也可用于文件处理、代码执行和通用 Agent 任务。|Use multiple model endpoints or local models. With models and dependencies prepared, deploy in an intranet or offline environment for sensitive workloads. The runtime also supports file processing, code execution and general Agent tasks.
使用场景与本地部署 ↗|Use cases & local deployment ↗
Agent 对话|Agent conversation
Agent 沙盒|Agent sandbox
安全检查|Security checks
模型接入|Model connections
任务描述、工具调用与执行结果。|Task instructions, tool calls and execution results.
Agent 实际生成的文件与 Markdown 预览。|An actual Agent-generated file and its Markdown preview.
授权范围、执行环境与扫描检查项。|Authorized scope, execution environment and scan checks.
官方端点、模型路由与上下文设置。|Official endpoints, model routing and context settings.
放大查看|Enlarge
放大查看Agent 对话|Enlarge Agent conversation
放大查看Agent 沙盒|Enlarge Agent sandbox
放大查看安全检查|Enlarge security checks
放大查看模型接入|Enlarge model connections
Agent 对话浅色主题实拍|Agent conversation screenshot, light theme
Agent 对话深色主题实拍|Agent conversation screenshot, dark theme
Agent 沙盒浅色主题实拍|Agent sandbox screenshot, light theme
Agent 沙盒深色主题实拍|Agent sandbox screenshot, dark theme
安全检查浅色主题实拍|Security checks screenshot, light theme
安全检查深色主题实拍|Security checks screenshot, dark theme
模型接入浅色主题实拍|Model connections screenshot, light theme
模型接入深色主题实拍|Model connections screenshot, dark theme
Agent 对话实拍|Actual Agent conversation
Agent 工作能力|Agent capabilities
Agent 的工作现场|The Agent at work
Agent 是入口，浏览器、流量、逆向和沙盒是它的工作环境。|Start with the Agent. Browsers, traffic, reverse engineering and the sandbox are its working environment.
沿着一个任务，看看这些能力怎样接在一起。|Follow a task to see how these capabilities work together.
Agent 怎么工作|How the Agent works
选择一个环节，查看它使用的材料、操作与产物。|Choose a stage to explore its inputs, actions and artifacts.
Agent 工作环节|Agent workflow stages
捕获与分析流量|Capture & analyze traffic
沙盒执行与验证|Run & verify in the sandbox
样本逆向|Reverse-engineer samples
协作与持续任务|Collaborate & continue
你交给 Agent 的任务|Your task for the Agent
工作流示意 · 非实时执行结果|Illustrative workflow · Not a live execution
梳理测试站点的登录与 API 请求，找出需要复核的权限边界。|Map login and API requests in a test site and identify access boundaries to review.
在浏览器中观察业务|Observe the application in a browser
在授权范围内操作页面，结合代理捕获请求与响应。|Interact with pages within the authorized scope and capture requests and responses through the proxy.
沿请求继续调查|Follow the requests
检索接口、身份和参数，调用请求工具做受控复核。|Inspect endpoints, identities and parameters, then use request tools for controlled verification.
把结论连回证据|Connect findings to evidence
保存相关流量、发现和复现材料，交付可复查的结果。|Save traffic, findings and reproduction material for review.
留下的材料|Artifacts
请求与响应 / 复核记录 / 风险与证据|Requests & responses / Verification records / Findings & evidence
看看实际工作界面|Explore the actual workspace
独立演示环境实拍，点击图片查看细节。|Actual screenshots from an isolated demo. The app interface shown is in Chinese. Click to inspect details.
Agent 在流量里找线索。|The Agent follows clues in your traffic.
Agent 可以结合浏览器操作与 MITM 代理捕获业务流量，检索请求、读取响应、组织重放与受控验证。已有 HAR 或 Burp 日志，也能作为分析材料。|The Agent combines browser actions with MITM traffic capture, searches requests, reads responses and organizes replay and controlled verification. Existing HAR files and Burp logs can also be used as input.
功能页用于观察和介入；Agent 对话接着当前任务继续调查，省去在多个工具之间反复搬运材料。|Inspect or intervene in the workspace while the Agent continues the investigation using the same task context.
浏览器与代理使用文档 ↗|Browser & proxy documentation ↗
分析材料|Analysis inputs
页面与交互|Pages & interactions
请求、响应与身份上下文|Requests, responses & identity context
HAR / Burp 日志|HAR / Burp logs
需要验证，就写代码运行。|When verification needs code, run it.
每个 Agent 会话有自己的文件工作区。它可以编写 Python、Node.js 或 Shell 脚本，执行后检查结果，再修改、复测，把报告、截图和中间文件留在同一个任务里。|Each Agent session has its own file workspace. It can write Python, Node.js or shell scripts, inspect their output, revise and retest, keeping reports, screenshots and intermediate files with the task.
运行时按需准备，操作遵循工具授权。会话沙盒用于组织文件和执行环境，实际访问权限由工具与宿主机共同决定。|Runtimes are prepared as needed and operations follow tool permissions. The session sandbox organizes files and execution; actual access is governed by tool policy and the host system.
会话工作区 · 文件组织示意|Session workspace · Illustrative file structure
会话、沙盒与文件 ↗|Sessions, sandbox & files ↗
逆向是一个可推进的任务。|Reverse engineering is a task the Agent can advance.
导入 JS、WASM、APK、IPA、PE、ELF 或 HAR，建立 ReverseJob。Agent 围绕样本安排静态初筛、解包、反编译和动态观察，编写复现代码，再用输入与输出核对结论。|Import JS, WASM, APK, IPA, PE, ELF or HAR into a ReverseJob. The Agent organizes static triage, unpacking, decompilation and dynamic observation, then writes reproductions and checks inputs against outputs.
按环境连接 JADX、APKTool、Ghidra、Frida 等工具，样本、日志和证据保留在同一个作业中。|Connect tools such as JADX, APKTool, Ghidra and Frida as your environment allows. Samples, logs and evidence stay with the job.
逆向工作台使用文档 ↗|Reverse workspace documentation ↗
逆向分析流程|Reverse-engineering workflow
代码 / 应用 / 二进制样本|Code / Applications / Binary samples
定位逻辑 · 工具分析 · 代码复现|Locate logic · Analyze with tools · Reproduce in code
验证记录与产物|Verification records & artifacts
任务可以持续，也可以分工。|Keep tasks running. Share the work.
主 Agent 可以拆分子任务，持久小队按角色、模型和工具范围协作。待办、检查点与证据记录帮助长任务持续推进。|The main Agent can delegate subtasks. Persistent teams collaborate with separate roles, models and tool scopes. Todos, checkpoints and evidence records support long-running work.
固定间隔、Cron、Webhook 或文件变化可以触发后续工作。指定会话接上历史；任务完成后收束计划。调度执行需要 ArgusAI 和所需资源保持可用。|Intervals, Cron, webhooks or file changes can trigger follow-up work in a chosen session. Plans can be concluded when tasks finish. Scheduled execution requires ArgusAI and its resources to remain available.
持续任务与调度 ↗|Continuous tasks & scheduling ↗
工作留下来，下次接着做。|Save your progress. Pick it up next time.
目标与计划|Goals & plans
会话与检查点|Sessions & checkpoints
文件与证据|Files & evidence
基线与变化|Baselines & changes
模型由你选。|Choose your model.
多端点回退、模型切换、自动上下文预算与手动覆盖。支持官方服务和本地模型。|Endpoint fallback, model switching, automatic context budgets and manual overrides. Use hosted services or local models.
在熟悉的聊天软件里用 Agent。|Use your Agent from your messaging app.
配对后发送任务和文件，接收执行结果与产物。电脑或服务器上的 ArgusAI 负责执行；渠道的消息格式、文件和交互能力随平台而异。|Pair a channel to send tasks and files and receive results. ArgusAI executes on your computer or server. Message formats, file support and interactions depend on the platform.
飞书|Feishu
也是你的通用 Agent 底座。|A foundation for your own Agents, too.
用 Python Check、Agent Tool、Markdown Skill 和 MCP 扩展能力，连接本地 ACP Agent 与已有工具。|Extend capabilities with Python Checks, Agent Tools, Markdown Skills and MCP, and connect local ACP Agents and existing tools.
查看源码 ↗|View source ↗
审计、逆向与持续巡检|Audits, reverse engineering & ongoing monitoring
选择一种任务，了解所需输入、执行过程和交付结果。|Choose a task to explore its inputs, execution and deliverables.
从任务到工作区|From task to workspace
对话、文件、扫描与模型配置。点击任一图片放大浏览。|Conversations, files, scans and model settings. Screenshots show the Chinese app interface. Click to enlarge.
Web 安全审计|Web security audit
请求、身份、权限与证据|Requests, identities, permissions & evidence
逆向分析|Reverse engineering
从样本进入分析工作区|Bring samples into the analysis workspace
持续调查与巡检|Ongoing investigation & monitoring
保留进度，按计划继续|Save progress and resume on schedule
Web 应用上线前审计|Pre-release web application audit
圈定目标|Define the scope
设置目标域名、测试身份与授权范围。|Set target domains, test identities and the authorized scope.
观察与验证|Observe & verify
结合扫描、浏览器和代理请求，逐项复核。|Review findings with scans, browser observations and proxy requests.
整理交付|Prepare deliverables
关联请求、响应与发现，形成报告并跟进复测。|Link requests, responses and findings, prepare reports and follow up with retesting.
本地模型与离线部署|Local models & offline deployment
接入本地大模型与内网工具，适配敏感业务。离线运行时，准备好模型和依赖，按环境禁用联网能力。|Connect local models and intranet tools for sensitive workloads. Prepare models and dependencies before going offline and disable network features as required.
查看部署文档 →|Deployment documentation →
部署结构示意|Deployment diagram
受控环境|Controlled environment
代码与样本|Code & samples
流量与日志|Traffic & logs
工具 · 会话 · 证据|Tools · Sessions · Evidence
本地模型|Local model
内网工具|Intranet tools
也可按需连接外部模型服务|External model services can also be connected as needed
工具执行|Tool execution
Agent 可以读取当前任务、调用工具、运行技能、组织小队。分析与操作沿着同一个任务推进。|The Agent can inspect the current task, invoke tools, run skills and coordinate teams, keeping analysis and execution connected.
会话与任务记录|Sessions & task history
会话、待办、检查点与定时任务保留进度。应用运行时，可以按计划触发后续工作。|Sessions, todos, checkpoints and schedules preserve progress. Follow-up work can run on schedule while the application remains active.
权限与模型设置|Permissions & model settings
多端点切换、上下文预算、工具确认与授权范围，都可以按你的环境配置。|Configure endpoints, context budgets, tool approvals and authorized scopes for your environment.
版本记录|Release history
查看功能变化与问题修复。安装包见对应的 GitHub Release。|Browse improvements and fixes. Installers are available in each GitHub Release.
最新记录|Latest release
修复|Fixes
修复 macOS 标题栏显示。|Fix macOS title bar rendering.
修复 macOS 自定义标题栏显示异常。|Fix incorrect rendering of the custom macOS title bar.
发布说明与下载 ↗|Release notes & downloads ↗
改进 · 修复|Improvements · Fixes
桌面交互与 AI 端点诊断。|Desktop interactions & model endpoint diagnostics.
修复 Windows 标题栏按钮与拖拽区域的交互冲突，保留空白区域拖拽和双击最大化。|Fix conflicts between Windows title bar buttons and drag regions, preserving dragging in empty areas and double-click maximization.
改善 Windows 窗口贴靠、任务栏恢复与窗口状态同步。|Improve Windows snapping, taskbar restoration and window state synchronization.
端点测试使用编辑器里尚未保存的连接、模型、协议及自定义参数。|Endpoint tests use unsaved connection, model, protocol and custom parameters from the editor.
改进已保存密钥的复用与模型探测，诊断结果不回显密钥。|Improve saved-key reuse and model probing without exposing keys in diagnostic results.
查看诊断细节|Diagnostic details
端点探测采用 16 token 输出上限和 30 秒超时；关闭阶段的异常不再覆盖测试结果。|Endpoint probes use a 16-token output limit and a 30-second timeout. Errors during cleanup no longer overwrite test results.
日期按北京时间显示。更早版本请参阅|Dates use Beijing time (UTC+8). For earlier versions, see
这个页面找不到了。|This page could not be found.
地址可能有误，或页面已经移动。|The address may be incorrect, or the page may have moved.
返回首页 →|Back to home →
应用界面|Application screenshot
上一张图片|Previous image
下一张图片|Next image
关闭图片预览|Close image preview
上一张（←）|Previous (←)
下一张（→）|Next (→)
关闭（Esc）|Close (Esc)
图片加载中…|Loading image…
真实应用截图 · 左右方向键切换 · Esc 关闭|Actual app screenshot · Arrow keys to browse · Esc to close
图片加载失败，请关闭后重试。|Image failed to load. Close the preview and try again.
给这些响应写一个检查脚本，运行验证，把结果整理成报告。|Write a script to check these responses, run it and prepare a report.
在会话工作区整理输入|Prepare inputs in the session workspace
读取任务引用的文件，明确脚本需要处理的数据。|Read the files referenced by the task and identify the data the script needs.
编写、执行并检查结果|Write, run and inspect
按需要使用 Python、Node.js 或 Shell，读取输出后继续修改。|Use Python, Node.js or shell as needed, inspect the output and revise.
保留可交付的文件|Save the deliverable files
将验证脚本、运行结果和报告保存在会话工作区。|Save verification scripts, execution results and reports in the session workspace.
验证脚本 / 运行输出 / 报告与文件|Verification scripts / Execution output / Reports & files
分析这个应用的请求签名，定位关键逻辑，编写代码核对结果。|Analyze this application's request signature, locate the key logic and verify it in code.
建立逆向作业|Create a reverse-engineering job
导入代码、应用、二进制或流量样本，关联 ReverseJob。|Import code, applications, binaries or traffic samples into a ReverseJob.
按线索组织工具|Choose tools to follow the clues
结合静态分析、解包、反编译和动态观察，追踪关键逻辑。|Trace key logic with static analysis, unpacking, decompilation and dynamic observation.
用代码复现与验证|Reproduce and verify in code
编写验证脚本，比较输入和输出，保留复现材料。|Write verification scripts, compare inputs and outputs and save reproduction material.
分析记录 / 复现代码 / 样本与证据|Analysis records / Reproduction code / Samples & evidence
持续复查这批已授权资产，有变化时接着调查，并把结果发到飞书。|Continuously review these authorized assets, investigate changes and send results to Feishu.
明确计划与基线|Establish a plan and baseline
保存范围、关注项、已有发现和后续待办。|Save the scope, focus areas, existing findings and follow-up todos.
拆分协作与安排触发|Delegate work and configure triggers
按需组织子 Agent 或小队，用定时或事件触发后续工作。|Coordinate subagents or teams and trigger follow-up work on schedules or events.
接上进度并报告变化|Resume progress and report changes
读取检查点与记录，继续调查，保存产物并投递结果。|Read checkpoints and records, continue investigating, save artifacts and deliver results.
任务计划 / 检查点 / 变化记录与渠道通知|Task plans / Checkpoints / Change records & channel notifications
JavaScript 请求签名分析|JavaScript request signature analysis
导入样本|Import samples
把代码或 HAR 放进逆向工作台，建立分析任务。|Import code or HAR into the reverse workspace and create an analysis task.
定位逻辑|Locate the logic
结合静态分析、浏览器观察与工具，追踪调用过程。|Trace calls using static analysis, browser observations and tools.
验证结论|Verify findings
用测试输入核对结果，保留复现代码和分析记录。|Check results against test inputs and save reproduction code and analysis records.
已授权资产的定期复查|Periodic review of authorized assets
记录基线|Record a baseline
确认资产范围、关注项和当前结果。|Confirm asset scope, focus areas and current findings.
配置计划|Configure a schedule
设置触发周期、固定会话和通知渠道。|Set the trigger interval, target session and notification channel.
跟进变化|Follow up on changes
应用运行时按计划检查，结合已有记录继续调查。|Run scheduled checks while the app is active and continue investigating with existing records.
ArgusAI 开源安全工作台与通用 Agent 底座。让 Agent 调用扫描、代理、浏览器和逆向工具，在本地推进完整任务。|ArgusAI is an open-source Agent security workspace and general Agent runtime. Connect scans, proxy traffic, browsers and reverse engineering to execute complete tasks locally.
了解 ArgusAI 的 Agent 执行、扫描、代理、逆向分析、多模型与通讯接入。|Explore ArgusAI Agent execution, scanning, proxy analysis, reverse engineering, models and messaging channels.
用 ArgusAI 推进安全审计、逆向分析与持续巡检，支持本地模型和企业内网环境。|Use ArgusAI for security audits, reverse engineering and continuous monitoring with local models and enterprise intranet deployment.
ArgusAI 版本更新日志：功能改进、问题修复与版本下载。|ArgusAI changelog: improvements, fixes and release downloads.
`;
  const en = Object.fromEntries(pairs.trim().split('\n').map(line => {
    const split = line.indexOf('|'); return [line.slice(0,split),line.slice(split+1)];
  }));
  const zh = Object.fromEntries(Object.entries(en).map(([source,translation])=>[translation,source]));
  let language;
  try { language = new URLSearchParams(location.search).get('lang') || localStorage.getItem('argusai.site.language'); } catch (_) {}
  if (!['zh','en'].includes(language)) language = navigator.language.toLowerCase().startsWith('zh') ? 'zh' : 'en';
  try { localStorage.setItem('argusai.site.language',language); } catch (_) {}
  const originalText = new WeakMap(), originalAttrs = new WeakMap();
  const attributes = ['aria-label','title','alt','content','data-preview-title'];
  let observer, button;
  function translate(text) { return language === 'en' ? (en[text] || text) : (zh[text] || text); }
  function localize(root) {
    if (root.nodeType === Node.TEXT_NODE) {
      if (root.parentElement?.closest('script,style,code,pre,[data-no-i18n]')) return;
      const current = root.nodeValue;
      const previous = originalText.get(root);
      const source = previous && current === previous.last ? previous.source : current;
      let next = source.replace(/\S[\s\S]*\S|\S/, value => translate(value));
      if (language === 'en' && root.nextSibling?.nodeName === 'BR') next += ' ';
      if (current !== next) root.nodeValue = next;
      originalText.set(root,{source,last:next});
      return;
    }
    if (root.nodeType !== Node.ELEMENT_NODE || root.matches('script,style,code,pre,[data-no-i18n]')) return;
    const saved = originalAttrs.get(root) || {};
    for (const name of attributes) {
      if (!root.hasAttribute(name)) continue;
      const current = root.getAttribute(name), previous = saved[name];
      const source = previous && current === previous.last ? previous.source : current;
      const next = translate(source);
      if (current !== next) root.setAttribute(name,next);
      saved[name] = {source,last:next};
    }
    originalAttrs.set(root,saved);
    root.childNodes.forEach(localize);
  }
  function apply() {
    observer?.disconnect();
    document.documentElement.lang = language === 'zh' ? 'zh-CN' : 'en';
    localize(document.documentElement);
    if(button) {
      button.querySelector('span').textContent = language === 'zh' ? 'EN' : '中';
      button.setAttribute('aria-label',language === 'zh' ? 'Switch to English' : '切换为中文');
      button.title = language === 'zh' ? 'English' : '简体中文';
    }
    observer?.observe(document.documentElement,{subtree:true,childList:true,characterData:true,attributes:true,attributeFilter:attributes});
  }
  document.documentElement.lang = language === 'zh' ? 'zh-CN' : 'en';
  document.addEventListener('DOMContentLoaded', () => {
    button = document.createElement('button');
    button.type='button'; button.className='language-switch'; button.dataset.noI18n='';
    button.innerHTML='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a17 17 0 0 1 0 18 17 17 0 0 1 0-18Z"/></svg><span></span>';
    (document.querySelector('.header-tools') || document.querySelector('.header'))?.prepend(button);
    button.addEventListener('click',()=>{
      language=language==='zh'?'en':'zh';
      try {localStorage.setItem('argusai.site.language',language);}catch(_){}
      const url=new URL(location.href);
      if(url.searchParams.has('lang')) {url.searchParams.set('lang',language);history.replaceState(null,'',url);}
      apply();
    });
    observer=new MutationObserver(apply);
    apply();
  });
})();
