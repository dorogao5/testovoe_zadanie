# AI Browser Agent

An autonomous AI-powered browser automation agent built with Python, Playwright, and Alibaba Model Studio (OpenAI-compatible API). The agent controls a real web browser to perform complex multi-step tasks — navigating pages, clicking elements, typing text, scrolling, and extracting information — all guided by an LLM with built-in planning, error recovery, and security checks.

---

## Features

- **Autonomous Browser Agent** — Give it a task in plain English and watch it browse the web, discover elements, and take actions without hardcoded selectors.
- **Context Management** — Smart conversation history with token-budget trimming and summarization to keep the LLM focused on what matters.
- **Sub-Agents** — Specialized sub-agents handle planning (PlannerAgent), page exploration (ExplorerAgent), task verification (CriticAgent), and history compression (SummarizerAgent).
- **Security Layer** — Automatic detection of destructive or high-risk actions (delete, pay, checkout, etc.) with user confirmation prompts before execution.
- **Error Recovery** — Automatic retries with exponential backoff, alternative selector strategies, and scroll-and-retry for flaky pages.
- **Rich Terminal UI** — Beautiful, color-coded output showing the agent's thoughts, actions, browser state, and results in real time.
- **Persistent Profiles** — Save browser sessions (cookies, localStorage) across runs with `--profile-dir`.
- **Headless or Visible** — Run with a visible browser window (default) or headless for automation pipelines.

---

## Installation

### Prerequisites

