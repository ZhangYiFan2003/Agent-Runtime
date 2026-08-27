# Durable Recovery Fault-Injection Baseline

> Deterministic offline recovery evidence.

- Scenarios: 21
- Repetitions: 2
- Total executions: 42
- Recovery rate: 1.0000
- Expected-safe outcomes: 2
- Failures: 0
- Duplicate Tool executions: 0
- Duplicate Child Runs: 0
- Lost completed results: 0
- Incorrect terminal states: 0

| Scenario | Category | Repetition | Classification | Passed |
| --- | --- | ---: | --- | --- |
| react_run_checkpoint | react | 1 | recovered | yes |
| react_tool_checkpoint | react | 1 | recovered | yes |
| react_tool_success_gap | react | 1 | recovered | yes |
| react_waiting_approval | react | 1 | recovered | yes |
| react_rejection_continuation | react | 1 | recovered | yes |
| react_manual_interrupt | react | 1 | recovered | yes |
| react_ambiguous_running | react | 1 | expected-safe | yes |
| plan_created | plan | 1 | recovered | yes |
| plan_before_checkpoint | plan | 1 | recovered | yes |
| plan_child_terminal | plan | 1 | recovered | yes |
| plan_tool_success_gap | plan | 1 | recovered | yes |
| plan_waiting_approval | plan | 1 | recovered | yes |
| plan_replan_v2 | plan | 1 | recovered | yes |
| plan_identity_before_spawn | plan | 1 | recovered | yes |
| plan_mixed_restart | plan | 1 | recovered | yes |
| multi_identity_before_spawn | multi-agent | 1 | recovered | yes |
| multi_terminal_unobserved | multi-agent | 1 | recovered | yes |
| multi_tool_success_gap | multi-agent | 1 | recovered | yes |
| multi_waiting_approval | multi-agent | 1 | recovered | yes |
| multi_parent_cancel | multi-agent | 1 | recovered | yes |
| multi_mixed_restart | multi-agent | 1 | recovered | yes |
| react_run_checkpoint | react | 2 | recovered | yes |
| react_tool_checkpoint | react | 2 | recovered | yes |
| react_tool_success_gap | react | 2 | recovered | yes |
| react_waiting_approval | react | 2 | recovered | yes |
| react_rejection_continuation | react | 2 | recovered | yes |
| react_manual_interrupt | react | 2 | recovered | yes |
| react_ambiguous_running | react | 2 | expected-safe | yes |
| plan_created | plan | 2 | recovered | yes |
| plan_before_checkpoint | plan | 2 | recovered | yes |
| plan_child_terminal | plan | 2 | recovered | yes |
| plan_tool_success_gap | plan | 2 | recovered | yes |
| plan_waiting_approval | plan | 2 | recovered | yes |
| plan_replan_v2 | plan | 2 | recovered | yes |
| plan_identity_before_spawn | plan | 2 | recovered | yes |
| plan_mixed_restart | plan | 2 | recovered | yes |
| multi_identity_before_spawn | multi-agent | 2 | recovered | yes |
| multi_terminal_unobserved | multi-agent | 2 | recovered | yes |
| multi_tool_success_gap | multi-agent | 2 | recovered | yes |
| multi_waiting_approval | multi-agent | 2 | recovered | yes |
| multi_parent_cancel | multi-agent | 2 | recovered | yes |
| multi_mixed_restart | multi-agent | 2 | recovered | yes |
