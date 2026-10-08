import { useSyncExternalStore } from 'react';
const subscribe = (callback: () => void) => { window.addEventListener('popstate', callback); return () => window.removeEventListener('popstate', callback); };
const change = (href: string, replace = false) => { const query = href.includes('?') ? href.slice(href.indexOf('?')) : ''; history[replace ? 'replaceState' : 'pushState']({}, '', window.location.pathname + query); window.dispatchEvent(new PopStateEvent('popstate')); };
const router = { push: (href: string) => change(href), replace: (href: string) => change(href, true), back: () => history.back(), refresh: () => location.reload() };
export function useRouter() { return router; }
export function useSearchParams() { return new URLSearchParams(useSyncExternalStore(subscribe, () => window.location.search)); }
export function usePathname() { return '/'; }
