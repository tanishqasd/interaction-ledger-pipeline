# AI Usage Disclosure

## Tools used

ChatGPT and OpenAI Codex.

## What I used them for

I used ChatGPT separately to understand the assignment, compare A and B, choose Assignment A, and draft milestone instructions and review checklists. I used it again to review Codex's milestone summaries and test results. Codex implemented the CLI, ingestion, SQLite storage, PII protection and reporting; it also debugged the code, wrote and ran the automated tests, and drafted the submission documentation.

## What I wrote or decided myself

I selected Assignment A and directed the work in stages. I specified what needed to be checked before moving on, reviewed the proposed plan and milestone evidence, and approved each next step. I did not make separate code corrections outside this recorded workflow.

## Where the AI was wrong or unhelpful

Codex's first generated-data check took its expected call count from the same call-derivation code used by reporting. That could verify consistency but could miss a shared counting bug. The final audit caught that gap and Codex replaced that check with a separate calculation from the raw inputs, comparing all five files at both drop boundaries. I did not personally edit the code for that change.

## How I verified AI-generated code

I asked Codex to run normalization and edge-case tests, the unchanged official generator on multiple seeds, clean-clone setup, and checks for replay, encryption and exact report values. Codex executed these checks and recorded the results; I reviewed the milestone summaries and test results with ChatGPT before approving the next steps.
