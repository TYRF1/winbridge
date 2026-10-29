# Plan

This file used to hold the initial wishlist. It now points at the documents that
own the decisions.

- **Design (source of truth):** [`docs/design.md`](design.md)
- **Implementation plan (build order):** [`docs/plan-v0.1.md`](plan-v0.1.md)

## v0.1 cut line

Host-native Wine backend; Hyprland + X11 adapters; `setup`, `verify`, `update`,
`expose`/`unexpose`, `unbridge`, `run`, `doctor`, runner store, zero-touch
profile, omarchy-shell pin. Login bridge is experimental. Bottles backend,
GNOME/KDE adapters, Proton runners, fonts, and GPU policy are v0.2+.

## Definition of done

A tester with a fresh Omarchy machine goes from an installer file to testing the
app — launched from the bar, files visible, login working — with three commands
and zero GUI detours.
