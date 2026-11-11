// Bundles the harness for the browser: React ships CJS, and a bare specifier cannot be imported from
// a page served over http. Run from the repo root: npx rollup -c scripts/gui-audit/rollup.config.mjs
import resolve from '@rollup/plugin-node-resolve'
import commonjs from '@rollup/plugin-commonjs'

export default {
  input: 'page/harness.mjs',
  output: { file: 'page/bundle.js', format: 'esm' },
  plugins: [
    commonjs({ include: /node_modules/ }),
    resolve({ browser: true, preferBuiltins: false, extensions: ['.mjs', '.js', '.json'] })
  ]
}
