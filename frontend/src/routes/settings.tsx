import { useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Eye, EyeOff, PlugZap, Trash2 } from "lucide-react";
import { testConnection, type ConnectionTestResult } from "../api/health";
import type { HealthView } from "../api/adapters/health";
import { clearConnection, getConnection, getConnectionSnapshot, saveConnection } from "../lib/connection";
import { useDocumentTitle } from "../lib/use-document-title";
import { queryKeys } from "../queries/keys";
import { Button } from "../components/ui/button";
import { ErrorState } from "../components/ui/error-state";
import { Input } from "../components/ui/input";
import { Label } from "../components/ui/label";
import { Separator } from "../components/ui/separator";
import { StatusDot } from "../components/ui/status-dot";

function HealthDetails({ health }: { health: HealthView }) {
  const rows: Array<[string, string]> = [
    ["storage", health.storageBackend],
    ["API-local task threads", String(health.workers)],
    ["database", health.database],
  ];
  if (health.capacity) {
    const { activeRuns, queuedRuns, maxActiveRuns, maxQueuedRuns } = health.capacity;
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
  const testGeneration = useRef(0);

  function discardConnectionCache() {
    const queryKey = queryKeys.connection(getConnectionSnapshot());
    void queryClient.cancelQueries({ queryKey });
    queryClient.removeQueries({ queryKey });
  }

  async function handleTest() {
    const generation = ++testGeneration.current;
    const connectionGeneration = getConnectionSnapshot().generation;
    setTesting(true);
    setResult(null);
    const submitted = { baseUrl: baseUrl.trim(), apiKey: apiKey.trim() };
    const outcome = await testConnection(submitted);
    if (generation !== testGeneration.current) return;
    if (connectionGeneration !== getConnectionSnapshot().generation) {
      setTesting(false);
      return;
    }
    setResult(outcome);
    if (outcome.ok) {
      // Clear the old generation even when the URL remains the same.
      discardConnectionCache();
      saveConnection(submitted);
    }
    setTesting(false);
  }

  function handleClear() {
    testGeneration.current += 1;
    setTesting(false);
    discardConnectionCache();
    clearConnection();
    setBaseUrl("");
    setApiKey("");
    setResult(null);
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
          <Label htmlFor="api-key">Runtime API Key</Label>
          <div className="relative">
            <Input
              id="api-key"
              type={showKey ? "text" : "password"}
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              placeholder="Runtime API key"
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
            Authenticates this Runtime; your model provider uses a separate key.
            Kept in memory / sessionStorage only.
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
              <StatusDot tone={!result.health || result.health.database === "error" ? "warn" : "accent"} />
              {result.health?.database === "error"
                ? "Credentials verified · database error"
                : "Credentials verified"}
            </div>
            <div className="mt-3">
              {result.health ? (
                <HealthDetails health={result.health} />
              ) : (
                <p className="text-xs text-fg-2">
                  Health information unavailable: {result.healthError.message}
                </p>
              )}
            </div>
          </div>
        )}

        {result && !result.ok && (
          <ErrorState
            title={
              result.error.status > 0
                ? `Credential verification failed · HTTP ${result.error.status}`
                : "Credential verification failed"
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
