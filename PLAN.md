# PLAN: implementation roadmap for the VLR AI browser agent

Дата подготовки: 2026-04-27
Цель: реализовать тестовое задание максимально близко к идеальному решению работодателя: видимый браузер, автономный агент, терминальный лог tool calls, persistent session, compact context, safety, recovery, sub-agents, видео с демонстрацией.

## 0. Главные инженерные решения

1. Стек: Python 3.11+, Playwright, Pydantic, Rich, Anthropic/OpenAI adapters.
2. Browser-first, но не screenshot-only:
   - основной observation = компактный accessibility/DOM snapshot с refs;
   - screenshot/vision = fallback и verification layer;
   - extraction tool = отдельный дорогой путь для длинного текста.
3. Модель не получает сырые полные страницы.
4. Модель не пишет и не выбирает site-specific CSS selectors.
5. Runtime хранит ref map и сам исполняет Playwright actions.
6. Все рискованные side effects проходят через SecurityLayer.
7. Логи должны выглядеть как в идеальном видео: tool name, JSON input, result.
8. Demo default должен использовать Claude или OpenAI, а не неофициальный OpenAI-compatible provider.

## 1. Исследование и фиксация требований

- [x] Извлечь Craft page через browser/API, потому что HTML требует JavaScript.
- [x] Раскрыть вложенные карточки примеров: spam, food, jobs.
- [x] Сохранить исходный JSON и readable outline в `references/craft/`.
- [x] Скачать и визуально изучить 3 изображения из секции "Как выглядит идеальное решение".
- [x] Изучить OpenAI Computer Use / CUA / ChatGPT Agent.
- [x] Изучить Anthropic Computer Use / Claude in Chrome / safety writeups.
- [x] Изучить Google Project Mariner.
- [x] Склонировать и просмотреть browser-use, Stagehand, Playwright MCP, Skyvern, Agent-E.
- [x] Зафиксировать выводы в `SPEC.md`.

## 2. Repository bootstrap

Deliverables:

- package structure;
- dependency management;
- runnable CLI;
- `.env.example`;
- README skeleton.

Checklist:

- [x] Создать `pyproject.toml` с Python `>=3.11,<3.13` или проверить совместимость с 3.13 отдельно.
- [x] Добавить зависимости:
  - `playwright`
  - `pydantic>=2`
  - `rich`
  - `python-dotenv`
  - `anthropic`
  - `openai`
  - `tenacity`
  - `tiktoken` or provider tokenizer fallback
  - `pytest`, `pytest-asyncio`
- [x] Добавить `ai_browser_agent/` по структуре из `SPEC.md`.
- [x] Добавить CLI entrypoint `ai-browser-agent`.
- [x] Добавить `ai-browser-agent doctor`:
  - проверяет Python version;
  - проверяет Playwright browser installation;
  - проверяет env keys;
  - проверяет возможность открыть видимый browser;
  - предупреждает, если выбран не Claude/OpenAI provider.
- [x] Добавить `.gitignore`:
  - `.env`
  - `profiles/`
  - `runs/`
  - `.pytest_cache/`
  - `__pycache__/`
  - Playwright traces/videos.

Acceptance:

- [x] `ai-browser-agent doctor` запускается.
- [x] `python -m ai_browser_agent.cli --help` работает.

## 3. BrowserController MVP

Deliverables:

- visible Chromium;
- persistent profile;
- basic actions;
- screenshot artifacts.

Tasks:

- [x] Implement `BrowserController.launch(profile_dir, headless=False)`.
- [x] Use `chromium.launch_persistent_context` when profile dir is present.
- [x] Default viewport: `1280x900`.
- [x] Default headless: `False`.
- [x] Implement:
  - [x] `navigate`
  - [x] `go_back`
  - [x] `click`
  - [x] `type_text`
  - [x] `press_key`
  - [x] `scroll`
  - [x] `wait_for_stable`
  - [x] `current_url/title`
  - [x] `screenshot`
- [x] Add trace/video options.
- [x] Add tab tracking.
- [x] Add popup/new page listener.

Acceptance:

- [x] Browser opens visibly.
- [x] Manual login persists when the same profile is reused.
- [x] Screenshot is saved into `runs/<id>/screenshots`.

## 4. SnapshotEngine and ref map

Deliverables:

- compact page state;
- model-facing indexed refs;
- no full-page raw HTML.

Tasks:

- [x] Implement accessibility snapshot collection through Playwright accessibility/CDP or injected DOM walker.
- [x] Build `ElementRef` schema:
  - `ref`
  - role/tag/name/text
  - attributes: placeholder, aria-label, title, href domain/path only when useful
  - bbox
  - visible/enabled/focused/checked/expanded
  - parent chain
  - DOM signature hash
