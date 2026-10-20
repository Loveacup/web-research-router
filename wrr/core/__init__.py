"""WRR 7 core: inventory → plan → execute → fuse → judge → select → evidence.

Design: docs/superpowers/specs/2026-10-19-wrr-core-design.md. The core owns
routing, judgment and evidence contracts; hosts inject capabilities (search,
judge) and never carry routing rules of their own.
"""
