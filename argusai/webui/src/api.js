// Thin wrapper around the pywebview js_api.
// pywebview sets `window.pywebview.api` after the webview is ready.

function waitReady() {
  if (window.pywebview && window.pywebview.api) return Promise.resolve();
  return new Promise((resolve) => {
    window.addEventListener("pywebviewready", () => resolve(), { once: true });
  });
}

function isPlainObject(value) {
  if (!value || typeof value !== "object") return false;
  const proto = Object.getPrototypeOf(value);
  return proto === Object.prototype || proto === null;
}

function safeClone(value, depth = 0, seen = new WeakSet()) {
  if (value == null || typeof value === "string" || typeof value === "number" || typeof value === "boolean") return value;
  if (typeof value === "bigint") return String(value);
  if (typeof value !== "object") return undefined;
  if (value instanceof Date) return value.toISOString();
  if (value instanceof File) {
    return { name: value.name, size: value.size, type: value.type, lastModified: value.lastModified };
  }
  if (value instanceof Blob) return { size: value.size, type: value.type };
  if (typeof Node !== "undefined" && value instanceof Node) return `[${value.nodeName}]`;
  if (typeof Window !== "undefined" && value instanceof Window) return "[Window]";
  if (seen.has(value)) return "[Circular]";
  if (depth >= 6) return "[MaxDepth]";
  seen.add(value);
  if (Array.isArray(value)) {
    return value.slice(0, 100).map((item) => safeClone(item, depth + 1, seen)).filter((item) => item !== undefined);
  }
  if (!isPlainObject(value)) {
    return String(value);
  }
  const out = {};
  for (const k of Object.keys(value).slice(0, 100)) {
    const v = value[k];
    if (typeof v === "function" || typeof v === "symbol") continue;
    const cloned = safeClone(v, depth + 1, seen);
    if (cloned !== undefined) out[k] = cloned;
  }
  return out;
}

async function call(method, ...args) {
  await waitReady();
  const fn = window.pywebview.api[method];
  if (typeof fn !== "function") {
    throw new Error(`Unknown API method: ${method}`);
  }
  const safeArgs = args.map((arg) => safeClone(arg));
  const startedAt = performance.now();
  const timer = window.setTimeout(() => {
    console.warn(`[bridge] ${method} is still pending`, safeArgs);
  }, 2000);
  try {
    return await fn(...safeArgs);
  } catch (error) {
    console.error(`[bridge] ${method} failed after ${Math.round(performance.now() - startedAt)}ms`, error);
    throw error;
  } finally {
    window.clearTimeout(timer);
  }
}

