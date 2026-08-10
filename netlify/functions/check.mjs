// POST /api/check
// Spustí workflow "Cinema watcher" v GitHub Actions (workflow_dispatch).
// Samotnú kontrolu, e-maily aj ukladanie stavu robí ten workflow — stránka
// len ťahá za rúčku, aby sa nemuselo čakať na najbližší cron.
//
// Premenné prostredia (Netlify -> Site configuration -> Environment variables):
//   GH_REPO           "meno/repozitar"
//   GH_TOKEN          PAT s právom actions:write (fine-grained: Actions: Read and write)
//   GH_BRANCH         vetva (default master)
//   GH_WORKFLOW       názov súboru workflowu (default cinema-watcher.yml)
//   TRIGGER_PASSWORD  voliteľné heslo — bez neho môže kontrolu spustiť ktokoľvek

import { ghConfig, ghFetch, json, latestRun } from "../lib/github.mjs";

export default async (req) => {
  if (req.method !== "POST") {
    return json({ error: "method_not_allowed" }, 405, { allow: "POST" });
  }

  const cfg = ghConfig();
  if (!cfg.repo || !cfg.token) {
    return json({ error: "not_configured", detail: "Chýba GH_REPO alebo GH_TOKEN." }, 501);
  }

  let body = {};
  try {
    body = await req.json();
  } catch {
    // Prázdne telo je v pohode — všetko má rozumný default.
  }

  if (cfg.password) {
    const given = req.headers.get("x-trigger-password") || body.password || "";
    if (given !== cfg.password) {
      return json({ error: "unauthorized", detail: "Nesprávne heslo." }, 401);
    }
  }

  // Keď už jeden beh čaká alebo beží, druhý nemá zmysel spúšťať.
  const running = await latestRun(cfg);
  if (running?.running) {
    return json({ ok: true, already_running: true, run: running });
  }

  const res = await ghFetch(cfg, `/repos/${cfg.repo}/actions/workflows/${cfg.workflow}/dispatches`, {
    method: "POST",
    body: JSON.stringify({
      ref: cfg.branch,
      inputs: {
        mail_on: ["tickets", "all", "never"].includes(body.mail_on) ? body.mail_on : "tickets",
      },
    }),
  });

  if (res.status !== 204) {
    return json({ error: "dispatch_failed", status: res.status, detail: await res.text() }, 502);
  }

  return json({ ok: true, dispatched: true });
};

export const config = { path: "/api/check" };
