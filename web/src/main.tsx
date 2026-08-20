import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { createBrowserRouter, RouterProvider } from 'react-router-dom';

import { AuthProvider } from './lib/auth';
import { ToastProvider, WorkspaceProvider } from './lib/UiProviders';
import { Advisor } from './routes/Advisor';
import { Alerts } from './routes/Alerts';
import { Billing } from './routes/Billing';
import { Budgets } from './routes/Budgets';
import { Cache } from './routes/Cache';
import { Onboarding } from './routes/Onboarding';
import { Overview } from './routes/Overview';
import { Root } from './routes/Root';
import { Routing } from './routes/Routing';
import { Settings } from './routes/Settings';
import './index.css';

const queryClient = new QueryClient({
  defaultOptions: { queries: { staleTime: 30_000, refetchOnWindowFocus: false, retry: false } },
});

// The router owns navigation; TanStack Query owns data. Deliberately no
// loaders — splitting fetching across two systems is how caches diverge.
// See docs/adr/0005-react-router.md.
const router = createBrowserRouter([
  {
    path: '/',
    element: <Root />,
    children: [
      { index: true, element: <Overview /> },
      { path: 'cache', element: <Cache /> },
      { path: 'routing', element: <Routing /> },
      { path: 'budgets', element: <Budgets /> },
      { path: 'alerts', element: <Alerts /> },
      { path: 'advisor', element: <Advisor /> },
      { path: 'settings', element: <Settings /> },
      { path: 'billing', element: <Billing /> },
      { path: 'setup', element: <Onboarding /> },
    ],
  },
]);

const container = document.getElementById('root');
if (!container) {
  throw new Error('#root not found');
}

createRoot(container).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <WorkspaceProvider>
          <ToastProvider>
            <RouterProvider router={router} />
          </ToastProvider>
        </WorkspaceProvider>
      </AuthProvider>
    </QueryClientProvider>
  </StrictMode>,
);
