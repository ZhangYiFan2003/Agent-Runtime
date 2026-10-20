import type { ConnectionSnapshot } from "../lib/connection";

const connection = ({ scope, generation }: Pick<ConnectionSnapshot, "scope" | "generation">) =>
  ["runtime", scope, generation] as const;

/** Credentials never enter query keys. Every read belongs to one connection generation. */
export const queryKeys = {
  connection,
  forConnection(snapshot: Pick<ConnectionSnapshot, "scope" | "generation">) {
    const prefix = connection(snapshot);
    return {
      health: [...prefix, "health"] as const,
      runs: [...prefix, "runs"] as const,
      run: (runId: string) => [...prefix, "run", runId] as const,
      children: (runId: string) => [...prefix, "run", runId, "children"] as const,
      trace: (runId: string) => [...prefix, "run", runId, "trace"] as const,
      metrics: (runId: string) => [...prefix, "run", runId, "metrics"] as const,
      artifacts: (runId: string) => [...prefix, "run", runId, "artifacts"] as const,
      claims: (runId: string) => [...prefix, "run", runId, "claims"] as const,
      claimProvenance: (claimId: string) => [...prefix, "claim", claimId, "provenance"] as const,
      messages: (threadId: string) => [...prefix, "thread", threadId, "messages"] as const,
    };
  },
};
