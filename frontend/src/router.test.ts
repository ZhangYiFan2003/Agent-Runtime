import { describe, expect, it } from "vitest";
import { router } from "./router";

describe("router", () => {
  it("registers the runs list and run detail routes", () => {
    const paths = Object.keys(router.routesByPath);
    expect(paths).toContain("/runs");
    expect(paths).toContain("/runs/$runId");
  });

  it("run detail is a child of the root shell, not a nav item", () => {
    const runsDetail = router.routesByPath["/runs/$runId"];
    expect(runsDetail).toBeDefined();
    // nav items are /, /runs, /settings — run detail must not shadow them
    expect(Object.keys(router.routesByPath)).toHaveLength(4);
  });
});
