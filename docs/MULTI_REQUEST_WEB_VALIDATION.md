# Multi-request Web validation — 2026-10-11

## Change origin and scope

New work adds a per-request approval workflow around the merged repo's existing
`run_case(prepare_only=True)` and confirmed-intent generation paths.
The shared launcher supplied earlier is extended with `-MultiRequest`; its
fixed-contract interface is preserved. The target installation is the user's
shared-launcher main branch (reported commit d769fff).

Modified existing files:

- `scripts/prepare_web.py`: select fixed or multi mode; multi snapshots context without a fixed contract.
- `scripts/start_web.ps1`: multi parameter set and separate DSH_HOME; same offline preflight and hidden key entry.

New runtime files:

- `scripts/web_multi_server.py`: prepare/status/generate MCP tools; no approval or context-override tool.
- `src/harness_agent/web/reviews.py`: request states, pinned inputs, browser approval contracts, idempotent completion.
- `src/harness_agent/web/approval_http.py`: loopback review pages with escaped data, form nonces and Host/Origin checks.
- `src/harness_agent/web/__init__.py`: module package.

New tests and user documentation accompany these files. No runner core, gate,
existing context, model configuration, local configuration or dataset file is
included in this update. The original merge manifest remains a historical
snapshot, not a manifest of this new feature.

## Checks performed

Environment: Linux, Python 3.12.14. A local validation copy of the merged source
and previously supplied shared launcher was used; this was not a fresh checkout
of the user's latest GitHub main or direct access to their Windows machine.

- Targeted suite: **32 passed in 18.90 seconds**.
- Included the existing eight shared-launcher tests and 24 new tests.
- Ruff check on all changed/new Python files: **All checks passed**.
- Tests used recording runners, actual local HTTP/MCP transports, and fake_dsh;
  no real model API calls were made and no device configuration was deployed.

Covered behavior:

- Multiple requests have independent approvals; no CLI package before approval.
- Reject and missing approval prevent generation.
- Context/config/model/proposal/contract changes invalidate generation.
- Browser decision nonce is request-bound and cannot be replayed.
- Cross-origin or missing-Origin form submission fails; untrusted HTML is escaped.
- Leak indicators/incomplete scans block approval; clarification offers no approval.
- Finished generation is idempotent; exceptions do not silently reuse approval.
- MCP rejects unknown tools, extra approval arguments and unknown request IDs.
- Fake end-to-end network_admin VLAN request accepts after approval.
- Same request with access_operator still rejects at G3 RBAC after approval.
- Changed CBA proposal after approval does not reach TSA or produce a package.
- Multi launcher has no fixed contract and preserves disabled-tool restrictions.
- Fixed-contract launcher regression tests still pass.

## Remaining verification and limits

- PowerShell was not installed in this environment. Run the documented Windows
  tests and `-CheckOnly`, then explicitly try the new web flow on the user's machine.
- No full repository suite was rerun for this feature. Earlier full-suite results
  remain historical evidence and are not claimed for this exact update.
- The real DeepSeek web UI/model workflow has not been exercised for this new
  multi-request mode; earlier fixed-case real-model runs do not validate it.
- Generation reruns CBA before TSA. A changed proposal is rejected rather than
  automatically reconciled, and the additional CBA call incurs usage in real mode.
- Context remains static; no live collection, deployment or accumulated changes.
- Approval service is for a trusted single-user local machine, not remote multi-user hosting.
- Bridge restart invalidates in-memory approvals. Audit and run artifacts remain local.

## Windows follow-up — 2026-10-11

- Targeted Ruff check: All checks passed.
- Targeted tests after the UTF-8 and Referrer-Policy fixes: 32 passed in 25.08s.
- Multi-request launcher CheckOnly: passed.
- Real web description request: accepted; description-only CLI.
  Request: 2e50a0fe3118456dab206098cdc2b7bf
- Real web VLAN request in the same session: accepted; only VLAN 30 FINANCE.
  Request: d432d3c6a5424c5ab82c15983023afd9
- Each request had separate browser approval.
- Syntax and CIS ran; Batfish was skipped. No deployment.
- Results reported from the operator's Windows machine; raw logs remain local.
- Full repository test suite was not rerun for this update.