- Python 3.10+
- [Playwright](https://playwright.dev/python/) browsers

### Step 1: Install Python dependencies

```bash
pip install -r requirements.txt
```

The `requirements.txt` includes:
- `openai>=1.0`
- `playwright`
- `pydantic`
- `rich`
- `python-dotenv`

### Step 2: Install Playwright browsers

```bash
playwright install chromium
```

### Step 3: Configure your API key

Create a `.env` file in the project root:

```bash
LLM_API_KEY=sk-your-api-key-here
LLM_BASE_URL=https://dashscope-intl.aliyuncs.com/compatible-mode/v1
LLM_MODEL=qwen3.6-max-preview
```

Or set the environment variable directly:

```bash
export LLM_API_KEY=sk-your-api-key-here
```

> **Note:** Do not commit real API keys. Keep them in `.env` (which is git-ignored).

---

## Usage

### Interactive Mode (default)

Launch the agent and chat with it interactively:

```bash
python main.py
```

Once running, type any task in natural language:

```
You ▸ Go to wikipedia.org and search for "Artificial Intelligence". Tell me the first paragraph.
```

Available commands in interactive mode:

| Command | Alias | Description |
|---------|-------|-------------|
| `/quit` | `/q` | Exit the agent |
| `/help` | `/h` | Show help message |
| `/screenshot` | `/ss` | Capture and save a screenshot |
| `/state` | `/st` | Show current browser state (URL, title, elements) |
| `/reset` | `/r` | Reset agent context and history |

### Single Task Mode

Run one task and exit:

```bash
python main.py --task "Go to wikipedia.org and search for 'Python programming'. Summarize the first section."
```

### Demo Mode

Run the built-in demo task (Wikipedia search):

```bash
python main.py --demo
```

### Persistent Browser Profile

Keep cookies and login state across sessions:

```bash
python main.py --profile-dir ./browser-profile --task "Check my email"
```

### Headless Mode

Run without a visible browser window (useful for CI/automation):

```bash
python main.py --headless --task "Go to example.com and report the page title"
```

### Override API Key via CLI

```bash
python main.py --api-key sk-your-key-here --task "Go to news.ycombinator.com and list the top 3 stories"
```

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│                        CLI Interface                         │
│                    (cli.py / main.py)                         │
└──────────────────────┬──────────────────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────────────────┐
│                      AgentCore (Main Loop)                   │
│         observe → reason → act → verify                     │
└──────────────────────┬──────────────────────────────────────┘
           ┌───────────┼───────────┐
           │           │           │
    ┌──────▼────┐ ┌────▼────┐ ┌────▼──────┐
    │  Browser  │ │ Security│ │  Error    │
    │Controller │ │  Layer  │ │ Handler   │
    └────┬──────┘ └─────────┘ └───────────┘
         │
    ┌────▼──────┐
    │ Playwright│
    │ (Chromium)│
    └───────────┘
```

### Module Descriptions

| Module | Responsibility |
|--------|----------------|
| `main.py` | CLI entry point, argument parsing, mode dispatch |
| `cli.py` | Rich terminal UI — banners, tables, panels, prompts |
| `agent_core.py` | Main OODA loop (observe, reason, act, verify) |
| `browser_controller.py` | Playwright wrapper, DOM distillation, element resolution |
| `kimi_client.py` | OpenAI-compatible API client (Alibaba/Qwen compatible) with tool calling |
| `context_manager.py` | Conversation history, token budget, summarization |
| `sub_agents.py` | Planner, Explorer, Critic, and Summarizer sub-agents |
| `security.py` | Risk classification and destructive-action gating |
| `error_handler.py` | Error classification, retry strategies, recovery |
| `models.py` | Pydantic data models for all structures |
| `utils.py` | Token counting, DOM cleaning, retry decorators |
| `prompts.py` | System prompts and tool definitions |

---

## Configuration

The agent reads configuration from:

1. **Command-line arguments** (highest priority)
2. **Environment variables** (via `.env` file or shell)
3. **Hard-coded defaults** (lowest priority)

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `LLM_API_KEY` | Alibaba Model Studio API key | _(required)_ |
| `LLM_BASE_URL` | OpenAI-compatible base URL | `https://dashscope-intl.aliyuncs.com/compatible-mode/v1` |
| `LLM_MODEL` | Model name | `qwen3.6-max-preview` |
| `BROWSER_HEADLESS` | Run browser headless | `false` |
| `BROWSER_PROFILE_DIR` | Persistent profile directory | `./browser-profile` |

### `.env` Example

```bash
# .env
LLM_API_KEY=sk-your-personal-key-here
LLM_BASE_URL=https://dashscope-intl.aliyuncs.com/compatible-mode/v1
LLM_MODEL=qwen3.6-max-preview
```

---

## Example Tasks

Here are tasks the agent can handle:

- **Information Retrieval**
  > "Go to Wikipedia, search for 'Machine Learning', and tell me the first paragraph."

- **Navigation & Exploration**
  > "Navigate to github.com, find the search box, type 'python asyncio', and open the first result."

- **Form Interaction**
  > "Go to example.com, find the contact form, fill in 'Name: Test', 'Email: test@example.com', and submit it."

- **Multi-step Workflows**
  > "Go to news.ycombinator.com, find the top 5 stories, open each one in a new tab, and summarize their headlines."

- **Verification**
  > "Go to my-website.com, verify the heading says 'Welcome', and check that there is a 'Sign Up' button."

---

## Safety Notice

This agent controls a **real web browser** and can perform actions that affect real websites and accounts. The built-in **Security Layer** automatically detects and warns about potentially destructive actions, but it is not foolproof.

**Actions that trigger security confirmation:**
- Clicking buttons containing: "delete", "remove", "trash", "unsubscribe", "confirm", "pay", "buy", "checkout", "submit order"
- Typing into password or credit card fields
- Navigating to URLs containing `/delete`, `/remove`, `/checkout`, `/pay`

**Always review the agent's actions when:**
- Using it with logged-in accounts (email, banking, social media)
- Performing actions that could result in data loss or financial impact
- Running on production systems

**Recommendations:**
- Use a dedicated browser profile (`--profile-dir`) to isolate sessions
- Run with the browser visible (default) so you can observe and intervene
- Review the security prompts carefully before confirming destructive actions
- Consider using headless mode only for well-tested, read-only tasks

---

## Development

### Project Structure

```
ai_browser_agent/
├── main.py                 # CLI entry point
├── cli.py                  # Rich terminal UI
├── agent_core.py           # Main agent loop
├── browser_controller.py   # Playwright wrapper
├── kimi_client.py          # OpenAI-compatible LLM client
├── context_manager.py      # Token budget management
├── sub_agents.py           # Sub-agent orchestrator
├── security.py             # Security layer
├── error_handler.py        # Error handling & recovery
├── models.py               # Pydantic models
├── utils.py                # Helpers
├── prompts.py              # System prompts
├── requirements.txt
├── .env.example
└── README.md
```

### Running Tests

```bash
# Unit tests
python -m unittest discover -s tests -p "test_*.py"

# Optional manual module sanity checks
python browser_controller.py
python kimi_client.py
```

---

## License

MIT License — see the project repository for full license text.

---

## Acknowledgments

- Built with [Playwright](https://playwright.dev/) for browser automation
- Powered by [Alibaba Model Studio](https://www.alibabacloud.com/help/en/model-studio/) for LLM reasoning
- Terminal UI rendered with [Rich](https://github.com/Textualize/rich)