export const api = {
  // Generic escape hatch for bridge methods without a dedicated wrapper
  // (e.g. the browser_live_* family used by the Browser panel).
  call: (method, ...args) => call(method, ...args),
  setAppAttentionState: (state) => call("set_app_attention_state", state || {}),
  // Introspection
  listChecks: () => call("list_checks"),
  appInfo: () => call("app_info"),
  setLocale: (locale) => call("set_locale", locale),

  // Task management
  listTasks: () => call("list_tasks"),
  getTask: (id) => call("get_task", id),
  getTaskEvents: (id, since) => call("get_task_events", id, since || 0),
  createTask: (params) => call("create_task", params),
  cancelTask: (id) => call("cancel_task", id),
  deleteTask: (id) => call("delete_task", id),
  rescanTask: (id) => call("rescan_task", id),

  // Settings
  getSettings: () => call("get_settings"),
  saveSettings: (data) => call("save_settings", data),
  chooseDataRootDirectory: (initialDir) => call("choose_data_root_directory", initialDir || ""),
  previewDataRootChange: (newRoot) => call("preview_data_root_change", newRoot || ""),
  applyDataRootChange: (newRoot, mode) => call("apply_data_root_change", newRoot || "", mode || "copy"),
  getDataRootChangeProgress: () => call("get_data_root_change_progress"),
  scanToolchainPaths: () => call("scan_toolchain_paths"),
  installReverseToolchain: (tool, opts) => call("install_reverse_toolchain", tool, !!(opts && opts.force)),
  sandboxRuntimeStatus: () => call("sandbox_runtime_status"),
  installSandboxRuntime: (runtime, opts) => call("install_sandbox_runtime", runtime, !!(opts && opts.force)),
  listAgentRuntimes: () => call("list_agent_runtimes"),
  testAgentRuntime: (runtimeId) => call("test_agent_runtime", runtimeId || ""),
  saveAgentRuntime: (payload) => call("save_agent_runtime", payload || {}),
  deleteAgentRuntime: (runtimeId) => call("delete_agent_runtime", runtimeId || ""),
  listSessionModels: (sessionId, refresh, runtimeId, runtime) =>
    call(
      "list_session_models",
      sessionId || "",
      !!refresh,
      runtimeId || "",
      runtime && typeof runtime === "object" ? runtime : {},
    ),
  switchSessionModel: (sessionId, model) =>
    call(
      "switch_session_model",
      sessionId || "",
      Number.isFinite(model && model.index) ? model.index : -1,
      (model && model.value) || "",
      (model && model.name) || "",
    ),
  chooseAgentProjectDirectory: (initialDir) => call("choose_agent_project_directory", initialDir || ""),
  chooseAgentRuntimeExecutable: (initialPath) => call("choose_agent_runtime_executable", initialPath || ""),
  listChannelProviders: () => call("list_channel_providers"),
  listChannels: () => call("list_channels"),
  saveChannel: (payload) => call("save_channel", payload || {}),
  setChannelEnabled: (id, enabled) => call("set_channel_enabled", id || "", !!enabled),
  deleteChannel: (id) => call("delete_channel", id || ""),
  testChannel: (id) => call("test_channel", id || ""),
  createChannelPairing: (id, sessionId) => call("create_channel_pairing", id || "", sessionId || ""),
  listChannelIdentities: (id) => call("list_channel_identities", id || ""),
  bindChannelIdentity: (identityId, sessionId) => call("bind_channel_identity", identityId || "", sessionId || ""),
  setChannelIdentityPermissions: (identityId, permissions) => call("set_channel_identity_permissions", identityId || "", permissions || []),
  deleteChannelIdentity: (identityId) => call("delete_channel_identity", identityId || ""),
  startWechatLogin: () => call("start_wechat_login"),
  pollWechatLogin: (qrcode, channelId) => call("poll_wechat_login", qrcode || "", channelId || ""),
  getDefaultDirs: () => call("default_dirs"),
  loggingStatus: () => call("logging_status"),
  openLogDir: () => call("open_log_dir"),
  listMcpServers: () => call("list_mcp_servers"),
  searchMcpRegistry: (query = "", cursor = "", limit = 20) => call("search_mcp_registry", query, cursor, limit),
  searchMcpMarketplace: (source = "modelscope", query = "", cursor = "", limit = 20, category = "", hosted = "") => call(
    "search_mcp_marketplace",
    source,
    query,
    cursor,
    limit,
    category,
    hosted,
  ),
  getMcpMarketplaceEntry: (source, id) => call("get_mcp_marketplace_entry", source || "", id || ""),
  saveMcpServer: (payload) => call("save_mcp_server", payload || {}),
  deleteMcpServer: (id) => call("delete_mcp_server", id),
  testMcpServer: (id) => call("test_mcp_server", id),
  listMcpServerTools: (id) => call("list_mcp_server_tools", id || ""),
  probeMcpServers: () => call("probe_mcp_servers"),
  ensureMcpStartupProbe: () => call("ensure_mcp_startup_probe"),

  // Headless host OS service (argusai serve)
  getHostServiceStatus: () => call("get_host_service_status"),
  setHostService: (opts) => call(
    "set_host_service",
    !!(opts && opts.enabled),
    !!(opts && opts.cronOnly),
    (opts && opts.bootPolicy) || "",
    opts && opts.startNow === false ? false : true,
  ),
  startHostService: () => call("start_host_service"),
  stopHostService: () => call("stop_host_service"),
  openMacosLoginItems: () => call("open_macos_login_items"),

  // Knowledge base
  kbList: (opts) => call("kb_list", !!(opts && opts.includeSuperseded), !!(opts && opts.includeTrashed)),
  kbGet: (id) => call("kb_get", id),
  kbSearch: (query, opts) => call(
    "kb_search",
    query || "",
    (opts && Array.isArray(opts.tags)) ? opts.tags : null,
    Number.isFinite(opts && opts.limit) ? opts.limit : 20,
    !!(opts && opts.includeSuperseded),
  ),
  kbSave: (payload) => call("kb_save", payload || {}),
  kbUpdate: (id, payload) => call("kb_update", id, payload || {}),
  kbDelete: (id, reason) => call("kb_delete", id, reason || ""),
  kbRestore: (id) => call("kb_restore", id),
  kbListTrash: () => call("kb_list_trash"),
  kbSuggestCleanup: (minAgeDays, maxResults) => call("kb_suggest_cleanup", Number.isFinite(minAgeDays) ? minAgeDays : 30, Number.isFinite(maxResults) ? maxResults : 50),
  kbPurgeTrash: (opts) => call(
    "kb_purge_trash",
    opts && Number.isFinite(opts.olderThanDays) ? opts.olderThanDays : null,
    (opts && Array.isArray(opts.ids)) ? opts.ids : null,
  ),

  // User Todos
  listUserTodos: async (params) => {
    const res = await call("user_todo_list", params?.status || "", params?.tag || "", params?.limit || 200);
    return res && res.items ? res.items : (Array.isArray(res) ? res : []);
  },
  createUserTodo: (payload) => call("user_todo_create", payload),
  updateUserTodo: (id, payload) => call("user_todo_update", id, payload),
  deleteUserTodo: (id) => call("user_todo_delete", id),

  // Reverse-engineering jobs
  reverseJobList: (opts) => call(
    "reverse_job_list",
    (opts && opts.scope) || "",
    (opts && opts.status) || "",
    !!(opts && opts.includeTrashed),
  ),
  reverseJobCreate: (payload) => call("reverse_job_create", payload || {}),
  reverseJobGet: (id, opts) => call("reverse_job_get", id, !!(opts && opts.includeTrashed)),
  reverseJobUpdate: (id, payload) => call("reverse_job_update", id, payload || {}),
  reverseJobTrash: (id) => call("reverse_job_trash", id),
  reverseJobRestore: (id) => call("reverse_job_restore", id),
  reverseJobHardDelete: (id) => call("reverse_job_hard_delete", id),
  reverseJobEvents: (id, tail) => call("reverse_job_events", id, Number.isFinite(tail) ? tail : 200),
  reverseJobSize: (id) => call("reverse_job_size", id),
  reverseToolchainScan: (jobId, tools) => call(
    "reverse_toolchain_scan",
    jobId || "",
    Array.isArray(tools) ? tools : [],
  ),
  fridaSessionList: () => call("frida_session_list"),
  fridaSessionStop: (sessionId, jobId) => call(
    "frida_session_stop",
    sessionId || "",
    jobId || "",
  ),
  reverseArtifactAdd: (jobId, payload) => call("reverse_artifact_add", jobId, payload || {}),
  reverseArtifactRead: (jobId, artId, maxBytes) => call(
    "reverse_artifact_read", jobId, artId,
    Number.isFinite(maxBytes) ? maxBytes : 2_000_000,
  ),
  reverseArtifactRemove: (jobId, artId) => call("reverse_artifact_remove", jobId, artId),
  reverseJobRevealDir: (jobId) => call("reverse_job_reveal_dir", jobId),
  reverseArtifactReveal: (jobId, artId) => call("reverse_artifact_reveal", jobId, artId),
  reverseArtifactCopyTo: (jobId, artId, destPath, sessionId) => call(
    "reverse_artifact_copy_to",
    jobId,
    artId,
    destPath || "",
    sessionId || "",
  ),
  reverseJobExport: (jobId, opts) => call(
    "reverse_job_export",
    jobId,
    (opts && opts.destDir) || "",
    !!(opts && opts.includeDecompiled),
    opts && opts.asZip === false ? false : true,
  ),
  reverseFindingAdd: (jobId, payload) => call("reverse_finding_add", jobId, payload || {}),
  reverseFindingRead: (jobId, findingId) => call("reverse_finding_read", jobId, findingId),
  reverseFindingRemove: (jobId, findingId) => call("reverse_finding_remove", jobId, findingId),
  reverseHookAdd: (jobId, payload) => call("reverse_hook_add", jobId, payload || {}),
  reverseHookRemove: (jobId, hookId) => call("reverse_hook_remove", jobId, hookId),
  reverseHookHits: (jobId, hookId, tail) => call(
    "reverse_hook_hits", jobId, hookId,
    Number.isFinite(tail) ? tail : 200,
  ),
  reverseReproduceAdd: (jobId, payload) => call("reverse_reproduce_add", jobId, payload || {}),
  reverseReproduceRead: (jobId, repId) => call("reverse_reproduce_read", jobId, repId),
  reverseRecipeList: () => call("reverse_recipe_list"),
  reverseRecipeApply: (jobId, recipeId) => call("reverse_recipe_apply", jobId, recipeId),
  reverseHookDrainRun: (jobId, hookId) => call("reverse_hook_drain_run", jobId, hookId || ""),
  reverseHookReattachRun: (jobId) => call("reverse_hook_reattach_run", jobId),

  testAIEndpoint: (endpoint) => call("test_ai_endpoint", endpoint),
  listAIModels: (endpoint) => call("list_ai_models", endpoint),
  listAIProviderPresets: () => call("list_ai_provider_presets"),
  scanSystemBrowsers: () => call("scan_system_browsers"),
  installPlaywrightEngine: (engine) => call("install_playwright_engine", engine || "chromium"),
  attachBrowserStatus: () => call("attach_browser_status"),
  launchBrowserForAttach: (params) => call("launch_browser_for_attach", params || {}),
  attachExistingBrowser: (endpoint) => call("attach_existing_browser", endpoint || "http://127.0.0.1:9222"),
  detachBrowser: () => call("detach_browser"),

  // Proxy
  startProxy: (params) => call("start_proxy", params || {}),
  stopProxy: () => call("stop_proxy"),
  getProxyStatus: () => call("get_proxy_status"),
  getProxyCaInfo: () => call("get_proxy_ca_info"),
  installProxyCa: () => call("install_proxy_ca"),
  uninstallProxyCa: () => call("uninstall_proxy_ca"),
  openProxyCaDir: () => call("open_proxy_ca_dir"),
  getProxyTraffic: (since, limit, resourceKind, host) =>
    call("get_proxy_traffic", since || 0, limit || 100, resourceKind || "", host || ""),
  getProxyTrafficAfter: (afterId, limit, generation, query = "", decodeSeq = null) =>
    call(
      "get_proxy_traffic_after",
      afterId || 0,
      limit || 500,
      generation == null ? null : generation,
      query || "",
      decodeSeq == null ? null : decodeSeq,
    ),
  clearProxyTraffic: () => call("clear_proxy_traffic"),
  queryProxyTraffic: (query) => call("query_proxy_traffic", query || ""),
  getProxyFlowDecoded: (flowId) => call("get_proxy_flow_decoded", flowId),
  getProxyDecryptRules: () => call("get_proxy_decrypt_rules"),
  saveProxyDecryptRule: (rule) => call("save_proxy_decrypt_rule", rule || {}),
  deleteProxyDecryptRule: (ruleId) => call("delete_proxy_decrypt_rule", ruleId || ""),
  testProxyDecryptRule: (rule, flowId) => call("test_proxy_decrypt_rule", rule || {}, flowId || 0),
  getProxyRewriteRules: () => call("get_proxy_rewrite_rules"),
  saveProxyRewriteRule: (rule) => call("save_proxy_rewrite_rule", rule || {}),
  deleteProxyRewriteRule: (ruleId) => call("delete_proxy_rewrite_rule", ruleId || ""),
  testProxyRewriteRule: (rule, flowId) => call("test_proxy_rewrite_rule", rule || {}, flowId || 0),
  getProxyFlowDetail: (flowId, textLimit = 0) => call("get_proxy_flow_detail", flowId, textLimit || 0, true),
  exportProxyHar: (since, limit, path) => call("export_proxy_har", since || 0, limit || 500, path || ""),
  importProxyHar: (path) => call("import_proxy_har", path || ""),
  importProxyBurpLog: (path) => call("import_proxy_burp_log", path || ""),
  saveProxyFlowBody: (flowId, part) => call("save_proxy_flow_body", flowId, part || "response"),
  saveProxyFlowBodyAs: (flowId, part) => call("save_proxy_flow_body_as", flowId, part || "response"),
  openProxyExportsDir: () => call("open_proxy_exports_dir"),
  openProxyCapturesDir: () => call("open_proxy_captures_dir"),
  getProxyFindings: () => call("get_proxy_findings"),

  // Window controls
  minimizeWindow: () => call("minimize_window"),
  maximizeWindow: () => call("maximize_window"),
  resizeWindow: (width, height) => call("resize_window", Math.round(width), Math.round(height)),
  moveWindow: (x, y) => call("move_window", Math.round(x), Math.round(y)),
  // Atomic move+resize: one SetWindowPos under the hood. Use this from
  // the frameless titlebar drag-resize path so we don't ship a frame
  // with (new position, OLD size) followed by another with (new size).
  // See WindowMixin.set_window_geometry for the Win32 rationale.
  setWindowGeometry: (x, y, width, height) => call("set_window_geometry", Math.round(x), Math.round(y), Math.round(width), Math.round(height)),
  saveWindowGeometry: (x, y, width, height, maximized) => call(
    "save_window_geometry",
    Math.round(x),
    Math.round(y),
    Math.round(width),
    Math.round(height),
    !!maximized,
  ),
  closeWindow: () => call("close_window"),
  quitApplication: () => call("quit_application"),
  setGuiCloseToTray: (enabled) => call("set_gui_close_to_tray", !!enabled),

  // External
  openReport: (path) => call("open_report", path),
  openFolder: (path) => call("open_folder", path),
  pathExists: (path, sessionId) => call("path_exists", path, sessionId || null),
  openUserPath: (path, sessionId) => call("open_user_path", path, sessionId || null),
  readMediaFile: (path, sessionId, offsetChars, maxChars) => call(
    "read_media_file",
    path,
    sessionId || null,
    Number.isFinite(offsetChars) ? Math.max(0, Math.floor(offsetChars)) : 0,
    Number.isFinite(maxChars) ? maxChars : 65_536,
  ),
  readMediaUrl: (url) => call("read_media_url", url),
  saveMediaData: (dataUrl, filename, sessionId, mode) => call("save_media_data", dataUrl, filename || "image.png", sessionId || null, mode || "dialog"),
  openExternalUrl: (url) => call("open_external_url", url),
  launchChrome: (port) => call("launch_chrome", { port: port || 9222 }),

  // AI Assistant
  chatAssistant: (params) => call("chat_assistant", params || {}),
  startAssistantStream: (params) => call("start_assistant_stream", params || {}),
  startAssistantActionStream: (params) => call("start_assistant_action_stream", params || {}),
  startAssistantExplain: (params) => call("start_assistant_explain", params || {}),
  getAssistantStreamEvents: (runId, offset) => call("get_assistant_stream_events", runId, offset || 0),
  cancelAssistantStream: (runId) => call("cancel_assistant_stream", runId),
  listAssistantRunJobs: (sessionId) => call("list_assistant_run_jobs", sessionId || ""),
  getAssistantRunJob: (sessionId, jobId) => call("get_assistant_run_job", sessionId || "", jobId || ""),
  resumeAssistantRunJob: (params) => call("resume_assistant_run_job", params || {}),
  dismissAssistantRunJob: (sessionId, jobId) => call("dismiss_assistant_run_job", sessionId || "", jobId || ""),
  listAssistantAgents: (sessionId, includeClosed = false) => call("list_assistant_agents", sessionId || "", Boolean(includeClosed)),
  getAssistantAgent: (sessionId, agentId) => call("get_assistant_agent", sessionId || "", agentId || ""),
  confirmAssistantAgentAction: (sessionId, agentId, confirmation) => call("confirm_assistant_agent_action", sessionId || "", agentId || "", confirmation || {}),
  pauseAssistantAgent: (sessionId, agentId) => call("pause_assistant_agent", sessionId || "", agentId || ""),
  resumeAssistantAgent: (sessionId, agentId, message = "") => call("resume_assistant_agent", sessionId || "", agentId || "", message || ""),
  sendAssistantAgentInput: (sessionId, agentId, message = "") => call("send_assistant_agent_input", sessionId || "", agentId || "", message || ""),
  closeAssistantAgent: (sessionId, agentId) => call("close_assistant_agent", sessionId || "", agentId || ""),
  deleteAssistantAgent: (sessionId, agentId) => call("delete_assistant_agent", sessionId || "", agentId || ""),
  listAssistantSessions: () => call("list_assistant_sessions"),
  getAssistantHistory: (id) => call("get_assistant_history", id),
  getAssistantSession: (id) => call("get_assistant_session", id),
  getAssistantTokenUsage: (id) => call("get_assistant_token_usage", id),
  listAssistantTools: (id) => call("list_assistant_tools", id || ""),
  setAssistantWebSearch: (id, enabled) => call("set_assistant_web_search", id, !!enabled),
  setAssistantThinking: (id, enabled, effort) => call(
    "set_assistant_thinking",
    id,
    !!enabled,
    effort == null ? null : String(effort),
  ),
  setAssistantEngagement: (id, declaration) => call("set_assistant_engagement", id, declaration || null),
  listAssistantCronJobs: () => call("list_assistant_cron_jobs"),
  listAssistantCronRuns: (jobId, limit) => call("list_assistant_cron_runs", jobId || "", limit || 20),
  saveAssistantCronJob: (payload) => call("save_assistant_cron_job", payload || {}),
  setAssistantCronEnabled: (jobId, enabled) => call("set_assistant_cron_enabled", jobId || "", !!enabled),
  deleteAssistantCronJob: (jobId) => call("delete_assistant_cron_job", jobId || ""),
  rotateAssistantCronWebhookSecret: (jobId) => call("rotate_assistant_cron_webhook_secret", jobId || ""),
  grantAssistantCronHighRisk: (jobId, granted, source, sessionId) => call(
    "grant_assistant_cron_high_risk",
    jobId || "",
    !!granted,
    source || "gui",
    sessionId || "",
  ),
  chooseAssistantCronWatchPath: (initialPath, directory) => call(
    "choose_assistant_cron_watch_path",
    initialPath || "",
    directory !== false,
  ),
  setAssistantAutoAllow: (id, toolName, enabled, force) => call("set_assistant_auto_allow", id, toolName, !!enabled, !!force),
  setAssistantAutoApproveMode: (id, enabled, persist) => call(
    "set_assistant_auto_approve_mode",
    id,
    !!enabled,
    persist === true,
  ),
  setAssistantAutoAllowAll: (id, enabled, includeHighRisk, includeReverseDynamic) => call(
    "set_assistant_auto_allow_all",
    id,
    !!enabled,
    !!includeHighRisk,
    !!includeReverseDynamic,
  ),
  openAssistantSandbox: (id, path = "") => call("open_assistant_sandbox", id, path || ""),
  getAssistantSandboxSummary: (id) => call("get_assistant_sandbox_summary", id),
  readAssistantSandboxFile: (id, path, maxChars, asBase64, offsetChars) => call(
    "read_assistant_sandbox_file",
    id,
    path,
    Number.isFinite(maxChars) ? maxChars : 60000,
    !!asBase64,
    Number.isFinite(offsetChars) ? Math.max(0, Math.floor(offsetChars)) : 0,
  ),
  listAssistantSandboxDirectory: (id, path) => call("list_assistant_sandbox_directory", id, path || ""),
  exportAssistantSandboxItems: (id, paths, destinationDir) => call(
    "export_assistant_sandbox_items",
    id,
    paths || [],
    destinationDir || "",
  ),
  exportAssistantSandboxFile: (id, path, destination) => call(
    "export_assistant_sandbox_file",
    id,
    path || "",
    destination || "",
  ),
  deleteAssistantSandboxItems: (id, paths, confirmLabel) => call("delete_assistant_sandbox_items", id, paths || [], confirmLabel || ""),
  moveAssistantSandboxItems: (id, paths, destination, confirmLabel) => call("move_assistant_sandbox_items", id, paths || [], destination || "", confirmLabel || ""),
  searchAssistantMemory: (id, query, limit) => call("search_assistant_memory", id, query, limit || 8),
  searchAllAssistantMemory: (query, limit) => call("search_all_assistant_memory", query, limit || 10),
  cancelAssistantRun: (id) => call("cancel_assistant_run", id),
  pauseAssistantAfterCurrentResponse: (id) => call("pause_assistant_after_current_response", id),
  assistantSideQuestion: (id, question) => call("assistant_side_question", id, question || ""),
  confirmAssistantAction: (id, action) => call("confirm_assistant_action", id, action),
  deleteAssistantSession: (id) => call("delete_assistant_session", id),
  clearAllAssistantSessions: () => call("clear_all_assistant_sessions"),
  renameAssistantSession: (id, name) => call("rename_assistant_session", id, name),
  updateAssistantSession: (id, patch) => call("update_assistant_session", id, patch || {}),

  // User-level todos (cross-session)
  userTodoList: (status, tag, limit) => call("user_todo_list", status || "", tag || "", limit || 100),
  userTodoCreate: (payload) => call("user_todo_create", payload || {}),
  userTodoUpdate: (todoId, payload) => call("user_todo_update", todoId, payload || {}),
  userTodoDelete: (todoId) => call("user_todo_delete", todoId),

  getDesktopActionStatus: (sessionId) => call("get_desktop_action_status", sessionId || ""),
  stopDesktopAction: (sessionId) => call("stop_desktop_action", sessionId || ""),
  openMacosPrivacySettings: (pane) => call("open_macos_privacy_settings", pane || "accessibility"),

  setUiContext: (data) => call("set_ui_context", data || {}),
  getUiContext: () => call("get_ui_context"),
  getAppSnapshot: () => call("get_app_snapshot"),
};
