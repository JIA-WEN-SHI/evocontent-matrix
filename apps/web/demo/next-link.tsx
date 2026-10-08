import type { AnchorHTMLAttributes } from 'react';
import { useRouter } from './next-navigation';
export default function Link({ href, children, ...props }: AnchorHTMLAttributes<HTMLAnchorElement> & { href: string }) {
  const router = useRouter();
  const target = href.startsWith('/chat') ? '/?view=overview&project=demo-knowledge' : href;
  return <a {...props} href={target.startsWith('/') ? window.location.pathname + (target.includes('?') ? target.slice(target.indexOf('?')) : '') : target} onClick={event => { if (target.startsWith('/')) { event.preventDefault(); router.push(target); } }}>{children}</a>;
}
