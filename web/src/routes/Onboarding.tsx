/**
 * Onboarding wizard — UC-02, UC-04, UC-05.
 *
 * P1's acceptance criterion is that a new user gets from signup to working
 * integration instructions *without leaving the wizard*, so all three steps and
 * the result live in one component with local state. No navigation, nothing to
 * lose the proxy key behind.
 */
import { useState, type FormEvent } from 'react';

import { IntegrationSnippet } from '../components/IntegrationSnippet';
import { Button, Card, ErrorNotice, Field } from '../components/ui';
import { addProviderKey, createProject, createProxyKey, type Provider } from '../lib/api';
import { describeError, useAuth } from '../lib/authContext';

type Step = 'provider-key' | 'project' | 'proxy-key' | 'done';

const STEP_ORDER: Step[] = ['provider-key', 'project', 'proxy-key', 'done'];

const STEP_LABELS: Record<Step, string> = {
  'provider-key': 'Connect a provider',
  project: 'Create a project',
  'proxy-key': 'Issue a proxy key',
  done: 'Start sending traffic',
};

function Steps({ current }: { current: Step }) {
  const currentIndex = STEP_ORDER.indexOf(current);
  return (
    <ol className="mb-6 flex flex-wrap gap-2 text-xs">
      {STEP_ORDER.map((step, index) => {
        const state =
          index < currentIndex ? 'done' : index === currentIndex ? 'current' : 'upcoming';
        const styles = {
          done: 'bg-slate-900 text-white',
          current: 'bg-slate-200 text-slate-900 font-medium',
          upcoming: 'bg-slate-100 text-slate-400',
        }[state];
        return (
          <li key={step} className={`rounded-full px-3 py-1 ${styles}`}>
            {index + 1}. {STEP_LABELS[step]}
          </li>
        );
      })}
    </ol>
  );
}

export function Onboarding() {
  const { accessToken } = useAuth();

  const [step, setStep] = useState<Step>('provider-key');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const [provider, setProvider] = useState<Provider>('openai');
  const [apiKey, setApiKey] = useState('');
  const [projectName, setProjectName] = useState('production');
  const [projectId, setProjectId] = useState<string | null>(null);
  const [proxyKey, setProxyKey] = useState<string | null>(null);

  if (!accessToken) return null;

  const run = async (action: () => Promise<void>) => {
    setError(null);
    setBusy(true);
    try {
      await action();
    } catch (caught) {
      setError(describeError(caught));
    } finally {
      setBusy(false);
    }
  };

  const submitProviderKey = (event: FormEvent) => {
    event.preventDefault();
    void run(async () => {
      await addProviderKey(accessToken, provider, apiKey);
      setApiKey(''); // do not keep the plaintext in component state
      setStep('project');
    });
  };

  const submitProject = (event: FormEvent) => {
    event.preventDefault();
    void run(async () => {
      const project = await createProject(accessToken, projectName);
      setProjectId(project.id);
      setStep('proxy-key');
    });
  };

  const submitProxyKey = (event: FormEvent) => {
    event.preventDefault();
    void run(async () => {
      if (!projectId) throw new Error('No project selected');
      const created = await createProxyKey(accessToken, projectId, 'default');
      setProxyKey(created.key);
      setStep('done');
    });
  };

  return (
    <Card>
      <h2 className="mb-1 text-lg font-semibold">Get set up</h2>
      <p className="mb-6 text-sm text-slate-600">
        Four steps. At the end you will have a base URL and a key to paste into your app.
      </p>

      <Steps current={step} />
      <ErrorNotice message={error} />

      {step === 'provider-key' && (
        <form onSubmit={submitProviderKey} className="mt-4 space-y-4">
          <label className="block">
            <span className="mb-1 block text-sm font-medium text-slate-700">Provider</span>
            <select
              value={provider}
              onChange={(event) => setProvider(event.target.value as Provider)}
              className="w-full rounded-md border border-slate-300 px-3 py-2 text-sm"
            >
              <option value="openai">OpenAI</option>
              <option value="anthropic">Anthropic</option>
              <option value="gemini">Gemini</option>
            </select>
          </label>

          <Field
            label="API key"
            type="password"
            required
            autoComplete="off"
            value={apiKey}
            onChange={(event) => setApiKey(event.target.value)}
            hint="Encrypted before this request returns. You will never see it again — only the last 4 characters."
          />

          <Button type="submit" disabled={busy || apiKey.length < 8}>
            {busy ? 'Encrypting…' : 'Add key'}
          </Button>
        </form>
      )}

      {step === 'project' && (
        <form onSubmit={submitProject} className="mt-4 space-y-4">
          <Field
            label="Project name"
            required
            value={projectName}
            onChange={(event) => setProjectName(event.target.value)}
            hint="Projects keep usage, budgets, and rules separate — e.g. production vs staging."
          />
          <Button type="submit" disabled={busy || projectName.trim().length === 0}>
            {busy ? 'Creating…' : 'Create project'}
          </Button>
        </form>
      )}

      {step === 'proxy-key' && (
        <form onSubmit={submitProxyKey} className="mt-4 space-y-4">
          <p className="text-sm text-slate-600">
            This is the key your application sends to us, in place of your provider key. You can
            revoke it at any time without touching your provider account.
          </p>
          <Button type="submit" disabled={busy}>
            {busy ? 'Issuing…' : 'Issue proxy key'}
          </Button>
        </form>
      )}

      {step === 'done' && proxyKey && (
        <div className="mt-4">
          <IntegrationSnippet proxyKey={proxyKey} />
        </div>
      )}
    </Card>
  );
}
