import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import tailwindcss from 'tailwindcss';
import autoprefixer from 'autoprefixer';
const here = dirname(fileURLToPath(import.meta.url));
export default {
  base: './',
  esbuild: { jsx: 'automatic' },
  resolve: { alias: [
    { find: '@', replacement: resolve(here, '..') },
    { find: 'next/link', replacement: resolve(here, 'next-link.tsx') },
    { find: 'next/navigation', replacement: resolve(here, 'next-navigation.tsx') },
    { find: /^react($|\/)/, replacement: resolve(here, 'node_modules/react') + '$1' },
    { find: /^react-dom($|\/)/, replacement: resolve(here, 'node_modules/react-dom') + '$1' },
    { find: /^lucide-react($|\/)/, replacement: resolve(here, 'node_modules/lucide-react') + '$1' },
    { find: /^@radix-ui\/react-dialog($|\/)/, replacement: resolve(here, 'node_modules/@radix-ui/react-dialog') + '$1' },
    { find: /^@radix-ui\/react-slot($|\/)/, replacement: resolve(here, 'node_modules/@radix-ui/react-slot') + '$1' },
    { find: /^class-variance-authority($|\/)/, replacement: resolve(here, 'node_modules/class-variance-authority') + '$1' },
    { find: /^clsx($|\/)/, replacement: resolve(here, 'node_modules/clsx') + '$1' },
    { find: /^tailwind-merge($|\/)/, replacement: resolve(here, 'node_modules/tailwind-merge') + '$1' },
    { find: /^recharts($|\/)/, replacement: resolve(here, 'node_modules/recharts') + '$1' },
    { find: /^tailwindcss($|\/)/, replacement: resolve(here, 'node_modules/tailwindcss') + '$1' },
    { find: /^autoprefixer($|\/)/, replacement: resolve(here, 'node_modules/autoprefixer') + '$1' }
  ] },
  define: { 'process.env.NEXT_PUBLIC_PORTFOLIO_DEMO': '"true"', 'process.env.NEXT_PUBLIC_LANDING_DEMO_FALLBACK': '"true"', 'process.env.NEXT_PUBLIC_DEFAULT_DOMAIN_SLUG': '"japan_immigration"', 'process.env.NEXT_PUBLIC_API_BASE_URL': '""' },
  css: { postcss: { plugins: [tailwindcss({ config: resolve(here, '../tailwind.config.ts') }), autoprefixer()] } },
  build: { outDir: 'dist', emptyOutDir: true }
};
