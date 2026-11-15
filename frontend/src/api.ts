// Thin typed client. The backend owns all fallback logic; the UI just shows what it is given.
export type Health = {
  ok: boolean;
  offline: boolean;
  llm_cache_only: boolean;
  llm_tiers: Record<string, boolean>;
};

async function j<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(path, init);
  if (!r.ok) throw new Error(`${r.status} ${path}`);
  return r.json() as Promise<T>;
}

export const api = {
  health: () => j<Health>("/api/health"),
  // TODO: digest(hours), alerts(), research(symbol)
};
