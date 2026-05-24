# Agent Rollout Memory Project

## Problem

Coding agents such as Codex and Claude generate rich rollout traces while they work: user intent, files inspected, commands run, mistakes made, corrections applied, tests chosen, review comments, and final outcomes. Today most of that experience is discarded at the end of a session. The next session often repeats the same avoidable mistakes, misses the same user preferences, or relearns project-specific working style from scratch.

This is unlike a human coworking relationship. A human collaborator gradually learns how a person thinks, what they care about, which mistakes to avoid, and which workflows are trusted. Current coding agents have strong within-session context but weak durable learning across sessions.

## Purpose

Build a system that turns agent rollout data into durable, inspectable, reusable memory for future coding sessions. The goal is not just to store transcripts, but to extract useful patterns: recurring failure modes, user preferences, project conventions, successful workflows, tool-use habits, and "next time, do this differently" lessons.

## Core Idea

Treat each coding-agent session as behavioral data. After or during a rollout, extract structured learning artifacts such as:

- user working-style preferences,
- repo-specific conventions and hidden constraints,
- repeated agent mistakes and their fixes,
- successful plans, command sequences, and debugging strategies,
- situations where the user corrected the agent,
- rules that should become future pre-flight checks.

These artifacts should be scored, versioned, and made available to Codex, Claude, and other agents at the start of future sessions.

## Research Questions

- Which parts of a rollout are useful enough to preserve across sessions?
- How can we separate durable lessons from one-off context?
- How should memories be retrieved: by repo, task type, user preference, failure mode, or agent state?
- How do we prevent stale or wrong memories from making future agents worse?
- Can rollout-derived memory measurably reduce repeated mistakes and improve user trust?

## First Success Criteria

A minimal version should:

1. Ingest a coding-agent transcript or trace.
2. Extract a small set of structured lessons.
3. Store those lessons with source links and confidence.
4. Retrieve relevant lessons for a new coding task.
5. Show measurable reduction in repeated mistakes on a replayed or held-out task set.
