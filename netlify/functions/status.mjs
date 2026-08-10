// GET /api/status
// Vráti aktuálny stav sledovania priamo z repa (súbor web/data/status.json),
// takže stránka vidí nové dáta hneď po behu workflowu a nemusí sa čakať na
// nový deploy Netlify. Zároveň pridá info o poslednom behu workflowu.
//
// Keď nie sú nastavené GH_REPO / GH_TOKEN, vráti 501 a stránka si vystačí
// so súborom data/status.json nasadeným spolu s webom.

import { ghConfig, ghFetch, json, latestRun } from "../lib/github.mjs";

export default async () => {
  const cfg = ghConfig();
  if (!cfg.repo || !cfg.token) {
    return json({ error: "not_configured", detail: "Chýba GH_REPO alebo GH_TOKEN." }, 501);
  }

  const res = await ghFetch(
    cfg,
    `/repos/${cfg.repo}/contents/${cfg.dataPath}?ref=${encodeURIComponent(cfg.branch)}`,
    { headers: { accept: "application/vnd.github.raw" } },
  );

  if (res.status === 404) {
    return json({ error: "no_data", detail: `${cfg.dataPath} v repe ešte nie je.` }, 404);
  }
  if (!res.ok) {
    return json({ error: "github_error", status: res.status, detail: await res.text() }, 502);
  }

  let data;
  try {
    data = JSON.parse(await res.text());
  } catch (e) {
    return json({ error: "bad_json", detail: String(e) }, 502);
  }

  return json({ data, run: await latestRun(cfg) });
};

export const config = { path: "/api/status" };
