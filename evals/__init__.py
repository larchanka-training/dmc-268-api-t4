"""LLM evaluation harness: loads the gold dataset, runs it, scores the findings.

Method and metric definitions: docs/TEST_PLAN.md § LLM quality method. Only `runner`
touches the network; everything else is pure and tested without a model.
"""
