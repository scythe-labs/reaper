// SPDX-License-Identifier: AGPL-3.0-or-later
// Registers the jest-dom matchers (toBeDisabled, toBeInTheDocument, ...) with vitest.
import "@testing-library/jest-dom/vitest";

// jsdom has no layout, so window.scrollTo is unimplemented and logs a noisy "Not
// implemented" on every call. ModalShell restores the scroll offset with it when a modal
// closes, so make it a quiet no-op here -- nothing in the tests reads a real scroll.
window.scrollTo = () => {};
