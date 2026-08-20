import '@testing-library/jest-dom/vitest';

/**
 * jsdom has no ResizeObserver, and Recharts' ResponsiveContainer constructs
 * one on mount — so any screen with a chart throws during render rather than
 * failing an assertion, which makes the real failure hard to see.
 */
class ResizeObserverStub {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
}

globalThis.ResizeObserver ??= ResizeObserverStub as unknown as typeof ResizeObserver;
