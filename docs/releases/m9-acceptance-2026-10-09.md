# M9 acceptance — 9 October 2026

Publication approved by Roland on 9 October. This records the test checkpoint before
the PR #65 merge; current integration/release status is maintained in AGENT and NEXT.

Tested deployment: `v2` at `bb762630730b50a50f7df381a676d2b96edaf568`.
Tests were completed on 9 October 2026, Europe/Helsinki time.

Infrastructure and restart recovery checks passed. Release remains pending review of
[PR #65](https://github.com/rolandmraiha-cmd/roland-agent/pull/65) and a decision on the model reliability finding below. `v2` has not been merged into `main`.

## Infrastructure and recovery

| Check | Result |
| --- | --- |
| Service restart | Core, browser, model and noVNC restarted; all six intended services healthy |
| Host verification | 12 pass, 0 fail; separate manual checks are recorded below, with deferred phone issues noted at the end |
| Schema and audit | Schema 3 current; audit valid, 574 rows at the post-restart checkpoint |
| Isolation and logging | Isolation checks and model egress denial passed; JSON logs limited to 10 MB × 3 |
| Restore drill | Integrity, foreign keys, schema, audit and expired sessions passed; disposable volumes removed, live state untouched |
| External TCP scan | Full scan completed; only 22, 80 and 443 open |
| External UDP scan | 443 reported `open\|filtered`; Caddy's UDP mapping was independently confirmed. This scan alone does not prove UDP reachability |
| TLS and login | Certificate validated without bypass; GET `/login` returned 200 |
| Browser/VNC sockets | Only intended IPv4 listeners; no IPv6 wildcard VNC listener |
| Saved state | Chat, saved fact, both test files, restored persona and active model survived restart |
| Browser profile | Persistent synthetic cookie survived restart, verified on the actual page before any post-restart cookie-setting navigation |
| Ownership and approvals | Watch/control/handback, reject-without-retry and confirmed dummy submission passed; composer disabled during pending approval |
| Tools after restart | Fresh chat successfully read the test file and browser page; execution confirmed in audit |

Capture and weekly training remained off. The active model remained
`qwen3-4b-q4km-base`, with no previous model. Persona version 10 retained the original settings.

## Measured performance

CPU benchmark: configured context 3072, three threads, three sequential samples,
128 output tokens each, prompt caching off. The benchmark's `current` alias was serving the base model above.

| Sample | Prompt tokens/s | Generated tokens/s | First token (s) | Total (s) |
| --- | ---: | ---: | ---: | ---: |
| 1 | 19.740 | 9.906 | 3.491 | 16.314 |
| 2 | 19.829 | 6.721 | 2.226 | 21.124 |
| 3 | 18.069 | 8.196 | 2.457 | 17.955 |

| Memory watch | Minimum available RAM | Browser peak | Model peak | OOMs / restarts |
| --- | ---: | ---: | ---: | --- |
| 10 minutes under browser load | 3746 MiB | 372.20 MiB | 3084.29 MiB | 0 / 0 |
| 30-minute soak | 3705 MiB | 376.20 MiB | 3088.38 MiB | 0 / 0 |

## Improvements before release

PR #65 fixes the misleading “No active page” label for active untitled pages,
excludes environment backups from Git and Docker while preserving `.env.example`,
corrects login checks to GET with Windows syntax, and explains the benchmark's silent wait.
Its head is `7da27f15df388439376fcdd5e426583b7df74334`; these changes are not yet deployed.

Final draft CI: all seven jobs passed at this head: lint, unit, browser integration,
frontend, edge, no-hosted-LLM and CPU pipeline. Main CI run: `37985871571`;
training/model-switch run: `37985871347`.

The model gave an unsupported answer in a longer chat: it reported a missing cookie
while the browser remained on a blank page and no browser tool execution appeared in
the audit. Direct inspection showed the cookie was present. A fresh chat then ran the
file/browser tools and reported the correct results. An earlier response also claimed
profile recovery before the restart had occurred. These are model reporting problems;
PR #65 does not repair them. Reproduce longer-chat behavior and improve reporting of
unfinished actions before treating it as resolved.

Mobile-data DNS/intermittent phone-screen faults and the separate GPU training workflow
remain deferred as previously agreed.

Next: review the polish PR, decide how to handle the model reporting finding, and
complete release review before the `v2` → `main` merge.
