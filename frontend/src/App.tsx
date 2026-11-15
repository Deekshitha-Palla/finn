import { useEffect, useState } from "react";
import { api, Health } from "./api";

type Tab = "push" | "pull";

export default function App() {
  const [tab, setTab] = useState<Tab>("push");
  const [health, setHealth] = useState<Health | null>(null);
  const [err, setErr] = useState(false);

  useEffect(() => {
    api.health().then(setHealth).catch(() => setErr(true));
  }, []);

  return (
    <div className="app">
      <header>
        <h1>FINN</h1>
        <span className={`dot ${err ? "bad" : health ? "ok" : ""}`} title={err ? "API unreachable" : "API ok"} />
        {health?.offline && <span className="chip">snapshots</span>}
      </header>
      <main>
        {/* Skeleton only: the interaction model is the thing to design. */}
        <p className="muted">{tab === "push" ? "Push: digest + alerts go here." : "Pull: research goes here."}</p>
      </main>
      <nav>
        <button className={tab === "push" ? "on" : ""} onClick={() => setTab("push")}>Push</button>
        <button className={tab === "pull" ? "on" : ""} onClick={() => setTab("pull")}>Pull</button>
      </nav>
    </div>
  );
}