- [ ] Filter to visible viewport by default.
- [x] Include page stats and scroll state.
- [x] Include modal/overlay candidates.
- [ ] Include new/changed markers after previous step if feasible.
- [x] Implement truncation:
  - max interactive items;
  - max text chars;
  - high-signal first.
- [x] Implement `full_light` mode with headings/forms/links only.
- [x] Redact password/secret fields.
- [x] Persist ref map per observation in memory only.

Acceptance:

- [x] `observe(visible)` returns a compact JSON/text summary under target token budget.
- [x] Snapshot of a search page includes search input and buttons with refs.
- [x] Hidden scripts/styles/SVG internals are absent.

## 5. ElementResolver

Deliverables:

- ref -> executable action;
- stale element recovery;
- semantic query fallback.

Tasks:

- [x] Store ref map for current observation.
- [x] Resolve ref by backend node id / locator / DOM signature.
- [x] Implement `query_dom(query)`:
  - [x] local lexical rank first;
  - fast model rank if ambiguous;
  - [x] returns candidate refs with evidence.
- [x] Implement scroll into view.
- [x] Detect multiple candidates and return ambiguity.
- [x] On stale ref:
  - [x] resnapshot;
  - [x] re-rank candidate by signature;
  - [x] retry once.
- [ ] Implement screenshot-coordinate fallback behind explicit trigger.

Acceptance:

- [x] Agent can click/type by ref.
- [x] If DOM changes after typing, stale ref does not crash the whole run.
- [x] No website-specific selectors appear in source.

## 6. LLM adapters and typed tool calls

Deliverables:

- provider-neutral model interface;
- Anthropic adapter;
- OpenAI adapter;
- structured outputs.

Tasks:

- [x] Define `LLMClient` protocol.
- [x] Define `LLMRequest`, `LLMResponse`, `ToolCall`.
- [x] Implement Anthropic tool-use adapter.
- [x] Implement OpenAI Responses adapter.
- [x] Normalize tool-call format across providers.
- [ ] Add timeout/retry/rate-limit handling.
- [x] Capture token usage when provider returns it.
- [x] Add fake LLM for tests.

Acceptance:

- [x] Same AgentCore can run with Anthropic or OpenAI config.
- [x] Tool-call JSON is logged identically for both providers.

## 7. ModelCascade

Deliverables:

- model routing;
- escalation logic;
- cost-aware behavior.

Tasks:

- [x] Define model roles:
  - fast;
  - primary;
  - strong;
  - vision.
- [x] Implement routing rules:
  - routine action -> fast/primary;
  - first plan -> primary/strong;
  - risky action -> strong/safety;
  - repeated failure -> strong;
  - screenshot fallback -> vision.
- [x] Add manual override flags.
- [ ] Add run log field `selected_model`.
- [ ] Add budget counters and warnings.

Acceptance:

- [x] Repeated failed action escalates.
- [x] Final `done(success=true)` is verified by strong model or deterministic checks.

## 8. Tool layer

Deliverables:

- model-facing tools from `SPEC.md`;
- strict schemas;
- helpful errors.

Tasks:

- [x] Implement tool schemas with Pydantic.
- [x] Implement tool dispatcher.
- [x] Tools:
  - [x] `observe`
  - [x] `query_dom`
  - [x] `take_screenshot`
  - [x] `navigate`
  - [x] `click`
  - [x] `type_text`
  - [x] `press_key`
  - [x] `scroll`
  - [x] `select_option`
  - [x] `extract`
  - [x] `wait`
  - [x] `ask_user`
  - [x] `handoff_to_user`
  - [x] `done`
- [ ] Every side-effecting tool requires `intent`.
- [ ] Every error returns:
  - class;
  - short message;
  - suggested recovery;
  - whether retryable.

Acceptance:

- [x] Bad model args fail validation without crashing.
- [x] Tool errors are readable enough for the model to recover.

## 9. ContextManager

Deliverables:

- compact context packet;
- memory compaction;
- strict separation of trusted/untrusted content.

Tasks:

- [ ] Define context budget per provider/model.
- [x] Always preserve:
  - system prompt;
  - original user task;
  - current plan;
  - safety policy;
  - latest observation.
- [x] Keep last N step summaries.
- [x] Summarize older steps into memory.
- [x] Summarize extraction results separately from browser state.
- [ ] Add page fingerprint deduplication.
- [x] Add prompt tags:
  - `trusted_user_task`
  - `agent_memory`
  - `untrusted_page_content`
  - `tool_result`
- [x] Add tests for compaction preserving critical facts.

Acceptance:

- [x] Long run does not grow unbounded context.
- [x] Full HTML is never sent in normal observe loop.
- [x] User task remains visible after compaction.

## 10. AgentCore and sub-agents

Deliverables:

- autonomous loop;
- planning;
- page exploration;
- verification.

Tasks:

