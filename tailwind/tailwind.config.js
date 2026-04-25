/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    "../app/templates/**/*.html",
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
