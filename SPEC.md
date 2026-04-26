# SPEC.md: AI Browser Automation Agent (Alibaba/Qwen)

## Overview
An autonomous AI agent that controls a visible web browser to perform complex multi-step tasks using Alibaba Model Studio (OpenAI-compatible). The agent observes the browser state, reasons about next actions, executes them via Playwright, and handles errors and security concerns autonomously.

## Technology Stack
- **Language**: Python 3.10+
- **Browser**: Playwright (Chromium, visible/non-headless)
- **LLM**: Alibaba Model Studio via OpenAI-compatible endpoint
  - Base URL: `https://dashscope-intl.aliyuncs.com/compatible-mode/v1`
  - Model: `qwen3.6-max-preview`
  - API Key env: `LLM_API_KEY`
- **Dependencies**: `openai>=1.0`, `playwright`, `pydantic`, `rich`, `python-dotenv`

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                        CLI Interface                         │
│                     (user input / display)                   │
└──────────────────────┬──────────────────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────────────────┐
│                      AgentCore (Main Loop)                   │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────┐  │
│  │   Planner   │  │   Executor  │  │   ContextManager    │  │
│  │  (SubAgent) │  │  (SubAgent) │  │                     │  │
│  └─────────────┘  └─────────────┘  └─────────────────────┘  │
└──────────────────────┬──────────────────────────────────────┘
                       │
        ┌──────────────┼──────────────┐
        │              │              │
┌───────▼─────┐ ┌──────▼──────┐ ┌────▼────────┐
│  Security   │ │   Error     │ │   Browser   │
│   Layer     │ │   Handler   │ │  Controller │
└─────────────┘ └─────────────┘ └────┬────────┘
                                     │
                              ┌──────▼──────┐
                              │  Playwright  │
                              │  (Chromium)  │
                              └─────────────┘
```

## Module Specifications

### 1. BrowserController (`browser_controller.py`)
**Responsibility**: Low-level browser automation via Playwright.

**Class**: `BrowserController`

**Methods**:
```python
class BrowserController:
    async def launch(self, headless: bool = False, user_data_dir: str | None = None) -> None
    async def close(self) -> None
    async def navigate(self, url: str) -> None
    async def get_current_url(self) -> str
    async def get_page_title(self) -> str
    
    # Element interaction (dynamic resolution — no hardcoded selectors)
    async def find_element(self, description: str) -> dict  # Returns element info with selector
    async def click(self, selector: str) -> None
    async def type_text(self, selector: str, text: str, clear_first: bool = True) -> None
    async def press_key(self, key: str) -> None
    async def scroll(self, direction: str = "down", amount: int = 500) -> None
    
    # State extraction
    async def get_screenshot(self, full_page: bool = False) -> bytes
    async def get_distilled_dom(self) -> str  # Semantic, interactive elements only
    async def get_full_dom(self) -> str  # For debugging
    
    # Session
    async def save_session_state(self) -> dict
    async def restore_session_state(self, state: dict) -> None
```

**DOM Distillation Rules**:
- Extract only interactive elements: `<a>`, `<button>`, `<input>`, `<textarea>`, `<select>`, elements with `onclick` or `role="button"`
- For each element, capture: tag, text content, placeholder, aria-label, title, href (for links), input type, visible coordinates
- Remove: scripts, styles, hidden elements, SVG internals, image src attributes
- Format as a structured text representation (not raw HTML) to save tokens

### 2. KimiClient (`kimi_client.py`)
**Responsibility**: OpenAI-compatible API client (Alibaba/Qwen compatible) with tool/function calling support.

**Class**: `KimiClient`

**Methods**:
```python
class KimiClient:
    def __init__(self, api_key: str, base_url: str = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1", model: str = "qwen3.6-max-preview")
    
    def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        temperature: float = 0.3,
        max_tokens: int = 4096,
    ) -> dict  # Returns response with optional tool_calls
    
    def parse_tool_calls(self, response: dict) -> list[ToolCall]
    def extract_content(self, response: dict) -> str
```

**Tool Schema Format**: OpenAI function calling format.

### 3. ContextManager (`context_manager.py`)
**Responsibility**: Manage conversation history and token budget.

**Class**: `ContextManager`

**Methods**:
```python
class ContextManager:
    def __init__(self, max_tokens: int = 6000, model: str = "qwen3.6-max-preview")
    
    def add_message(self, role: str, content: str) -> None
    def add_tool_result(self, tool_call_id: str, role: str, content: str) -> None
    def get_messages(self) -> list[dict]
    def get_token_count(self) -> int
    def trim_history(self) -> None  # Summarize oldest messages when approaching limit
    def add_system_prompt(self, prompt: str) -> None
    def clear(self) -> None
    
    def summarize_old_messages(self, messages: list[dict]) -> str
