"""Patch Status: every open change of a branch, sorted by what it still
needs before it can land.

A run (job.run) reads Gerrit and Jira without logging in, so the page can
only show what is public already; it classifies the changes (classify),
says per change what is missing (comment) and renders the page body
(render). The web page (blueprints/patch_status) shows the last body and
starts a run when someone presses Update; ``portal-patch-status`` runs one
from the command line.
"""
