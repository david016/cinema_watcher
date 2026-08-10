// Spoločná časť pre Netlify funkcie: konfigurácia z premenných prostredia
// a volanie GitHub API. Bez závislostí — Netlify beží na Node 18+, kde je
// fetch v jadre.

export function ghConfig() {
  const env = process.env;
  return {
    repo: env.GH_REPO || "", // "meno/repozitar"
    token: env.GH_TOKEN || "", // PAT s právom actions:write + contents:read
    branch: env.GH_BRANCH || "master",
    workflow: env.GH_WORKFLOW || "cinema-watcher.yml",
    dataPath: env.GH_DATA_PATH || "web/data/status.json",
    // Ak je nastavené, /api/check vyžaduje toto heslo (stránka sa naň spýta).
    // Bez neho by kontrolu mohol spustiť ktokoľvek, kto pozná URL.
    password: env.TRIGGER_PASSWORD || "",
  };
}

export function json(body, status = 200, headers = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      "content-type": "application/json; charset=utf-8",
      "cache-control": "no-store",
      ...headers,
    },
  });
}

export async function ghFetch(cfg, path, init = {}) {
  const res = await fetch(`https://api.github.com${path}`, {
    ...init,
    headers: {
      accept: "application/vnd.github+json",
      authorization: `Bearer ${cfg.token}`,
      "x-github-api-version": "2022-11-28",
      "user-agent": "cinema-watcher",
      ...(init.headers || {}),
    },
  });
  return res;
}

// Je sledovanie zapnuté? GitHub vie workflow "disablovať" — vtedy neplatí ani
// cron, ani ručné spustenie. Stránka to potrebuje vedieť, aby vedela ukázať
// správne tlačidlo (Pozastaviť / Spustiť) a nehlásila staré dáta ako poruchu.
export async function workflowState(cfg) {
  const res = await ghFetch(cfg, `/repos/${cfg.repo}/actions/workflows/${cfg.workflow}`);
  if (!res.ok) return null;
  const wf = await res.json();
  return {
    // active | disabled_manually | disabled_inactivity
    // (to posledné si nastaví GitHub sám po 60 dňoch bez aktivity v repe)
    state: wf.state,
    active: wf.state === "active",
  };
}

// Beží práve teraz kontrola? Nech stránka po kliknutí na tlačidlo vie povedať
// "už to beží" namiesto spustenia druhého behu.
export async function latestRun(cfg) {
  const res = await ghFetch(
    cfg,
    `/repos/${cfg.repo}/actions/workflows/${cfg.workflow}/runs?per_page=1`,
  );
  if (!res.ok) return null;
  const body = await res.json();
  const run = body.workflow_runs?.[0];
  if (!run) return null;
  return {
    status: run.status, // queued | in_progress | completed
    conclusion: run.conclusion, // success | failure | ...
    started_at: run.run_started_at,
    url: run.html_url,
    running: run.status === "queued" || run.status === "in_progress",
  };
}
