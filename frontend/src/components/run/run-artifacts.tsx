import { useState } from "react";
import type { Artifact } from "../../api/adapters/artifact";
import { fetchArtifactContent } from "../../api/run";
import { formatRelativeTime, truncateId } from "../../lib/format";
import { getConnection } from "../../lib/connection";
import { Button } from "../ui/button";
import { CopyId } from "./copy-id";

export function RunArtifactsTable({ artifacts }: { artifacts: Artifact[] }) {
  const [downloading, setDownloading] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const download = async (artifact: Artifact) => {
    setDownloading(artifact.artifactId);
    setError(null);
    try {
      const blob = await fetchArtifactContent(getConnection(), artifact.artifactId);
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = artifact.name;
      anchor.click();
      URL.revokeObjectURL(url);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Download failed");
    } finally {
      setDownloading(null);
    }
  };

  if (artifacts.length === 0) {
    return (
      <div className="px-6 py-16 text-center">
        <div className="text-sm font-medium text-fg-0">No artifacts</div>
        <div className="mt-1 font-mono text-xs text-fg-2">
          No artifacts produced by this run.
        </div>
      </div>
    );
  }
  return (
    <div className="overflow-x-auto">
      {error !== null && <div className="border-b border-danger/30 px-4 py-2 text-xs text-danger">{error}</div>}
      <table className="w-full border-collapse text-left">
        <thead>
          <tr className="border-b border-border">
            {["Name", "Type", "Size", "SHA-256", "Producer", "Reuse", "Created", ""].map(
              (heading) => (
                <th key={heading} className="px-3 py-2 text-[11px] font-medium tracking-wider text-fg-2 uppercase first:pl-4 last:pr-4">
                  {heading}
                </th>
              ),
            )}
          </tr>
        </thead>
        <tbody>
          {artifacts.map((artifact) => (
            <tr key={artifact.artifactId} className="h-11 border-b border-border/60 last:border-b-0">
              <td className="px-3 first:pl-4">
                <div className="max-w-64 truncate text-13 text-fg-0" title={artifact.name}>{artifact.name}</div>
                <CopyId id={artifact.artifactId} head={16} />
              </td>
              <td className="px-3 font-mono text-xs whitespace-nowrap text-fg-1">{artifact.mediaType}</td>
              <td className="px-3 font-mono text-xs whitespace-nowrap text-fg-1">{formatBytes(artifact.sizeBytes)}</td>
              <td className="px-3 font-mono text-xs whitespace-nowrap text-fg-2" title={artifact.sha256}>{truncateId(artifact.sha256, 12)}</td>
              <td className="px-3 font-mono text-xs whitespace-nowrap text-fg-1">{artifact.toolName}</td>
              <td className="px-3 font-mono text-xs whitespace-nowrap text-fg-2">{artifact.reused ? "reused" : "new"}</td>
              <td className="px-3 font-mono text-xs whitespace-nowrap text-fg-2">{formatRelativeTime(artifact.createdAt)}</td>
              <td className="px-3 text-right last:pr-4">
                <Button size="sm" disabled={downloading === artifact.artifactId} onClick={() => void download(artifact)}>
                  {downloading === artifact.artifactId ? "Downloading" : "Download"}
                </Button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KiB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MiB`;
}
