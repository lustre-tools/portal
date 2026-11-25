# TLC design system (vendored)

The stylesheets, the display-settings script and the fonts (SIL OFL, see
`fonts/`) of the TLC design system (`tlc-design-system`, commit 334b1ac),
as gerrit-dashboard vendors them (`vendor/llm_tools/gerrit_dashboard/
gerrit_dashboard/static/tlc`). The portal's TLC pages use the same files,
so they look like the dashboard and share its display settings. Update by
copying the same files again; do not edit them here. The portal's own
styles for these pages are in `portal/static/tlc-page.css` and use only
the `--tlc-*` tokens.
