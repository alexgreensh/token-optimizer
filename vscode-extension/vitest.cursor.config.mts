// Durable Cursor-mode regression config. The repo's .gitignore keeps
// src/__tests__/ and vitest.config.ts local-only; this config and the
// tests/ directory live outside those ignored paths on purpose so Cursor
// coverage ships with the feature.
import { defineConfig } from 'vitest/config';
export default defineConfig({
  test: {
    include: ['tests/**/*.test.ts'],
    environment: 'node',
  },
});
