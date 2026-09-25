"""Instruments for reviewing vBot Tools the way an Agent meets them.

``python -m scripts.tool_lab definitions`` shows what a fresh Agent receives,
``probe`` dispatches calls through the production Tool path and shows the
Model-visible result and the resulting files, and ``sessions`` measures how
Agents actually called the Tools in a copy of a sessions database. The review
procedure that uses them is ``.vorch/workflows/tool-review-workflow.md``.
"""
