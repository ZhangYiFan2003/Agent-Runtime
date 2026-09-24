import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Eye, EyeOff, PlugZap, Trash2 } from "lucide-react";
import { testConnection, type ConnectionTestResult } from "../api/health";
import { clearConnection, getConnection, saveConnection } from "../lib/connection";
import { useDocumentTitle } from "../lib/use-document-title";
import { queryKeys } from "../queries/keys";
import { Button } from "../components/ui/button";
import { ErrorState } from "../components/ui/error-state";
import { Input } from "../components/ui/input";
import { Label } from "../components/ui/label";
import { Separator } from "../components/ui/separator";
import { StatusDot } from "../components/ui/status-dot";

function HealthDetails({ health }: { health: ConnectionTestResult & { ok: true } }) {
  const rows: Array<[string, string]> = [
    ["storage", health.health.storageBackend],
    ["workers", String(health.health.workers)],
    ["database", health.health.database],
  ];
  if (health.health.capacity) {
    const { activeRuns, queuedRuns, maxActiveRuns, maxQueuedRuns } = health.health.capacity;
    if (activeRuns !== null || queuedRuns !== null) {
      rows.push([
        "capacity",
        [
          activeRuns !== null ? `${activeRuns} active` : null,
          maxActiveRuns !== null ? `max ${maxActiveRuns}` : null,
          queuedRuns !== null ? `${queuedRuns} queued` : null,
          maxQueuedRuns !== null ? `max ${maxQueuedRuns}` : null,
        ]
          .filter(Boolean)
          .join(" · "),
      ]);
    }
  }
  return (
    <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5 font-mono text-xs">
      {rows.map(([k, v]) => (
        <div key={k} className="contents">
          <dt className="text-fg-2">{k}</dt>
          <dd className="text-fg-1">{v}</dd>
        </div>
      ))}
    </dl>
  );
}

export function SettingsPage() {
  useDocumentTitle("Settings · Axiom");
  const queryClient = useQueryClient();
  const initial = getConnection();
  const [baseUrl, setBaseUrl] = useState(initial.baseUrl);
  const [apiKey, setApiKey] = useState(initial.apiKey);
  const [showKey, setShowKey] = useState(false);
  const [testing, setTesting] = useState(false);
  const [result, setResult] = useState<ConnectionTestResult | null>(null);

  async function handleTest() {
    setTesting(true);
    setResult(null);
    const outcome = await testConnection({ baseUrl: baseUrl.trim(), apiKey: apiKey.trim() });
    setResult(outcome);
    if (outcome.ok) {
      // Persist only on a verified connection; then refresh the shell status.
      saveConnection({ baseUrl: baseUrl.trim(), apiKey: apiKey.trim() });
      void queryClient.invalidateQueries({ queryKey: queryKeys.health });
    }
    setTesting(false);
  }

  function handleClear() {
    clearConnection();
    setBaseUrl("");
    setApiKey("");
    setResult(null);
    void queryClient.invalidateQueries({ queryKey: queryKeys.health });
  }

  return (
    <div className="mx-auto w-full max-w-xl p-4 pb-24 md:p-6 md:pb-6">
      <h1 className="text-base font-semibold text-fg-0">Settings</h1>
      <p className="mt-1 text-xs text-fg-2">Runtime connection</p>

      <div className="mt-5 flex flex-col gap-4">
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="base-url">Runtime Base URL</Label>
          <Input
            id="base-url"
            value={baseUrl}
            onChange={(e) => setBaseUrl(e.target.value)}
            placeholder="same origin — dev proxy → 127.0.0.1:8080"
            autoComplete="off"
            spellCheck={false}
            className="font-mono text-xs"
          />
          <p className="text-[11px] text-fg-2">
            Leave empty to use the dev-server proxy (<span className="font-mono">/v1</span>,{" "}
            <span className="font-mono">/health</span>).
          </p>
        </div>

        <div className="flex flex-col gap-1.5">
          <Label htmlFor="api-key">API Key</Label>
          <div className="relative">
            <Input
              id="api-key"
              type={showKey ? "text" : "password"}
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              placeholder="sk-…"
              autoComplete="off"
              spellCheck={false}
              className="pr-9 font-mono text-xs"
            />
            <button
              type="button"
              onClick={() => setShowKey((v) => !v)}
              className="absolute top-1/2 right-2 -translate-y-1/2 rounded p-1 text-fg-2 hover:text-fg-0"
              aria-label={showKey ? "Hide API key" : "Show API key"}
            >
              {showKey ? <EyeOff className="size-3.5" /> : <Eye className="size-3.5" />}
            </button>
          </div>
          <p className="text-[11px] text-fg-2">
            Kept in memory / sessionStorage only — never written to .env, the bundle, or this
            repository.
          </p>
        </div>

        <div className="flex items-center gap-2">
          <Button variant="primary" size="default" onClick={() => void handleTest()} disabled={testing}>
            <PlugZap />
            {testing ? "Testing…" : "Test Connection"}
          </Button>
          <Button variant="ghost" size="default" onClick={handleClear}>
            <Trash2 />
            Clear credentials
          </Button>
        </div>

        <Separator />

        {result?.ok && (
          <div className="rounded-md border border-border bg-bg-1 p-3">
            <div className="flex items-center gap-2 text-13 font-medium text-fg-0">
              <StatusDot tone="accent" />
              Connected
              <span className="font-mono text-xs font-normal text-fg-2">
                /health → {result.health.status}
              </span>
            </div>
            <div className="mt-3">
              <HealthDetails health={result} />
            </div>
          </div>
        )}

        {result && !result.ok && (
          <ErrorState
            title={
              result.error.status > 0
                ? `Disconnected · HTTP ${result.error.status}`
                : "Disconnected"
            }
            message={
              result.error.code
                ? `${result.error.code}: ${result.error.message}`
                : result.error.message
            }
          />
        )}

        {!result && (
          <p className="font-mono text-xs text-fg-2">
            // run the local Runtime, then press Test Connection
          </p>
        )}
      </div>
    </div>
  );
}
