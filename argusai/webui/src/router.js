// Minimal hash-based router. Exposes a reactive `route` and a `navigate()`.

import { reactive } from 'vue'

// Supported routes. Order matters — first match wins.
const ROUTES = [
  { name: "tasks", re: /^#\/?$/ },
  { name: "new", re: /^#\/new\/?$/ },
  { name: "task", re: /^#\/task\/(.+)$/, params: ["id"] },
  { name: "proxy", re: /^#\/proxy\/?$/ },
  { name: "browser", re: /^#\/browser\/?$/ },
  { name: "team", re: /^#\/team\/?$/ },
  { name: "mcp", re: /^#\/mcp\/?$/ },
  { name: "local_agents", re: /^#\/local-agents\/?$/ },
  { name: "channels", re: /^#\/channels\/?$/ },
  { name: "assistant_sessions", re: /^#\/assistant\/?$/ },
  { name: "user_todos", re: /^#\/user-todos\/?$/ },
  { name: "cron", re: /^#\/cron\/?$/ },
  { name: "knowledge", re: /^#\/knowledge\/?$/ },
  { name: "knowledge_entry", re: /^#\/knowledge\/(.+)$/, params: ["id"] },
  { name: "reverse", re: /^#\/reverse\/?$/ },
  { name: "reverse_job", re: /^#\/reverse\/(.+)$/, params: ["id"] },
  { name: "settings", re: /^#\/settings\/?$/ },
  { name: "about", re: /^#\/about\/?$/ },
];

export const route = reactive({
  name: "tasks",
  params: {},
  raw: location.hash || "#/",
});

function parse(hash) {
  if (!hash) hash = "#/";
  for (const r of ROUTES) {
    const m = hash.match(r.re);
    if (m) {
      const params = {};
      (r.params || []).forEach((p, i) => (params[p] = decodeURIComponent(m[i + 1])));
      return { name: r.name, params, raw: hash };
    }
  }
  return { name: "tasks", params: {}, raw: "#/" };
}

function apply() {
  const r = parse(location.hash);
  route.name = r.name;
  route.params = r.params;
  route.raw = r.raw;
}

export function navigate(path) {
  if (!path.startsWith("#")) path = "#" + (path.startsWith("/") ? "" : "/") + path;
  if (location.hash === path) apply();
  else location.hash = path;
}

export function href(path) {
  return path.startsWith("#") ? path : "#" + (path.startsWith("/") ? "" : "/") + path;
}

window.addEventListener("hashchange", apply);
apply();
