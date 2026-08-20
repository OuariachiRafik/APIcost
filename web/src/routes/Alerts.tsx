/**
 * Alerts — APICOST_FRONTEND_SPEC §5.6.
 *
 * The kill switch lives in its own danger-zone card at the bottom, visually
 * separated from routine alert-browsing, and takes type-the-project-name to
 * confirm. It revokes every proxy key for the project in under a second; those
 * keys cannot be un-revoked, they have to be reissued and redeployed. That is
 * the heaviest-consequence action in the product, so it gets the highest
 * friction confirmation in the product.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';

import { RequireProject, ScreenHeader } from '../components/Screen';
import {
  Badge,
  Button,
  Card,
  Cell,
  EmptyState,
  ErrorBanner,
  Modal,
  Row,
  Select,
  Spinner,
  Table,
  type Tone,
} from '../components/ui';
import { describeError, useAuth } from '../lib/authContext';
import { dateTime, relative } from '../lib/format';
import { keys, killProject, listAlerts, resolveAlert, type AlertResponse } from '../lib/queries';
import { useToast, useWorkspace } from '../lib/uiContext';

const SEVERITY_TONE: Record<string, Tone> = {
  info: 'info',
  warning: 'warning',
  critical: 'critical',
};

const TYPE_LABEL: Record<string, string> = {
  spend_spike: 'Spend spike',
  usage_pattern: 'Usage pattern',
  budget_threshold: 'Budget threshold',
  budget_exceeded: 'Budget exceeded',
  kill_switch: 'Kill switch',
};

export function Alerts() {
  return (
    <>
      <ScreenHeader
        title="Alerts"
        description="Spend spikes, unfamiliar usage patterns, and budget events."
      />
      <RequireProject>{(projectId) => <AlertsBody projectId={projectId} />}</RequireProject>
    </>
  );
}

function AlertsBody({ projectId }: { projectId: string }) {
  const { accessToken } = useAuth();
  const [status, setStatus] = useState('');
  const [selected, setSelected] = useState<AlertResponse | null>(null);

  const alerts = useQuery({
    queryKey: keys.alerts(projectId, status),
    queryFn: () => listAlerts(accessToken ?? '', projectId, status || undefined),
    enabled: Boolean(accessToken),
  });

  return (
    <div className="flex flex-col gap-4">
      <Card
        title="History"
        action={
          <div className="w-40">
            <Select value={status} onChange={setStatus}>
              <option value="">All alerts</option>
              <option value="open">Open</option>
              <option value="acknowledged">Acknowledged</option>
              <option value="resolved">Resolved</option>
            </Select>
          </div>
        }
      >
        {alerts.isLoading ? (
          <Spinner />
        ) : alerts.isError ? (
          <ErrorBanner
            message={describeError(alerts.error)}
            onRetry={() => void alerts.refetch()}
          />
        ) : alerts.data && alerts.data.alerts.length > 0 ? (
          <Table head={['Severity', 'Alert', 'Type', 'When', 'Status', '']}>
            {alerts.data.alerts.map((alert) => (
              <Row key={alert.id}>
                <Cell>
                  <Badge tone={SEVERITY_TONE[alert.severity] ?? 'neutral'}>{alert.severity}</Badge>
                </Cell>
                <Cell>{alert.title}</Cell>
                <Cell>
                  <span className="text-xs text-muted">
                    {TYPE_LABEL[alert.alert_type] ?? alert.alert_type}
                  </span>
                </Cell>
                <Cell>
                  <span className="text-xs text-muted" title={dateTime(alert.created_at)}>
                    {relative(alert.created_at)}
                  </span>
                </Cell>
                <Cell>
                  <span className="text-xs text-muted">{alert.status}</span>
                </Cell>
                <Cell className="text-right">
                  <Button variant="ghost" onClick={() => setSelected(alert)}>
                    {alert.status === 'resolved' ? 'View' : 'Resolve'}
                  </Button>
                </Cell>
              </Row>
            ))}
          </Table>
        ) : (
          <EmptyState title="No alerts triggered">
            We&rsquo;ll notify you here the moment something looks unusual.
          </EmptyState>
        )}
      </Card>

      {selected && (
        <ResolveDialog
          alert={selected}
          projectId={projectId}
          status={status}
          onClose={() => setSelected(null)}
        />
      )}

      <KillSwitch projectId={projectId} />
    </div>
  );
}

function ResolveDialog({
  alert,
  projectId,
  status,
  onClose,
}: {
  alert: AlertResponse;
  projectId: string;
  status: string;
  onClose: () => void;
}) {
  const { accessToken } = useAuth();
  const client = useQueryClient();
  const { push } = useToast();
  const [note, setNote] = useState(alert.resolution ?? '');

  const resolve = useMutation({
    mutationFn: (next: 'acknowledged' | 'resolved') =>
      resolveAlert(accessToken ?? '', alert.id, { status: next, resolution: note || null }),
    onSuccess: () => {
      push({ tone: 'success', message: 'Alert updated.' });
      void client.invalidateQueries({ queryKey: keys.alerts(projectId, status) });
      onClose();
    },
    onError: (error) => push({ tone: 'error', message: describeError(error) }),
  });

  const details = Object.entries(alert.detail ?? {});

  return (
    <Modal
      title={alert.title}
      onClose={onClose}
      footer={
        <>
          <Button variant="secondary" onClick={onClose}>
            Close
          </Button>
          <Button variant="secondary" onClick={() => resolve.mutate('acknowledged')}>
            Acknowledge
          </Button>
          <Button onClick={() => resolve.mutate('resolved')} disabled={resolve.isPending}>
            Mark resolved
          </Button>
        </>
      }
    >
      {details.length > 0 && (
        <dl className="mb-4 divide-y divide-edge/60 rounded-md border border-edge">
          {details.map(([key, value]) => (
            <div key={key} className="flex justify-between gap-4 px-3 py-1.5">
              <dt className="text-xs text-muted">{key.replace(/_/g, ' ')}</dt>
              <dd className="tnum font-mono text-xs text-ink">{String(value)}</dd>
            </div>
          ))}
        </dl>
      )}

      <label htmlFor="resolution" className="mb-1 block text-xs font-medium text-muted">
        What did you do about it?
      </label>
      <textarea
        id="resolution"
        rows={3}
        value={note}
        onChange={(event) => setNote(event.target.value)}
        placeholder="Checked the logs — it was our own load test."
        className="w-full rounded-md border border-edge bg-page px-3 py-2 text-sm text-ink
                   placeholder:text-muted/60 focus:border-info focus:outline-none"
      />
      <p className="mt-1 text-[11px] text-muted">
        Kept with the alert. &ldquo;Was this real, and what did we do&rdquo; is most of what an
        alert history is for.
      </p>
    </Modal>
  );
}

function KillSwitch({ projectId }: { projectId: string }) {
  const { accessToken } = useAuth();
  const { project, projects } = useWorkspace();
  const client = useQueryClient();
  const { push } = useToast();

  const [open, setOpen] = useState(false);
  const [typed, setTyped] = useState('');

  const name = project?.name ?? projects.find((p) => p.id === projectId)?.name ?? '';
  const armed = typed.trim() === name && name.length > 0;

  const kill = useMutation({
    mutationFn: () => killProject(accessToken ?? '', projectId),
    onSuccess: (result) => {
      setOpen(false);
      setTyped('');
      push({
        tone: 'success',
        message: `Revoked ${result.keys_revoked} proxy key${
          result.keys_revoked === 1 ? '' : 's'
        } in ${Math.round(result.took_ms)} ms.`,
      });
      void client.invalidateQueries({ queryKey: keys.proxyKeys(projectId) });
      void client.invalidateQueries({ queryKey: ['alerts'] });
    },
    onError: (error) => push({ tone: 'error', message: describeError(error) }),
  });

  return (
    <Card title="Danger zone" tone="critical">
      <div className="flex items-start justify-between gap-6">
        <div>
          <p className="text-sm font-medium text-ink">Kill proxy access for this project</p>
          <p className="mt-1 max-w-xl text-xs text-muted">
            Revokes every proxy key for <span className="font-mono text-ink">{name}</span>{' '}
            immediately. Any application using them starts failing within a second. Your provider
            keys, projects and history are untouched — this stops traffic, it does not delete
            anything.
          </p>
        </div>
        <Button variant="danger" onClick={() => setOpen(true)}>
          Kill access
        </Button>
      </div>

      {open && (
        <Modal
          title="Revoke every proxy key?"
          tone="critical"
          onClose={() => {
            setOpen(false);
            setTyped('');
          }}
          footer={
            <>
              <Button
                variant="secondary"
                onClick={() => {
                  setOpen(false);
                  setTyped('');
                }}
              >
                Cancel
              </Button>
              <Button
                variant="danger"
                disabled={!armed || kill.isPending}
                onClick={() => kill.mutate()}
              >
                {kill.isPending ? 'Revoking…' : 'Revoke all keys'}
              </Button>
            </>
          }
        >
          <p>
            Every proxy key for this project stops working immediately. You will need to issue a new
            key and redeploy your application before traffic can resume.
          </p>
          <label htmlFor="confirm-name" className="mt-4 mb-1 block text-xs font-medium text-muted">
            Type <span className="font-mono text-ink">{name}</span> to confirm
          </label>
          <input
            id="confirm-name"
            value={typed}
            onChange={(event) => setTyped(event.target.value)}
            autoComplete="off"
            className="w-full rounded-md border border-edge bg-page px-3 py-1.5 font-mono
                       text-sm text-ink focus:border-critical focus:outline-none"
          />
        </Modal>
      )}
    </Card>
  );
}