```

**Trimming Strategy**:
1. Always keep system prompt
2. Keep most recent N interactions (where N depends on token count)
3. Summarize older interactions into a condensed "memory" message
4. Never discard the current task description

### 4. AgentCore (`agent_core.py`)
**Responsibility**: Main agent loop — observe, reason, act, verify.

**Class**: `AgentCore`

**Methods**:
```python
class AgentCore:
    def __init__(
        self,
        browser: BrowserController,
        llm: KimiClient,
        context: ContextManager,
        security: SecurityLayer,
        error_handler: ErrorHandler,
        sub_agents: SubAgentOrchestrator,
    )
    
    async def run_task(self, task: str, max_steps: int = 50) -> AgentResult
    async def step(self) -> StepResult  # One observation-reasoning-action cycle
    async def observe(self) -> BrowserState
    async def reason(self, state: BrowserState) -> AgentDecision
    async def act(self, decision: AgentDecision) -> ActionResult
    
    # Built-in tools exposed to LLM
    async def tool_navigate(self, url: str) -> str
    async def tool_click(self, element_description: str) -> str
    async def tool_type(self, element_description: str, text: str) -> str
    async def tool_scroll(self, direction: str) -> str
    async def tool_find_information(self, query: str) -> str
    async def tool_ask_user(self, question: str) -> str
    async def tool_done(self, result: str) -> str
    async def tool_wait(self, seconds: int) -> str
```

**System Prompt** (excerpt):
```
You are an autonomous browser automation agent. Your goal is to complete tasks by controlling a web browser.

Rules:
1. You receive a distilled view of the current web page (interactive elements, text content).
2. You can perform actions: navigate, click, type, scroll, find information, ask the user, mark task as done.
3. NEVER assume hardcoded URLs or selectors. Discover them from the page content.
4. When clicking or typing, describe the target element in natural language (e.g., "button with text 'Submit'"). The system will resolve it.
5. For multi-step tasks, plan ahead but be ready to adapt if the page changes unexpectedly.
6. If you're stuck, ask the user for guidance.
7. Before destructive actions (delete, purchase, submit), the security layer will prompt for confirmation.
8. Always verify your actions had the expected effect by observing the resulting page state.
```

### 5. SubAgentOrchestrator (`sub_agents.py`)
**Responsibility**: Delegate specialized sub-tasks to focused sub-agents.

**Class**: `SubAgentOrchestrator`

**Sub-agents**:
```python
class PlannerAgent:  # Breaks complex tasks into steps
    async def plan(self, task: str, current_state: BrowserState) -> list[Step]

class ExplorerAgent:  # Discovers page structure and available actions
    async def explore(self, state: BrowserState) -> PageAnalysis
    
class ExecutorAgent:  # Executes a single action with retry logic
    async def execute(self, action: Action, browser: BrowserController) -> ActionResult
    
class CriticAgent:  # Verifies task completion and suggests corrections
    async def verify(self, task: str, state: BrowserState, history: list[Step]) -> VerificationResult
```

**Integration**: The main AgentCore uses the PlannerAgent for complex tasks, ExplorerAgent when stuck, and CriticAgent after key milestones.

### 6. SecurityLayer (`security.py`)
**Responsibility**: Gate destructive or irreversible actions.

**Class**: `SecurityLayer`

**Methods**:
```python
class SecurityLayer:
    def __init__(self, auto_approve: list[str] | None = None)
    
    def check_action(self, action_type: str, action_params: dict) -> SecurityDecision
    # Returns: ALLOW, BLOCK, or ASK_USER (with explanation)
    
    def is_destructive(self, action_type: str, params: dict) -> bool
    def get_risk_level(self, action_type: str, params: dict) -> str  # low, medium, high, critical
```

**Destructive Actions** (require confirmation):
- `click` on elements containing: "delete", "remove", "trash", "unsubscribe", "confirm", "pay", "buy", "checkout", "submit order"
- `type` into password fields, credit card fields, SSN fields
- `navigate` to URLs containing: /delete, /remove, /checkout, /pay

### 7. ErrorHandler (`error_handler.py`)
**Responsibility**: Handle and recover from failures.

**Class**: `ErrorHandler`

**Methods**:
```python
class ErrorHandler:
    def __init__(self, max_retries: int = 3)
    
    async def execute_with_retry(
        self,
        action_func: callable,
        context: dict,
        strategy: str = "simple"  # simple, alternative_selector, scroll_and_retry
    ) -> ActionResult
    
    def classify_error(self, error: Exception) -> ErrorType  # timeout, selector_not_found, navigation_error, network_error, etc.
    def suggest_recovery(self, error_type: ErrorType) -> RecoveryStrategy
```

### 8. CLI Interface (`main.py`, `cli.py`)
**Responsibility**: User-facing entry point.

**Features**:
- Start browser, enter task, watch agent work
- Rich terminal output (agent thoughts, actions, screenshots)
- Persistent session support ( `--profile-dir` )
- Demo mode with predefined example tasks

## Data Schemas (Pydantic)

```python
class ToolCall(BaseModel):
    id: str
    name: str
    arguments: dict

class BrowserState(BaseModel):
    url: str
    title: str
    distilled_dom: str
    screenshot: bytes | None
    timestamp: datetime

class Step(BaseModel):
    number: int
    thought: str
    action: ToolCall
    result: str
    timestamp: datetime

class AgentResult(BaseModel):
    success: bool
    task: str
    steps: list[Step]
    final_answer: str
    total_steps: int
    total_time_seconds: float

