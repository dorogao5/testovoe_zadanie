# Plan: AI Browser Automation Agent (Alibaba/Qwen)

## Overview
Build an AI agent that autonomously controls a web browser to perform complex multi-step tasks using Alibaba Model Studio (`qwen3.6-max-preview`) via OpenAI-compatible API.

## Test Task Requirements (from https://vlrdev.craft.me/ai_test_task)
- **Browser automation**: Programmatic control, persistent sessions, visible (non-headless) browser
- **Autonomous AI agent**: Uses LLM, makes decisions autonomously, handles multi-step tasks with page transitions
- **Context management**: Token limit strategies — cannot send entire pages to AI context
- **Advanced patterns** (at least one): Sub-agent architecture, error handling/recovery, security layer for destructive actions
- **Anti-requirements**: No hardcoded action plans, no preset selectors, no hardcoded links or element hints
- **Example tasks**: Delete spam emails, order food, search and apply for jobs

## Architecture

### Technology Stack
- **Language**: Python 3.10+
- **Browser automation**: Playwright (visible mode, supports persistent contexts)
- **AI SDK**: OpenAI-compatible API (Alibaba Model Studio)
- **Base URL**: `https://dashscope-intl.aliyuncs.com/compatible-mode/v1`
- **Model**: `qwen3.6-max-preview`
- **Environment variables**: `LLM_API_KEY`, `LLM_BASE_URL`, `LLM_MODEL`

### Core Components
1. **BrowserController** (Playwright wrapper)
   - Launch visible browser
   - Persistent context support (user can login manually)
   - Actions: navigate, click, type, scroll, screenshot, extract DOM
   - Dynamic element resolution (no hardcoded selectors)

2. **ContextManager**
   - DOM distillation (extract only interactive + semantic elements)
   - Rolling window / summarization for history
   - Token budgeting

3. **AgentCore**
   - Main loop: observe → think → act → verify
   - Tool definitions for LLM function calling
   - OpenAI-compatible API integration (Alibaba/Qwen) with function calling

4. **SubAgentOrchestrator**
   - Planner agent: breaks down complex tasks
   - Explorer agent: discovers page structure
   - Executor agent: performs actions
   - Critic agent: verifies results

5. **SecurityLayer**
   - Intercepts destructive actions (payments, deletions, form submissions)
   - Asks user for confirmation
   - Whitelist/blacklist patterns

6. **ErrorHandler**
   - Retry with backoff
   - Alternative strategy selection
   - Screenshot capture on failure

## Stage 1: Foundation (parallel)
- Implement BrowserController with Playwright
- Implement OpenAI-compatible LLM client

## Stage 2: Agent Core (parallel)
- Implement tool definitions (navigate, click, type, scroll, screenshot, find_element, ask_user)
- Implement agent loop
- Implement DOM distillation for context management

## Stage 3: Advanced Patterns
- Sub-agent architecture
- Security layer
- Error handling & recovery

## Stage 4: Integration & Demo
- CLI interface
- Demo script for "delete spam" or "search jobs" task
- Documentation

## Output
- Runnable Python project in `/mnt/agents/output/ai_browser_agent/`
- README with setup instructions
- Demo script ready to run
