"""Instruments for reviewing vBot Tools the way an Agent meets them.

``python -m scripts.tool_lab definitions`` shows what a fresh Agent receives,
``probe`` dispatches calls through the production Tool path and shows the
Model-visible result and the resulting files, and ``sessions`` measures how
Agents actually called the Tools in a copy of a sessions database, and
``replay`` replays real file-edit calls through the current Tools. The Skill
``vbot-agent-tools`` describes how a Tool review uses them.
"""
