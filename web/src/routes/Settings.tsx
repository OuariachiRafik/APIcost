/**
 * Settings — APICOST_FRONTEND_SPEC §5.1.
 *
 * Built first because provider keys, projects and proxy keys are prerequisites
 * for every other screen having any data at all (§3.3).
 *
 * Terminology is load-bearing here (§6): the key *we* issue is a **proxy key**
 * and the key the user brings from OpenAI/Anthropic/Gemini is a **provider
 * key**. Calling both "API key" is the single most likely source of support
 * confusion in the product, so the words never blur on this screen.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';

import { RequireProject, ScreenHeader, SectionTitle } from '../components/Screen';
import {
  Badge,
  Button,
  Card,
  CodeBlock,
  EmptyState,
  ErrorBanner,
  Field,
  Modal,
  Row,
  Select,
  Spinner,
  Table,
  Cell,
  Toggle,
} from '../components/ui';
import { addProviderKey, deleteProviderKey, listProviderKeys, createProject } from '../lib/api';
import type { Provider } from '../lib/api';
import { describeError, useAuth } from '../lib/authContext';
import { date, dateTime } from '../lib/format';
import {
  createProxyKey,
  getProject,
  keys,
  listProxyKeys,
  revokeProxyKey,
  testConnection,
  updateSettings,
} from '../lib/queries';
import { useToast, useWorkspace } from '../lib/uiContext';

const PROVIDERS: { value: Provider; label: string }[] = [
  { value: 'openai', label: 'OpenAI' },
  { value: 'anthropic', label: 'Anthropic' },
  { value: 'gemini', label: 'Google Gemini' },
];

export function Settings() {
  return (
    <>
      <ScreenHeader
        title="Settings"
        description="Provider keys, projects, proxy keys, and how much we store."
      />
      <div className="grid gap-4 lg:grid-cols-2">
        <ProviderKeys />
        <Projects />
      </div>
      <div className="mt-4 grid gap-4">
        <RequireProject>{(projectId) => <ProxyKeys projectId={projectId} />}</RequireProject>
        <RequireProject>{(projectId) => <ProjectSettings projectId={projectId} />}</RequireProject>
      </div>
    </>
  );
}

// -- Provider keys ----------------------------------------------------------

function ProviderKeys() {
  const { accessToken } = useAuth();
  const client = useQueryClient();
  const { push } = useToast();

  const [provider, setProvider] = useState<Provider>('openai');
  const [apiKey, setApiKey] = useState('');

  const list = useQuery({
    queryKey: keys.providerKeys(),
    queryFn: () => listProviderKeys(accessToken ?? ''),
    enabled: Boolean(accessToken),
  });

  const add = useMutation({
    mutationFn: () => addProviderKey(accessToken ?? '', provider, apiKey),
    onSuccess: () => {
      setApiKey('');
      push({ tone: 'success', message: 'Provider key added.' });
      void client.invalidateQueries({ queryKey: keys.providerKeys() });
    },
    onError: (error) => push({ tone: 'error', message: describeError(error) }),
  });

  const remove = useMutation({
    mutationFn: (id: string) => deleteProviderKey(accessToken ?? '', id),
    onSuccess: () => {
      push({ tone: 'success', message: 'Provider key removed.' });
      void client.invalidateQueries({ queryKey: keys.providerKeys() });
    },
    onError: (error) => push({ tone: 'error', message: describeError(error) }),
  });

  return (
    <Card title="Provider keys">
      <p className="mb-3 text-xs text-muted">
        Your key from OpenAI, Anthropic or Google. It is encrypted at rest and decrypted only in
        memory at forward time — after you add it, we never show it again.
      </p>

      {list.isLoading ? (
        <Spinner />
      ) : list.data && list.data.length > 0 ? (
        <Table head={['Provider', 'Key', 'Added', 'Last used', '']}>
          {list.data.map((key) => (
            <Row key={key.id}>
              <Cell>{key.provider}</Cell>
              <Cell>
                <span className="font-mono text-xs text-muted">····{key.last4}</span>
              </Cell>
              <Cell>
                <span className="text-xs text-muted">{date(key.added_at)}</span>
              </Cell>
              <Cell>
                <span className="text-xs text-muted">
                  {key.last_used_at ? dateTime(key.last_used_at) : 'never'}
                </span>
              </Cell>
              <Cell className="text-right">
                <Button variant="ghost" onClick={() => remove.mutate(key.id)}>
                  Remove
                </Button>
              </Cell>
            </Row>
          ))}
        </Table>
      ) : (
        <EmptyState title="No provider keys yet">
          Add the key you already use with your model provider. APICost forwards your requests using
          it.
        </EmptyState>
      )}

      <form
        className="mt-4 flex items-end gap-2 border-t border-edge pt-4"
        onSubmit={(event) => {
          event.preventDefault();
          add.mutate();
        }}
      >
        <div className="w-40">
          <Select
            label="Provider"
            value={provider}
            onChange={(value) => setProvider(value as Provider)}
          >
            {PROVIDERS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </Select>
        </div>
        <Field
          label="API key"
          className="flex-1"
          type="password"
          autoComplete="off"
          placeholder="sk-…"
          value={apiKey}
          onChange={(event) => setApiKey(event.target.value)}
        />
        <Button type="submit" disabled={!apiKey || add.isPending}>
          {add.isPending ? 'Adding…' : 'Add key'}
        </Button>
      </form>
    </Card>
  );
}

// -- Projects ---------------------------------------------------------------

function Projects() {
  const { accessToken } = useAuth();
  const { projects, projectId, setProjectId, refetchProjects } = useWorkspace();
  const { push } = useToast();
  const [name, setName] = useState('');

  const create = useMutation({
    mutationFn: () => createProject(accessToken ?? '', name),
    onSuccess: (project) => {
      setName('');
      setProjectId(project.id);
      refetchProjects();
      push({ tone: 'success', message: `Project “${project.name}” created.` });
    },
    onError: (error) => push({ tone: 'error', message: describeError(error) }),
  });

  return (
    <Card title="Projects">
      <p className="mb-3 text-xs text-muted">
        A project scopes everything else — keys, budgets, routing rules, alerts. Most people use one
        per application or environment.
      </p>

      {projects.length > 0 ? (
        <Table head={['Name', 'Created', '']}>
          {projects.map((project) => (
            <Row key={project.id}>
              <Cell>
                <span className="flex items-center gap-2">
                  {project.name}
                  {project.id === projectId && <Badge tone="info">Selected</Badge>}
                </span>
              </Cell>
              <Cell>
                <span className="text-xs text-muted">{date(project.created_at)}</span>
              </Cell>
              <Cell className="text-right">
                {project.id !== projectId && (
                  <Button variant="ghost" onClick={() => setProjectId(project.id)}>
                    Select
                  </Button>
                )}
              </Cell>
            </Row>
          ))}
        </Table>
      ) : (
        <EmptyState title="No projects yet">Create one to get started.</EmptyState>
      )}

      <form
        className="mt-4 flex items-end gap-2 border-t border-edge pt-4"
        onSubmit={(event) => {
          event.preventDefault();
          create.mutate();
        }}
      >
        <Field
          label="New project"
          className="flex-1"
          placeholder="production"
          value={name}
          onChange={(event) => setName(event.target.value)}
        />
        <Button type="submit" disabled={!name || create.isPending}>
          Create
        </Button>
      </form>
    </Card>
  );
}

// -- Proxy keys -------------------------------------------------------------

function ProxyKeys({ projectId }: { projectId: string }) {
  const { accessToken } = useAuth();
  const client = useQueryClient();
  const { push } = useToast();

  const [name, setName] = useState('default');
  const [issued, setIssued] = useState<string | null>(null);
  const [acknowledged, setAcknowledged] = useState(false);

  const list = useQuery({
    queryKey: keys.proxyKeys(projectId),
    queryFn: () => listProxyKeys(accessToken ?? '', projectId),
    enabled: Boolean(accessToken),
  });

  const create = useMutation({
    mutationFn: () => createProxyKey(accessToken ?? '', projectId, name),
    onSuccess: (key) => {
      // Shown exactly once (§5.1) — there is no endpoint that can return it
      // again, so the modal below gates its own dismissal.
      setIssued(key.key);
      setAcknowledged(false);
      void client.invalidateQueries({ queryKey: keys.proxyKeys(projectId) });
    },
    onError: (error) => push({ tone: 'error', message: describeError(error) }),
  });

  const revoke = useMutation({
    mutationFn: (id: string) => revokeProxyKey(accessToken ?? '', id),
    onSuccess: () => {
      push({ tone: 'success', message: 'Proxy key revoked.' });
      void client.invalidateQueries({ queryKey: keys.proxyKeys(projectId) });
    },
    onError: (error) => push({ tone: 'error', message: describeError(error) }),
  });

  const test = useMutation({
    mutationFn: () => testConnection(accessToken ?? '', projectId),
    onSuccess: (result) =>
      push({
        tone: result.ok ? 'success' : 'error',
        message: result.ok
          ? `Connection works — ${result.model} responded in ${Math.round(result.latency_ms ?? 0)} ms.`
          : result.message,
      }),
    onError: (error) => push({ tone: 'error', message: describeError(error) }),
  });

  return (
    <Card
      title="Proxy keys"
      action={
        <Button variant="secondary" onClick={() => test.mutate()} disabled={test.isPending}>
          {test.isPending ? 'Testing…' : 'Send test request'}
        </Button>
      }
    >
      <p className="mb-3 text-xs text-muted">
        This is the key your application sends to APICost, in place of your provider key. Swap your
        base URL and this key, and nothing else in your code changes.
      </p>

      {list.isLoading ? (
        <Spinner />
      ) : list.data && list.data.length > 0 ? (
        <Table head={['Name', 'Key', 'Created', 'Last used', '']}>
          {list.data.map((key) => (
            <Row key={key.id}>
              <Cell>{key.name ?? 'unnamed'}</Cell>
              <Cell>
                <span className="font-mono text-xs text-muted">····{key.last4}</span>
              </Cell>
              <Cell>
                <span className="text-xs text-muted">{date(key.created_at)}</span>
              </Cell>
              <Cell>
                <span className="text-xs text-muted">
                  {key.last_used_at ? dateTime(key.last_used_at) : 'never'}
                </span>
              </Cell>
              <Cell className="text-right">
                {key.revoked_at ? (
                  <Badge tone="critical">Revoked</Badge>
                ) : (
                  <Button variant="ghost" onClick={() => revoke.mutate(key.id)}>
                    Revoke
                  </Button>
                )}
              </Cell>
            </Row>
          ))}
        </Table>
      ) : (
        <EmptyState title="No proxy keys yet">
          Issue one to point your application at APICost.
        </EmptyState>
      )}

      <form
        className="mt-4 flex items-end gap-2 border-t border-edge pt-4"
        onSubmit={(event) => {
          event.preventDefault();
          create.mutate();
        }}
      >
        <Field
          label="New proxy key"
          className="flex-1"
          placeholder="production"
          value={name}
          onChange={(event) => setName(event.target.value)}
        />
        <Button type="submit" disabled={!name || create.isPending}>
          Issue key
        </Button>
      </form>

      {issued && (
        <Modal
          title="Save your proxy key"
          onClose={() => {
            if (acknowledged) setIssued(null);
          }}
          footer={
            <Button disabled={!acknowledged} onClick={() => setIssued(null)}>
              Done
            </Button>
          }
        >
          <p className="mb-3 text-xs text-muted">
            This is the only time this key is shown. We store a hash, not the key itself, so it
            cannot be retrieved later — if you lose it, revoke it and issue another.
          </p>
          <CodeBlock code={issued} />
          <label className="mt-4 flex items-center gap-2 text-xs text-ink">
            <input
              type="checkbox"
              checked={acknowledged}
              onChange={(event) => setAcknowledged(event.target.checked)}
              className="accent-[var(--accent-positive)]"
            />
            I&rsquo;ve saved my key
          </label>
        </Modal>
      )}
    </Card>
  );
}

// -- Project settings -------------------------------------------------------

function ProjectSettings({ projectId }: { projectId: string }) {
  const { accessToken } = useAuth();
  const client = useQueryClient();
  const { push } = useToast();

  const project = useQuery({
    queryKey: ['project', projectId],
    queryFn: () => getProject(accessToken ?? '', projectId),
    enabled: Boolean(accessToken),
  });

  const save = useMutation({
    mutationFn: (body: Parameters<typeof updateSettings>[2]) =>
      updateSettings(accessToken ?? '', projectId, body),
    onSuccess: () => {
      push({ tone: 'success', message: 'Settings saved.' });
      void client.invalidateQueries({ queryKey: ['project', projectId] });
      void client.invalidateQueries({ queryKey: keys.projects() });
    },
    onError: (error) => push({ tone: 'error', message: describeError(error) }),
  });

  if (project.isLoading)
    return (
      <Card title="Project settings">
        <Spinner />
      </Card>
    );
  if (project.isError)
    return (
      <Card title="Project settings">
        <ErrorBanner
          message={describeError(project.error)}
          onRetry={() => void project.refetch()}
        />
      </Card>
    );

  const data = project.data;
  if (!data) return null;

  return (
    <Card title="Privacy & data">
      <SectionTitle hint="Off by default. When off we store only a hash and an embedding of each prompt — never the text.">
        Store raw prompt content
      </SectionTitle>
      <Toggle
        size="lg"
        label="Store raw prompts and responses"
        description="Turning this on lets you read full request bodies in the request log. It also means we hold your prompt text at rest."
        checked={data.store_raw_content}
        onChange={(next) => save.mutate({ store_raw_content: next })}
        disabled={save.isPending}
      />
    </Card>
  );
}
