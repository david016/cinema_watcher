// POST /api/toggle   telo: { "enable": true | false }
// Zapne alebo pozastaví sledovanie — cez GitHub Actions enable/disable workflow.
//
// Pozastavený workflow neberie cron ANI ručné spustenie, takže kým je vypnutý,
// nefunguje ani tlačidlo "Skontrolovať teraz". Je to zámer: pauza má znamenať
// naozaj ticho (žiadne e-maily), nie polovičný stav. Zapnúť sa dá kedykoľvek
// tým istým tlačidlom.
//
// Premenné prostredia sú tie isté ako pri /api/check (GH_REPO, GH_TOKEN,
// GH_WORKFLOW, TRIGGER_PASSWORD). Token potrebuje Actions: Read and write —
// to isté právo, aké už používa spúšťanie kontroly, takže netreba nový token.

import { ghConfig, ghFetch, json, workflowState } from "../lib/github.mjs";

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
    // Prázdne telo -> chýbajúce `enable` zachytí kontrola nižšie.
  }

  if (cfg.password) {
    const given = req.headers.get("x-trigger-password") || body.password || "";
    if (given !== cfg.password) {
      return json({ error: "unauthorized", detail: "Nesprávne heslo." }, 401);
    }
  }

  // Vedomé rozhodnutie, nie prepínač: keby stránka poslala "prepni", dve
  // otvorené záložky by si vedeli stav navzájom preklopiť.
  if (typeof body.enable !== "boolean") {
    return json({ error: "bad_request", detail: "Chýba enable: true/false." }, 400);
  }

  const action = body.enable ? "enable" : "disable";
  const res = await ghFetch(
    cfg,
    `/repos/${cfg.repo}/actions/workflows/${cfg.workflow}/${action}`,
    { method: "PUT" },
  );

  if (res.status !== 204) {
    return json({ error: "toggle_failed", status: res.status, detail: await res.text() }, 502);
  }

  return json({ ok: true, workflow: await workflowState(cfg) });
};

export const config = { path: "/api/toggle" };