class PageAnalysis(BaseModel):
    page_type: str  # login, listing, form, detail, search_results, etc.
    available_actions: list[str]
    key_elements: list[dict]
    navigation_options: list[str]
```

## Tool Definitions (OpenAI format)

```json
[
  {
    "type": "function",
    "function": {
      "name": "navigate",
      "description": "Navigate to a URL. Use when you need to visit a new page.",
      "parameters": {
        "type": "object",
        "properties": {
          "url": {"type": "string", "description": "Full URL to navigate to"}
        },
        "required": ["url"]
      }
    }
  },
  {
    "type": "function",
    "function": {
      "name": "click",
      "description": "Click on an element. Describe the element in natural language (e.g., 'button with text Submit', 'link with text About Us'). The system will find it.",
      "parameters": {
        "type": "object",
        "properties": {
          "element_description": {"type": "string", "description": "Natural language description of the element to click"},
          "reason": {"type": "string", "description": "Why you want to click this element"}
        },
        "required": ["element_description", "reason"]
      }
    }
  },
  {
    "type": "function",
    "function": {
      "name": "type_text",
      "description": "Type text into an input field. Describe the field in natural language.",
      "parameters": {
        "type": "object",
        "properties": {
          "element_description": {"type": "string", "description": "Natural language description of the input field"},
          "text": {"type": "string", "description": "Text to type"},
          "submit_after": {"type": "boolean", "description": "Whether to press Enter after typing", "default": false}
        },
        "required": ["element_description", "text"]
      }
    }
  },
  {
    "type": "function",
    "function": {
      "name": "scroll",
      "description": "Scroll the page up or down.",
      "parameters": {
        "type": "object",
        "properties": {
          "direction": {"type": "string", "enum": ["up", "down"], "description": "Scroll direction"},
          "amount": {"type": "integer", "description": "Pixels to scroll", "default": 500}
        },
        "required": ["direction"]
      }
    }
  },
  {
    "type": "function",
    "function": {
      "name": "ask_user",
      "description": "Ask the user a question when you need clarification or additional information.",
      "parameters": {
        "type": "object",
        "properties": {
          "question": {"type": "string", "description": "Question to ask the user"}
        },
        "required": ["question"]
      }
    }
  },
  {
    "type": "function",
    "function": {
      "name": "done",
      "description": "Mark the task as completed and provide the final result.",
      "parameters": {
        "type": "object",
        "properties": {
          "result": {"type": "string", "description": "Summary of what was accomplished"}
        },
        "required": ["result"]
      }
    }
  },
  {
    "type": "function",
    "function": {
      "name": "find_information",
      "description": "Search for specific information on the current page. Returns matching text snippets.",
      "parameters": {
        "type": "object",
        "properties": {
          "query": {"type": "string", "description": "What information to look for"}
        },
        "required": ["query"]
      }
    }
  }
]
```

## Project Structure

```
ai_browser_agent/
├── main.py                 # CLI entry point
├── cli.py                  # Rich terminal UI
├── agent_core.py           # Main agent loop
├── browser_controller.py   # Playwright wrapper + DOM distillation
├── kimi_client.py          # OpenAI-compatible LLM client
├── context_manager.py      # Token budget management
├── sub_agents.py           # Sub-agent orchestrator
├── security.py             # Security layer
├── error_handler.py         # Error handling & recovery
├── models.py               # Pydantic models
├── utils.py                # Helpers (token counter, etc.)
├── prompts.py              # System prompts and prompt templates
├── requirements.txt
├── .env.example
└── README.md
```

## Key Implementation Details

### Element Resolution Strategy
When the agent describes an element in natural language (e.g., "button with text 'Add to Cart'"), the BrowserController:
1. Uses Playwright's `page.get_by_text()` for text-based matching
2. Falls back to `page.locator()` with aria-label, title, placeholder
3. Uses `page.evaluate()` to run JavaScript that finds elements by visible text
4. Returns the most confident match with a selector

### Context Management Strategy
1. Initial DOM is distilled to ~500-1500 tokens (semantic elements only)
2. After each action, only the changed region + navigation elements are refreshed
3. History is maintained as a rolling window of last 10 steps
4. Older steps are summarized by a sub-agent when the window is full

### Security Flow
1. Agent proposes an action
2. SecurityLayer classifies risk level
3. If HIGH/CRITICAL: pause execution, ask user for confirmation
4. If MEDIUM: log warning but proceed (can be configured)
5. If LOW: proceed silently

## Testing Approach
- Unit tests for each module
- Integration test: navigate to example.com, find and click a link
- Demo test: search for "Python" on Wikipedia and extract first paragraph
- Automated command: `python -m unittest discover -s ai_browser_agent/tests -p "test_*.py"`

## Example Usage
```bash
# Install dependencies
pip install -r requirements.txt
playwright install chromium

# Run with a task
python main.py --task "Go to wikipedia.org and search for 'Artificial Intelligence'. Tell me the first paragraph."

# Run with persistent profile
python main.py --profile-dir ./browser-profile --task "Check my email"
```
