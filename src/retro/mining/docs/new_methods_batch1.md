# Agentic Memory Extraction: State-of-the-Art Methods (2025-2026)

This document outlines the five frontier architectures for extracting procedural memory from Large Language Model (LLM) agent trajectories without updating the model's underlying weights.

---

## 1. Trajectory-Informed Memory Generation (TIMG)
**Source:** [Trajectory-Informed Memory Generation for Self-Improving Agent Systems (arXiv:2603.10600)](https://arxiv.org/abs/2603.10600)

**How it works:**
TIMG is a highly deterministic, forensic pipeline that moves beyond simple LLM summarization. It treats a completed rollout as an event log and processes it in three strict phases:
1. **Trajectory Intelligence Extractor:** Semantically parses the raw execution log to categorize the agent's actions into analytical, planning, validation, and reflection types.
2. **Decision Attribution Analyzer:** Performs a causal trace to map the final task outcome back to specific intermediate decisions (e.g., identifying the exact edit that fixed a recurring error).
3. **Contextual Learning Generator:** Generates structured, actionable JSON cards. Instead of general notes, it outputs three specific schemas:
   - *Strategy Tips:* Extracted from clean, successful execution paths.
   - *Recovery Tips:* Extracted from failure-to-success pivots.
   - *Optimization Tips:* Extracted from successful but inefficient runs (e.g., searching 10 files to find 1 variable).

---

## 2. The Evolutionary Abstraction Pipeline (S-R-E)
**Source:** [From Storage to Experience: A Survey on the Evolution of LLM Agent Memory Mechanisms (arXiv:2605.06716)](https://arxiv.org/abs/2605.06716)

**How it works:**
The S-R-E framework treats memory not as a single extraction event, but as an evolutionary process across multiple sessions to prevent the agent from "overfitting" to a single strange rollout.
1. **Storage (Trajectory Preservation):** The raw observation-action pairs are saved immutably.
2. **Reflection (Trajectory Refinement):** Environment feedback (like an exit code of `1`) is used to highlight the pivotal moments in the trace, stripping away the irrelevant "happy path" noise.
3. **Experience (Cross-Trajectory Abstraction):** The core extraction mechanism. The system waits until multiple reflected trajectories share a similar semantic trigger. It then abstracts a generalized, universal "Skill" or "Rule" that is completely detached from the specific file names or tasks of the original sessions.

---

## 3. RL-Optimized Extraction Lifecycle (Memory-R2)
**Source:** [Fair Credit Assignment for Long-Horizon Memory-Augmented LLM Agents (arXiv:2605.21768)](https://arxiv.org/abs/2605.21768)

**How it works:**
Instead of hardcoding the rules for what constitutes a "good" memory, Memory-R2 uses reinforcement learning (specifically RL techniques applied to prompting) to train the extraction mechanism itself.
* **Co-Learning Architecture:** It instantiates two distinct roles from the same LLM backbone: a **Fact Extractor** and a **Memory Manager**.
* **The Loop:** When a rollout finishes, the Fact Extractor proposes new signals. The Memory Manager evaluates the existing vector database and chooses one of three actions: *Insert* (new rule), *Update* (modify existing rule), or *Delete* (remove a stale rule).
* **The Optimization:** The system is optimized based on long-term task success. It solves the "non-stationarity" problem (where updating a memory changes the environment for the next session) by tying the extractor's reward directly to how useful the memory proved to be in subsequent tasks.

---

## 4. Context Repositories (Letta / Zettelkasten-Style Memory)
**Source:** [Introducing Context Repositories: Git-based Memory for Coding Agents](https://www.letta.com/blog/context-repositories) | **Repo:** [Letta Code (`@letta-ai/letta-code`)](https://github.com/letta-ai/letta)

**How it works:**
Pioneered by the UC Berkeley team behind MemGPT, Letta treats memory extraction as a local filesystem and Git repository operation, effectively allowing the agent to act as its own Zettelkasten librarian.
* **Sleep-Time Compute:** Extraction happens via a background "sleep-time" process. While the agent is idle, it periodically reviews recent conversation history and execution traces.
* **Git-Backed Reflection:** Instead of saving to a vector database, the agent persists important information into a `memory/` directory as local Markdown files (e.g., `skills.md` or `api_patterns.md`). It commits these changes with informative Git commit messages.
* **Dynamic Restructuring:** Adding a new memory allows the agent to use its own code-execution capabilities to reorganize its file hierarchy, merge contradictory rules, and update frontmatter to control what is pinned to its future system prompts.

---

## 5. Code-as-Action Programmable Scratchpads (Execution Memory)
**Source:** Represented in modern stateful execution environments (e.g., Databricks agent architectures, Jupyter-backed agents). 

**How it works:**
This method flips traditional extraction on its head. Instead of parsing a chat log to extract JSON memories after the fact, the agent's interaction medium *is* the persistent memory.
* **The Mechanism:** The agent operates inside a persistent Python REPL or Stateful Sandbox. 
* **Extraction by Execution:** As the agent works through a trajectory, it explicitly writes variables, dataframes, and helper functions into this persistent scope. 
* **The Result:** The procedural memory isn't "extracted" via a secondary LLM judge. The memory is the executable workspace state itself, which future sessions simply load and invoke, ensuring zero loss of fidelity between what the agent learned and what it can execute.