- [x] Implement `AgentCore.run_task`.
- [x] Implement step loop with max steps/failures.
- [x] Implement `PlannerAgent`.
- [x] Implement `ExplorerAgent`.
- [x] Implement `ExecutorAgent`.
- [x] Implement `ExtractorAgent`.
- [x] Implement `CriticAgent`.
- [x] Add plan update format:
  - `[ ] pending`
  - `[>] current`
  - `[x] done`
  - `[-] skipped`
- [ ] Trigger replan on:
  - changed site flow;
  - repeated failures;
  - missing required information;
  - user answer.
- [x] Ensure no prewritten task workflows are embedded.

Acceptance:

- [x] Given an arbitrary new task, agent plans at goal level and chooses actions from observations.
- [x] Agent asks user only for missing login/2FA/confirmation/blocker.

## 11. SecurityLayer

Deliverables:

- risk classifier;
- confirmation UX;
- prompt-injection defenses.

Tasks:

- [x] Implement deterministic risk keyword/pattern classifier.
- [ ] Implement LLM safety reviewer for ambiguous cases.
- [x] Define `SecurityDecision`: allow, confirm, handoff, block.
- [x] Classify:
  - delete/spam/trash/archive;
  - send/submit/apply/post;
  - pay/buy/checkout/order;
  - upload/download/run/install;
  - password/2FA/personal data.
- [x] Confirm at point of risk, not before safe prep work.
- [x] Add user prompt with action/object/data/risk.
- [ ] Add direct-user-instruction rule.
- [x] Add webpage/email prompt-injection detector.
- [x] Add site-level warning/blocklist config for high-risk domains.

Acceptance:

- [ ] Food demo stops before final payment.
- [ ] Spam demo asks before deletion.
- [ ] Job demo asks before sending applications.
- [x] Prompt injection fixture fails safely.

## 12. Error recovery

Deliverables:

- robust retries;
- adaptive recovery;
- loop detection.

Tasks:

- [x] Implement error taxonomy.
- [ ] Wrap tool execution in retry policy.
- [ ] Recovery strategies:
  - [x] wait/resnapshot;
  - [ ] close popup;
  - [x] scroll into view;
  - [x] rerun query_dom;
  - [x] screenshot fallback;
  - [x] navigate back;
  - [x] ask user for login/2FA;
  - [x] replan.
- [x] Add page fingerprint stagnation.
- [x] Add action repetition detection.
- [x] Add provider retry/backoff.
- [x] Save failure screenshots.

Acceptance:

- Local popup fixture is handled.
- [x] Delayed SPA fixture waits instead of failing immediately.
- [ ] Loop fixture triggers replan/escalation.

## 13. Extraction

Deliverables:

- structured data extraction without dumping entire pages into main loop.

Tasks:

- [x] Implement clean text/markdown extraction.
- [ ] Chunk long pages by headings/containers.
- [x] Support query-specific extraction.
- [x] Support optional JSON schema.
- [x] Return evidence snippets and source refs.
- [x] Cache extract results by URL + query + page fingerprint.
- [ ] Use extraction for:
  - email content;
  - vacancy details;
  - product/cart details;
  - search results.

Acceptance:

- Extracting 10 email summaries does not send whole mailbox DOM repeatedly.
- [x] Extraction output includes evidence and uncertainty.

## 14. Observability

Deliverables:

- terminal log;
- artifacts;
- replayable run.

Tasks:

- [x] Implement JSONL event logger.
- [x] Implement Rich live console:
  - current step;
  - URL/title;
  - action/tool call;
  - args;
  - result;
  - warnings;
  - token usage.
- [x] Save screenshots and annotate refs.
- [x] Save final `summary.md`.
- [x] Add `replay` command to print run timeline.
- [x] Add optional Playwright trace/video.

Acceptance:

- [x] Terminal view is good enough for the employer video.
- [x] A failed run can be debugged from artifacts.

## 15. Local eval fixtures

Deliverables:

- deterministic tests without real accounts.

Tasks:

- [x] Add fixture server, e.g. FastAPI or static Vite.
- [x] Fake mail:
  - inbox with 10 emails;
  - spam indicators;
  - phishing prompt injection;
  - delete/spam buttons.
- [x] Fake delivery:
  - search;
  - similar products;
  - cart;
  - checkout stop.
- [x] Fake jobs:
  - profile/resume page;
  - search filters;
  - vacancy pages;
  - application form.
- [ ] Dynamic UI cases:
  - delayed loading;
  - popup;
  - iframe;
  - shadow DOM;
  - hidden malicious DOM text.
- [x] Define `evals/tasks.yaml`.
- [x] Implement `evals/run_eval.py`.

Acceptance:

- [x] CI can run non-LLM unit tests.
- [x] Manual LLM eval can run 3 end-to-end fixture tasks.

## 16. Real-site demo preparation

