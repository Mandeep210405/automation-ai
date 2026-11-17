"""
config.py — every setting you are likely to change lives here.

MODEL CHOICE (all free, all run locally through Ollama)
--------------------------------------------------------
Default: qwen2.5:7b for every role.
  * Very good at following instructions and returning strict JSON (most important
    thing for this project), 128k context support, Apache-2.0 licence, ~4.7 GB.
  * Using ONE model for all roles means Ollama keeps it loaded. Mixing models
    forces Ollama to swap them in/out of memory on every call, which is slow.

Pick by your hardware:
  * 8 GB RAM, no GPU ............ qwen2.5:3b      (fast, weaker planning)
  * 16 GB RAM or 6-8 GB GPU ..... qwen2.5:7b      (default, recommended)
  * 12-16 GB+ GPU ............... qwen2.5:14b     (better plans and repairs)
  * Big GPU (24 GB+) and want max quality: planner/repair = qwen2.5:14b,
    step = qwen2.5:7b (both stay loaded).
  * Alternative family: qwen3:8b — set OLLAMA_THINK = False so it does not
    spend tokens on long hidden "thinking".

Roles:
  planner : reads the user prompt + first filtered page, writes the test plan
            ("ANALYSE -> DEFINE FLOW OF STEPS" in the diagram). Also re-plans.
  step    : turns ONE plan step + current filtered DOM into ONE concrete action
            ("AI -> PATH OF TEST STEP"). Called most often.
  repair  : after a failure, gets error + previous attempt + NEW filtered DOM
            and proposes a corrected action ("AI ANALYSE" in the diagram).
"""

# ---------------------------------------------------------------- Ollama / models
# OLLAMA_HOST = "http://localhost:11434"
# MODELS = {
#     "planner": "qwen2.5:7b",
#     "step": "qwen2.5:7b",
#     "repair": "qwen2.5:7b",
# }
# OLLAMA_NUM_CTX = 8192        # context window sent to Ollama. Default in Ollama is small
#                              # (2k-4k) and silently cuts long prompts, so we set it.
# OLLAMA_TEMPERATURE = 0.6     # low = consistent, repeatable output
# OLLAMA_TIMEOUT_S = 300       # first call can be slow while the model loads
# OLLAMA_KEEP_ALIVE = "30m"    # keep model in memory between calls
# OLLAMA_THINK = None          # None = don't send; False = disable thinking (qwen3, deepseek-r1)
import os

OLLAMA_HOST = "https://ollama.com"
OLLAMA_API_KEY = "73bbb1ebef3a49d8b0059e14140804aa.loRT-4_Bum4LAt4AO5u2qmiR"   # set it in your terminal, never hardcode it

MODELS = {
    "planner": "gpt-oss:120b",
    "step":    "gpt-oss:120b",
    "repair":  "gpt-oss:120b",
}
OLLAMA_NUM_CTX = 8192
OLLAMA_TEMPERATURE = 0.1
OLLAMA_TIMEOUT_S = 300
OLLAMA_KEEP_ALIVE = "30m"   # ignored by cloud, harmless
OLLAMA_THINK = "low"        # gpt-oss wants "low"/"medium"/"high"; False doesn't work for it


# ---------------------------------------------------------------- Browser
HEADLESS = False             # keep False: you need to see it (and log in)
VIEWPORT = {"width": 1366, "height": 768}
SLOW_MO_MS = 0               # e.g. 300 to watch actions slowly
NAVIGATION_TIMEOUT_MS = 60_000
ACTION_TIMEOUT_MS = 10_000
ASSERT_TIMEOUT_MS = 7_000
SETTLE_NETWORKIDLE_MS = 4_000  # max wait for network to calm down after an action
SETTLE_EXTRA_MS = 400          # small pause for React re-renders / animations

# ---------------------------------------------------------------- Filtration / context budget
MAX_ELEMENTS = 4000          # hard cap of interactive candidates collected per frame
MAX_TEXT_BLOCKS = 400
INCLUDE_HIDDEN = False       # True = also keep invisible (zero-size / visibility:hidden) controls
PLANNER_CONTEXT_TOKENS = 3500  # how much filtered page the planner sees
STEP_CONTEXT_TOKENS = 3000     # how much filtered page each step/repair call sees
SAVE_RAW_DOM = False           # True = also save page.content() for each capture (big files)

# ---------------------------------------------------------------- Agent loop
RETRY_LIMIT = 3              # failed attempts per step before asking a human
MAX_PLAN_STEPS = 25
MAX_ACTIONS_PER_STEP = 4     # one plan step may need several actions (e.g. custom dropdown)
MAX_TOTAL_ACTIONS = 80       # safety stop for the whole run
REPLAN_ON_URL_CHANGE = True  # diagram: "UPDATED FLOW OF STEPS + FDOM -> AI"

# ---------------------------------------------------------------- Files
SESSIONS_DIR = "sessions"    # saved login sessions (cookies + localStorage), one per site
RUNS_DIR = "runs"            # one folder per run: Excel, script, screenshots, logs
TEST_DATA_FILE = "test_data.json"

MASTER_TEMPLATE = "templates/Automation_Master_Controller.xlsx"  # put your master xlsx here (copied, never modified)
MASTER_DELAY_AFTER_CLICK_S = 1
MASTER_DELAY_AFTER_NAVIGATE_S = 2
MASTER_KEYWORDS = {}   # override keyword names, e.g. {"select_option": "yourDropdownKeyword"}