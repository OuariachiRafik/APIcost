/** Signup and login — UC-01. One component; the two differ only in copy and call. */
import { useState, type FormEvent } from 'react';

import { Button, Card, ErrorNotice, Field } from '../components/ui';
import { describeError, useAuth } from '../lib/authContext';

const MIN_PASSWORD_LENGTH = 12; // matches SignupRequest in api/routers/auth.py

export function AuthForm() {
  const { signup, login } = useAuth();

  const [mode, setMode] = useState<'signup' | 'login'>('signup');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const isSignup = mode === 'signup';
  const tooShort = isSignup && password.length > 0 && password.length < MIN_PASSWORD_LENGTH;

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setError(null);
    setBusy(true);
    try {
      await (isSignup ? signup(email, password) : login(email, password));
    } catch (caught) {
      setError(describeError(caught));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card>
      <h2 className="mb-1 text-lg font-semibold">
        {isSignup ? 'Create your account' : 'Welcome back'}
      </h2>
      <p className="mb-6 text-sm text-muted">
        {isSignup
          ? 'You will connect a provider key next. It is encrypted before it touches disk.'
          : 'Sign in to your dashboard.'}
      </p>

      <form onSubmit={submit} className="space-y-4">
        <ErrorNotice message={error} />

        <Field
          label="Email"
          type="email"
          required
          autoComplete="email"
          value={email}
          onChange={(event) => setEmail(event.target.value)}
        />

        <Field
          label="Password"
          type="password"
          required
          autoComplete={isSignup ? 'new-password' : 'current-password'}
          value={password}
          onChange={(event) => setPassword(event.target.value)}
          hint={isSignup ? `At least ${MIN_PASSWORD_LENGTH} characters.` : undefined}
        />

        {tooShort && (
          <p className="text-xs text-amber-700">
            {MIN_PASSWORD_LENGTH - password.length} more characters needed.
          </p>
        )}

        <Button type="submit" disabled={busy || !email || !password || tooShort}>
          {busy ? 'Working…' : isSignup ? 'Create account' : 'Sign in'}
        </Button>
      </form>

      <p className="mt-6 text-sm text-muted">
        {isSignup ? 'Already have an account?' : 'Need an account?'}{' '}
        <button
          type="button"
          className="font-medium text-ink underline"
          onClick={() => {
            setMode(isSignup ? 'login' : 'signup');
            setError(null);
          }}
        >
          {isSignup ? 'Sign in' : 'Sign up'}
        </button>
      </p>
    </Card>
  );
}
