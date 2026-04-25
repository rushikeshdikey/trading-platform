/** @type {import('tailwindcss').Config} */
module.exports = {
  // Tailwind resolves these relative to the cwd when invoked via the CLI,
  // so paths are repo-root-relative. tailwind/build.sh `cd`s to repo root
  // before invoking the CLI; running ad-hoc requires the same.
  content: [
    "./app/templates/**/*.html",
  ],
  // Templates use arbitrary classes/values via Alpine bindings (e.g. computed
  // class strings) and via class-pill literals composed at render-time. The
  // safelist captures patterns the JIT can't see in the templates' static text.
  safelist: [
    { pattern: /^(bg|text|border)-(emerald|rose|amber|sky|zinc|red|green|blue)-(50|100|200|300|400|500|600|700|800|900)$/ },
  ],
  theme: {
    extend: {},
  },
  plugins: [],
}
