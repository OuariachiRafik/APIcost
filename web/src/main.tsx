import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { createBrowserRouter, RouterProvider } from 'react-router-dom';

import { AuthProvider } from './lib/auth';
import { Dashboard } from './routes/Dashboard';
import { Onboarding } from './routes/Onboarding';
import { Requests } from './routes/Requests';
import { Root } from './routes/Root';
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
      { index: true, element: <Dashboard /> },
      { path: 'requests', element: <Requests /> },
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
        <RouterProvider router={router} />
      </AuthProvider>
    </QueryClientProvider>
  </StrictMode>,
);
