# Accessibility Implementation Plan (WCAG 2.1 AA)

This plan outlines the steps to address accessibility oversights (by the guy with one eye) identified (by a machine) in the "Who Owns Atlanta?" web interface.

## Status Summary
- **Phase 1: Semantic Structure & Landmarks**: [x] Complete
- **Phase 2: Visual & Focus Improvements (CSS)**: [x] Complete
- **Phase 3: Interactive ARIA & Keyboard Support (Refactor)**: [x] Complete

---

## Phase 1: Semantic Structure & Landmarks
Goal: Improve screen reader navigation and document structure.

- [x] **Global Skip Link**: Add `<a href="#main-content" class="skip-link">Skip to main content</a>` to all top-level HTML files.
- [x] **Landmark Labeling**: 
    - Add `id="main-content"` to `<main>` tags.
    - Add `aria-label="Primary"` to header `<nav>`.
    - Add `aria-label="Footer"` to footer `<nav>`.
- [x] **Map Context**: Add `role="region" aria-label="Interactive property ownership map"` to `#map`.
- [x] **Decorative Icons**: Add `aria-hidden="true"` to all decorative SVGs.
- [x] **Status Regions**: Add `role="status" aria-live="polite"` to `#cluster-loading`.

## Phase 2: Visual & Focus Improvements (CSS)
Goal: Ensure sufficient color contrast and focus visibility.

- [x] **Color Contrast (Text)**:
    - Darken `--pico-muted-color` to `#526071`.
    - Darken `.badge-institutional` background to `#b45309`.
- [x] **Focus Visibility**:
    - Remove `outline: none` from custom controls.
    - Standardize focus rings for interactive elements.
- [x] **Skip Link Styles**: Implement "pop-down on focus" CSS.

## Phase 3: Interactive ARIA & Keyboard Support (Refactor)
Goal: Use native browser primitives to solve accessibility with minimal custom JS.

- [ ] **Filter Panel Refactor**: 
    - Convert `#filter-toggle` and `#filter-panel` to `<details>` and `<summary>`.
    - Benefit: Native `Enter/Space` support and `aria-expanded` state.
- [ ] **Detail Panel Refactor**:
    - Convert `#detail-panel` to a `<dialog>` element (styled as a sidebar).
    - Benefit: Native `Esc` key handling and focus trapping.
- [ ] **Interactive Lists (Condos/Search)**:
    - Wrap clickable rows in `<button>` elements.
    - Benefit: Native `Tab` and `Enter` support.
- [ ] **Combobox Pattern**:
    - Implement proper `role="combobox"` and `aria-activedescendant` for search results in `app.js`.
    - Benefit: Screen readers announce selection state accurately.
