import { createRoot } from 'react-dom/client';
import Page from '../app/page';
import '../app/globals.css';
// Explicit offline demo: even an accidental API call must not reach an external service.
window.fetch = async () => { throw new Error('公开演示不连接后台；请使用页面中的示例流程'); };
createRoot(document.getElementById('root')!).render(<Page/>);