Preferred demo: food ordering up to checkout, because it visually matches the employer's screenshots and avoids irreversible final action.

Steps:

- [ ] Choose site where manual login is available.
- [ ] Start browser with profile:
  - `ai-browser-agent profile login --profile ./profiles/demo --url <site>`
- [ ] User logs in manually.
- [ ] Run task:
  - "Открой сайт доставки, найди BBQ-бургер и картошку фри, добавь подходящие позиции в корзину, дойди до checkout, но не оплачивай."
- [ ] Verify:
  - visible browser left;
  - terminal right;
  - tool calls visible;
  - final report includes items/cart/stop before payment.
- [ ] Record short video.

Fallback demo:

- local fake delivery site if real site blocks automation;
- explain in README that local demo is deterministic and real-site demo uses persistent login.

## 17. Anti-hardcode audit

Tasks:

- [x] Search code for suspicious domain-specific strings:
  - `hh.ru`
  - `yandex`
  - `lavka`
  - `mail`
  - `vacancy`
  - `burger`
  - `spam`
  - `data-qa`
  - fixed XPath/CSS from real sites.
- [x] Allow such strings only in docs/tests/demo task text, not agent logic.
- [x] Add test or lint script `scripts/check_no_hardcoded_flows.py`.
- [x] Review prompts for hidden hints like "for jobs click vacancies".

Acceptance:

- [x] Agent logic contains no site-specific flows.
- [x] README explicitly states this and explains how elements are discovered.

## 18. README and submission docs

README must include:

- project purpose;
- quickstart;
- provider setup for Anthropic/OpenAI;
- Playwright install;
- persistent login/profile flow;
- running a task;
- demo commands;
- architecture diagram;
- tool list;
- context strategy;
- safety policy;
- limitations;
- how to inspect runs/artifacts;
- video recording notes.

Submission notes:

- [ ] Include repo link.
- [ ] Include video link.
- [x] Mention that AI tools were used for development, as expected by employer.
- [x] Mention researched inspirations and tradeoffs.

## 19. Definition of done

Functional:

- [x] Visible browser opens.
- [x] User can enter arbitrary text task.
- [x] Agent operates autonomously for multiple page transitions.
- [x] Persistent session works.
- [x] Agent can ask user for login/2FA/confirmation.
- [x] Agent can finish with grounded report.

Architecture:

- [x] Playwright controller.
- [x] Compact snapshot engine.
- [x] Ref-based element resolver.
- [x] Context manager with compaction.
- [x] Model cascade.
- [x] Sub-agent prompts.
- [x] Security layer.
- [x] Error recovery.
- [x] Observability artifacts.

Compliance:

- [x] Claude or OpenAI is configured as default provider.
- [x] No hardcoded real-site selectors.
- [x] No hardcoded real-site flows.
- [x] No full pages sent to context by default.
- [x] Risky actions confirmed.
- [x] Tool calls visible in terminal.

Verification:

- [x] Unit tests pass.
- [x] Integration fixtures pass or are manually verified.
- [ ] One complex demo task recorded.
- [ ] README can be followed on a clean checkout.

## 20. Known risks and mitigations

Risk: real services block automation.

- Mitigation: persistent profile, visible browser, realistic waits, no stealth claims, local fixtures for deterministic proof.

Risk: model clicks wrong element.

- Mitigation: refs, annotated screenshots, post-action verification, strong/vision fallback.

Risk: prompt injection from emails/webpages.

- Mitigation: untrusted tags, safety reviewer, confirmations, suspicious content detector.

Risk: token overuse.

- Mitigation: viewport-first snapshot, extraction only by query, compaction, cached page fingerprints, model cascade.

Risk: overengineering.

- Mitigation: implement thin versions first, but keep contracts clean.

Risk: demo requires irreversible action.

- Mitigation: stop before payment/delete/send unless explicit confirmation is recorded.

## 21. Suggested implementation order

1. BrowserController + CLI smoke.
2. SnapshotEngine + refs.
3. Tool dispatcher + fake LLM tests.
4. Anthropic/OpenAI adapter.
5. AgentCore minimal loop.
6. ContextManager.
7. SecurityLayer.
8. ErrorHandler.
9. Sub-agents and ModelCascade.
10. Observability polishing.
11. Local fixtures/evals.
12. README and video demo.

Do not start with real Yandex/hh flows. First prove the generic agent loop on controlled fixtures, then run real sites as manual demo.

## 22. Quality bar for final review

Before submitting, inspect:

- Does terminal make the agent's reasoning and tool use understandable without reading code?
- Does browser visibly move through the task?
- Does final answer avoid overclaiming?
- Can reviewer see context management exists?
- Can reviewer see safety exists exactly where it matters?
- Would changing the target site layout require code changes? If yes, remove the brittle logic.
- Does any source file look like a scripted automation disguised as an agent? If yes, refactor.
