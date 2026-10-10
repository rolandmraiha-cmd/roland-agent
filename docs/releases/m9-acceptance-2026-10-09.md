# M9 acceptance — 9 October 2026

Publication approved by Roland on 9 October. The original checkpoint below predates
the PR #65 merge; the 10 October addendum records the final deployed runtime candidate.
Current integration/release status is maintained in AGENT, NEXT and release PR #66.

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

## Final deployed candidate — 10 October 2026

Roland's deployment log confirms `v2` at
`7b3de9dcc032d50462b9999bc228576dbd3ec96f`. PR #65 polish and PR #67's official-image
mirror fix are included. All five image builds succeeded; preflight reported zero failures
and warnings, all six intended services were healthy, and deployment finished successfully.
An online DB/workspace backup was taken before the build. Secrets permissions and the
existing host configuration were retained; capture, weekly training and trainer stay off.

| Final focused check | Result |
| --- | --- |
| Host verify | 12 pass / 0 fail / 2 manual categories; previous full manual evidence above remains applicable |
| Schema / audit | Database version 3, target 3, up to date; audit `ok: true`, 591 rows, no bad row |
| Idle available memory | 3819 MiB, above the 1200 MiB idle target |
| Untitled page | Active cookie-test page showed `Untitled page`, rather than `No active page` |
| Watch / reconnect | Watch connected; a reload reconnected with the agent still working |
| Take control / Hand back | Connected control showed the agent paused and User mode; Hand back restored Agent mode and thumbnail refresh |
| Fresh tool request | `browser_open` and `browser_snapshot` executed against `https://example.com`; actual Browser tab/thumbnail showed that page and audit recorded both results |
| Candidate CI | All seven jobs passed at this exact head: [CI 38030093440](https://github.com/rolandmraiha-cmd/roland-agent/actions/runs/38030093440) and [CPU/model switch 38030093416](https://github.com/rolandmraiha-cmd/roland-agent/actions/runs/38030093416) |

The final fresh-chat reply reported the page title correctly, but labelled the visible
paragraph text as a heading. Tool execution and browser state passed; accurate content
labels remain part of the known model-reporting follow-up. No new ownership, isolation or
deployment failure was found. This smoke does not mark the model-quality issue resolved.

Roland authorized posting/merging the release with the other issues addressed afterwards;
the final deployment/browser checks it was waiting for are complete. The 2.0.0 release
paperwork retains the agreed limitations, folds the completed plan into history and is
checked again before the main merge in [PR #66](https://github.com/rolandmraiha-cmd/roland-agent/pull/66).
The final paperwork changes no runtime source, image pins, settings or secrets, so it does
not require another full restore/load/soak or host rebuild.
